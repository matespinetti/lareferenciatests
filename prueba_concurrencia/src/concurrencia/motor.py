"""Mini motor con corrutinas: el modo previsto para el descargador.

Un despachador saca del planificador las tareas habilitadas mientras haya un lugar libre, y N
lugares de ejecución las hacen con un único `ClientSession` compartido. Toda la E/S es
asíncrona: aiohttp para HTTP y asyncpg para la base (`--db`).
"""

import asyncio
import os
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

from concurrencia import config, metricas, muestra_real, pedido, resolucion, resolucion_real
from concurrencia.config import Escenario
from concurrencia.db import Registro
from concurrencia.distribucion import items_por_repo
from concurrencia.muestra_real import Catalogo
from concurrencia.planificador import Planificador, SeguimientoItems, Tarea
from concurrencia.spool import Directorios, Spool

_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)


class ResolverLocal(AbstractResolver):
    """Manda cada `*.sim.localhost` a `IP_SERVIDOR` (127.0.0.1 si no se cambia) y rechaza cualquier otro host."""

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if not host.endswith("." + config.DOMINIO):
            raise OSError(f"host fuera de la prueba: {host}")
        return [
            {
                "hostname": host,
                "host": config.IP_SERVIDOR,
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


def sesion_real(
    contacto: str, resolver: AbstractResolver | None = None, contexto: ssl.SSLContext | None = None
) -> aiohttp.ClientSession:
    """Contra repositorios reales (modo muestra): el DNS del sistema, TLS verificado y un User-Agent
    con contacto. `resolver` y `contexto` son solo para los tests, que levantan repositorios locales."""
    if not contacto.strip():
        raise ValueError("el modo muestra necesita CONTACTO: el correo para el User-Agent")
    conector = aiohttp.TCPConnector(limit=0, limit_per_host=0, resolver=resolver, ssl=contexto or True)
    return aiohttp.ClientSession(
        connector=conector,
        timeout=_TIMEOUT,
        headers={"User-Agent": f"pruebamateo-descarga/0.1 (contacto: {contacto.strip()})"},
    )


_ERRORES_RED = frozenset({
    "ClientConnectorError", "ClientConnectorDNSError", "ClientOSError", "ServerDisconnectedError", "TimeoutError",
    "ServerTimeoutError", "ConnectionTimeoutError", "SocketTimeoutError", "ClientPayloadError", "OSError",
})  # fmt: skip
_ERRORES_TLS = frozenset({"ClientConnectorCertificateError", "ClientConnectorSSLError", "ClientSSLError"})
ABANDONO_RED_SEGUIDOS = 3


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
        catalogo: Catalogo | None = None,
    ) -> None:
        self.e = escenario
        self._sesion = sesion
        self._puerto = puerto
        self._procesos = procesos
        self._spool = spool
        self._registro = registro
        self.contadores = contadores
        self._loop = asyncio.get_running_loop()
        self.items = SeguimientoItems()
        self.catalogo = catalogo
        if catalogo is None:
            items = items_por_repo(escenario.repos, escenario.items, semilla)
            self.plan = Planificador(items, escenario.intervalo, self._loop.time())
        else:
            self.plan = Planificador(catalogo.items_por_repo(), escenario.intervalo, self._loop.time(), catalogo.precargadas)
            for (repo, item), archivos in catalogo.archivos_por_item.items():
                self.items.resuelto(repo, item, archivos)
        # Modo muestra: un pedido por fila, los repositorios abandonados y los hosts con TLS inválido.
        self.pedidos: list[tuple[Any, ...]] = []
        self.abandonados: dict[int, str] = {}
        self.tls_invalido: set[str] = set()
        self._red_seguidos: dict[int, int] = {}
        self._403_seguidos: dict[int, int] = {}
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
        if self.catalogo is not None:
            await self._ejecutar_real(tarea, despacho)
            return
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


    async def _ejecutar_real(self, tarea: Tarea, despacho: float) -> None:
        """Un pedido a un repositorio real. Un certificado inválido se reintenta una vez sin verificar,
        en el mismo turno (y ese host ya va sin verificar), como en el relevamiento."""
        assert self.catalogo is not None
        url = self.catalogo.url(tarea)
        host = muestra_real.host_de(url)
        destino = self._spool.nuevo if self._spool else None
        opciones: dict[str, Any] = {"max_redirects": 5}
        if host in self.tls_invalido:
            opciones["ssl"] = False
        r = await pedido.hacer(self._sesion, tarea, url, self.contadores.recibidos, destino, self._descargas_de, opciones)
        if r.error in _ERRORES_TLS and host not in self.tls_invalido:
            self._anotar(tarea, host, r, reintento_tls=False)
            self.tls_invalido.add(host)
            r = await pedido.hacer(self._sesion, tarea, url, self.contadores.recibidos, destino, self._descargas_de,
                                   {**opciones, "ssl": False})  # fmt: skip
            self._anotar(tarea, host, r, reintento_tls=True)
        else:
            self._anotar(tarea, host, r, reintento_tls=False)
        pedido.aplicar(self.plan, self.items, self.contadores, tarea, r, despacho)
        if (motivo := self._motivo_abandono(tarea.repo, r)) and tarea.repo not in self.abandonados:
            self.abandonados[tarea.repo] = motivo
            self.plan.abandonar(tarea.repo)
        if self._registro is not None:
            await self._registro.registrar(tarea, r.codigo, r.recibidos, r.sha)

    def _descargas_de(self, tarea: Tarea, cuerpo: bytes, url: str) -> list[Tarea]:
        assert self.catalogo is not None
        urls = resolucion_real.descargas_de_pagina(cuerpo, url, config.ARCHIVOS_POR_PAGINA)
        return self.catalogo.registrar(tarea.repo, tarea.item, urls)

    def _anotar(self, tarea: Tarea, host: str, r: pedido.Resultado, reintento_tls: bool) -> None:
        assert self.catalogo is not None
        url = self.catalogo.url(tarea)
        self.pedidos.append((tarea.repo, host, tarea.tipo.value, round(r.inicio, 4), round(r.fin, 4), r.codigo,
                             r.recibidos, r.error or "", int(reintento_tls), url, r.detalle or ""))  # fmt: skip

    def _motivo_abandono(self, repo: int, r: pedido.Resultado) -> str | None:
        """Como en el relevamiento y en producción: ante dos 403 seguidos (anti-bot), un Retry-After
        eterno o tres errores de red seguidos, el repositorio no recibe más pedidos en esta corrida."""
        # Dos 403 seguidos, como el relevamiento: un archivo restringido suelto también da 403.
        if r.codigo == 403:
            self._403_seguidos[repo] = self._403_seguidos.get(repo, 0) + 1
            if self._403_seguidos[repo] >= 2:
                return "http_403"
            return None
        self._403_seguidos[repo] = 0
        if r.codigo in (429, 503) and r.retry_after > config.RETRY_AFTER_MAXIMO:
            return f"retry_after_{int(r.retry_after)}"
        if r.error in _ERRORES_RED or r.error in _ERRORES_TLS:
            self._red_seguidos[repo] = self._red_seguidos.get(repo, 0) + 1
            if self._red_seguidos[repo] >= ABANDONO_RED_SEGUIDOS:
                return f"red_{r.error}"
        else:
            self._red_seguidos[repo] = 0
        return None


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
        extras: dict[str, float] = {"repos_con_trabajo": motor.plan.con_trabajo()}
        if motor.spool is not None:
            await motor.spool.actualizar()
            extras |= motor.spool.metricas()
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
    if escenario.muestra:
        return await _correr_muestra(escenario, certs, salida, semilla, dsn, tamano_pool, puerto_s3, dir_spool)
    contadores = metricas.Contadores(con_lock=False)
    subida = (
        _Subida(dir_spool, certs, puerto_s3, escenario.modo_subida, escenario.subidor_mismo_nucleo, escenario.id)
        if escenario.subida
        else None
    )
    async with sesion(certs) as cliente:
        await control(cliente, puerto, "reiniciar", "POST", procesos)
        if subida is not None:
            await subida.arrancar(cliente)
        registro = (
            await Registro.abrir(dsn, tamano_pool, contadores, escenario.db_lotes) if escenario.db and dsn else None
        )
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


async def _correr_muestra(
    escenario: Escenario,
    certs: Path,
    salida: Path,
    semilla: int,
    dsn: str | None,
    tamano_pool: int,
    puerto_s3: int,
    dir_spool: Path,
    *,
    resolver: AbstractResolver | None = None,
    contexto: ssl.SSLContext | None = None,
) -> dict[str, Any]:
    """Modo muestra: el mismo motor contra repositorios reales (pedido por el usuario, nov 2026).

    No hay servidor simulado que cuente la cortesía: la verifica el motor sobre su propio registro de
    pedidos (`<salida>-pedidos.csv`). La subida va igual que en los t*: S3 real con S3_BUCKET o el S3
    falso (los tests), con su propia sesión. `resolver` y `contexto` son solo para los tests."""
    catalogo = await asyncio.to_thread(
        muestra_real.cargar, Path(config.MUESTRA), escenario.parte, escenario.partes, config.TOPE_REPO_PASO
    )
    contadores = metricas.Contadores(con_lock=False)
    subida = (
        _Subida(dir_spool, certs, puerto_s3, escenario.modo_subida, escenario.subidor_mismo_nucleo, escenario.id)
        if escenario.subida
        else None
    )
    async with sesion_real(config.CONTACTO, resolver, contexto) as cliente:
        if subida is not None:
            # La sesión con la CA de prueba solo hace falta para el S3 falso; con S3 real no se usa.
            if subida.real:
                await subida.arrancar(cliente)
            else:
                async with sesion(certs) as control_s3:
                    await subida.arrancar(control_s3)
        registro = (
            await Registro.abrir(dsn, tamano_pool, contadores, escenario.db_lotes) if escenario.db and dsn else None
        )
        if escenario.db and registro is None:
            raise ValueError("el escenario usa --db y falta el DSN de PostgreSQL")
        motor = Motor(escenario, cliente, 0, semilla, registro, contadores, 1, subida.spool if subida else None, catalogo)
        muestreador = metricas.Muestreador(contadores, escenario.calentamiento, escenario.lugares)
        terminado_solo = await _durante(
            escenario.calentamiento + escenario.duracion,
            motor.correr,
            [lambda: _medir_bucle(contadores), lambda: _muestrear(muestreador, motor)],
            contadores,
        )
        if registro is not None:
            await registro.cerrar()
    resumen_subida = None
    if subida is not None:
        async with sesion_real(config.CONTACTO) if subida.real else sesion(certs) as control_s3:
            resumen_subida = await subida.terminar(control_s3)
    servidor = {**muestra_real.verificar_cortesia(motor.pedidos, escenario.intervalo), "cpu_servidor": [], "real": True}
    resumen = _resumen(escenario, semilla, 0, contadores, servidor, terminado_solo)
    resumen["despliegue"]["ip_servidor"] = None
    resumen["despliegue"]["dos_maquinas"] = False
    resumen["muestra"] = {
        **catalogo.resumen(),
        "directorio": config.MUESTRA,
        "abandonados": {catalogo.repos[r]: motivo for r, motivo in sorted(motor.abandonados.items())},
        "hosts_tls_invalido": len(motor.tls_invalido),
        "tope_bytes_por_repo": config.TOPE_REPO_PASO,
    }
    if resumen_subida is not None:
        resumen["subida"] = resumen_subida
    resumen["bucle"] = muestreador.resumen_bucle()
    metricas.escribir(salida, muestreador.filas, resumen)
    metricas.escribir_pedidos(salida.with_name(salida.name + "-pedidos.csv"), motor.pedidos)
    muestra_real.escribir_indice(salida.with_name(salida.name + "-indice.json"), catalogo)
    return resumen


class _Subida:
    """El spool, el subidor y el estado del S3 falso de un escenario con subida.

    El subidor corre según `modo`: `proceso` (aparte, en su propio núcleo), `hilos` (un hilo con el
    pool de s3transfer dentro de este proceso) o `corrutinas` (un pool de corrutinas con
    aiobotocore dentro de este bucle). El spool es el mismo en los tres.

    Con `S3_BUCKET` sube a S3 real, bajo `S3_PREFIJO/<escenario>-<fecha>/`, en vez de al S3 falso.
    """

    def __init__(
        self, dir_spool: Path, certs: Path, puerto_s3: int, modo: str, mismo_nucleo: bool = False, corrida: str = ""
    ) -> None:
        import time

        if modo not in config.MODOS_SUBIDA:
            raise ValueError(f"modo de subida desconocido: {modo} (opciones: {', '.join(config.MODOS_SUBIDA)})")
        self.real = config.S3_BUCKET is not None
        if self.real and modo == "corrutinas":
            raise ValueError("con S3 real la subida corre en proceso aparte o en hilos, no en corrutinas")
        if config.SUBIDOR_CLIENTE not in config.CLIENTES_SUBIDA:
            raise ValueError(f"cliente de subida desconocido: {config.SUBIDOR_CLIENTE} ({', '.join(config.CLIENTES_SUBIDA)})")
        if config.SUBIDOR_CLIENTE == "crt" and not (self.real and modo != "corrutinas"):
            raise ValueError("el cliente CRT es solo contra S3 real (S3_BUCKET), con el subidor en proceso o hilos")
        self.bucket = config.S3_BUCKET or config.BUCKET
        self.prefijo = f"{config.S3_PREFIJO}/{corrida}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}/" if self.real else ""
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
        if not self.real:
            async with cliente.post(self._url("reiniciar")) as respuesta:
                respuesta.raise_for_status()
        endpoint = None if self.real else f"https://s3.{config.DOMINIO}:{self._puerto_s3}"
        ca = self._certs / "ca.pem"
        destino = {"bucket": self.bucket, "prefijo": self.prefijo}
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
                kwargs=destino,
                daemon=True,
            )
        else:
            cpu = None if self.mismo_nucleo else os.environ.get("SUBIDOR_CPU")
            self._proceso = multiprocessing.get_context("spawn").Process(
                target=subidor.correr,
                args=(self.dirs, endpoint, ca, config.SUBIDAS_EN_PARALELO, int(cpu) if cpu else None),
                kwargs=destino,
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
        estado = self.spool.subidor
        if self.real:
            # S3 real no informa nada: lo subido es lo que cuenta el subidor.
            s3 = {"objetos": estado.get("archivos", 0), "multipart": 0}
        else:
            async with cliente.get(self._url("estado")) as respuesta:
                s3 = await respuesta.json()
        resumen = {
            "destino": f"s3://{self.bucket}/{self.prefijo}" if self.real else "s3falso",
            "cliente": config.SUBIDOR_CLIENTE if self.modo != "corrutinas" else "aiobotocore",
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
        # En la validación en dos máquinas, el servidor simulado está en otra instancia (IP_SERVIDOR).
        "despliegue": {
            "ip_servidor": config.IP_SERVIDOR,
            "dos_maquinas": config.IP_SERVIDOR != "127.0.0.1",
            "s3": "real" if config.S3_BUCKET else "falso",
            # Con Docker: `puente` (la red por defecto) o `host` (docker/compose.red-host.yaml).
            "red_docker": os.environ.get("RED_DOCKER"),
        },
        "versiones": {
            "python": sys.version.split()[0],
            "aiohttp": aiohttp.__version__,
            "plataforma": platform.platform(),
        },
        "supuestos": {
            "ancho_por_conexion_mb_s": config.ANCHO_POR_CONEXION / config.MB,
            "ancho_sigma": config.ANCHO_SIGMA,
            "descargas_por_item": config.DESCARGAS_POR_ITEM,
            "intervalo_s": escenario.intervalo,
        },
    }
