"""El informe se arma con resultados sintéticos."""

import json
from dataclasses import asdict, replace

import pytest

from concurrencia import config
from concurrencia.distribucion import items_por_repo
from concurrencia.informe import armar, barridos, cargar, criterio, curva, proyeccion, resumir
from concurrencia.metricas import COLUMNAS, escribir


def _resultado(destino, id: str, cpu: float, rss: float, mb_s: float = 50, **cambios) -> None:
    filas = []
    for t in range(1, 41):
        fila = {c: 0 for c in COLUMNAS}
        fila |= {
            "t": t, "calentamiento": int(t <= 5), "cpu": cpu, "cpu_sistema": cpu / 4, "rss_mb": rss,
            "pedidos": 100, "mb_recibidos": mb_s, "items_ok": mb_s / 16, "ocupacion": 50,
            "bucle_p99_ms": 2, "despacho_p99_ms": 3, "duracion_resolucion_s": 0.7, "duracion_descarga_s": 6.0,
            "lat_p50_ms": 470, "lat_p95_ms": 1900,
        }  # fmt: skip
        filas.append(fila)
    e = replace(config.ESCENARIOS[id], duracion=35, calentamiento=5, **cambios)
    resumen = {
        "escenario": asdict(e),
        "errores_por_tipo": {},
        "servidor": {"pedidos": 4000, "violaciones": 0, "cpu_servidor": [30.0] * 40},
        "versiones": {"python": "3.12"},
        "bucle": {"p50_ms": 1.0, "p99_ms": 2.5, "max_ms": 9.0},
    }
    escribir(destino / id, filas, resumen)


def _barrido_sintetico(destino) -> None:
    # Rinde más hasta 25 lugares y después se aplana: la red está llena.
    for n in config.BARRIDO_LUGARES:
        _resultado(destino, f"b{n}", cpu=5 + n / 100, rss=100 + n * 0.02, mb_s=min(15.0, 0.75 * n))


def test_arma_el_informe(tmp_path):
    resultados = tmp_path / "results"
    resultados.mkdir()
    _resultado(resultados, "1", 5, 60)
    _resultado(resultados, "2", 12, 80)
    _resultado(resultados, "4", 30, 300)
    _resultado(resultados, "2h", 25, 200)
    _barrido_sintetico(resultados)
    (resultados / "instancia.json").write_text(json.dumps({"tipo": "t3.small"}))
    armar(resultados, tmp_path / "report.md")
    texto = (tmp_path / "report.md").read_text()
    for seccion in ("## Barrido", "**Rodilla:** con 25 lugares", "### A lo largo de toda la descarga", "### Sensibilidad a la velocidad por conexión",
                    "## Proyección al problema real", "## Criterio de decisión"):  # fmt: skip
        assert seccion in texto
    for grafico in ("graficos.png", "barrido.png", "curva.png"):
        assert (resultados / grafico).exists()


def test_rodilla_y_costo_de_sobrar(tmp_path):
    _barrido_sintetico(tmp_path)
    grupos = barridos([resumir(r) for r in cargar(tmp_path)])
    datos = grupos[config.BARRIDO_RED]
    assert datos["rodilla_mb"]["lugares"] == 25
    assert datos["sobran"] == 2400 - 25
    assert datos["rss_kb_por_lugar"] == pytest.approx(0.02 * 1024)


def test_proyeccion_y_criterio(tmp_path):
    _resultado(tmp_path, "4", 30, 300)
    resumenes = [resumir(r) for r in cargar(tmp_path)]
    assert resumenes[0]["segundos"] == 35
    p = proyeccion(resumenes)
    # El repositorio más grande: del orden de cientos de miles de ítems. La duración la fija el que
    # más tarda solo (grande y lento), y con velocidades distintas da un rango.
    assert 200_000 < p["items_max"] < 600_000
    assert p["duracion_cortesia_dias"] > 1
    rango = p["rango"]["sin tope de red"]
    assert rango["p10"] <= rango["p50"] <= rango["p90"]
    filas = criterio(resumenes)
    assert all(cumple for id, _, cumple in filas if id == "4")
    assert all(cumple is None for id, _, cumple in filas if id == "9")


def test_curva_sin_tope_dura_lo_del_repo_mas_grande():
    items = items_por_repo(2400, 6_000_000, semilla=1)
    c = curva(items, ciclos=12.0, ocupados=9.0, bytes_item=16e6, red=None)
    assert c["duracion_dias"] * 86400 == pytest.approx(max(items) * 12.0)
    activos = [p["activos"] for p in c["puntos"]]
    assert activos == sorted(activos, reverse=True)
    assert c["puntos"][0]["lugares"] == pytest.approx(2400 * 9 / 12)


def test_curva_con_poca_red_dura_mas_y_pide_menos_lugares():
    items = items_por_repo(2400, 6_000_000, semilla=1)
    sin_tope = curva(items, 12.0, 9.0, 16e6, None)
    con_red = curva(items, 12.0, 9.0, 16e6, 16e6)
    assert con_red["duracion_dias"] > sin_tope["duracion_dias"]
    # La red se llena con 16 MB/s × 9 s / 16 MB = 9 lugares.
    assert con_red["lugares_red"] == pytest.approx(9.0)
    assert max(p["lugares"] for p in con_red["puntos"]) == pytest.approx(9.0)


def test_seccion_de_validacion_en_dos_maquinas(tmp_path):
    from concurrencia.informe import _a_mb

    resultados = tmp_path / "results"
    resultados.mkdir()
    _resultado(resultados, "b120", 20, 150)
    _resultado(resultados, "v120", 35, 180)
    for id in ("b120", "v120"):
        ruta = resultados / f"{id}.json"
        datos = json.loads(ruta.read_text())
        datos["despliegue"] = {"ip_servidor": "10.0.1.23", "dos_maquinas": True, "s3": "real"}
        ruta.write_text(json.dumps(datos))
    (resultados / "instancia.json").write_text(json.dumps({"tipo": "c6gn.medium", "memoria_mb": 1900}))
    (resultados / "tiempos.csv").write_text(
        "escenario,inicio_utc,fin_utc\nb120,2026-11-06T10:00:00Z,2026-11-06T10:04:00Z\n"
        "v120,2026-11-06T10:05:00Z,2026-11-06T10:09:00Z\n"
    )
    # 10:06 UTC = 1793959560; 10:01 UTC = 1793959260.
    (resultados / "docker_stats.csv").write_text(
        "t,contenedor,cpu,memoria\n"
        "1793959260,candidata-motor-run-1a2b,40%,300MiB / 1.8GiB\n"
        "1793959560,candidata-motor-run-3c4d,60%,512.5MiB / 1.8GiB\n"
        "1793959560,candidata-postgres-1,5%,95MiB / 1.8GiB\n"
    )
    armar(resultados, tmp_path / "report.md")
    texto = (tmp_path / "report.md").read_text()
    assert "## Validación en una c6gn.medium real" in texto
    assert "La subida va a **S3 real**" in texto
    fila_v120 = next(linea for linea in texto.splitlines() if linea.startswith("| v120 |") and "de 1.900" in linea)
    assert "| 512 | 95 |" in fila_v120
    assert _a_mb("1.5GiB / 1.8GiB") == 1536 and _a_mb("512KiB / 1GiB") == 0.5


def test_curva_con_velocidades_distintas():
    # Dos repositorios iguales en ítems: el lento (ciclo el doble) tarda el doble; con poca red,
    # el que pide menos que su parte se lleva lo que pide y el otro el resto.
    sin_tope = curva([100, 100], ciclos=[1.0, 2.0], ocupados=[0.5, 1.0], bytes_item=1e6, red=None)
    assert sin_tope["duracion_dias"] * 86400 == pytest.approx(200.0)
    con_red = curva([100, 100], ciclos=[1.0, 2.0], ocupados=[0.5, 1.0], bytes_item=1e6, red=1.2e6)
    # Piden 1 y 0,5 MB/s con 1,2 de red: el lento recibe su 0,5 y el rápido 0,7. El rápido termina
    # más tarde (143 s en vez de 100), pero la duración la sigue fijando el lento.
    assert con_red["puntos"][0]["mb_s"] * 2**20 == pytest.approx(1.2e6)
    assert con_red["puntos"][1]["t"] == pytest.approx(100 / 0.7)
    assert con_red["duracion_dias"] == pytest.approx(sin_tope["duracion_dias"])


def test_velocidad_por_repositorio_calibrada():
    import statistics

    from concurrencia.distribucion import ancho_repo

    anchos = [ancho_repo(1, r) / config.MB for r in range(2400)]
    q = statistics.quantiles(anchos, n=4)
    assert q[1] == pytest.approx(2.15, rel=0.05)
    assert 0.8 < q[0] < 1.3 and 3.2 < q[2] < 5.0
    assert min(anchos) >= 0.3 * 2.15 - 1e-9 and max(anchos) <= 6 * 2.15 + 1e-9
    assert ancho_repo(1, 7) == ancho_repo(1, 7)
    assert ancho_repo(1, 7, sigma=0) == config.ANCHO_POR_CONEXION
