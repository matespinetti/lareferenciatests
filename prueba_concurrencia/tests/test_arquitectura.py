"""Reglas del CLAUDE.md que se pueden verificar mirando el código."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "concurrencia"
CORRUTINAS = ["motor.py", "servidor.py", "db.py", "resolucion.py", "planificador.py", "distribucion.py", "metricas.py"]
PUROS = ["planificador.py", "distribucion.py", "resolucion.py"]
# Escribe el CSV y el JSON al terminar la corrida, no mientras se mide.
_PERMITIDO_OPEN = {("metricas.py", "escribir")}


def _arbol(nombre: str) -> ast.Module:
    return ast.parse((SRC / nombre).read_text())


def _importados(arbol: ast.Module) -> set[str]:
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres |= {a.name.split(".")[0] for a in nodo.names}
        elif isinstance(nodo, ast.ImportFrom) and nodo.module:
            nombres.add(nodo.module.split(".")[0])
            nombres |= {f"{nodo.module}.{a.name}" for a in nodo.names}
    return nombres


@pytest.mark.parametrize("nombre", CORRUTINAS)
def test_corrutinas_sin_e_s_bloqueante(nombre: str):
    arbol = _arbol(nombre)
    importados = _importados(arbol)
    assert "requests" not in importados
    assert "concurrencia.hilos" not in importados
    for funcion in [n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]:
        for nodo in ast.walk(funcion):
            if not isinstance(nodo, ast.Call):
                continue
            llamada = ast.unparse(nodo.func)
            assert llamada != "time.sleep", f"{nombre}:{nodo.lineno} usa time.sleep"
            if llamada == "open" or llamada.endswith(".open"):
                assert (nombre, funcion.name) in _PERMITIDO_OPEN, f"{nombre}:{nodo.lineno} abre un archivo"


@pytest.mark.parametrize("nombre", PUROS)
def test_modulos_puros_sin_red_ni_async(nombre: str):
    importados = _importados(_arbol(nombre))
    assert not importados & {"aiohttp", "asyncio", "socket", "requests", "psutil", "asyncpg"}
