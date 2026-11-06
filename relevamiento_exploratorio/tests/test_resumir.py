import csv

import repos_falsos
from relevamiento.relevar import relevar
from relevamiento.resumir import cuantiles, resumir


def leer_csv(ruta):
    with open(ruta, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


async def test_resumir_sobre_una_corrida_completa(internet, salida):
    repos, items = repos_falsos.armar(internet)
    await relevar(repos, items, salida, "prueba@example.org",
                  transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)
    rutas = resumir(salida, repos)
    assert [r.name for r in rutas] == ["repositorios.csv", "resultados.csv", "resumen.md"]

    filas = {f["repo_id"]: f for f in leer_csv(salida / "repositorios.csv")}
    assert (filas["r6"]["plataforma"], filas["r6"]["version"], filas["r6"]["rest"]) == ("dspace", "6.3", "v5_6")
    assert filas["r6"]["oai_formatos"] == "oai_dc xoai ore mets qdc"
    assert filas["r6"]["hosts_alcanzados"] == "doi.org repo6.org"
    assert filas["bot"]["antibot"] == "anubis (bot.org)" and filas["bot"]["terminado"] == "abandono"
    assert filas["bot"]["motivo_abandono"] == "bot.org: anti-bot: anubis"
    assert filas["tls"]["tls_invalido"] == "tls.org"
    assert filas["r7"]["detecciones"] == "generator=dspace 7.6.1; html=dspace; oai_identify=dspace; rest7=dspace 7.6.1"
    assert all(f["latencia_mediana_ms"] for f in filas.values() if f["repo_id"] != "bot")

    resultados = leer_csv(salida / "resultados.csv")
    xoai = next(f for f in resultados if f["repo_id"] == "r6" and f["item_idx"] == "0" and f["metodo"] == "oai_xoai")
    assert (xoai["archivos_encontrados"], xoai["archivos_verificados"], xoai["bundles"]) == ("3", "1", "LICENSE ORIGINAL THUMBNAIL")
    rest = next(f for f in resultados if f["repo_id"] == "r6" and f["item_idx"] == "0" and f["metodo"] == "rest56")
    assert (rest["archivos_verificados"], rest["archivos_equivalentes"], rest["tipos"]) == ("0", "1", "pdf")
    assert len(resultados) == 8 * (3 + 1 + 2 + 1)  # métodos × ítems de r6, r7, ojs y tls

    resumen = (salida / "resumen.md").read_text(encoding="utf-8")
    for titulo in ["## 1. Plataforma y versión", "## 2. Métodos", "## 3. Archivos por ítem", "## 4. Tipos de URL",
                   "## 5. Anti-bot", "## 6. Latencias", "## Cortesía"]:
        assert titulo in resumen
    assert "verificar-cortesia: **OK**" in resumen
    assert "| dspace 6 | — | 2/2 · 1 ped. | 1/1 · 2 ped. | 3/3" in resumen  # el ítem con DOI lo cubre doi_handle
    assert "en 4 de 4 ítems con bundles conocidos" in resumen  # 3 de DSpace 6 y 1 de DSpace 7


def test_cuantiles():
    assert cuantiles([]) == (None, None)
    assert cuantiles([7]) == (7, 7)
    assert cuantiles(list(range(1, 11))) == (6, 9)
