"""Un pedido (al servidor simulado o, en el modo muestra, a un repositorio real) y lo que su
resultado le cambia al plan.

Lo comparten el motor de un proceso y los workers del modo multiproceso: `hacer` ejecuta el
pedido (resolución o descarga con SHA-256 y, si hay, escritura al spool) y devuelve un
`Resultado`; `aplicar` lo vuelca en el planificador y los contadores. En el modo multiproceso
`hacer` corre en un worker y `aplicar` en el planificador.
"""

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from concurrencia import config, metricas, resolucion
from concurrencia.planificador import Planificador, SeguimientoItems, Tarea, Tipo


@dataclass(slots=True)
class Resultado:
    inicio: float
    fin: float = 0.0
    codigo: int | None = None
    latencia: float | None = None
    servicio: float | None = None
    recibidos: int = 0
    sha: str | None = None
    descargas: list[Tarea] = field(default_factory=list)
    retry_after: float = float(config.RETRY_AFTER)
    error: str | None = None
    detalle: str | None = None
    """Para diagnosticar un error contra un repositorio real: `Location`, tamaño anunciado y codificación."""


def retry_after(valor: str | None) -> float:
    """Segundos de un `Retry-After`: un número o una fecha HTTP. Si no viene o no se entiende, el de
    `config.RETRY_AFTER`."""
    if not valor:
        return float(config.RETRY_AFTER)
    try:
        return max(0.0, float(valor))
    except ValueError:
        pass
    try:
        fecha = parsedate_to_datetime(valor)
    except (TypeError, ValueError):
        return float(config.RETRY_AFTER)
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone.utc)
    return max(0.0, (fecha - datetime.now(timezone.utc)).total_seconds())


async def hacer(
    sesion: aiohttp.ClientSession,
    tarea: Tarea,
    url: str,
    al_recibir: Callable[[int, bool], None],
    destino_para: Callable[[int | None], Any] | None = None,
    descargas_de: Callable[[Tarea, bytes, str], list[Tarea]] | None = None,
    opciones: dict[str, Any] | None = None,
) -> Resultado:
    """Hace el pedido de `tarea`. `al_recibir(bytes, hasheados)` se llama a medida que llegan bytes;
    `destino_para(largo)` da dónde guardar un archivo (el spool) o no se guarda. `descargas_de(tarea,
    cuerpo, url_final)` saca las descargas de la página de un ítem (por defecto, las del simulador);
    `opciones` va tal cual a `sesion.get` (en el modo muestra, `ssl=False` para un certificado inválido)."""
    loop = asyncio.get_running_loop()
    r = Resultado(inicio=loop.time())
    try:
        async with sesion.get(url, **(opciones or {})) as respuesta:
            r.latencia = loop.time() - r.inicio
            r.codigo = respuesta.status
            r.servicio = float(respuesta.headers.get("X-Tiempo-Servicio", "nan"))
            if r.codigo in (429, 503):
                r.recibidos = len(await respuesta.read())
                al_recibir(r.recibidos, False)
                r.retry_after = retry_after(respuesta.headers.get("Retry-After"))
            elif r.codigo != 200:
                r.recibidos = len(await respuesta.read())
                al_recibir(r.recibidos, False)
                r.error = f"http_{r.codigo}"
                if 300 <= r.codigo < 400:
                    r.detalle = f"location={respuesta.headers.get('Location', '(sin Location)')}"
                else:
                    r.detalle = f"tipo={respuesta.headers.get('Content-Type', '')}"
            elif tarea.tipo is Tipo.RESOLUCION:
                cuerpo = await respuesta.read()
                r.recibidos = len(cuerpo)
                al_recibir(r.recibidos, False)
                if descargas_de is None:
                    r.descargas = resolucion.descargas(cuerpo)
                else:
                    r.descargas = descargas_de(tarea, cuerpo, str(respuesta.url))
            else:
                await _descargar(respuesta, tarea, r, al_recibir, destino_para)
    except (aiohttp.ClientError, TimeoutError, OSError) as error:
        r.error = type(error).__name__
    except Exception as error:
        # Un error inesperado se cuenta y no debe dejar el repositorio ocupado para siempre.
        r.error = f"inesperado_{type(error).__name__}"
    r.fin = loop.time()
    return r


async def _descargar(
    respuesta: aiohttp.ClientResponse,
    tarea: Tarea,
    r: Resultado,
    al_recibir: Callable[[int, bool], None],
    destino_para: Callable[[int | None], Any] | None,
) -> None:
    h = hashlib.sha256()
    destino = destino_para(respuesta.content_length) if destino_para else None
    try:
        async for chunk in respuesta.content.iter_chunked(config.CHUNK):
            h.update(chunk)
            r.recibidos += len(chunk)
            al_recibir(len(chunk), True)
            if destino is not None:
                await destino.escribir(chunk)
        # Con Content-Encoding (gzip, deflate, br), Content-Length es el tamaño comprimido y aiohttp
        # entrega los bytes descomprimidos: no se pueden comparar.
        codificacion = respuesta.headers.get("Content-Encoding", "identity").lower()
        if respuesta.content_length is not None and codificacion == "identity" and r.recibidos != respuesta.content_length:
            r.error = "archivo_incompleto"
            r.detalle = f"anunciado={respuesta.content_length} recibido={r.recibidos}"
        else:
            r.sha = h.hexdigest()
            if destino is not None:
                await destino.cerrar(f"{tarea.repo}_{tarea.item}_{tarea.archivo}", r.recibidos)
    finally:
        if destino is not None:
            await destino.descartar()


def aplicar(
    plan: Planificador,
    items: SeguimientoItems,
    contadores: metricas.Contadores,
    tarea: Tarea,
    r: Resultado,
    despacho: float,
) -> None:
    """Vuelca el resultado en el plan: descargas nuevas, pausas por 429/503, ítems completos, y
    libera el repositorio con el instante en que terminó el pedido."""
    if r.codigo in (429, 503):
        plan.pausar(tarea.repo, r.fin + r.retry_after)
        plan.agregar(tarea)
        contadores.estado(r.codigo)
    if r.error is not None:
        contadores.error(r.error)
    elif tarea.tipo is Tipo.RESOLUCION and r.codigo == 200:
        for descarga in r.descargas:
            plan.agregar(descarga)
        if items.resuelto(tarea.repo, tarea.item, len(r.descargas)):
            contadores.item_completo()
    elif r.sha is not None and items.descargado(tarea.repo, tarea.item):
        contadores.item_completo()
    plan.terminada(tarea, r.fin)
    contadores.pedido(tarea.tipo, r.fin - r.inicio, r.latencia, r.servicio, despacho, descarga_ok=r.sha is not None)
