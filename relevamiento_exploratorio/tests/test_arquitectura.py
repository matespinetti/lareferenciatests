"""Reglas de estructura del CLAUDE.md, comprobadas sobre el código fuente."""

import re
from pathlib import Path

FUENTES = Path(__file__).parent.parent / "src" / "relevamiento"


def codigo(nombre: str) -> str:
    return (FUENTES / nombre).read_text(encoding="utf-8")


def test_solo_la_cortesia_hace_pedidos():
    for archivo in FUENTES.glob("*.py"):
        if archivo.name == "cortesia.py":
            continue
        texto = archivo.read_text(encoding="utf-8")
        assert not re.search(r"AsyncClient|httpx\.(get|post|stream|request|Client)\b|HTTPTransport|urllib\.request|import requests", texto), archivo.name


def test_detectores_y_extractores_son_puros():
    for nombre in ("detectores.py", "extractores.py", "parseo.py", "modelos.py"):
        texto = codigo(nombre)
        assert not re.search(r"^\s*(import|from)\s+(asyncio|httpx|relevamiento\.(cortesia|registro|relevar))\b", texto, re.M), nombre
        assert "open(" not in texto, nombre


def test_no_hay_correos_en_el_codigo():
    for archivo in FUENTES.glob("*.py"):
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", archivo.read_text(encoding="utf-8")), archivo.name
