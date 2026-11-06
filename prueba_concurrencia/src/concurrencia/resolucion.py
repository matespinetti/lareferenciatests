"""Resolución de un ítem: parsear su página y sacar los archivos a descargar.

Funciones puras sobre el cuerpo ya leído, compartidas por los dos modos. Se parsea el HTML de
verdad (selectolax, como el descargador), porque ese costo es parte de lo que se mide.
"""

import re

from selectolax.lexbor import LexborHTMLParser

from concurrencia import config
from concurrencia.planificador import Tarea, Tipo

_HREF = re.compile(r"^/repo/(\d+)/archivo/(\d+)/(\d+)$")


def url(tarea: Tarea, puerto: int, procesos: int = 1) -> str:
    """Cada repositorio tiene su propio host, para que haya una conexión por repositorio.

    Con el servidor en varios procesos, cada repositorio va siempre al mismo (`puerto + repo %
    procesos`): así cada proceso ve todos los pedidos de sus repositorios y controla su cortesía.
    """
    base = f"https://r{tarea.repo}.{config.DOMINIO}:{puerto + tarea.repo % procesos}/repo/{tarea.repo}"
    if tarea.tipo is Tipo.RESOLUCION:
        return f"{base}/item/{tarea.item}"
    return f"{base}/archivo/{tarea.item}/{tarea.archivo}"


def url_control(puerto: int, ruta: str) -> str:
    return f"https://control.{config.DOMINIO}:{puerto}/__control/{ruta}"


def descargas(cuerpo: bytes) -> list[Tarea]:
    tareas = []
    for enlace in LexborHTMLParser(cuerpo).css("a.archivo"):
        coincide = _HREF.match(enlace.attributes.get("href") or "")
        if coincide:
            repo, item, archivo = (int(g) for g in coincide.groups())
            tareas.append(Tarea(Tipo.DESCARGA, repo, item, archivo))
    return tareas
