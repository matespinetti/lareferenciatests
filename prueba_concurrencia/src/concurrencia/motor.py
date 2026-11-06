"""Mini motor con corrutinas: el modo previsto para el descargador.

Un despachador saca del planificador las tareas habilitadas mientras haya un lugar libre, y N
lugares de ejecución las hacen con un único `ClientSession` compartido. Toda la E/S es
asíncrona: aiohttp para HTTP y asyncpg para la base (`--db`).
"""

import asyncio
import hashlib
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

from concurrencia import config, metricas, resolucion
from concurrencia.config import Escenario
from concurrencia.db import Registro
from concurrencia.distribucion import items_por_repo
from concurrencia.planificador import Planificador, SeguimientoItems, Tarea, Tipo

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
    ) -> None:
        self.e = escenario
        self._sesion = sesion
        self._puerto = puerto
        self._procesos = procesos
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
        t0 = self._loop.time()
        latencia = servicio = None
        codigo: int | None = None
        recibidos = 0
        sha: str | None = None
        try:
            async with self._sesion.get(resolucion.url(tarea, self._puerto, self._procesos)) as respuesta:
                latencia = self._loop.time() - t0
                codigo = respuesta.status
                servicio = float(respuesta.headers.get("X-Tiempo-Servicio", "nan"))
                if codigo in (429, 503):
                    recibidos = len(await respuesta.read())
                    self.contadores.recibidos(recibidos)
                    espera = float(respuesta.headers.get("Retry-After", config.RETRY_AFTER))
                    self.plan.pausar(tarea.repo, self._loop.time() + espera)
                    self.plan.agregar(tarea)
                    self.contadores.estado(codigo)
                elif codigo != 200:
                    recibidos = len(await respuesta.read())
                    self.contadores.recibidos(recibidos)
                    self.contadores.error(f"http_{codigo}")
                elif tarea.tipo is Tipo.RESOLUCION:
                    cuerpo = await respuesta.read()
                    recibidos = len(cuerpo)
                    self.contadores.recibidos(recibidos)
                    descargas = resolucion.descargas(cuerpo)
                    for descarga in descargas:
                        self.plan.agregar(descarga)
                    if self.items.resuelto(tarea.repo, tarea.item, len(descargas)):
                        self.contadores.item_completo()
                else:
                    h = hashlib.sha256()
                    async for chunk in respuesta.content.iter_chunked(config.CHUNK):
                        h.update(chunk)
                        recibidos += len(chunk)
                        self.contadores.recibidos(len(chunk), hasheados=True)
                    if respuesta.content_length is not None and recibidos != respuesta.content_length:
                        self.contadores.error("archivo_incompleto")
                    else:
                        sha = h.hexdigest()
                        if self.items.descargado(tarea.repo, tarea.item):
                            self.contadores.item_completo()
        except (aiohttp.ClientError, TimeoutError, OSError) as error:
            self.contadores.error(type(error).__name__)
        except Exception as error:
            # Un error inesperado se cuenta y no debe dejar el repositorio ocupado para siempre.
            self.contadores.error(f"inesperado_{type(error).__name__}")
        fin = self._loop.time()
        self.plan.terminada(tarea, fin)
        self.contadores.pedido(
            tarea.tipo, fin - t0, latencia, servicio, despacho, descarga_ok=sha is not None,
        )  # fmt: skip
        if self._registro is not None:
            await self._registro.registrar(tarea, codigo, recibidos, sha)


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
        muestreador.muestra(motor.plan.en_curso, motor.plan.listos_sin_atender(loop.time()))


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
) -> dict[str, Any]:
    """Corre el escenario contra un servidor ya levantado y escribe CSV y JSON en `salida`."""
    contadores = metricas.Contadores(con_lock=False)
    async with sesion(certs) as cliente:
        await control(cliente, puerto, "reiniciar", "POST", procesos)
        registro = await Registro.abrir(dsn, tamano_pool, contadores) if escenario.db and dsn else None
        if escenario.db and registro is None:
            raise ValueError("el escenario usa --db y falta el DSN de PostgreSQL")
        motor = Motor(escenario, cliente, puerto, semilla, registro, contadores, procesos)
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
    resumen = _resumen(escenario, semilla, puerto, contadores, servidor, terminado_solo)
    resumen["bucle"] = muestreador.resumen_bucle()
    metricas.escribir(salida, muestreador.filas, resumen)
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
