import random

import pytest

from concurrencia import config
from concurrencia.distribucion import ARCHIVO, LATENCIA, PAGINA, LogNormal, items_por_repo


def test_items_por_repo_suma_el_total_y_es_determinista():
    tamanos = items_por_repo(2400, 6_000_000, semilla=1)
    assert sum(tamanos) == 6_000_000
    assert tamanos == items_por_repo(2400, 6_000_000, semilla=1)


def test_dos_tercios_en_el_4_por_ciento():
    tamanos = sorted(items_por_repo(2400, 6_000_000, semilla=1), reverse=True)
    grandes = sum(tamanos[: 2400 // 25])
    assert 0.55 < grandes / 6_000_000 < 0.8


@pytest.mark.parametrize(
    ("dist", "p50", "p95"),
    [
        (LATENCIA, config.LATENCIA_P50, config.LATENCIA_P95),
        (PAGINA, config.PAGINA_P50, config.PAGINA_P95),
        (ARCHIVO, config.ARCHIVO_P50, config.ARCHIVO_P95),
    ],
)
def test_percentiles_calibrados(dist: LogNormal, p50: float, p95: float):
    rng = random.Random(0)
    muestras = sorted(dist.muestra(rng) for _ in range(20_000))
    assert muestras[10_000] == pytest.approx(p50, rel=0.05)
    assert muestras[19_000] == pytest.approx(p95, rel=0.1)


def test_media_truncada_coincide_con_las_muestras():
    rng = random.Random(0)
    n = 200_000
    media = sum(ARCHIVO.muestra(rng) for _ in range(n)) / n
    assert media == pytest.approx(ARCHIVO.media(), rel=0.1)
    # Del mismo orden que la media de los pilotos (11,5 MB con solo 51 archivos).
    assert 5 * config.MB < ARCHIVO.media() < 20 * config.MB
