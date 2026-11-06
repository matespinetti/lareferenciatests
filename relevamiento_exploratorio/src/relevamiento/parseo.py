"""Ayudas puras para parsear respuestas y URLs. Sin I/O."""

import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from lxml import etree, html

_PARSER_XML = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)


def parsear_html(cuerpo: bytes, base_url: str | None = None) -> html.HtmlElement | None:
    if not cuerpo.strip():
        return None
    try:
        doc = html.document_fromstring(cuerpo, base_url=base_url)
    except (etree.ParserError, ValueError):
        return None
    if base_url:
        doc.make_links_absolute(base_url, resolve_base_href=True, handle_failures="discard")
    return doc


def parsear_xml(cuerpo: bytes) -> etree._Element | None:
    if not cuerpo.strip():
        return None
    try:
        return etree.fromstring(cuerpo, _PARSER_XML)
    except etree.XMLSyntaxError:
        return None


def parsear_json(cuerpo: bytes) -> Any:
    try:
        return json.loads(cuerpo)
    except (ValueError, UnicodeDecodeError):
        return None


def nombre_local(tag: object) -> str | None:
    """Nombre sin namespace; tolera prefijos sin declarar que deja un XML truncado."""
    if not isinstance(tag, str):
        return None
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def hijos(el: etree._Element, nombre: str) -> list[etree._Element]:
    """Hijos directos con ese nombre local, ignorando el namespace."""
    return [h for h in el if nombre_local(h.tag) == nombre]


def descendientes(el: etree._Element, nombre: str) -> list[etree._Element]:
    return [d for d in el.iter() if nombre_local(d.tag) == nombre]


def texto_hijo(el: etree._Element, nombre: str) -> str | None:
    for h in hijos(el, nombre):
        if h.text and h.text.strip():
            return h.text.strip()
    return None


def host_de(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def es_host_publico(host: str) -> bool:
    """Descarta loopback, redes privadas y nombres de intranet. No resuelve DNS: solo mira el nombre."""
    host = host.rstrip(".").lower()
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        pass
    if "." not in host or host == "localhost":
        return False
    return not host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa"))


def normalizar_url(url: str) -> str:
    """Esquema y host en minúsculas, sin puerto por defecto ni fragmento."""
    partes = urlsplit(url.strip())
    esquema = partes.scheme.lower()
    host = (partes.hostname or "").lower()
    puerto = partes.port
    if puerto and not ((esquema == "http" and puerto == 80) or (esquema == "https" and puerto == 443)):
        host = f"{host}:{puerto}"
    return urlunsplit((esquema, host, partes.path or "/", partes.query, ""))


def raiz(url: str) -> str:
    partes = urlsplit(url)
    return urlunsplit((partes.scheme, partes.netloc, "", "", ""))
