"""Spool del motor para la subida a S3 (escenarios con `subida`).

Cada archivo se guarda mientras se baja: los de menos de `UMBRAL_DISCO` en el spool en RAM
(`/dev/shm`), los demás en el spool en disco. Recién cuando terminó de bajar se pasa a `listos/`
con un rename atómico, nombrado `repo_item_archivo` (la clave en S3 es por repositorio); de ahí lo
toma el subidor, otro proceso. La
descarga nunca espera a la subida: si lo pendiente pasa un límite, el motor deja de repartir tareas
nuevas (`frenar`), pero lo que está en curso termina normalmente.

La E/S de archivos se hace con `asyncio.to_thread`, fuera del bucle de eventos: las funciones con
`open` de este módulo solo corren en hilos.
"""

import asyncio
import itertools
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from concurrencia import config

_NOMBRES = itertools.count()


def directorio_ram(base_disco: Path) -> Path:
    """El spool en RAM: `/dev/shm` en Linux; donde no existe (macOS), un directorio común."""
    shm = Path("/dev/shm")
    return shm / "concurrencia-spool" if shm.is_dir() else base_disco.parent / (base_disco.name + "-ram")


@dataclass(frozen=True, slots=True)
class Directorios:
    ram: Path
    disco: Path
    estado: Path
    """JSON que el subidor reescribe cada segundo con lo que lleva subido."""
    parar: Path
    """Archivo que el motor crea para que el subidor termine."""

    @classmethod
    def en(cls, base: Path) -> "Directorios":
        return cls(directorio_ram(base), base, base / "subidor.json", base / "parar")

    def listos(self) -> list[Path]:
        return [self.ram / "listos", self.disco / "listos"]

    def preparar(self) -> None:
        """Deja los spools vacíos (de una corrida anterior pueden quedar archivos)."""
        for raiz in (self.ram, self.disco):
            shutil.rmtree(raiz, ignore_errors=True)
            (raiz / "tmp").mkdir(parents=True)
            (raiz / "listos").mkdir()

    def limpiar(self) -> None:
        shutil.rmtree(self.ram, ignore_errors=True)
        shutil.rmtree(self.disco, ignore_errors=True)


class Spool:
    def __init__(self, dirs: Directorios, umbral: int = config.UMBRAL_DISCO, en_memoria: bool = False) -> None:
        self.dirs = dirs
        self._en_memoria = en_memoria
        """Si el subidor comparte el proceso y escribe su estado directo en `subidor`."""
        self.umbral = umbral
        self.entregado = {"ram": 0, "disco": 0}
        """Bytes pasados a `listos/`, por spool."""
        self.subidor: dict[str, Any] = {}
        """Último estado que informó el subidor."""

    def nuevo(self, largo: int | None) -> "Destino":
        return Destino(self, largo)

    def pendiente(self, spool: str) -> int:
        """Bytes entregados que el subidor todavía no subió (según su último estado)."""
        subido = self.subidor.get(f"bytes_{spool}", 0)
        return max(0, self.entregado[spool] - subido)

    def frenar(self) -> bool:
        return (
            self.pendiente("ram") > config.LIMITE_PENDIENTE_RAM
            or self.pendiente("disco") > config.LIMITE_PENDIENTE_DISCO
        )

    def metricas(self) -> dict[str, float]:
        """Lo que el muestreador agrega a cada fila en los escenarios con subida."""
        cpu = self.subidor.get("cpu") or [0.0]
        rss = self.subidor.get("rss_mb") or [0.0]
        return {
            "subidor_rss_mb": rss[-1],
            "pendiente_ram_mb": self.pendiente("ram") / config.MB,
            "pendiente_disco_mb": self.pendiente("disco") / config.MB,
            "subido_bytes": self.subidor.get("bytes_ram", 0) + self.subidor.get("bytes_disco", 0),
            "subidor_cpu": cpu[-1],
        }

    async def actualizar(self) -> None:
        if not self._en_memoria:
            self.subidor = await asyncio.to_thread(_leer_estado, self.dirs.estado)


def _leer_estado(ruta: Path) -> dict[str, Any]:
    try:
        datos: dict[str, Any] = json.loads(ruta.read_text())
        return datos
    except (OSError, ValueError):
        return {}


class Destino:
    """Un archivo que se está bajando: junta los bytes y los vuelca al spool que le toca."""

    def __init__(self, spool: Spool, largo: int | None) -> None:
        self._spool = spool
        self._buffer = bytearray()
        # Sin Content-Length arranca en RAM y pasa a disco si crece más del umbral.
        self.en_disco = largo is not None and largo >= spool.umbral
        self._archivo: Any = None
        self._tmp: Path | None = None
        self._cerrado = False

    async def escribir(self, trozo: bytes) -> None:
        self._buffer += trozo
        if not self.en_disco and len(self._buffer) >= self._spool.umbral:
            self.en_disco = True
        if self.en_disco and len(self._buffer) >= config.BLOQUE_DISCO:
            bloque, self._buffer = bytes(self._buffer), bytearray()
            await asyncio.to_thread(self._volcar, bloque)

    async def cerrar(self, nombre: str, tamano: int) -> None:
        """El archivo terminó de bajar: lo pasa a `listos/` del spool que le toca."""
        self._cerrado = True
        resto, self._buffer = bytes(self._buffer), bytearray()
        spool = "disco" if self.en_disco else "ram"
        destino = (self._spool.dirs.disco if self.en_disco else self._spool.dirs.ram) / "listos" / nombre
        await asyncio.to_thread(self._terminar, resto, destino)
        self._spool.entregado[spool] += tamano

    async def descartar(self) -> None:
        """La descarga falló o se cortó: se borra lo que se haya escrito."""
        if self._cerrado:
            return
        self._cerrado = True
        self._buffer = bytearray()
        if self._archivo is not None or self._tmp is not None:
            await asyncio.to_thread(self._borrar)

    # Lo que sigue corre en hilos (asyncio.to_thread).

    def _volcar(self, bloque: bytes) -> None:
        if self._archivo is None:
            self._tmp = self._spool.dirs.disco / "tmp" / f"{os.getpid()}-{next(_NOMBRES)}"
            self._archivo = open(self._tmp, "wb")  # noqa: SIM115 - se cierra en _terminar o _borrar
        self._archivo.write(bloque)

    def _terminar(self, resto: bytes, destino: Path) -> None:
        if self.en_disco:
            self._volcar(resto)
            self._archivo.close()
            assert self._tmp is not None
            os.replace(self._tmp, destino)
        else:
            tmp = self._spool.dirs.ram / "tmp" / f"{os.getpid()}-{next(_NOMBRES)}"
            with open(tmp, "wb") as f:
                f.write(resto)
            os.replace(tmp, destino)

    def _borrar(self) -> None:
        if self._archivo is not None:
            self._archivo.close()
        if self._tmp is not None:
            self._tmp.unlink(missing_ok=True)
