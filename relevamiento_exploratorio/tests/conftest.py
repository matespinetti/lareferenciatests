"""Internet falso sobre httpx.MockTransport. Ningún test hace pedidos reales."""

import asyncio
import inspect
import ssl
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

import httpx
import pytest

from relevamiento.config import ConfigCortesia
from relevamiento.cortesia import Cortesia
from relevamiento.registro import Registro

Manejador = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


@dataclass
class Llamada:
    host: str
    url: str
    inicio: float
    fin: float
    verificado: bool
    encabezados: dict[str, str]


class InternetFalso:
    def __init__(self, latencia: float = 0.1):
        self.rutas: dict[tuple[str, str], tuple[Manejador, float | None]] = {}
        self.hosts: dict[str, Callable[[httpx.Request], httpx.Response | None]] = {}
        self.llamadas: list[Llamada] = []
        self.tls_invalido: set[str] = set()
        self.latencia = latencia
        self._en_vuelo = 0
        self._por_host: Counter[str] = Counter()
        self.max_en_vuelo = 0
        self.max_por_host = 0
        self.transporte = httpx.MockTransport(lambda r: self._manejar(r, True))
        self.transporte_sin_verificar = httpx.MockTransport(lambda r: self._manejar(r, False))

    def agregar(
        self,
        url: str,
        manejador: Manejador | None = None,
        *,
        codigo: int = 200,
        cuerpo: bytes | str = b"",
        tipo: str = "text/html; charset=utf-8",
        encabezados: dict[str, str] | None = None,
        latencia: float | None = None,
    ) -> None:
        if manejador is None:
            datos = cuerpo.encode() if isinstance(cuerpo, str) else cuerpo
            cabecera = {"content-type": tipo, **(encabezados or {})}
            manejador = lambda _r: httpx.Response(codigo, headers=cabecera, content=datos)  # noqa: E731
        self.rutas[self._clave(httpx.URL(url))] = (manejador, latencia)

    def agregar_host(self, host: str, manejador: Callable[[httpx.Request], httpx.Response | None]) -> None:
        """Manejador para todo un host; las rutas exactas tienen prioridad y None significa 404."""
        self.hosts[host] = manejador

    def redireccion(self, desde: str, hacia: str, codigo: int = 302) -> None:
        self.agregar(desde, codigo=codigo, encabezados={"location": hacia})

    def llamadas_a(self, host: str) -> list[Llamada]:
        return [ll for ll in self.llamadas if ll.host == host]

    def _por_defecto(self, request: httpx.Request) -> httpx.Response:
        manejador = self.hosts.get(request.url.host)
        respuesta = manejador(request) if manejador else None
        return respuesta or httpx.Response(404, text="no existe")

    @staticmethod
    def _clave(url: httpx.URL) -> tuple[str, str]:
        return url.host, url.raw_path.decode()

    async def _manejar(self, request: httpx.Request, verificado: bool) -> httpx.Response:
        loop = asyncio.get_running_loop()
        host = request.url.host
        inicio = loop.time()
        self._en_vuelo += 1
        self._por_host[host] += 1
        self.max_en_vuelo = max(self.max_en_vuelo, self._en_vuelo)
        self.max_por_host = max(self.max_por_host, self._por_host[host])
        try:
            if verificado and host in self.tls_invalido:
                raise httpx.ConnectError("certificate verify failed") from ssl.SSLCertVerificationError(
                    "certificate verify failed"
                )
            manejador, latencia = self.rutas.get(self._clave(request.url), (self._por_defecto, None))
            await asyncio.sleep(self.latencia if latencia is None else latencia)
            respuesta = manejador(request)
            if inspect.isawaitable(respuesta):
                respuesta = await respuesta
            return respuesta
        finally:
            self._en_vuelo -= 1
            self._por_host[host] -= 1
            self.llamadas.append(
                Llamada(host, str(request.url), inicio, loop.time(), verificado, dict(request.headers))
            )


class CuerpoInfinito(httpx.AsyncByteStream):
    """Cuerpo enorme o lento para probar el corte de lectura y el cierre de la conexión."""

    def __init__(self, trozo: bytes = b"x" * 65536, pausa: float = 0.0, total: int | None = None):
        self.trozo = trozo
        self.pausa = pausa
        self.total = total
        self.entregados = 0
        self.cerrado = False

    async def __aiter__(self):
        while self.total is None or self.entregados < self.total:
            if self.pausa:
                await asyncio.sleep(self.pausa)
            self.entregados += len(self.trozo)
            yield self.trozo

    async def aclose(self) -> None:
        self.cerrado = True


@pytest.fixture
def internet() -> InternetFalso:
    return InternetFalso()


@pytest.fixture
def salida(tmp_path: Path) -> Path:
    return tmp_path / "salida"


@pytest.fixture
def crear_cortesia(internet: InternetFalso, salida: Path) -> Iterator[Callable[..., Cortesia]]:
    registros: list[Registro] = []

    def crear(cfg: ConfigCortesia | None = None, corrida: str = "c1", **kw: Any) -> Cortesia:
        registro = Registro(salida, corrida)
        registros.append(registro)
        return Cortesia(
            cfg or ConfigCortesia(),
            registro,
            "prueba@example.org",
            transporte=internet.transporte,
            transporte_sin_verificar=internet.transporte_sin_verificar,
            **kw,
        )

    yield crear
    for r in registros:
        r.cerrar()
