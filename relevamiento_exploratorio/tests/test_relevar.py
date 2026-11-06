import asyncio
from collections import defaultdict

import pytest

import repos_falsos
from relevamiento.config import ConfigCortesia
from relevamiento.modelos import Deteccion, Repositorio
from relevamiento.registro import EVENTOS, PEDIDOS, leer_jsonl
from relevamiento.relevar import plan, relevar, rutas_oai, rutas_rest
from relevamiento.verificar import verificar

CONTACTO = "prueba@example.org"


async def correr(internet, salida, cfg=ConfigCortesia(), repos=None):
    todos, items = repos_falsos.armar(internet)
    return await relevar(repos or todos, items, salida, CONTACTO, cfg,
                         transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)


def eventos(salida, tipo):
    return [e for e in leer_jsonl(salida / EVENTOS) if e["tipo"] == tipo]


def resultados(salida):
    tabla = defaultdict(dict)
    for e in eventos(salida, "resultado"):
        tabla[(e["repo"], e["item_idx"])][e["metodo"]] = e["resultado"]
    return tabla


async def test_corrida_completa_sobre_el_internet_falso(internet, salida):
    await correr(internet, salida)

    res = verificar(salida)
    assert res.ok, res.violaciones
    assert res.estadisticas["max_en_vuelo"] <= 3

    terminados = {e["repo"]: e["motivo_fin"] for e in eventos(salida, "repo_terminado")}
    assert terminados == {"r6": "completo", "r7": "completo", "ojs": "completo", "bot": "abandono", "tls": "completo"}
    assert not eventos(salida, "error_interno")

    recon = {e["repo"]: e for e in eventos(salida, "reconocimiento")}
    assert (recon["r6"]["plataforma"], recon["r6"]["version"], recon["r6"]["rest"]) == ("dspace", "6.3", "v5_6")
    assert (recon["r7"]["version"], recon["r7"]["rest_base"]) == ("7.6.1", "https://api.repo7.org/server")
    assert recon["r7"]["oai_endpoint"] == "https://api.repo7.org/server/oai/request"
    assert (recon["ojs"]["plataforma"], recon["ojs"]["oai_endpoint"]) == ("ojs", "https://revista.org/index.php/rev/oai")
    assert recon["bot"]["antibot"] is None and "inicio" in recon["bot"]["no_probado"]
    assert (recon["tls"]["plataforma"], recon["tls"]["oai_fuente"]) == ("eprints", "csv")

    fuentes = {e["repo"]: e["fuente"] for e in eventos(salida, "items")}
    assert fuentes["ojs"].startswith("oai_listrecords_desde_")  # registros del último año
    assert fuentes["r6"] == "oai_listrecords"  # con from no hubo registros: repite sin filtro
    assert (fuentes["r7"], fuentes["bot"]) == ("csv", "ninguna")
    pedidos_lr = [p["url"] for p in leer_jsonl(salida / PEDIDOS) if p["metodo"] == "oai_listrecords" and p["repo"] == "r6"]
    assert len(pedidos_lr) == 2 and "&from=" in pedidos_lr[0] and "&from=" not in pedidos_lr[1]

    tabla = resultados(salida)
    assert tabla[("r6", 0)] == {
        "directo": "no aplica", "pagina": "ok", "doi_handle": "no aplica", "oai_xoai": "ok", "oai_ore": "ok",
        "oai_mets": "ok", "rest56": "ok", "rest7": "no aplica"}
    assert tabla[("r6", 2)]["doi_handle"] == "ok"  # doi.org redirige a la página del ítem
    assert tabla[("r6", 2)]["rest56"] == "ok"  # el handle salió de la página de destino
    assert tabla[("r7", 0)]["rest7"] == "ok" and tabla[("r7", 0)]["pagina"] == "ok"
    assert tabla[("r7", 0)]["oai_xoai"] == "no aplica"  # sin handle no hay identificador OAI
    assert tabla[("ojs", 0)]["pagina"] == "ok" and tabla[("ojs", 1)]["pagina"] == "ok"
    assert tabla[("tls", 0)]["pagina"] == "ok"

    verificaciones = eventos(salida, "verificacion")
    r6 = [v for v in verificaciones if v["repo"] == "r6" and v["item_idx"] == 0]
    assert {v["motivo"] for v in r6 if not v["verificado"]} == {
        "bundle derivado", "mismo nombre y bundle que otro archivo verificado"}
    assert all(v["ok"] for v in r6 if v["verificado"])
    galerada_html = next(v for v in verificaciones if v["url"].endswith("/45/31"))
    assert galerada_html["verificado"] and not galerada_html["ok"] and galerada_html["magic"] == "html"

    pedidos = list(leer_jsonl(salida / PEDIDOS))
    assert all(p["tipo"] == "archivo" for p in pedidos if p["paso"] == "verificacion")
    assert {p["tls_verificado"] for p in pedidos if p["host"] == "tls.org"} == {True, False}
    assert max(sum(1 for p in pedidos if p["repo"] == r) for r in terminados) <= 40


async def test_con_poco_presupuesto_lo_que_falta_queda_no_probado(internet, salida):
    await correr(internet, salida, ConfigCortesia(pedidos_por_repo=10), repos=[repos_falsos.REPOS[0]])
    assert eventos(salida, "repo_terminado")[0]["motivo_fin"] == "presupuesto"
    tabla = resultados(salida)
    assert "no probado" in {r for metodos in tabla.values() for r in metodos.values()}
    assert sum(1 for p in leer_jsonl(salida / PEDIDOS)) == 10
    assert verificar(salida, ConfigCortesia(pedidos_por_repo=10)).ok


async def test_ctrl_c_y_reanudacion(internet, salida):
    tarea = asyncio.create_task(correr(internet, salida))
    await asyncio.sleep(90)
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea
    primera = {e["repo"] for e in eventos(salida, "repo_terminado")}
    pedidos_primera = list(leer_jsonl(salida / PEDIDOS))
    assert pedidos_primera and len(primera) < 5

    await correr(internet, salida)
    assert {e["repo"] for e in eventos(salida, "repo_terminado")} == {r.id for r in repos_falsos.REPOS}
    pedidos = list(leer_jsonl(salida / PEDIDOS))
    corridas = {p["corrida"] for p in pedidos}
    assert len(corridas) == 2
    segunda = max(corridas)
    assert not [p for p in pedidos if p["corrida"] == segunda and p["repo"] in primera]
    res = verificar(salida)
    assert res.ok, res.violaciones
    assert all(sum(1 for p in pedidos if p["repo"] == r.id) <= 40 for r in repos_falsos.REPOS)


def test_rutas_oai_y_rest_segun_la_plataforma():
    assert rutas_oai("https://r.org/", None, None) == [
        "https://r.org/oai/request", "https://r.org/server/oai/request", "https://r.org/cgi/oai2"]
    assert rutas_oai("https://r.org/index.php/rev", Deteccion("ojs", None), None)[0] == "https://r.org/index.php/rev/oai"
    assert rutas_oai("https://r.org/", Deteccion("dspace", "7.6"), "https://api.r.org/server")[0] == (
        "https://api.r.org/server/oai/request")
    assert rutas_rest("https://r.org/xmlui", Deteccion("dspace", None, "xmlui"), None) == [
        ("v5_6", "https://r.org/rest/status", "https://r.org/rest")]
    assert rutas_rest("https://r.org/", Deteccion("ojs", "3.3"), None) == []
    assert [t for t, _, _ in rutas_rest("https://r.org/", None, None)] == ["v7", "v5_6"]


def test_plan_no_hace_pedidos():
    lineas = plan([Repositorio("x", "X", "https://x.org/")], {})
    assert "  robots.txt: https://x.org/robots.txt" in lineas
    assert any("ListRecords" in linea for linea in lineas)
