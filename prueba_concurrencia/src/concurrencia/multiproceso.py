"""Modo multiproceso: un proceso planificador y N procesos workers, conectados por TCP.

Es la alternativa a un solo motor: el planificador aplica la cortesía (el mismo `Planificador`) y
reparte cada tarea a un worker con lugar libre; cada worker la ejecuta con sus propias corrutinas
y devuelve el resultado. El canal es TCP (en la prueba, por 127.0.0.1), así que los mismos workers
podrían correr en otras máquinas. El protocolo son mensajes JSON de una línea:

- planificador → worker: `{"k": "t", "id", "tipo", "repo", "item", "archivo"}` (una tarea) y
  `{"k": "fin"}`;
- worker → planificador: `{"k": "hola", "pid", "lugares"}`, `{"k": "r", "id", ...}` (un resultado),
  `{"k": "b", "n", "h", "lag"}` (bytes recibidos y retraso de su bucle, cada 250 ms) y
  `{"k": "resumen", ...}` al terminar.

Cada worker mide el retraso de su propio bucle; el planificador mide el suyo. La CPU y la memoria
de los workers se suman en las columnas `cpu_workers` y `rss_workers_mb`.
"""

import asyncio
import json
import math
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil

from concurrencia import config, metricas, pedido, resolucion
from concurrencia.config import Escenario
from concurrencia.distribucion import items_por_repo
from concurrencia.planificador import Planificador, SeguimientoItems, Tarea, Tipo

_PROGRESO = 0.25


def _linea(datos: dict[str, Any]) -> bytes:
    return (json.dumps(datos, separators=(",", ":")) + "\n").encode()


# ---------------------------------------------------------------------------------- worker


def worker_main(planificador: tuple[str, int], indice: int, puerto: int, procesos: int, certs: Path, cpu: int | None) -> None:
    """Punto de entrada de cada proceso worker."""
    if cpu is not None and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, {cpu})
    try:
        asyncio.run(_worker(planificador, indice, puerto, procesos, certs))
    except KeyboardInterrupt:
        pass


async def _worker(planificador: tuple[str, int], indice: int, puerto: int, procesos: int, certs: Path) -> None:
    from concurrencia.motor import sesion

    loop = asyncio.get_running_loop()
    lector, escritor = await asyncio.open_connection(*planificador)
    escritor.write(_linea({"k": "hola", "pid": os.getpid(), "indice": indice}))
    bytes_ = [0, 0]  # [sin hash, con hash] desde el último aviso
    lag = {"max": 0.0, "muestras": []}

    def al_recibir(n: int, hasheados: bool) -> None:
        bytes_[1 if hasheados else 0] += n

    async def avisar() -> None:
        while True:
            await asyncio.sleep(_PROGRESO)
            escritor.write(_linea({"k": "b", "n": bytes_[0], "h": bytes_[1], "lag": lag["max"]}))
            bytes_[0] = bytes_[1] = 0
            lag["max"] = 0.0

    async def medir_bucle() -> None:
        while True:
            t = loop.time()
            await asyncio.sleep(0.1)
            retraso = loop.time() - t - 0.1
            lag["max"] = max(lag["max"], retraso)
            lag["muestras"].append(retraso)

    async def ejecutar(cliente: Any, id: int, tarea: Tarea) -> None:
        r = await pedido.hacer(cliente, tarea, resolucion.url(tarea, puerto, procesos), al_recibir)
        datos = asdict(r)
        datos["descargas"] = [[d.repo, d.item, d.archivo] for d in r.descargas]
        if datos["servicio"] is not None and math.isnan(datos["servicio"]):
            datos["servicio"] = None
        escritor.write(_linea({"k": "r", "id": id, **datos}))

    tareas: set[asyncio.Task[None]] = set()
    async with sesion(certs) as cliente:
        auxiliares = [asyncio.create_task(avisar()), asyncio.create_task(medir_bucle())]
        try:
            while linea := await lector.readline():
                mensaje = json.loads(linea)
                if mensaje["k"] == "fin":
                    break
                tarea = Tarea(Tipo(mensaje["tipo"]), mensaje["repo"], mensaje["item"], mensaje["archivo"])
                t = asyncio.create_task(ejecutar(cliente, mensaje["id"], tarea))
                tareas.add(t)
                t.add_done_callback(tareas.discard)
        finally:
            for t in [*tareas, *auxiliares]:
                t.cancel()
            await asyncio.gather(*tareas, *auxiliares, return_exceptions=True)
    muestras = lag["muestras"]
    escritor.write(_linea({
        "k": "resumen",
        "bucle_p99_ms": round(1000 * metricas.percentil(muestras, 0.99), 2),
        "bucle_max_ms": round(1000 * max(muestras, default=0.0), 2),
    }))  # fmt: skip
    await escritor.drain()
    escritor.close()


# ---------------------------------------------------------------------------- planificador


@dataclass
class _Worker:
    escritor: asyncio.StreamWriter
    pid: int
    libres: int
    proceso: psutil.Process
    cpu_antes: float = 0.0
    lag_max: float = 0.0
    resumen: dict[str, Any] = field(default_factory=dict)


class PlanificadorCentral:
    """El proceso planificador: reparte tareas a los workers y aplica sus resultados."""

    def __init__(self, escenario: Escenario, semilla: int, contadores: metricas.Contadores) -> None:
        self.e = escenario
        self.contadores = contadores
        self._loop = asyncio.get_running_loop()
        self.plan = Planificador(items_por_repo(escenario.repos, escenario.items, semilla), config.INTERVALO, self._loop.time())
        self.items = SeguimientoItems()
        self.workers: list[_Worker] = []
        self._pendientes: dict[int, tuple[Tarea, float]] = {}
        self._ids = 0
        self._hay_libre = asyncio.Event()
        self._cambio = asyncio.Event()
        self._conectados = asyncio.Event()
        self._resumenes = 0
        self._resumen_listo = asyncio.Event()

    def lugares_por_worker(self) -> list[int]:
        n = self.e.workers
        base, resto = divmod(self.e.lugares, n)
        return [base + (1 if i < resto else 0) for i in range(n)]

    async def atender(self, lector: asyncio.StreamReader, escritor: asyncio.StreamWriter) -> None:
        hola = json.loads(await lector.readline())
        w = _Worker(escritor, hola["pid"], self.lugares_por_worker()[hola["indice"]], psutil.Process(hola["pid"]))
        tiempos = w.proceso.cpu_times()
        w.cpu_antes = tiempos.user + tiempos.system
        self.workers.append(w)
        if len(self.workers) == self.e.workers:
            self._conectados.set()
        self._hay_libre.set()
        while linea := await lector.readline():
            mensaje = json.loads(linea)
            if mensaje["k"] == "b":
                self.contadores.recibidos(mensaje["n"])
                self.contadores.recibidos(mensaje["h"], hasheados=True)
                w.lag_max = max(w.lag_max, mensaje["lag"])
            elif mensaje["k"] == "r":
                self._resultado(w, mensaje)
            elif mensaje["k"] == "resumen":
                w.resumen = mensaje
                self._resumenes += 1
                if self._resumenes == len(self.workers):
                    self._resumen_listo.set()

    def _resultado(self, w: _Worker, m: dict[str, Any]) -> None:
        tarea, listo_en = self._pendientes.pop(m["id"])
        r = pedido.Resultado(
            inicio=m["inicio"], fin=m["fin"], codigo=m["codigo"], latencia=m["latencia"], servicio=m["servicio"],
            recibidos=m["recibidos"], sha=m["sha"], retry_after=m["retry_after"], error=m["error"],
            descargas=[Tarea(Tipo.DESCARGA, *d) for d in m["descargas"]],
        )  # fmt: skip
        # Los relojes monotónicos de los procesos de una máquina son el mismo: el despacho se mide
        # desde que la tarea quedó habilitada hasta que el worker empezó el pedido.
        pedido.aplicar(self.plan, self.items, self.contadores, tarea, r, r.inicio - listo_en)
        w.libres += 1
        self._hay_libre.set()
        self._cambio.set()

    async def despachar(self) -> None:
        await self._conectados.wait()
        while True:
            while not (libres := [w for w in self.workers if w.libres > 0]):
                self._hay_libre.clear()
                await self._hay_libre.wait()
            while (tomada := self.plan.siguiente(self._loop.time())) is None:
                if self.plan.terminado():
                    return
                proximo = self.plan.proximo_listo()
                espera = None if proximo is None else max(0.0, proximo - self._loop.time())
                self._cambio.clear()
                try:
                    async with asyncio.timeout(espera):
                        await self._cambio.wait()
                except TimeoutError:
                    pass
            tarea, listo_en = tomada
            w = max(libres, key=lambda w: w.libres)
            self._ids += 1
            self._pendientes[self._ids] = (tarea, listo_en)
            w.libres -= 1
            w.escritor.write(_linea(
                {"k": "t", "id": self._ids, "tipo": tarea.tipo.value, "repo": tarea.repo, "item": tarea.item,
                 "archivo": tarea.archivo}
            ))  # fmt: skip

    def extras(self, dt: float) -> dict[str, float]:
        """CPU y memoria de los workers y el peor retraso de sus bucles en el último segundo."""
        cpu = rss = 0.0
        lag = 0.0
        for w in self.workers:
            try:
                tiempos = w.proceso.cpu_times()
                ahora = tiempos.user + tiempos.system
                cpu += 100 * (ahora - w.cpu_antes) / dt
                w.cpu_antes = ahora
                rss += w.proceso.memory_info().rss / 2**20
            except psutil.Error:
                pass
            lag = max(lag, w.lag_max)
            w.lag_max = 0.0
        return {"cpu_workers": cpu, "rss_workers_mb": rss, "bucle_workers_max_ms": 1000 * lag}

    async def terminar(self) -> None:
        for w in self.workers:
            w.escritor.write(_linea({"k": "fin"}))
        try:
            async with asyncio.timeout(15):
                await self._resumen_listo.wait()
        except TimeoutError:
            pass


async def correr(
    escenario: Escenario,
    puerto: int,
    certs: Path,
    salida: Path,
    semilla: int = config.SEMILLA,
    procesos: int = 1,
) -> dict[str, Any]:
    """Corre el escenario con un planificador (este proceso) y `escenario.workers` procesos workers."""
    import multiprocessing

    from concurrencia.motor import _durante, _medir_bucle, _resumen, control, sesion

    contadores = metricas.Contadores(con_lock=False)
    planificador = PlanificadorCentral(escenario, semilla, contadores)
    servidor_tcp = await asyncio.start_server(planificador.atender, "127.0.0.1", 0)
    direccion = servidor_tcp.sockets[0].getsockname()[:2]
    nucleos = [int(c) for c in os.environ.get("WORKERS_CPU", "").split(",") if c.strip()]
    contexto = multiprocessing.get_context("spawn")
    procesos_workers = [
        contexto.Process(
            target=worker_main,
            args=(direccion, i, puerto, procesos, certs, nucleos[i % len(nucleos)] if nucleos else None),
            daemon=True,
        )
        for i in range(escenario.workers)
    ]
    async with sesion(certs) as cliente:
        await control(cliente, puerto, "reiniciar", "POST", procesos)
        for p in procesos_workers:
            p.start()
        async with asyncio.timeout(60):
            await planificador._conectados.wait()
        muestreador = metricas.Muestreador(contadores, escenario.calentamiento, escenario.lugares)

        async def muestrear() -> None:
            anterior = time.monotonic()
            while True:
                await asyncio.sleep(1.0)
                ahora = time.monotonic()
                extras = planificador.extras(ahora - anterior)
                anterior = ahora
                muestreador.muestra(planificador.plan.en_curso, planificador.plan.listos_sin_atender(planificador._loop.time()), extras)

        terminado_solo = await _durante(
            escenario.calentamiento + escenario.duracion,
            planificador.despachar,
            [lambda: _medir_bucle(contadores), muestrear],
            contadores,
        )
        await planificador.terminar()
    servidor_tcp.close()
    for p in procesos_workers:
        await asyncio.to_thread(p.join, 10)
        if p.is_alive():
            p.terminate()
    async with sesion(certs) as cliente:
        servidor = await control(cliente, puerto, "estado", procesos=procesos)
    resumen = _resumen(escenario, semilla, puerto, contadores, servidor, terminado_solo)
    resumen["bucle"] = muestreador.resumen_bucle()
    resumen["multiproceso"] = {
        "workers": escenario.workers,
        "lugares_por_worker": planificador.lugares_por_worker(),
        "bucle_workers": [w.resumen for w in planificador.workers],
    }
    metricas.escribir(salida, muestreador.filas, resumen)
    return resumen
