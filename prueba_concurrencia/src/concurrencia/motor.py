"""Mini motor con corrutinas: el modo previsto para el descargador.

Un despachador saca del planificador las tareas habilitadas mientras haya un lugar libre, y N
lugares de ejecución las hacen con un único `ClientSession` compartido. Toda la E/S es
asíncrona: aiohttp para HTTP y asyncpg para la base (`--db`).
"""

import asyncio
import platform
import socket
import ssl
import sys
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from concurrencia import config, metricas, pedido, resolucion
from concurrencia.config import Escenario
from concurrencia.db import Registro
from concurrencia.distribucion import items_por_repo
from concurrencia.planificador import Planificador, SeguimientoItems, Tarea, Tipo
from concurrencia.spool import Directorios, Spool

_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)


class ResolverLocal(AbstractResolver):
    """Manda cada `*.sim.localhost` a 127.0.0.1 y rechaza cualquier otro host."""

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if not host.endswith("." + config.DOMINIO):
            raise OSError(f"host fuera de la prueba: {host}")
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        pass


def contexto_ssl(certs: Path) -> ssl.SSLContext:
    """Verifica contra la CA de la prueba: la verificación también es parte del costo."""
    return ssl.create_default_context(cafile=certs / "ca.pem")


def sesion(certs: Path) -> aiohttp.ClientSession:
    # limit=0: sin tope de conexiones. El de 100 por defecto limitaría la concurrencia sin avisar.
    conector = aiohttp.TCPConnector(limit=0, limit_per_host=0, resolver=ResolverLocal(), ssl=contexto_ssl(certs))
    return aiohttp.ClientSession(connector=conector, timeout=_TIMEOUT, auto_decompress=False)


class Motor:
    def __init__(
        self,
        escenario: Escenario,
        sesion: aiohttp.ClientSession,
        puerto: int,
        semilla: int,
        registro: Registro | None,
        contadores: metricas.Contadores,
        procesos: int = 1,
        spool: Spool | None = None,
    ) -> None:
        self.e = escenario
        self._sesion = sesion
        self._puerto = puerto
        self._procesos = procesos
        self._spool = spool
        self._registro = registro
        self.contadores = contadores
        self._loop = asyncio.get_running_loop()
        items = items_por_repo(escenario.repos, escenario.items, semilla)
        self.plan = Planificador(items, config.INTERVALO, self._loop.time())
        self.items = SeguimientoItems()
        # Lugares libres esperando tarea: el despachador le entrega cada tarea directamente a uno.
        self._libres: deque[asyncio.Future[tuple[Tarea, float]]] = deque()
        self._hay_libre = asyncio.Event()
        self._cambio = asyncio.Event()

    @property
    def spool(self) -> Spool | None:
        return self._spool

    async def correr(self) -> None:
        lugares = [asyncio.create_task(self._lugar()) for _ in range(self.e.lugares)]
        try:
            await self._despachar()
        finally:
            for lugar in lugares:
                lugar.cancel()
            await asyncio.gather(*lugares, return_exceptions=True)

    async def _despachar(self) -> None:
        while True:
            while not self._libres:
                self._hay_libre.clear()
                await self._hay_libre.wait()
            # Freno de la subida: si lo pendiente pasa el límite, no se empieza nada nuevo; lo que
            # está en curso termina normalmente (la descarga nunca espera a la subida).
            while self._spool is not None and self._spool.frenar():
                self.contadores.frenado(0.1)
                await asyncio.sleep(0.1)
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
            self._libres.popleft().set_result(tomada)

    async def _lugar(self) -> None:
        while True:
            turno: asyncio.Future[tuple[Tarea, float]] = self._loop.create_future()
            self._libres.append(turno)
            self._hay_libre.set()
            tarea, listo_en = await turno
            despacho = self._loop.time() - listo_en
            try:
                await self._ejecutar(tarea, despacho)
            finally:
                self._cambio.set()

    async def _ejecutar(self, tarea: Tarea, despacho: float) -> None:
        r = await pedido.hacer(
            self._sesion,
            tarea,
            resolucion.url(tarea, self._puerto, self._procesos),
            self.contadores.recibidos,
            self._spool.nuevo if self._spool else None,
        )
        pedido.aplicar(self.plan, self.items, self.contadores, tarea, r, despacho)
        if self._registro is not None:
            await self._registro.registrar(tarea, r.codigo, r.recibidos, r.sha)


async def _medir_bucle(contadores: metricas.Contadores) -> None:
    """Retraso del bucle: cuánto se pasa una corrutina que duerme 100 ms."""
    loop = asyncio.get_running_loop()
    while True:
        t = loop.time()
        await asyncio.sleep(0.1)
        contadores.bucle(loop.time() - t - 0.1)


async def _muestrear(muestreador: metricas.Muestreador, motor: Motor) -> None:
    loop = asyncio.get_running_loop()
    while True:
        # Si el bucle se atrasa, la fila cubre más de un segundo; no se recuperan filas perdidas.
        await asyncio.sleep(1.0)
        extras = None
        if motor.spool is not None:
            await motor.spool.actualizar()
            extras = motor.spool.metricas()
        muestreador.muestra(motor.plan.en_curso, motor.plan.listos_sin_atender(loop.time()), extras)


async def control(
    sesion: aiohttp.ClientSession, puerto: int, ruta: str, metodo: str = "GET", procesos: int = 1
) -> dict[str, Any]:
    """Llama a la ruta de control de cada proceso del servidor y combina las respuestas."""
    estados = []
    for i in range(procesos):
        async with sesion.request(metodo, resolucion.url_control(puerto + i, ruta)) as respuesta:
            respuesta.raise_for_status()
            estados.append(await respuesta.json())
    return metricas.combinar_estados(estados) if ruta == "estado" else estados[0]


async def correr(
    escenario: Escenario,
    puerto: int,
    certs: Path,
    salida: Path,
    semilla: int = config.SEMILLA,
    dsn: str | None = None,
    tamano_pool: int = 10,
    procesos: int = 1,
    puerto_s3: int = config.PUERTO_S3,
    dir_spool: Path = Path("spool"),
) -> dict[str, Any]:
    """Corre el escenario contra un servidor ya levantado y escribe CSV y JSON en `salida`.

    Con `subida`, además: el spool en `dir_spool` (y en /dev/shm), el subidor en otro proceso y el
    S3 falso ya levantado en `puerto_s3`.
    """
    contadores = metricas.Contadores(con_lock=False)
    subida = _Subida(dir_spool, certs, puerto_s3, escenario.modo_subida, escenario.subidor_mismo_nucleo) if escenario.subida else None
    async with sesion(certs) as cliente:
        await control(cliente, puerto, "reiniciar", "POST", procesos)
        if subida is not None:
            await subida.arrancar(cliente)
        registro = await Registro.abrir(dsn, tamano_pool, contadores) if escenario.db and dsn else None
        if escenario.db and registro is None:
            raise ValueError("el escenario usa --db y falta el DSN de PostgreSQL")
        motor = Motor(escenario, cliente, puerto, semilla, registro, contadores, procesos, subida.spool if subida else None)
        muestreador = metricas.Muestreador(contadores, escenario.calentamiento, escenario.lugares)
        terminado_solo = await _durante(
            escenario.calentamiento + escenario.duracion,
            motor.correr,
            [lambda: _medir_bucle(contadores), lambda: _muestrear(muestreador, motor)],
            contadores,
        )
        if registro is not None:
            await registro.cerrar()
    # El estado del servidor se pide con otra sesión: la del motor puede tener conexiones cortadas.
    async with sesion(certs) as cliente:
        servidor = await control(cliente, puerto, "estado", procesos=procesos)
        resumen_subida = await subida.terminar(cliente) if subida is not None else None
    resumen = _resumen(escenario, semilla, puerto, contadores, servidor, terminado_solo)
    if resumen_subida is not None:
        resumen["subida"] = resumen_subida
    resumen["bucle"] = muestreador.resumen_bucle()
    metricas.escribir(salida, muestreador.filas, resumen)
    return resumen


class _Subida:
    """El spool, el subidor y el estado del S3 falso de un escenario con subida.

    El subidor corre según `modo`: `proceso` (aparte, en su propio núcleo), `hilos` (un hilo con el
    pool de s3transfer dentro de este proceso) o `corrutinas` (un pool de corrutinas con
    aiobotocore dentro de este bucle). El spool es el mismo en los tres.
    """

    def __init__(self, dir_spool: Path, certs: Path, puerto_s3: int, modo: str, mismo_nucleo: bool = False) -> None:
        if modo not in config.MODOS_SUBIDA:
            raise ValueError(f"modo de subida desconocido: {modo} (opciones: {', '.join(config.MODOS_SUBIDA)})")
        self.modo = modo
        self.mismo_nucleo = mismo_nucleo
        """Si el subidor hereda el núcleo del motor en vez de ir al suyo (SUBIDOR_CPU)."""
        self.dirs = Directorios.en(dir_spool.resolve())
        self.spool = Spool(self.dirs, en_memoria=modo == "corrutinas")
        self._certs = certs
        self._puerto_s3 = puerto_s3
        self._proceso: Any = None
        self._corrutinas: Any = None
        self._tarea: asyncio.Task[None] | None = None

    def _url(self, ruta: str) -> str:
        return f"https://s3.{config.DOMINIO}:{self._puerto_s3}/__s3/{ruta}"

    async def arrancar(self, cliente: aiohttp.ClientSession) -> None:
        import multiprocessing
        import os

        from concurrencia import subidor

        await asyncio.to_thread(self.dirs.preparar)
        async with cliente.post(self._url("reiniciar")) as respuesta:
            respuesta.raise_for_status()
        endpoint = f"https://s3.{config.DOMINIO}:{self._puerto_s3}"
        ca = self._certs / "ca.pem"
        if self.modo == "corrutinas":
            from concurrencia.subida_async import SubidorCorrutinas

            self._corrutinas = SubidorCorrutinas(self.dirs, self.spool, endpoint, ca, config.SUBIDAS_EN_PARALELO)
            self._tarea = asyncio.create_task(self._corrutinas.correr())
            return
        if self.modo == "hilos":
            import threading

            self._proceso = threading.Thread(
                target=subidor.correr,
                args=(self.dirs, endpoint, ca, config.SUBIDAS_EN_PARALELO, None, True),
                daemon=True,
            )
        else:
            cpu = None if self.mismo_nucleo else os.environ.get("SUBIDOR_CPU")
            self._proceso = multiprocessing.get_context("spawn").Process(
                target=subidor.correr,
                args=(self.dirs, endpoint, ca, config.SUBIDAS_EN_PARALELO, int(cpu) if cpu else None),
                daemon=True,
            )
        self._proceso.start()

    async def terminar(self, cliente: aiohttp.ClientSession) -> dict[str, Any]:
        """Para el subidor y junta lo que quedó: subido, pendiente y lo que recibió el S3 falso."""
        if self._corrutinas is not None and self._tarea is not None:
            self._corrutinas.parar()
            try:
                async with asyncio.timeout(30):
                    await self._tarea
            except TimeoutError:
                self._tarea.cancel()
        else:
            await asyncio.to_thread(self.dirs.parar.touch)
            await asyncio.to_thread(self._proceso.join, 30)
        await self.spool.actualizar()
        async with cliente.get(self._url("estado")) as respuesta:
            s3 = await respuesta.json()
        estado = self.spool.subidor
        resumen = {
            "modo": self.modo,
            "mismo_nucleo": self.mismo_nucleo,
            "paralelo": config.SUBIDAS_EN_PARALELO,
            "entregado_bytes": dict(self.spool.entregado),
            "pendiente_final_bytes": {s: self.spool.pendiente(s) for s in ("ram", "disco")},
            "subidor": {k: v for k, v in estado.items() if k != "cpu"},
            "subidor_cpu": estado.get("cpu", []),
            "s3": s3,
        }
        await asyncio.to_thread(self.dirs.limpiar)
        return resumen


async def _durante(
    segundos: float,
    principal: Callable[[], Awaitable[None]],
    auxiliares: list[Callable[[], Awaitable[None]]],
    contadores: metricas.Contadores,
) -> bool:
    """Corre `principal` hasta que termine o pasen `segundos`. Devuelve si terminó sola."""
    contadores.instalar_gc()
    tareas = [asyncio.create_task(a()) for a in auxiliares]  # type: ignore[arg-type]
    try:
        async with asyncio.timeout(segundos):
            await principal()
        return True
    except TimeoutError:
        return False
    finally:
        contadores.quitar_gc()
        for tarea in tareas:
            tarea.cancel()
        await asyncio.gather(*tareas, return_exceptions=True)


def _resumen(
    escenario: Escenario,
    semilla: int,
    puerto: int,
    contadores: metricas.Contadores,
    servidor: dict[str, Any],
    terminado_solo: bool,
) -> dict[str, Any]:
    return {
        "escenario": asdict(escenario),
        "semilla": semilla,
        "puerto": puerto,
        "terminado_antes_de_tiempo": terminado_solo,
        "errores_por_tipo": dict(contadores.errores_por_tipo),
        "servidor": servidor,
        "versiones": {
            "python": sys.version.split()[0],
            "aiohttp": aiohttp.__version__,
            "plataforma": platform.platform(),
        },
        "supuestos": {
            "ancho_por_conexion_mb_s": config.ANCHO_POR_CONEXION / config.MB,
            "intervalo_s": config.INTERVALO,
        },
    }
