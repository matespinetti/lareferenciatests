"""Métricas del motor: una fila por segundo, comunes a los dos modos.

Los contadores se acumulan durante el segundo y el muestreador los vuelca en una fila junto con
lo que da psutil. Las filas quedan en memoria y se escriben al final, así no hay E/S de disco
mientras se mide.
"""

import contextlib
import csv
import gc
import json
import statistics
import threading
import time
from array import array
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from concurrencia.planificador import Tipo

COLUMNAS = [
    "t", "calentamiento",
    "cpu_usuario", "cpu_sistema", "cpu", "rss_mb", "hilos", "descriptores", "conexiones_tcp",
    "ctx_voluntarios", "ctx_involuntarios",
    "maquina_cpu", "steal", "memoria_disponible_mb",
    "en_curso", "ocupacion", "listos_sin_atender", "items_ok",
    "pedidos", "pedidos_resolucion", "pedidos_descarga", "descargas_ok", "mb_recibidos", "mb_hasheados",
    "duracion_media_s", "duracion_resolucion_s", "duracion_descarga_s", "lat_p50_ms", "lat_p95_ms", "lat_p99_ms", "agregada_p50_ms", "agregada_p95_ms",
    "despacho_p50_ms", "despacho_p99_ms", "despacho_max_ms",
    "bucle_p50_ms", "bucle_p99_ms", "bucle_max_ms",
    "gc_pausas", "gc_ms",
    "errores", "r429", "r503",
    "db_tx", "db_filas", "db_commit_p95_ms", "db_espera_p95_ms",
    "disco_escritura_mb_s", "pendiente_ram_mb", "pendiente_disco_mb", "subida_mb_s", "subidor_cpu", "frenado_s",
    "cpu_workers", "rss_workers_mb", "bucle_workers_max_ms", "subidor_rss_mb",
    "repos_con_trabajo",
]  # fmt: skip


def percentil(valores: list[float], q: float) -> float:
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    return ordenados[min(len(ordenados) - 1, int(q * len(ordenados)))]


def _media(valores: list[float]) -> float:
    return round(statistics.fmean(valores), 4) if valores else 0.0


@dataclass
class _Segundo:
    pedidos: Counter[str] = field(default_factory=Counter)
    descargas_ok: int = 0
    items_ok: int = 0
    bytes: int = 0
    bytes_hash: int = 0
    duraciones: list[float] = field(default_factory=list)
    por_tipo: dict[str, list[float]] = field(default_factory=lambda: {t: [] for t in Tipo})
    latencias: list[float] = field(default_factory=list)
    agregadas: list[float] = field(default_factory=list)
    despachos: list[float] = field(default_factory=list)
    bucle: list[float] = field(default_factory=list)
    gc_pausas: list[float] = field(default_factory=list)
    errores: int = 0
    r429: int = 0
    r503: int = 0
    db_commits: list[float] = field(default_factory=list)
    frenado: float = 0.0
    db_esperas: list[float] = field(default_factory=list)
    db_filas: int = 0


class Contadores:
    """Lo que el motor cuenta por su cuenta. Con `con_lock` es seguro entre hilos."""

    def __init__(self, con_lock: bool) -> None:
        self._lock: Any = threading.Lock() if con_lock else contextlib.nullcontext()
        self._actual = _Segundo()
        self.errores_por_tipo: Counter[str] = Counter()
        self._gc_inicio: float | None = None

    def pedido(
        self,
        tipo: Tipo,
        duracion: float,
        latencia: float | None,
        servicio: float | None,
        despacho: float,
        descarga_ok: bool = False,
    ) -> None:
        with self._lock:
            s = self._actual
            s.pedidos[tipo] += 1
            s.duraciones.append(duracion)
            s.por_tipo[tipo].append(duracion)
            s.despachos.append(despacho)
            s.descargas_ok += descarga_ok
            if latencia is not None:
                s.latencias.append(latencia)
                if servicio is not None:
                    s.agregadas.append(latencia - servicio)

    def recibidos(self, n: int, hasheados: bool = False) -> None:
        """Bytes recibidos, contados a medida que llegan: un archivo grande tarda decenas de
        segundos, y contarlo recién al final corre los MB/s hacia el segundo en que termina."""
        with self._lock:
            self._actual.bytes += n
            if hasheados:
                self._actual.bytes_hash += n

    def frenado(self, segundos: float) -> None:
        """Tiempo que el despachador no repartió tareas porque la subida estaba atrasada."""
        with self._lock:
            self._actual.frenado += segundos

    def item_completo(self) -> None:
        with self._lock:
            self._actual.items_ok += 1

    def estado(self, codigo: int) -> None:
        with self._lock:
            if codigo == 429:
                self._actual.r429 += 1
            elif codigo == 503:
                self._actual.r503 += 1

    def error(self, tipo: str) -> None:
        with self._lock:
            self._actual.errores += 1
            self.errores_por_tipo[tipo] += 1

    def bucle(self, retraso: float) -> None:
        with self._lock:
            self._actual.bucle.append(retraso)

    def db(self, espera: float, commit: float, filas: int = 1) -> None:
        with self._lock:
            self._actual.db_esperas.append(espera)
            self._actual.db_commits.append(commit)
            self._actual.db_filas += filas

    def cortar(self) -> _Segundo:
        with self._lock:
            actual, self._actual = self._actual, _Segundo()
            return actual

    # Pausas del recolector de basura, medidas con gc.callbacks.
    def instalar_gc(self) -> None:
        gc.callbacks.append(self._gc)

    def quitar_gc(self) -> None:
        if self._gc in gc.callbacks:
            gc.callbacks.remove(self._gc)

    def _gc(self, fase: str, info: dict[str, Any]) -> None:
        if fase == "start":
            self._gc_inicio = time.perf_counter()
        elif self._gc_inicio is not None:
            self._actual.gc_pausas.append(time.perf_counter() - self._gc_inicio)
            self._gc_inicio = None


class Muestreador:
    """Arma la fila de cada segundo con psutil y los contadores."""

    def __init__(self, contadores: Contadores, calentamiento: float, lugares: int) -> None:
        self.contadores = contadores
        self.lugares = lugares
        self.calentamiento = calentamiento
        self.filas: list[dict[str, Any]] = []
        self._proceso = psutil.Process()
        self._cpu = self._proceso.cpu_times()
        self._ctx = self._proceso.num_ctx_switches()
        self._t = time.monotonic()
        self._origen = self._t
        self._conexiones = 0
        self._bucle = array("d")
        self._disco = self._escrito_en_disco()
        self._subido = 0
        """Todas las muestras del retraso del bucle fuera del calentamiento, para sus percentiles."""
        psutil.cpu_times_percent(interval=None)
        psutil.cpu_percent(interval=None)

    @staticmethod
    def _escrito_en_disco() -> int:
        contadores = psutil.disk_io_counters()
        return contadores.write_bytes if contadores else 0

    def muestra(
        self, en_curso: int, listos_sin_atender: int, extras: dict[str, float] | None = None
    ) -> dict[str, Any]:
        ahora = time.monotonic()
        dt = ahora - self._t
        cpu, ctx = self._proceso.cpu_times(), self._proceso.num_ctx_switches()
        s = self.contadores.cortar()
        t = ahora - self._origen
        if len(self.filas) % 5 == 0:
            # Contar conexiones es caro con miles abiertas: cada 5 s alcanza.
            try:
                self._conexiones = len(self._proceso.net_connections(kind="tcp"))
            except (psutil.AccessDenied, OSError):
                self._conexiones = -1
        maquina = psutil.cpu_times_percent(interval=None)
        ms = 1000.0
        fila = {
            "t": round(t, 3),
            # Una fila es de calentamiento si su segundo empezó durante el calentamiento.
            "calentamiento": int(t - dt < self.calentamiento),
            "cpu_usuario": round(100 * (cpu.user - self._cpu.user) / dt, 2),
            "cpu_sistema": round(100 * (cpu.system - self._cpu.system) / dt, 2),
            "cpu": round(100 * ((cpu.user + cpu.system) - (self._cpu.user + self._cpu.system)) / dt, 2),
            "rss_mb": round(self._proceso.memory_info().rss / 2**20, 1),
            "hilos": self._proceso.num_threads(),
            "descriptores": self._proceso.num_fds(),
            "conexiones_tcp": self._conexiones,
            "ctx_voluntarios": ctx.voluntary - self._ctx.voluntary,
            "ctx_involuntarios": ctx.involuntary - self._ctx.involuntary,
            "maquina_cpu": round(100 - maquina.idle, 2),
            "steal": round(getattr(maquina, "steal", 0.0), 2),
            "memoria_disponible_mb": round(psutil.virtual_memory().available / 2**20, 1),
            "en_curso": en_curso,
            "ocupacion": round(100 * en_curso / self.lugares, 1),
            "listos_sin_atender": listos_sin_atender,
            "items_ok": round(s.items_ok / dt, 2),
            "pedidos": round(sum(s.pedidos.values()) / dt, 2),
            "pedidos_resolucion": round(s.pedidos[Tipo.RESOLUCION] / dt, 2),
            "pedidos_descarga": round(s.pedidos[Tipo.DESCARGA] / dt, 2),
            "descargas_ok": round(s.descargas_ok / dt, 2),
            "mb_recibidos": round(s.bytes / 2**20 / dt, 3),
            "mb_hasheados": round(s.bytes_hash / 2**20 / dt, 3),
            "duracion_media_s": _media(s.duraciones),
            "duracion_resolucion_s": _media(s.por_tipo[Tipo.RESOLUCION]),
            "duracion_descarga_s": _media(s.por_tipo[Tipo.DESCARGA]),
            "lat_p50_ms": round(ms * percentil(s.latencias, 0.5), 1),
            "lat_p95_ms": round(ms * percentil(s.latencias, 0.95), 1),
            "lat_p99_ms": round(ms * percentil(s.latencias, 0.99), 1),
            "agregada_p50_ms": round(ms * percentil(s.agregadas, 0.5), 2),
            "agregada_p95_ms": round(ms * percentil(s.agregadas, 0.95), 2),
            "despacho_p50_ms": round(ms * percentil(s.despachos, 0.5), 2),
            "despacho_p99_ms": round(ms * percentil(s.despachos, 0.99), 2),
            "despacho_max_ms": round(ms * max(s.despachos, default=0.0), 2),
            "bucle_p50_ms": round(ms * percentil(s.bucle, 0.5), 2),
            "bucle_p99_ms": round(ms * percentil(s.bucle, 0.99), 2),
            "bucle_max_ms": round(ms * max(s.bucle, default=0.0), 2),
            "gc_pausas": len(s.gc_pausas),
            "gc_ms": round(ms * sum(s.gc_pausas), 2),
            "errores": s.errores,
            "r429": s.r429,
            "r503": s.r503,
            "db_tx": round(len(s.db_commits) / dt, 2),
            "db_filas": round(s.db_filas / dt, 2),
            "db_commit_p95_ms": round(ms * percentil(s.db_commits, 0.95), 2),
            "db_espera_p95_ms": round(ms * percentil(s.db_esperas, 0.95), 2),
            "frenado_s": round(s.frenado, 2),
        }
        disco = self._escrito_en_disco()
        fila["disco_escritura_mb_s"] = round((disco - self._disco) / 2**20 / dt, 2)
        self._disco = disco
        extras = extras or {}
        subido = int(extras.get("subido_bytes", self._subido))
        fila["subida_mb_s"] = round((subido - self._subido) / 2**20 / dt, 2)
        self._subido = subido
        for clave in ("pendiente_ram_mb", "pendiente_disco_mb", "subidor_cpu", "cpu_workers", "rss_workers_mb",
                      "bucle_workers_max_ms", "subidor_rss_mb", "repos_con_trabajo"):  # fmt: skip
            fila[clave] = round(extras.get(clave, 0.0), 1)
        self._cpu, self._ctx, self._t = cpu, ctx, ahora
        if not fila["calentamiento"]:
            self._bucle.extend(s.bucle)
        self.filas.append(fila)
        return fila

    def resumen_bucle(self) -> dict[str, float]:
        """Percentiles del retraso del bucle de toda la medición, en ms."""
        muestras = list(self._bucle)
        return {
            "p50_ms": round(1000 * percentil(muestras, 0.5), 2),
            "p99_ms": round(1000 * percentil(muestras, 0.99), 2),
            "max_ms": round(1000 * max(muestras, default=0.0), 2),
            "muestras": len(muestras),
        }


def combinar_estados(estados: list[dict[str, Any]]) -> dict[str, Any]:
    """El estado de un servidor en varios procesos: sumas, y la CPU del proceso más cargado de
    cada segundo, que es la que dice si el servidor se saturó."""
    if len(estados) == 1:
        return estados[0]
    segundos = min(len(e["cpu_servidor"]) for e in estados)
    return {
        "procesos": len(estados),
        **{clave: sum(e[clave] for e in estados) for clave in ("pedidos", "violaciones", "simultaneos", "intervalos_cortos")},
        "ejemplos": [ejemplo for e in estados for ejemplo in e["ejemplos"]][:20],
        "cpu_servidor": [max(e["cpu_servidor"][i] for e in estados) for i in range(segundos)],
        "cpu_servidor_total": [round(sum(e["cpu_servidor"][i] for e in estados), 1) for i in range(segundos)],
    }


COLUMNAS_PEDIDOS = ["repo", "host", "tipo", "inicio", "fin", "codigo", "bytes", "error", "reintento_tls", "url", "detalle", "vuelta"]


def escribir_pedidos(ruta: Path, pedidos: list[tuple[Any, ...]]) -> None:
    """Un pedido por fila (modo muestra), al terminar la corrida: tiempos relativos al reloj del bucle."""
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        escritor = csv.writer(f)
        escritor.writerow(COLUMNAS_PEDIDOS)
        escritor.writerows(pedidos)


def escribir(salida: Path, filas: list[dict[str, Any]], resumen: dict[str, Any]) -> None:
    """Escribe `<salida>.csv` con la serie y `<salida>.json` con parámetros y resultados."""
    salida.parent.mkdir(parents=True, exist_ok=True)
    with salida.with_suffix(".csv").open("w", newline="") as f:
        escritor = csv.DictWriter(f, fieldnames=COLUMNAS)
        escritor.writeheader()
        escritor.writerows(filas)
    salida.with_suffix(".json").write_text(json.dumps(resumen, indent=2, ensure_ascii=False))
