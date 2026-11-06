"""`--db` contra un PostgreSQL local. Solo corre con CONCURRENCIA_DSN definido."""

import os

import pytest

from concurrencia import motor
from concurrencia.config import Escenario
from concurrencia.servidor import Parametros
from tests.conftest import latencia_fija

DSN = os.environ.get("CONCURRENCIA_DSN")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DSN is None, reason="falta CONCURRENCIA_DSN (PostgreSQL local)"),
]


async def test_registra_cada_resultado(levantar, certs, tmp_path):
    _, puerto = await levantar(Parametros(tam_archivo=100_000, latencia=latencia_fija(0.05), prob_429=0, prob_503=0))
    escenario = Escenario("db", 10, 100, 1000, "db", db=True, duracion=3.0, calentamiento=0.5, tam_archivo=100_000)
    resumen = await motor.correr(escenario, puerto, certs, tmp_path / "db", dsn=DSN)
    assert resumen["errores_por_tipo"] == {}
    import csv

    with (tmp_path / "db.csv").open() as f:
        assert sum(float(fila["db_tx"]) for fila in csv.DictReader(f)) > 0
