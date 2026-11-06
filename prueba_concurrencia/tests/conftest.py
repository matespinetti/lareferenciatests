"""Servidor simulado real en un puerto efímero de 127.0.0.1. Ningún test sale de la máquina."""

import math
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from aiohttp import web

from concurrencia.distribucion import LogNormal
from concurrencia.servidor import Parametros, Servidor, contexto_ssl

RAIZ = Path(__file__).parent.parent


@pytest.fixture(scope="session")
def certs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directorio = tmp_path_factory.mktemp("certs")
    subprocess.run(["sh", str(RAIZ / "scripts" / "certificado.sh"), str(directorio)], check=True)
    return directorio


def latencia_fija(segundos: float) -> LogNormal:
    return LogNormal(math.log(segundos), 0.0)


ServidorDePrueba = tuple[Servidor, int]


@pytest.fixture
async def levantar(certs: Path) -> AsyncIterator[Callable[[Parametros], "object"]]:
    runners: list[web.AppRunner] = []

    async def _levantar(parametros: Parametros, puerto: int = 0) -> ServidorDePrueba:
        servidor = Servidor(parametros)
        runner = web.AppRunner(servidor.app(), access_log=None)
        await runner.setup()
        sitio = web.TCPSite(runner, "127.0.0.1", puerto, ssl_context=contexto_ssl(certs))
        await sitio.start()
        runners.append(runner)
        puerto = runner.addresses[0][1]
        return servidor, puerto

    yield _levantar
    for runner in runners:
        await runner.cleanup()


def puertos_libres(cantidad: int) -> int:
    """El primero de `cantidad` puertos consecutivos libres en 127.0.0.1."""
    import socket

    for _ in range(50):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            base = s.getsockname()[1]
        sockets = []
        try:
            for i in range(cantidad):
                otro = socket.socket()
                sockets.append(otro)
                otro.bind(("127.0.0.1", base + i))
            return base
        except OSError:
            continue
        finally:
            for otro in sockets:
                otro.close()
    raise RuntimeError("no hay puertos consecutivos libres")
