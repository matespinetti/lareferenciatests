"""Muestra para la app: clasificación de fallas, ítems por repositorio, buscador y armado de muestra_app/."""

import csv
import json
from pathlib import Path

import httpx
import pytest

from relevamiento.config import CORTESIA_BUSCADOR
from relevamiento.muestra import COSECHA, UBICACION, Candidata, escribir_candidatos
from relevamiento.muestra_app import (
    BUSCADOR,
    clasificar_falla,
    items_de_buscador,
    objetivo_items,
    paginas_repartidas,
    pedir_al_buscador,
    plan,
    repartir,
    resumir_muestra_app,
)
from relevamiento.parseo import parsear_xml
from relevamiento.verificar import verificar


def test_xml_con_basura_adelante():
    # El OAI de la USP antepone `else` (un error de PHP) a la declaración XML.
    raiz = parsear_xml(b'else<?xml version="1.0"?><OAI-PMH><ListMetadataFormats/></OAI-PMH>')
    assert raiz is not None and raiz.tag == "OAI-PMH"
    assert parsear_xml(b"basura sin xml") is None


def _p(metodo: str, codigo: int | None = None, error: str | None = None, url: str = "https://x.org/") -> dict:
    return {"metodo": metodo, "codigo": codigo, "error": error, "url": url}


@pytest.mark.parametrize(
    "estado, motivo, pedidos, ubicacion, ibict, causa",
    [
        ("antibot", "cloudflare", [], {"host": "a.org"}, None, "antibot"),
        ("sin_ubicacion", None, [], {}, None, "no_ubicado"),
        # Sitio que anda y OAI que no está en las rutas probadas: nuestro.
        ("sin_oai", None, [_p("inicio", 200), _p("oai_identify", 404)], {"host": "a.org"}, None, "oai_no_encontrado"),
        # Con el endpoint del IBICT probado, una falla del OAI es del repositorio.
        ("sin_oai", None, [_p("oai_identify", 404, url="https://a.org/oai/x")], {"host": "a.org"}, "https://a.org/oai/x", "oai_roto"),
        ("sin_oai", None, [_p("inicio", 404), _p("oai_identify", 500)], {"host": "a.org"}, None, "sitio_con_error"),
        ("sin_oai", None, [_p("inicio", 403), _p("robots", 468)], {"host": "a.org"}, None, "rechazo_403"),
        ("abandono", "código 503", [_p("oai_identify", 503)], {"host": "a.org"}, None, "servicio_no_disponible"),
        ("abandono", "2 respuestas 403 seguidas", [_p("inicio", 403)], {"host": "a.org"}, None, "rechazo_403"),
        # Errores de red en un host que no es el del ítem del buscador: lo dedujimos mal.
        ("abandono", "3 errores de red seguidos", [_p("inicio", error="red")] * 3,
         {"host": "oai.revista.org", "url_ejemplo": "https://revista.org/article/view/1"}, None, "url_mal_deducida"),
        # El mismo host que el ítem (o un handle): el servidor está caído.
        ("abandono", "3 errores de red seguidos", [_p("inicio", error="timeout_conexion")] * 3,
         {"host": "a.org", "url_ejemplo": "https://a.org/handle/1"}, None, "caido"),
        ("abandono", "3 errores de red seguidos", [_p("inicio", error="red")] * 3,
         {"host": "a.org", "url_ejemplo": "https://hdl.handle.net/1/2"}, None, "caido"),
        ("sin_items", "http_500", [], {"host": "a.org"}, None, "oai_vacio"),
    ],
)  # fmt: skip
def test_clasificar_falla(estado, motivo, pedidos, ubicacion, ibict, causa):
    assert clasificar_falla(estado, motivo, pedidos, ubicacion, ibict) == causa


def test_objetivo_items_y_reparto():
    assert objetivo_items(50) == 50  # chico: entero
    assert objetivo_items(462) == 200  # el piso
    assert objetivo_items(231_604) == 4633  # el 2 %
    items = [{"item": f"i{k}", "datestamp": f"20{10 + k % 15}-01-01"} for k in range(100)]
    elegidos = repartir(items, 10)
    assert len(elegidos) == 10 and len({i["item"] for i in elegidos}) == 10
    assert elegidos[0]["datestamp"] < elegidos[-1]["datestamp"]  # repartidos en el tiempo
    assert paginas_repartidas(231_604, 4) == [1, 580, 1159, 1738]
    assert paginas_repartidas(150, 10) == [1, 2]


def _buscador(total: int, registros: list[tuple[str, list[str]]], pais: str = "CO") -> str:
    return json.dumps({"resultCount": total, "status": "OK", "records": [
        {"rawData": {"oai_identifier_str": i, "url": urls, "network_acronym_str": pais, "publishDate": ["2020"]}}
        for i, urls in registros
    ]})  # fmt: skip


def test_items_de_buscador():
    total, items = items_de_buscador(_buscador(2125, [("oai:a:1", ["https://hdl.handle.net/1/1", "https://a.org/handle/1/1"]),
                                                     ("oai:a:2", "https://a.org/2")]).encode())  # fmt: skip
    assert total == 2125 and [i["item"] for i in items] == ["oai:a:1", "oai:a:2"]
    assert items[1]["urls"] == ["https://a.org/2"] and items[0]["pais"] == "CO"
    assert items_de_buscador(b"<html>no</html>") == (None, [])


async def test_muestra_app_de_punta_a_punta(internet, tmp_path: Path):
    """Un útil con ítems OAI (se toman 200 de 300), uno con anti-bot (sus ítems salen del buscador) y uno con
    el OAI no encontrado (nuestro: queda afuera)."""
    muestra = tmp_path / "muestra"
    candidatas = [Candidata("m0001", 1, "D", "Útil", 900), Candidata("m0002", 2, "D", "Con anti-bot", 250),
                  Candidata("m0003", 3, "D", "Sin OAI", 400)]  # fmt: skip
    escribir_candidatos(muestra, candidatas, [(c.repositorio, c.documentos) for c in candidatas] + [("Otro", 500)])
    (muestra / UBICACION).mkdir()
    (muestra / UBICACION / "eventos.jsonl").write_text("".join(
        json.dumps({"tipo": "ubicacion", "repo": c.id, "host": f"{c.id}.org", "pais": "AR"}) + "\n" for c in candidatas))
    (muestra / COSECHA).mkdir()
    eventos = [{"tipo": "repo_terminado", "repo": "m0001", "estado": "ok", "corrida": "c1"},
               {"tipo": "repo_terminado", "repo": "m0002", "estado": "antibot", "motivo": "cloudflare", "corrida": "c1"},
               {"tipo": "repo_terminado", "repo": "m0003", "estado": "sin_oai", "corrida": "c1"}]  # fmt: skip
    (muestra / COSECHA / "eventos.jsonl").write_text("".join(json.dumps(e) + "\n" for e in eventos))
    pedidos = [{"repo": "m0003", "corrida": "c1", "metodo": "inicio", "codigo": 200, "url": "https://m0003.org"}]
    (muestra / COSECHA / "pedidos.jsonl").write_text("".join(json.dumps(p) + "\n" for p in pedidos))
    with open(muestra / "items.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "item", "datestamp", "pagina", "archivos"])
        w.writerows(["m0001", f"oai:m1:{k}", f"20{10 + k % 15}-01-01", f"https://m0001.org/handle/{k}", 1] for k in range(300))
    with open(muestra / "archivos.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["repo", "item", "url", "tamano", "mime", "bundle", "nombre"])
        w.writerows(["m0001", f"oai:m1:{k}", f"https://m0001.org/bitstream/{k}.pdf", 1000, "application/pdf", "ORIGINAL", ""] for k in range(300))

    def buscador(request: httpx.Request) -> httpx.Response | None:
        if request.url.path != "/api/v1/search":
            return None
        assert 'reponame_str:"Con anti-bot"' in request.url.params["filter[]"]
        pagina = int(request.url.params["page"])
        registros = [(f"oai:m2:{k}", [f"https://hdl.handle.net/9/{k}", f"https://m0002.org/handle/9/{k}"])
                     for k in range(100 * (pagina - 1), min(250, 100 * pagina))]  # fmt: skip
        return httpx.Response(200, headers={"content-type": "application/json"}, text=_buscador(250, registros))

    internet.agregar_host("buscador.lareferencia.info", buscador)
    filas = {f["candidata"].id: f for f in plan(muestra)}
    assert (filas["m0001"]["de_oai"], filas["m0001"]["faltan"]) == (200, 0)
    assert (filas["m0002"]["estado"], filas["m0002"]["faltan"]) == ("antibot", 200)
    assert filas["m0003"]["nuestra"] and filas["m0003"]["faltan"] == 0

    resultado = await pedir_al_buscador(muestra, "prueba@example.org", CORTESIA_BUSCADOR, transporte=internet.transporte)
    assert resultado["items_nuevos"] >= 200 and internet.max_por_host == 1
    assert verificar(muestra / BUSCADOR, CORTESIA_BUSCADOR).ok
    assert all(f["faltan"] == 0 for f in plan(muestra))
    antes = len(internet.llamadas)
    await pedir_al_buscador(muestra, "prueba@example.org", CORTESIA_BUSCADOR, transporte=internet.transporte)
    assert len(internet.llamadas) == antes  # al relanzar no falta nada

    destino = tmp_path / "muestra_app"
    resumen = resumir_muestra_app(muestra, destino)
    repos = {r["id"]: r for r in csv.DictReader(open(destino / "repositorios.csv", encoding="utf-8"))}
    assert set(repos) == {"m0001", "m0002"} and repos["m0002"]["estado"] == "antibot"
    assert repos["m0001"]["peso"] == "2.0"  # 4 de la población en el estrato D, 2 en la muestra
    items = list(csv.DictReader(open(destino / "items.csv", encoding="utf-8")))
    assert sum(1 for i in items if i["repo"] == "m0001" and i["fuente"] == "oai") == 200
    de_m2 = [i for i in items if i["repo"] == "m0002"]
    assert len(de_m2) == 200 and all(i["fuente"] == "buscador" for i in de_m2)
    assert all("m0002.org" in i["url"] for i in de_m2)  # la URL del repositorio antes que el handle
    excluidos = list(csv.DictReader(open(destino / "excluidos.csv", encoding="utf-8")))
    assert [e["id"] for e in excluidos] == ["m0003"] and excluidos[0]["estado"] == "oai_no_encontrado"
    assert resumen["items"] == 400 and sum(1 for _ in csv.DictReader(open(destino / "archivos.csv", encoding="utf-8"))) == 200
