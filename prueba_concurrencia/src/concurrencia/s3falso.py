"""S3 falso: el subconjunto de la API de S3 que usa boto3 para subir, sobre HTTPS.

Atiende PUT de objetos, multipart (iniciar, subir partes, completar y abortar) y descarta lo que
recibe: solo cuenta bytes, objetos y partes. Sirve para medir el costo de la subida del lado del
descargador, no a S3. Corre en su propio proceso.
"""

import asyncio
import itertools
import time
from pathlib import Path

import psutil
from aiohttp import web

from concurrencia import config

_IDS = itertools.count(1)


class S3Falso:
    def __init__(self) -> None:
        self.bytes = 0
        self.objetos = 0
        self.partes = 0
        self.multipart = 0
        self._cpu: list[float] = []
        self._proceso = psutil.Process()
        self._muestreo: asyncio.Task[None] | None = None

    def app(self) -> web.Application:
        app = web.Application(client_max_size=0)
        app.router.add_get("/__s3/estado", self._estado)
        app.router.add_post("/__s3/reiniciar", self._reiniciar)
        app.router.add_put("/{bucket}/{clave:.+}", self._put)
        app.router.add_post("/{bucket}/{clave:.+}", self._post)
        app.router.add_delete("/{bucket}/{clave:.+}", self._abortar)
        app.on_startup.append(self._arrancar)
        app.on_cleanup.append(self._parar)
        return app

    async def _leer(self, request: web.Request) -> int:
        recibidos = 0
        async for trozo in request.content.iter_chunked(config.CHUNK):
            recibidos += len(trozo)
        self.bytes += recibidos
        return recibidos

    async def _put(self, request: web.Request) -> web.Response:
        await self._leer(request)
        if "partNumber" in request.query:
            self.partes += 1
        else:
            self.objetos += 1
        return web.Response(headers={"ETag": f'"{next(_IDS):032x}"'})

    async def _post(self, request: web.Request) -> web.Response:
        bucket, clave = request.match_info["bucket"], request.match_info["clave"]
        if "uploads" in request.query:
            self.multipart += 1
            cuerpo = (
                "<InitiateMultipartUploadResult><Bucket>{}</Bucket><Key>{}</Key><UploadId>{}</UploadId>"
                "</InitiateMultipartUploadResult>"
            ).format(bucket, clave, next(_IDS))
        else:
            await request.read()
            self.objetos += 1
            cuerpo = (
                "<CompleteMultipartUploadResult><Bucket>{}</Bucket><Key>{}</Key><ETag>\"{:032x}\"</ETag>"
                "</CompleteMultipartUploadResult>"
            ).format(bucket, clave, next(_IDS))
        return web.Response(text='<?xml version="1.0" encoding="UTF-8"?>' + cuerpo, content_type="application/xml")

    async def _abortar(self, request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def _estado(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"bytes": self.bytes, "objetos": self.objetos, "partes": self.partes,
             "multipart": self.multipart, "cpu": self._cpu}  # fmt: skip
        )

    async def _reiniciar(self, request: web.Request) -> web.Response:
        self.bytes = self.objetos = self.partes = self.multipart = 0
        self._cpu = []
        return web.json_response({"ok": True})

    async def _arrancar(self, app: web.Application) -> None:
        self._muestreo = asyncio.create_task(self._muestrear_cpu())

    async def _parar(self, app: web.Application) -> None:
        if self._muestreo:
            self._muestreo.cancel()

    async def _muestrear_cpu(self) -> None:
        anterior, t_anterior = self._proceso.cpu_times(), time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            actual, t_actual = self._proceso.cpu_times(), time.monotonic()
            usado = (actual.user + actual.system) - (anterior.user + anterior.system)
            self._cpu.append(round(100 * usado / (t_actual - t_anterior), 1))
            anterior, t_anterior = actual, t_actual


async def servir(certs: Path, puerto: int) -> None:
    from concurrencia.servidor import contexto_ssl

    runner = web.AppRunner(S3Falso().app(), access_log=None, handle_signals=True)
    await runner.setup()
    sitio = web.TCPSite(runner, "127.0.0.1", puerto, ssl_context=contexto_ssl(certs), backlog=4096)
    await sitio.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
