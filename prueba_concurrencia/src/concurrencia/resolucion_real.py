"""Resolución de un artículo OJS real: de su página a la URL del PDF (modo muestra).

La misma lógica que `relevamiento/extractores.extraer_pagina`, portada a selectolax (que ya usa la
resolución del simulador): los dos proyectos no comparten dependencias ni imagen de Docker.

Candidatos, en orden: el meta `citation_pdf_url` (el estándar de Google Scholar que OJS siempre
pone), los enlaces a galeradas y a descargas de OJS, y los de DSpace. En OJS 3, tanto el meta como
la galerada suelen apuntar a `/article/view/A/B`, que es el visor HTML: se cambian a
`/article/download/A/B`, que es el archivo.

Funciones puras sobre el cuerpo ya leído.
"""

import re
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

_GALERADA = re.compile(r"/article/view/(\d+)/(\d+)/?$")
_DESCARGA_OJS = re.compile(r"/article/download/\d+/\d+")
_DSPACE = re.compile(r"/bitstream/|/bitstreams/[0-9a-fA-F-]{36}/download|/core/bitstreams/[0-9a-fA-F-]{36}/content")


def a_descarga(url: str) -> str:
    """`/article/view/A/B` (el visor de una galerada OJS) → `/article/download/A/B` (el archivo)."""
    partes = urlsplit(url)
    if m := _GALERADA.search(partes.path):
        ruta = partes.path[: m.start()] + f"/article/download/{m.group(1)}/{m.group(2)}"
        return partes._replace(path=ruta).geturl()
    return url


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def descargas_de_pagina(cuerpo: bytes, url: str, maximo: int = 3) -> list[str]:
    """Las URLs de archivo de la página de un artículo, sin repetidos y como mucho `maximo`.

    Solo del mismo host que la página: las referencias bibliográficas de un artículo enlazan PDFs de
    otros repositorios (CEPAL, RACO, …), que no son archivos del ítem y además tienen su propia cortesía."""
    arbol = LexborHTMLParser(cuerpo)
    encontrados: list[str] = []
    propio = _host(url)

    def agregar(href: str | None) -> None:
        if not href:
            return
        absoluta = a_descarga(urljoin(url, href.strip()))
        if absoluta.startswith(("http://", "https://")) and _host(absoluta) == propio and absoluta not in encontrados:
            encontrados.append(absoluta)

    for meta in arbol.css("meta"):
        if (meta.attributes.get("name") or "").lower() == "citation_pdf_url":
            agregar(meta.attributes.get("content"))
    for enlace in arbol.css("a"):
        href = enlace.attributes.get("href") or ""
        ruta = urlsplit(urljoin(url, href)).path
        if _GALERADA.search(ruta) or _DESCARGA_OJS.search(ruta) or _DSPACE.search(ruta):
            agregar(href)
    return encontrados[:maximo]
