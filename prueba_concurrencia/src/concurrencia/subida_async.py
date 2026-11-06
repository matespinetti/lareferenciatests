"""Subidor con corrutinas (modo de subida `corrutinas`): un pool de corrutinas dentro del bucle del
motor que sube con aiobotocore, como propone la especificación técnica (aioboto3).

Toma los mismos archivos del spool que el subidor en proceso aparte, así la única diferencia es
dónde corre la subida. Lee los archivos con `asyncio.to_thread` para no bloquear el bucle, y
escribe su estado directo en el `Spool` (no hace falta archivo: comparte el proceso).
"""

import asyncio
import os
from pathlib import Path
from typing import Any

from concurrencia import config
from concurrencia.spool import Directorios, Spool
from concurrencia.subidor import clave


class SubidorCorrutinas:
    def __init__(self, dirs: Directorios, spool: Spool, endpoint: str, ca: Path, paralelo: int) -> None:
        self._dirs = dirs
        self._spool = spool
        self._endpoint = endpoint
        self._ca = ca
        self._paralelo = paralelo
        self._parar = asyncio.Event()
        self.estado: dict[str, Any] = {"bytes_ram": 0, "bytes_disco": 0, "archivos": 0, "errores": 0, "cpu": []}
        spool.subidor = self.estado

    def parar(self) -> None:
        self._parar.set()

    async def correr(self) -> None:
        from concurrencia.hilos import resolver_local

        # aiobotocore resuelve nombres con getaddrinfo: `*.sim.localhost` tiene que ir a 127.0.0.1.
        with resolver_local():
            await self._correr()

    async def _correr(self) -> None:
        from aiobotocore.config import AioConfig
        from aiobotocore.session import get_session

        cfg = AioConfig(
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            max_pool_connections=self._paralelo * 2,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        sesion = get_session()
        async with sesion.create_client(
            "s3", endpoint_url=self._endpoint, region_name="us-east-1", aws_access_key_id="prueba",
            aws_secret_access_key="prueba", verify=str(self._ca), config=cfg,
        ) as s3:  # fmt: skip
            cupos = asyncio.Semaphore(self._paralelo)
            en_curso: dict[Path, asyncio.Task[None]] = {}
            while not self._parar.is_set():
                for ruta, spool, tamano in await asyncio.to_thread(self._listos):
                    if ruta not in en_curso:
                        en_curso[ruta] = asyncio.create_task(self._subir(s3, cupos, ruta, spool, tamano))
                for ruta in [r for r, t in en_curso.items() if t.done()]:
                    del en_curso[ruta]
                self.estado["en_curso"] = len(en_curso)
                try:
                    async with asyncio.timeout(0.05):
                        await self._parar.wait()
                except TimeoutError:
                    pass
            for tarea in en_curso.values():
                tarea.cancel()
            await asyncio.gather(*en_curso.values(), return_exceptions=True)
        self.estado["final"] = True

    def _listos(self) -> list[tuple[Path, str, int]]:
        salida = []
        for listos, spool in zip(self._dirs.listos(), ("ram", "disco"), strict=True):
            try:
                with os.scandir(listos) as entradas:
                    for entrada in entradas:
                        salida.append((Path(entrada.path), spool, entrada.stat().st_size))
            except FileNotFoundError:
                continue
        return salida

    async def _subir(self, s3: Any, cupos: asyncio.Semaphore, ruta: Path, spool: str, tamano: int) -> None:
        async with cupos:
            try:
                if tamano < config.PARTE_S3:
                    cuerpo = await asyncio.to_thread(ruta.read_bytes)
                    await s3.put_object(Bucket=config.BUCKET, Key=clave(ruta.name), Body=cuerpo)
                else:
                    await self._multipart(s3, ruta, tamano)
            except Exception:
                # Se reintenta desde el spool: el archivo sigue ahí y el próximo barrido lo vuelve a tomar.
                self.estado["errores"] += 1
                return
            await asyncio.to_thread(ruta.unlink, True)
            self.estado[f"bytes_{spool}"] += tamano
            self.estado["archivos"] += 1

    async def _multipart(self, s3: Any, ruta: Path, tamano: int) -> None:
        key = clave(ruta.name)
        inicio = await s3.create_multipart_upload(Bucket=config.BUCKET, Key=key)
        partes = []
        for numero, desde in enumerate(range(0, tamano, config.PARTE_S3), start=1):
            datos = await asyncio.to_thread(_leer, ruta, desde, config.PARTE_S3)
            parte = await s3.upload_part(
                Bucket=config.BUCKET, Key=key, UploadId=inicio["UploadId"], PartNumber=numero, Body=datos
            )
            partes.append({"ETag": parte["ETag"], "PartNumber": numero})
        await s3.complete_multipart_upload(
            Bucket=config.BUCKET, Key=key, UploadId=inicio["UploadId"], MultipartUpload={"Parts": partes}
        )


def _leer(ruta: Path, desde: int, largo: int) -> bytes:
    with open(ruta, "rb") as f:
        f.seek(desde)
        return f.read(largo)
