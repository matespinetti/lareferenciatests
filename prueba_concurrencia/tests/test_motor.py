"""Humo de punta a punta: servidor y motor reales en localhost durante unos segundos."""

import csv
import json
from dataclasses import replace

import pytest

from concurrencia import config, hilos, motor
from concurrencia.config import Escenario, Modo
from concurrencia.metricas import COLUMNAS
from concurrencia.servidor import Parametros
from tests.conftest import latencia_fija, puertos_libres

LUGARES = 20


def _escenario(modo: Modo) -> Escenario:
    return Escenario(
        "humo", LUGARES, repos=200, items=200 * 50, descripcion="humo", modo=modo,
        duracion=4.0, calentamiento=1.0, tam_archivo=200_000,
    )  # fmt: skip


def _verificar(resumen: dict, salida) -> None:
    assert resumen["servidor"]["violaciones"] == 0, resumen["servidor"]["ejemplos"]
    assert resumen["errores_por_tipo"] == {}
    assert resumen["servidor"]["pedidos"] > LUGARES
    with salida.with_suffix(".csv").open() as f:
        filas = list(csv.DictReader(f))
    assert list(filas[0]) == COLUMNAS
    assert 4 <= len(filas) <= 6
    assert filas[0]["calentamiento"] == "1" and filas[-1]["calentamiento"] == "0"
    # Las tareas en curso llegan a la cantidad de lugares: no hay un tope escondido.
    assert max(int(f["en_curso"]) for f in filas) == LUGARES
    assert sum(float(f["mb_hasheados"]) for f in filas) > 0
    assert sum(float(f["items_ok"]) for f in filas) > 0
    assert max(float(f["ocupacion"]) for f in filas) == 100.0
    assert json.loads(salida.with_suffix(".json").read_text())["escenario"]["id"] == "humo"


@pytest.mark.parametrize("modo", [Modo.CORRUTINAS, Modo.HILOS])
async def test_humo(levantar, certs, tmp_path, modo):
    _, puerto = await levantar(Parametros(tam_archivo=200_000, latencia=latencia_fija(0.1)))
    salida = tmp_path / "humo"
    if modo is Modo.CORRUTINAS:
        resumen = await motor.correr(_escenario(modo), puerto, certs, salida)
    else:
        # El modo hilos bloquea: corre en un hilo aparte para que el servidor siga atendiendo.
        import asyncio

        resumen = await asyncio.to_thread(hilos.correr, _escenario(modo), puerto, certs, salida)
    _verificar(resumen, salida)
    assert resumen["escenario"]["modo"] == modo


def test_escenarios_de_la_spec():
    assert {e.id for e in config.ESCENARIOS.values()} >= {str(n) for n in range(1, 10)}
    for e in config.ESCENARIOS.values():
        if not e.real:
            assert e.repos >= 10 * e.lugares
    for prefijo, grupo in (("b", config.BARRIDO_RED), ("bs", config.BARRIDO_SIN_TOPE)):
        barrido = [config.ESCENARIOS[f"{prefijo}{n}"] for n in config.BARRIDO_LUGARES]
        assert all(e.real and e.grupo == grupo for e in barrido)
        assert [e.lugares for e in barrido] == list(config.BARRIDO_LUGARES)


async def test_servidor_en_varios_procesos(levantar, certs, tmp_path):
    # Dos servidores en puertos consecutivos: cada repositorio va siempre al mismo.
    base = puertos_libres(2)
    parametros = Parametros(tam_archivo=200_000, latencia=latencia_fija(0.1))
    servidores = [(await levantar(replace(parametros, indice=i), base + i))[0] for i in range(2)]
    resumen = await motor.correr(_escenario(Modo.CORRUTINAS), base, certs, tmp_path / "varios", procesos=2)
    assert resumen["servidor"]["procesos"] == 2
    assert resumen["servidor"]["violaciones"] == 0
    assert resumen["servidor"]["pedidos"] == sum(s.cortesia.pedidos for s in servidores)
    assert all(s.cortesia.pedidos > 0 for s in servidores)
    for i, s in enumerate(servidores):
        assert all(repo % 2 == i for repo in s.cortesia._ultimo_fin)


def test_red_de_la_instancia_simulada():
    assert config.escenario("4", instancia_="t3.small").red == config.INSTANCIAS["t3.small"].red_base
    assert config.escenario("b10", instancia_="c6gn.medium").red == 1.6e9 / 8
    assert config.escenario("6", instancia_="c6gn.medium").red is None
    assert config.INSTANCIAS["t3.small"].cpu_disponible == 40.0
    assert config.INSTANCIAS["c6gn.medium"].cpu_disponible == 80.0
