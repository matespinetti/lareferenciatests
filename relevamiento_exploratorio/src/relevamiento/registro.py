"""Escritura de pedidos.jsonl, eventos.jsonl y capturas/. Cada línea se escribe y se vuelca enseguida."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from relevamiento.modelos import Respuesta

PEDIDOS = "pedidos.jsonl"
EVENTOS = "eventos.jsonl"
CAPTURAS = "capturas"


class Registro:
    def __init__(self, dir_salida: Path, corrida: str):
        self.dir = dir_salida
        self.corrida = corrida
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / CAPTURAS).mkdir(exist_ok=True)
        self._pedidos = open(self.dir / PEDIDOS, "a", encoding="utf-8")
        self._eventos = open(self.dir / EVENTOS, "a", encoding="utf-8")

    def pedido(self, datos: dict[str, Any]) -> None:
        self._escribir(self._pedidos, {"corrida": self.corrida, **datos})

    def evento(self, tipo: str, **datos: Any) -> None:
        momento = datetime.now(timezone.utc).isoformat()
        self._escribir(self._eventos, {"tipo": tipo, "corrida": self.corrida, "momento": momento, **datos})

    def captura(
        self,
        pedido_id: str,
        host: str,
        *,
        url: str,
        codigo: int,
        encabezados: tuple[tuple[str, str], ...],
        cuerpo: bytes,
        truncado: bool,
        tipo: str,
    ) -> str:
        """Guarda encabezados y cuerpo truncado; devuelve la ruta relativa al directorio de salida."""
        carpeta = self.dir / CAPTURAS / _seguro(host or "sin-host")
        carpeta.mkdir(exist_ok=True)
        meta = {
            "pedido": pedido_id,
            "url": url,
            "codigo": codigo,
            "encabezados": [list(par) for par in encabezados],
            "truncado": truncado,
            "tipo": tipo,
        }
        (carpeta / f"{pedido_id}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        (carpeta / f"{pedido_id}.cuerpo").write_bytes(cuerpo)
        return str(Path(CAPTURAS) / carpeta.name / f"{pedido_id}.json")

    def cerrar(self) -> None:
        self._pedidos.close()
        self._eventos.close()

    @staticmethod
    def _escribir(archivo, datos: dict[str, Any]) -> None:
        archivo.write(json.dumps(datos, ensure_ascii=False, default=str) + "\n")
        archivo.flush()


def leer_jsonl(ruta: Path) -> Iterator[dict[str, Any]]:
    """Lee un JSONL tolerando una última línea cortada por una interrupción."""
    if not ruta.exists():
        return
    with open(ruta, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            try:
                yield json.loads(linea)
            except json.JSONDecodeError:
                continue


def respuesta_desde_captura(ruta_json: Path) -> Respuesta:
    meta = json.loads(ruta_json.read_text(encoding="utf-8"))
    cuerpo = ruta_json.with_suffix(".cuerpo").read_bytes()
    return Respuesta(
        url_inicial=meta["url"],
        url=meta["url"],
        codigo=meta["codigo"],
        encabezados=tuple(tuple(par) for par in meta["encabezados"]),
        cuerpo=cuerpo,
        truncado=meta["truncado"],
    )


def _seguro(nombre: str) -> str:
    return re.sub(r"[^\w.-]", "_", nombre)
