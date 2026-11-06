"""Subida a S3: el S3 falso entiende a boto3, el spool decide RAM o disco, y el subidor sube."""

import asyncio
import json
import multiprocessing

from aiohttp import web

from concurrencia import config
from concurrencia.s3falso import S3Falso
from concurrencia.servidor import contexto_ssl
from concurrencia.spool import Directorios, Spool
from tests.conftest import puertos_libres


async def _levantar_s3(certs) -> tuple[S3Falso, web.AppRunner, int]:
    s3 = S3Falso()
    runner = web.AppRunner(s3.app(), access_log=None)
    await runner.setup()
    puerto = puertos_libres(1)
    await web.TCPSite(runner, "127.0.0.1", puerto, ssl_context=contexto_ssl(certs)).start()
    return s3, runner, puerto


async def test_boto3_sube_al_s3_falso_con_put_y_multipart(certs, tmp_path):
    from concurrencia.hilos import resolver_local
    from concurrencia.subidor import _cliente

    s3, runner, puerto = await _levantar_s3(certs)
    chico, grande = tmp_path / "chico", tmp_path / "grande"
    chico.write_bytes(b"x" * 100_000)
    grande.write_bytes(b"y" * (2 * config.PARTE_S3 + 1000))

    def subir() -> None:
        from s3transfer.manager import TransferConfig, TransferManager

        with resolver_local():
            cliente = _cliente(f"https://s3.{config.DOMINIO}:{puerto}", certs / "ca.pem", 4)
            gestor = TransferManager(cliente, TransferConfig(multipart_threshold=config.PARTE_S3,
                                                             multipart_chunksize=config.PARTE_S3))  # fmt: skip
            for ruta in (chico, grande):
                gestor.upload(str(ruta), config.BUCKET, f"sha256/{ruta.name}").result()
            gestor.shutdown()

    try:
        await asyncio.to_thread(subir)
    finally:
        await runner.cleanup()
    assert s3.bytes == 100_000 + 2 * config.PARTE_S3 + 1000
    assert s3.multipart == 1 and s3.partes == 3 and s3.objetos == 2


async def test_el_spool_decide_ram_o_disco_y_entrega_por_hash(tmp_path):
    dirs = Directorios(tmp_path / "ram", tmp_path / "disco", tmp_path / "estado.json", tmp_path / "parar")
    dirs.preparar()
    spool = Spool(dirs, umbral=1000)

    chico = spool.nuevo(500)
    await chico.escribir(b"a" * 500)
    await chico.cerrar("aaa", 500)
    grande = spool.nuevo(5000)
    await grande.escribir(b"b" * 5000)
    await grande.cerrar("bbb", 5000)
    sin_largo = spool.nuevo(None)  # crece y pasa a disco
    for _ in range(3):
        await sin_largo.escribir(b"c" * 600)
    await sin_largo.cerrar("ccc", 1800)
    cortado = spool.nuevo(5000)
    await cortado.escribir(b"d" * 2000)
    await cortado.descartar()

    assert (dirs.ram / "listos" / "aaa").read_bytes() == b"a" * 500
    assert (dirs.disco / "listos" / "bbb").stat().st_size == 5000
    assert (dirs.disco / "listos" / "ccc").stat().st_size == 1800
    assert list((dirs.disco / "tmp").iterdir()) == []
    assert spool.entregado == {"ram": 500, "disco": 6800}
    spool.subidor = {"bytes_ram": 500, "bytes_disco": 5000}
    assert spool.pendiente("ram") == 0 and spool.pendiente("disco") == 1800


async def test_el_subidor_sube_lo_listo_y_no_repite(certs, tmp_path):
    from concurrencia.subidor import correr

    s3, runner, puerto = await _levantar_s3(certs)
    dirs = Directorios(tmp_path / "ram", tmp_path / "disco", tmp_path / "estado.json", tmp_path / "parar")
    dirs.preparar()
    (dirs.ram / "listos" / "h1").write_bytes(b"1" * 1000)
    (dirs.disco / "listos" / "h2").write_bytes(b"2" * (config.PARTE_S3 + 10))
    contexto = multiprocessing.get_context("spawn")
    proceso = contexto.Process(target=correr, args=(dirs, f"https://s3.{config.DOMINIO}:{puerto}", certs / "ca.pem", 4, None))
    proceso.start()
    try:
        # Esperar a que el subidor informe las dos subidas como terminadas.
        for _ in range(200):
            if dirs.estado.exists() and json.loads(dirs.estado.read_text()).get("archivos") == 2:
                break
            await asyncio.sleep(0.05)
        # El mismo contenido otra vez: se descarta sin subir.
        (dirs.ram / "listos" / "h1").write_bytes(b"1" * 1000)
        await asyncio.sleep(0.5)
        dirs.parar.touch()
        await asyncio.to_thread(proceso.join, 10)
    finally:
        await runner.cleanup()
    estado = json.loads(dirs.estado.read_text())
    assert estado["final"] and estado["errores"] == 0
    assert estado["bytes_ram"] == 1000 and estado["bytes_disco"] == config.PARTE_S3 + 10
    assert estado["duplicados"] == 1
    assert list((dirs.ram / "listos").iterdir()) == [] and list((dirs.disco / "listos").iterdir()) == []
