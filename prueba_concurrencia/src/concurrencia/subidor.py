"""Subidor: sube a S3 lo que el motor deja en `listos/`.

Modo `proceso` (el recomendado): corre en su propio proceso, para no competir por el GIL con el
bucle de eventos del motor. Modo `hilos`: la misma función corre en un hilo dentro del proceso del
motor, con el pool de hilos de s3transfer; existe para medir cuánto le cuesta eso al motor. Usa boto3
con su gestor de transferencias (`s3transfer`): un solo PUT para los archivos chicos y multipart
para los de `PARTE_S3` o más. La clave es por repositorio (`repo/item/archivo`), como en el
descargador; volver a subir el mismo archivo sobrescribe el mismo objeto. Al terminar cada subida
borra el archivo del spool.

Cada segundo reescribe `subidor.json` con lo que lleva subido; el motor lo lee para saber cuánto
quedó pendiente y para frenar si se atrasa.

Sin `endpoint` sube a S3 real (`S3_BUCKET`, con las credenciales del rol de la instancia): es la
validación en una instancia real (GUIA_DOS_MAQUINAS.md).
"""

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

import psutil

from concurrencia import config
from concurrencia.spool import Directorios

_PAUSA = 0.05


def _cliente(endpoint: str | None, ca: Path, concurrencia: int) -> Any:
    import boto3
    from botocore.config import Config

    if endpoint is None:
        # S3 real: región y credenciales del entorno (el rol de la instancia), checksums por defecto.
        return boto3.client(
            "s3",
            region_name=config.S3_REGION,
            config=Config(max_pool_connections=concurrencia * 2, retries={"max_attempts": 3, "mode": "standard"}),
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="prueba",
        aws_secret_access_key="prueba",
        verify=str(ca),
        config=Config(
            s3={"addressing_style": "path"},
            # Sin checksums en trailer (aws-chunked): el S3 falso recibe el cuerpo tal cual.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            max_pool_connections=concurrencia * 2,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def correr(
    dirs: Directorios,
    endpoint: str | None,
    ca: Path,
    concurrencia: int,
    cpu: int | None,
    en_hilo: bool = False,
    bucket: str = config.BUCKET,
    prefijo: str = "",
) -> None:
    """Sube hasta que aparezca `dirs.parar`; entonces escribe su estado final y termina.

    Con `en_hilo` corre dentro del proceso del motor: no se ata a un núcleo y no informa CPU propia
    (la de su proceso es la del motor). Sin `endpoint`, sube a S3 real en `bucket`, bajo `prefijo`."""
    from s3transfer.manager import TransferConfig, TransferManager

    from concurrencia.hilos import resolver_local

    if not en_hilo and cpu is not None and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, {cpu})
    proceso = psutil.Process()
    # Con el S3 falso, `*.sim.localhost` va a la máquina de la prueba; con S3 real hay que resolver AWS.
    with resolver_local() if endpoint else contextlib.nullcontext():
        cliente = _cliente(endpoint, ca, concurrencia)
        gestor = TransferManager(
            cliente,
            TransferConfig(
                multipart_threshold=config.PARTE_S3,
                multipart_chunksize=config.PARTE_S3,
                max_request_concurrency=concurrencia,
            ),
        )
        estado: dict[str, Any] = {
            "bytes_ram": 0, "bytes_disco": 0, "archivos": 0, "errores": 0,
            "cpu": [], "rss_mb": [],
        }  # fmt: skip
        en_curso: dict[Path, tuple[Any, str, int]] = {}
        cpu_antes, t_antes = proceso.cpu_times(), time.monotonic()
        proximo_estado = t_antes + 1.0
        try:
            while not dirs.parar.exists():
                _tomar(dirs, gestor, en_curso, bucket, prefijo)
                _terminados(en_curso, estado)
                if time.monotonic() >= proximo_estado:
                    ahora, cpu_ahora = time.monotonic(), proceso.cpu_times()
                    usado = (cpu_ahora.user + cpu_ahora.system) - (cpu_antes.user + cpu_antes.system)
                    if not en_hilo:
                        estado["cpu"].append(round(100 * usado / (ahora - t_antes), 1))
                        estado["rss_mb"].append(round(proceso.memory_info().rss / 2**20, 1))
                    cpu_antes, t_antes = cpu_ahora, ahora
                    proximo_estado = ahora + 1.0
                    estado["en_curso"] = len(en_curso)
                    _escribir_estado(dirs.estado, estado)
                time.sleep(_PAUSA)
        finally:
            _terminados(en_curso, estado)
            estado["en_curso"] = len(en_curso)
            estado["final"] = True
            _escribir_estado(dirs.estado, estado)
            # `shutdown(cancel=True)` de s3transfer 0.19 le pasa los argumentos corridos a `_shutdown`
            # y falla con lo que esté en curso: se llama directo, con el orden correcto.
            gestor._shutdown(True, "fin de la corrida")


def clave(nombre: str) -> str:
    """La clave en S3 de un archivo del spool: `repo_item_archivo` → `repo/item/archivo`."""
    return nombre.replace("_", "/")


def _tomar(dirs: Directorios, gestor: Any, en_curso: dict, bucket: str, prefijo: str) -> None:
    for listos, spool in zip(dirs.listos(), ("ram", "disco"), strict=True):
        try:
            entradas = list(os.scandir(listos))
        except FileNotFoundError:
            continue
        for entrada in entradas:
            ruta = Path(entrada.path)
            if ruta in en_curso:
                continue
            tamano = entrada.stat().st_size
            futuro = gestor.upload(str(ruta), bucket, prefijo + clave(entrada.name))
            en_curso[ruta] = (futuro, spool, tamano)


def _terminados(en_curso: dict, estado: dict[str, Any]) -> None:
    for ruta, (futuro, spool, tamano) in list(en_curso.items()):
        if not futuro.done():
            continue
        del en_curso[ruta]
        try:
            futuro.result()
        except Exception:
            # Se reintenta desde el spool: el archivo sigue ahí y el próximo barrido lo vuelve a tomar.
            estado["errores"] += 1
            continue
        ruta.unlink(missing_ok=True)
        estado[f"bytes_{spool}"] += tamano
        estado["archivos"] += 1


def _escribir_estado(ruta: Path, estado: dict[str, Any]) -> None:
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(estado))
    os.replace(tmp, ruta)
