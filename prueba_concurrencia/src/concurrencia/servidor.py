"""Servidor simulado: muchos repositorios detrás de `/repo/<id>/...`, servidos por HTTPS.

Latencias y tamaños calibrados con los pilotos, archivos en streaming a la velocidad por conexión
de cada repositorio (distinta para cada uno, alrededor de la mediana medida), 429/503 configurables y control de cortesía desde afuera: para cada repositorio
cuenta los pedidos simultáneos y los intervalos menores al configurado.

Las rutas `/__control/...` no son de ningún repositorio y no cuentan para la cortesía.
"""

import asyncio
import os
import random
import ssl
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil
from aiohttp import web

from concurrencia import config, distribucion

_RELLENO = b"<p>" + b"lorem ipsum dolor sit amet " * (config.PAGINA_MAXIMO // 27 + 1)
_BLOQUE = os.urandom(config.CHUNK)


@dataclass
class Cortesia:
    """Lo que ve el servidor de la cortesía del motor, repositorio por repositorio."""

    intervalo: float
    tolerancia: float = config.TOLERANCIA
    pedidos: int = 0
    simultaneos: int = 0
    intervalos_cortos: int = 0
    ejemplos: list[dict[str, float]] = field(default_factory=list)
    _en_curso: dict[int, int] = field(default_factory=dict)
    _ultimo_fin: dict[int, float] = field(default_factory=dict)

    @property
    def violaciones(self) -> int:
        return self.simultaneos + self.intervalos_cortos

    def inicio(self, repo: int, ahora: float) -> None:
        self.pedidos += 1
        if self._en_curso.get(repo, 0) > 0:
            self.simultaneos += 1
            self._ejemplo(repo, 0.0)
        elif repo in self._ultimo_fin:
            hueco = ahora - self._ultimo_fin[repo]
            if hueco < self.intervalo - self.tolerancia:
                self.intervalos_cortos += 1
                self._ejemplo(repo, hueco)
        self._en_curso[repo] = self._en_curso.get(repo, 0) + 1

    def fin(self, repo: int, ahora: float) -> None:
        self._en_curso[repo] -= 1
        self._ultimo_fin[repo] = ahora

    def _ejemplo(self, repo: int, hueco: float) -> None:
        if len(self.ejemplos) < 20:
            self.ejemplos.append({"repo": repo, "hueco": round(hueco, 4)})


class Red:
    """Tope de ancho de banda de todo el servidor, como la placa de red de la instancia.

    Cada envío reserva su turno en orden de llegada y espera a que le toque.
    """

    def __init__(self, bytes_por_segundo: float) -> None:
        self._ancho = bytes_por_segundo
        self._libre_en = 0.0

    def turno(self, n: int, ahora: float) -> float:
        inicio = max(ahora, self._libre_en)
        self._libre_en = inicio + n / self._ancho
        return inicio


@dataclass
class Parametros:
    semilla: int = config.SEMILLA
    indice: int = 0
    """Número de proceso cuando el servidor corre en varios."""
    intervalo: float = config.INTERVALO
    ancho: float = config.ANCHO_POR_CONEXION
    """Mediana entre repositorios de la velocidad por conexión."""
    ancho_sigma: float = config.ANCHO_SIGMA
    """Dispersión de la velocidad entre repositorios; 0, todos a `ancho`."""
    tam_archivo: int | None = None
    red: float | None = None
    """Tope de bytes por segundo de todo el servidor; None, sin tope."""
    prob_429: float = config.PROB_429
    prob_503: float = config.PROB_503
    latencia: distribucion.LogNormal = distribucion.LATENCIA


class Servidor:
    def __init__(self, parametros: Parametros) -> None:
        self.p = parametros
        self.cortesia = Cortesia(parametros.intervalo)
        self._red = Red(parametros.red) if parametros.red else None
        # El contenido sale de la semilla; la latencia, de un generador propio de cada proceso.
        self._rng = random.Random(f"{parametros.semilla}:{parametros.indice}")
        self._proceso = psutil.Process()
        self._cpu: list[float] = []
        self._muestreo: asyncio.Task[None] | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/repo/{repo}/item/{item}", self._item)
        app.router.add_get("/repo/{repo}/archivo/{item}/{archivo}", self._archivo)
        app.router.add_get("/__control/estado", self._estado)
        app.router.add_post("/__control/reiniciar", self._reiniciar)
        app.on_startup.append(self._arrancar)
        app.on_cleanup.append(self._parar)
        return app

    async def _item(self, request: web.Request) -> web.StreamResponse:
        repo, item = int(request.match_info["repo"]), int(request.match_info["item"])

        async def responder(t0: float) -> web.StreamResponse:
            descargas = distribucion.descargas_del_item(self.p.semilla, repo, item)
            enlaces = b"".join(
                b'<a class="archivo" href="/repo/%d/archivo/%d/%d">archivo %d</a>\n' % (repo, item, k, k)
                for k in range(descargas)
            )
            cabeza = b"<html><head><title>item %d</title></head><body>\n" % item
            pie = b"</body></html>"
            relleno = max(0, distribucion.tam_pagina(self.p.semilla, repo, item) - len(cabeza) - len(enlaces) - len(pie))
            cuerpo = cabeza + enlaces + _RELLENO[:relleno] + pie
            await self._esperar_red(len(cuerpo))
            respuesta = web.Response(body=cuerpo, content_type="text/html", headers=self._cabeceras(t0))
            await respuesta.prepare(request)
            await respuesta.write_eof()
            return respuesta

        return await self._atender(request, repo, responder)

    async def _archivo(self, request: web.Request) -> web.StreamResponse:
        repo = int(request.match_info["repo"])
        item, archivo = int(request.match_info["item"]), int(request.match_info["archivo"])

        async def responder(t0: float) -> web.StreamResponse:
            tamano = distribucion.tam_archivo(self.p.semilla, repo, item, archivo, self.p.tam_archivo)
            respuesta = web.StreamResponse(headers=self._cabeceras(t0))
            respuesta.content_type = "application/pdf"
            respuesta.content_length = tamano
            await respuesta.prepare(request)
            loop = asyncio.get_running_loop()
            inicio = loop.time()
            enviados = 0
            # Cada repositorio entrega a su propia velocidad, siempre la misma.
            ancho = distribucion.ancho_repo(self.p.semilla, repo, self.p.ancho, self.p.ancho_sigma)
            # Cada archivo es único (si no, todos darían el mismo hash y el subidor no subiría casi nada).
            marca = b"%d:%d:%d:%d\n" % (self.p.semilla, repo, item, archivo)
            while enviados < tamano:
                n = min(config.CHUNK, tamano - enviados)
                # Ancho de banda fijo por conexión: no adelantarse al ritmo previsto.
                espera = inicio + enviados / ancho - loop.time()
                if espera > 0:
                    await asyncio.sleep(espera)
                await self._esperar_red(n)
                await respuesta.write((marca + _BLOQUE)[:n] if enviados == 0 else _BLOQUE[:n])
                enviados += n
            await respuesta.write_eof()
            return respuesta

        return await self._atender(request, repo, responder)

    async def _atender(self, request: web.Request, repo: int, responder) -> web.StreamResponse:  # type: ignore[no-untyped-def]
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        self.cortesia.inicio(repo, t0)
        try:
            await asyncio.sleep(self.p.latencia.muestra(self._rng))
            sorteo = self._rng.random()
            if sorteo < self.p.prob_429:
                cabeceras = self._cabeceras(t0) | {"Retry-After": str(config.RETRY_AFTER)}
                return self._cerrar(repo, web.Response(status=429, headers=cabeceras))
            if sorteo < self.p.prob_429 + self.p.prob_503:
                cabeceras = self._cabeceras(t0) | {"Retry-After": str(config.RETRY_AFTER)}
                return self._cerrar(repo, web.Response(status=503, headers=cabeceras))
            respuesta = self._cerrar(repo, await responder(t0))
            return respuesta
        except ConnectionError:
            # El cliente se fue: el pedido termina acá. aiohttp no cancela el handler por eso.
            return web.Response(status=499)
        finally:
            self.cortesia.fin(repo, loop.time())

    async def _esperar_red(self, n: int) -> None:
        if self._red is None:
            return
        loop = asyncio.get_running_loop()
        espera = self._red.turno(n, loop.time()) - loop.time()
        if espera > 0:
            await asyncio.sleep(espera)

    def _cerrar(self, repo: int, respuesta: web.StreamResponse) -> web.StreamResponse:
        if distribucion.cierra_conexion(self.p.semilla, repo):
            respuesta.force_close()
        return respuesta

    @staticmethod
    def _cabeceras(t0: float) -> dict[str, str]:
        return {"X-Tiempo-Servicio": f"{asyncio.get_running_loop().time() - t0:.6f}"}

    async def _estado(self, request: web.Request) -> web.Response:
        c = self.cortesia
        return web.json_response(
            {
                "pedidos": c.pedidos,
                "violaciones": c.violaciones,
                "simultaneos": c.simultaneos,
                "intervalos_cortos": c.intervalos_cortos,
                "ejemplos": c.ejemplos,
                "cpu_servidor": self._cpu,
            }
        )

    async def _reiniciar(self, request: web.Request) -> web.Response:
        self.cortesia = Cortesia(self.p.intervalo)
        self._cpu = []
        return web.json_response({"ok": True})

    async def _arrancar(self, app: web.Application) -> None:
        self._muestreo = asyncio.create_task(self._muestrear_cpu())

    async def _parar(self, app: web.Application) -> None:
        if self._muestreo:
            self._muestreo.cancel()

    async def _muestrear_cpu(self) -> None:
        """CPU propia por segundo, en porcentaje de un núcleo. Solo sirve de control."""
        anterior = self._proceso.cpu_times()
        t_anterior = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            actual, t_actual = self._proceso.cpu_times(), time.monotonic()
            usado = (actual.user + actual.system) - (anterior.user + anterior.system)
            self._cpu.append(round(100 * usado / (t_actual - t_anterior), 1))
            anterior, t_anterior = actual, t_actual


def contexto_ssl(certs: Path) -> ssl.SSLContext:
    contexto = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    contexto.load_cert_chain(certs / "servidor.pem", certs / "servidor.key")
    return contexto


def servir_en_proceso(parametros: Parametros, certs: Path, puerto: int, host: str = "127.0.0.1") -> None:
    """Punto de entrada de cada proceso cuando el servidor corre en varios."""
    try:
        asyncio.run(servir(parametros, certs, puerto, host=host))
    except KeyboardInterrupt:
        pass


async def servir(
    parametros: Parametros, certs: Path, puerto: int, listo: asyncio.Event | None = None, host: str = "127.0.0.1"
) -> None:
    servidor = Servidor(parametros)
    runner = web.AppRunner(servidor.app(), access_log=None, handle_signals=True)
    await runner.setup()
    sitio = web.TCPSite(
        runner, host, puerto, ssl_context=contexto_ssl(certs), backlog=8192, reuse_port=True
    )
    await sitio.start()
    if listo:
        listo.set()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
