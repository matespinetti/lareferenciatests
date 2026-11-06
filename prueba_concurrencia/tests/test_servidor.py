import asyncio
import time

import aiohttp
import pytest

from concurrencia import config, motor, resolucion
from concurrencia.planificador import Tarea, Tipo
from concurrencia.servidor import Parametros
from tests.conftest import latencia_fija


def _sin_errores(**extra: object) -> Parametros:
    return Parametros(prob_429=0.0, prob_503=0.0, latencia=latencia_fija(0.05), **extra)  # type: ignore[arg-type]


async def test_cuenta_pedidos_simultaneos(levantar, certs):
    servidor, puerto = await levantar(_sin_errores())
    async with motor.sesion(certs) as cliente:

        async def pedir(item: int) -> None:
            async with cliente.get(resolucion.url(Tarea(Tipo.RESOLUCION, 7, item), puerto)) as r:
                await r.read()

        await asyncio.gather(pedir(0), pedir(1))
    assert servidor.cortesia.simultaneos == 1
    assert servidor.cortesia.pedidos == 2


async def test_cuenta_intervalos_cortos_y_respeta_los_largos(levantar, certs):
    servidor, puerto = await levantar(_sin_errores())
    async with motor.sesion(certs) as cliente:
        for item, espera in [(0, 0.0), (1, 0.2), (2, 1.05)]:
            await asyncio.sleep(espera)
            async with cliente.get(resolucion.url(Tarea(Tipo.RESOLUCION, 3, item), puerto)) as r:
                await r.read()
    assert servidor.cortesia.intervalos_cortos == 1
    assert servidor.cortesia.simultaneos == 0


async def test_ancho_de_banda_por_conexion(levantar, certs):
    _, puerto = await levantar(_sin_errores(tam_archivo=config.MB))
    async with motor.sesion(certs) as cliente:
        t0 = time.monotonic()
        async with cliente.get(resolucion.url(Tarea(Tipo.DESCARGA, 1, 0, 0), puerto)) as r:
            cuerpo = await r.read()
        transcurrido = time.monotonic() - t0
    assert len(cuerpo) == config.MB
    # 1 MB a 2 MB/s, más 50 ms de latencia.
    assert transcurrido == pytest.approx(0.55, abs=0.15)


async def test_tope_de_red_compartido(levantar, certs):
    # Dos descargas de 1 MB a la vez con 2 MB/s de red total: cada una tarda ~1 s, no 0,5 s.
    _, puerto = await levantar(_sin_errores(tam_archivo=config.MB, red=2 * config.MB))
    async with motor.sesion(certs) as cliente:

        async def bajar(repo: int) -> None:
            async with cliente.get(resolucion.url(Tarea(Tipo.DESCARGA, repo, 0, 0), puerto)) as r:
                await r.read()

        t0 = time.monotonic()
        await asyncio.gather(bajar(1), bajar(2))
        transcurrido = time.monotonic() - t0
    assert transcurrido == pytest.approx(1.05, abs=0.2)


async def test_429_con_retry_after(levantar, certs):
    _, puerto = await levantar(Parametros(prob_429=1.0, latencia=latencia_fija(0.01)))
    async with motor.sesion(certs) as cliente:
        async with cliente.get(resolucion.url(Tarea(Tipo.RESOLUCION, 1, 0), puerto)) as r:
            assert r.status == 429
            assert r.headers["Retry-After"] == str(config.RETRY_AFTER)


async def test_pagina_con_enlaces_y_tiempo_de_servicio(levantar, certs):
    _, puerto = await levantar(_sin_errores())
    async with motor.sesion(certs) as cliente:
        async with cliente.get(resolucion.url(Tarea(Tipo.RESOLUCION, 5, 9), puerto)) as r:
            cuerpo = await r.read()
            servicio = float(r.headers["X-Tiempo-Servicio"])
    from concurrencia.resolucion import descargas

    tareas = descargas(cuerpo)
    assert config.DESCARGAS_MIN <= len(tareas) <= config.DESCARGAS_MAX
    assert {(t.repo, t.item) for t in tareas} == {(5, 9)}
    assert servicio >= 0.05


async def test_el_resolver_rechaza_hosts_de_afuera(certs):
    async with motor.sesion(certs) as cliente:
        with pytest.raises(aiohttp.ClientConnectorDNSError) as error:
            await cliente.get("https://example.org/")
    assert "fuera de la prueba" in str(error.value.os_error)
