"""Muestra de LA Referencia: selección por estratos, ubicación con el buscador, cosecha con reemplazos y resumen."""

import json
from pathlib import Path

import httpx
import pytest

import repos_falsos
from relevamiento.config import CORTESIA_AMPLIACION, CORTESIA_MUESTRA, ESTRATOS_MUESTRA
from relevamiento.extractores import items_de_pagina, ubicar
from relevamiento.modelos import Respuesta
from relevamiento.muestra import (
    AMPLIACION,
    COSECHA,
    a_ampliar,
    ampliar_items,
    UBICACION,
    Candidata,
    coincide,
    cosechar,
    elegir,
    escribir_candidatos,
    leer_candidatos,
    leer_ubicaciones,
    resumir_muestra,
    ubicar_candidatas,
    url_buscador,
)
from relevamiento.registro import respuesta_desde_captura
from relevamiento.verificar import verificar

TAMANOS = Path(__file__).parent.parent / "tamanos" / "capturas"


def _respuesta(cuerpo: bytes | str, truncado: bool = False, url: str = "https://a.org/oai") -> Respuesta:
    datos = cuerpo.encode() if isinstance(cuerpo, str) else cuerpo
    return Respuesta(url_inicial=url, url=url, codigo=200, cuerpo=datos, truncado=truncado)


# --- Elegir ------------------------------------------------------------------------------


def _poblacion() -> list[tuple[str, int]]:
    return ([(f"A{i}", 60_000 + i) for i in range(3)] + [(f"B{i}", 20_000 + i) for i in range(5)]
            + [(f"C{i}", 5_000 + i) for i in range(200)] + [(f"D{i}", 500 + i) for i in range(200)]
            + [(f"E{i}", 10) for i in range(50)])  # fmt: skip


def test_elegir_censo_de_grandes_cupo_con_reservas_y_sin_los_muy_chicos():
    candidatas = elegir(_poblacion())
    por = {e: [c for c in candidatas if c.estrato == e] for e, *_ in ESTRATOS_MUESTRA}
    assert [c.repositorio for c in por["A"]] == ["A2", "A1", "A0"]  # todos, de mayor a menor
    assert len(por["B"]) == 5 and not any(c.reserva for c in por["A"] + por["B"])
    assert len(por["C"]) == 100 and sum(not c.reserva for c in por["C"]) == 50
    assert len(por["D"]) == 50 and sum(not c.reserva for c in por["D"]) == 25
    assert por["E"] == []
    assert elegir(_poblacion()) == candidatas  # misma semilla, misma muestra
    assert len({c.id for c in candidatas}) == len(candidatas)


def test_candidatos_se_escriben_y_se_leen(tmp_path):
    candidatas = elegir(_poblacion())
    escribir_candidatos(tmp_path, candidatas, _poblacion())
    assert leer_candidatos(tmp_path) == candidatas
    poblacion = json.loads((tmp_path / "poblacion.json").read_text())
    assert poblacion["C"] == {"repositorios": 200, "documentos": sum(5_000 + i for i in range(200))}


def test_url_buscador_filtra_por_nombre_exacto():
    url = httpx.URL(url_buscador('Repo "raro" (X)'))
    assert url.host == "buscador.lareferencia.info"
    assert url.params["filter[]"] == 'reponame_str:"Repo \\"raro\\" (X)"' and url.params["field[]"] == "rawData"


# --- Ubicar ------------------------------------------------------------------------------


def _buscador(nombre: str, oai: str | None, urls: list[str], pais: str = "UY") -> str:
    crudo = {"reponame_str": nombre, "network_acronym_str": pais, "url": urls}
    if oai:
        crudo["oai_identifier_str"] = oai
    return json.dumps({"resultCount": 1, "records": [{"rawData": crudo}], "status": "OK"})


@pytest.mark.parametrize(
    "oai, urls, host, url_base, plataforma",
    [
        # DSpace: el namespace es el host; el handle no sirve.
        ("oai:redi.anii.org.uy:20.500.12381/343", ["https://hdl.handle.net/20.500.12381/343"],
         "redi.anii.org.uy", "https://redi.anii.org.uy", None),
        # OJS: la revista sale de la URL del artículo, aunque el namespace sea el genérico.
        ("oai:ojs.pkp.sfu.ca:article/12", ["https://revistas.x.edu/index.php/rev/article/view/12"],
         "revistas.x.edu", "https://revistas.x.edu/index.php/rev", "ojs"),
        ("oai:ojs.revistas.y.edu:article/9", ["https://revistas.y.edu/cuadernos/article/view/9"],
         "revistas.y.edu", "https://revistas.y.edu/cuadernos", "ojs"),
        # Namespace que no es host: el de la URL del ítem.
        ("oai:repositorio:123/4", ["https://doi.org/10.1/x", "https://repo.z.org/handle/123/4"],
         "repo.z.org", "https://repo.z.org", None),
    ],
)  # fmt: skip
def test_ubicar(oai, urls, host, url_base, plataforma):
    u = ubicar(_respuesta(_buscador("R", oai, urls)), "R")
    assert (u.host, u.url_base, u.plataforma, u.pais, u.problema) == (host, url_base, plataforma, "UY", None)


def test_ubicar_sin_registros_otro_repositorio_solo_handle_y_sin_host():
    vacio = json.dumps({"resultCount": 0, "records": []})
    assert ubicar(_respuesta(vacio), "R").problema == "sin_registros"
    assert ubicar(_respuesta(_buscador("Otro", "oai:a.org:1", [])), "R").problema == "otro_repositorio"
    # España llega por RECOLECTA con `oai:dnet:…` y solo handles: queda el handle para resolverlo.
    u = ubicar(_respuesta(_buscador("R", "oai:dnet:csic::abc", ["http://hdl.handle.net/10261/430853"])), "R")
    assert (u.problema, u.handle) == ("solo_handle", "10261/430853")
    assert ubicar(_respuesta(_buscador("R", "oai:x:1", ["https://doi.org/10.1/x"])), "R").problema == "sin_host"


def _varios(nombre: str, registros: list[tuple[str | None, list[str]]]) -> str:
    return json.dumps({"records": [
        {"rawData": {"reponame_str": nombre, "network_acronym_str": "BR", "url": urls,
                     **({"oai_identifier_str": oai} if oai else {})}}
        for oai, urls in registros
    ]})  # fmt: skip


def test_ubicar_vota_entre_registros_y_deja_una_alternativa():
    # El primer registro apunta afuera; los demás, al repositorio.
    cuerpo = _varios("R", [("002299392", ["http://www.externo.com.br/x.html"])]
                     + [(None, [f"https://repositorio.x.br/items/{i}"]) for i in range(4)])
    u = ubicar(_respuesta(cuerpo), "R")
    assert (u.url_base, u.registros) == ("https://repositorio.x.br", 5)
    # Namespace de otro repositorio (un agregador) y páginas de ítem en el propio: manda la página de ítem.
    cuerpo = _varios("R", [(f"oai:sedici.unlp.edu.ar:10915/{i}", [f"https://repositorio.unal.edu.co/handle/unal/{i}"])
                           for i in range(3)])  # fmt: skip
    u = ubicar(_respuesta(cuerpo), "R")
    assert (u.url_base, u.alternativa) == ("https://repositorio.unal.edu.co", "https://sedici.unlp.edu.ar")


def test_ubicar_un_portal_de_varias_revistas_usa_el_endpoint_del_sitio():
    cuerpo = _varios("Portal", [(None, [f"https://revistas.u.ac.cr/index.php/{r}/article/view/1"]) for r in ("a", "b", "c")])
    u = ubicar(_respuesta(cuerpo), "Portal")
    assert (u.url_base, u.plataforma) == ("https://revistas.u.ac.cr/index.php/index", "ojs")


def test_url_de_handle_y_ubicacion_desde_handle():
    from relevamiento.extractores import ubicacion_desde_handle, url_de_handle
    from relevamiento.modelos import Ubicacion

    cuerpo = json.dumps({"responseCode": 1, "values": [
        {"index": 1, "type": "URL", "data": {"format": "string", "value": "http://digital.csic.es/handle/10261/430853"}}]})
    url = url_de_handle(_respuesta(cuerpo))
    assert url == "http://digital.csic.es/handle/10261/430853"
    u = ubicacion_desde_handle(Ubicacion(pais="ES", handle="10261/430853", problema="solo_handle"), url)
    assert (u.host, u.url_base, u.problema, u.pais) == ("digital.csic.es", "http://digital.csic.es", None, "ES")


def test_coincide():
    assert coincide("redi.anii.org.uy", "https://otro.org/oai/request", "redi.anii.org.uy") is True
    assert coincide(None, "https://redi.anii.org.uy/server/oai/request", "redi.anii.org.uy") is True
    assert coincide("x.org", "https://x.org/oai", "y.org") is False
    assert coincide("x.org", "https://x.org/oai", "ojs.pkp.sfu.ca") is None
    assert coincide("ojs.www.una.ac.cr", "https://www.revistas.una.ac.cr/index.php/index/oai", "ojs.www.una.ac.cr") is None


# --- Páginas de ListRecords --------------------------------------------------------------


def _mets(ident: str, archivos: list[tuple[str, int]], uri: str | None = None) -> str:
    grupos = "".join(
        f'<fileGrp USE="{uso}"><file MIMETYPE="application/pdf" SIZE="{tam}">'
        f'<FLocat xlink:href="https://a.org/bitstreams/{ident}-{i}/download"/></file></fileGrp>'
        for i, (uso, tam) in enumerate(archivos)
    )
    ident_uri = f'<mods:identifier type="uri">{uri}</mods:identifier>' if uri else ""
    return (
        f"<record><header><identifier>{ident}</identifier><datestamp>2024-01-01T00:00:00Z</datestamp></header><metadata>"
        f'<mets xmlns="http://www.loc.gov/METS/" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:mods="http://www.loc.gov/mods/v3">'
        f"<dmdSec>{ident_uri}</dmdSec><fileSec>{grupos}</fileSec></mets></metadata></record>"
    )


def _pagina(registros: list[str], token: str | None = None, total: int | None = None) -> str:
    fin = f'<resumptionToken completeListSize="{total}">{token or ""}</resumptionToken>' if total is not None else ""
    return ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>'
            + "".join(registros) + fin + "</ListRecords></OAI-PMH>")  # fmt: skip


def test_items_de_pagina_mets_con_url_tamano_y_pagina():
    cuerpo = _pagina([
        _mets("oai:a.org:1/1", [("ORIGINAL", 1000), ("THUMBNAIL", 5)], uri="http://hdl.handle.net/1/1"),
        _mets("oai:a.org:1/2", []),
        '<record><header status="deleted"><identifier>oai:a.org:1/3</identifier></header></record>',
    ], token="mets////100", total=250)  # fmt: skip
    p = items_de_pagina(_respuesta(cuerpo), "mets")
    assert (p.total, p.token, p.problema) == (250, "mets////100", None)
    primero, segundo, borrado = p.items
    assert primero.pagina == "http://hdl.handle.net/1/1" and primero.datestamp == "2024-01-01T00:00:00Z"
    assert [(a.url, a.tamano, a.mime, a.bundle) for a in primero.archivos] == [
        ("https://a.org/bitstreams/oai:a.org:1/1-0/download", 1000, "application/pdf", "ORIGINAL")]
    assert segundo.archivos == () and borrado.borrado


def test_items_de_pagina_cortada_descarta_el_ultimo_y_el_token():
    cuerpo = _pagina([_mets("i1", [("ORIGINAL", 1)]), _mets("i2", [("ORIGINAL", 2)])], token="t", total=9)
    p = items_de_pagina(_respuesta(cuerpo[:-80], truncado=True), "mets")
    assert [i.identificador for i in p.items] == ["i1"] and p.token is None


def test_items_de_pagina_oai_dc_solo_toma_archivos_directos():
    registro = ('<record><header><identifier>oai:r.org:article/1</identifier></header><metadata>'
                '<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                '<dc:identifier>https://r.org/index.php/rev/article/view/1</dc:identifier>'
                '<dc:identifier>https://r.org/files/1.pdf</dc:identifier><dc:identifier>10.1/x</dc:identifier>'
                "</oai_dc:dc></metadata></record>")  # fmt: skip
    (item,) = items_de_pagina(_respuesta(_pagina([registro])), "oai_dc").items
    assert item.pagina == "https://r.org/index.php/rev/article/view/1"
    assert [a.url for a in item.archivos] == ["https://r.org/files/1.pdf"]


def test_items_de_pagina_con_una_captura_real_de_dspace_7():
    captura = next(p for p in sorted((TAMANOS / "api.arca.fiocruz.br").glob("*.json"))
                   if "ListRecords" in json.loads(p.read_text())["url"])  # fmt: skip
    p = items_de_pagina(respuesta_desde_captura(captura), "mets")
    assert len(p.items) == 100 and p.total == 69700 and p.token
    con_archivos = [i for i in p.items if i.archivos]
    assert len(con_archivos) == 100
    assert all(a.url.startswith("https://arca.fiocruz.br/bitstreams/") and a.tamano for i in con_archivos for a in i.archivos)
    assert all(i.pagina and "arca.fiocruz.br/handle/" in i.pagina for i in con_archivos)


def test_items_de_pagina_error_oai():
    vacia = '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="noRecordsMatch"/></OAI-PMH>'
    assert items_de_pagina(_respuesta(vacia), "mets").problema == "noRecordsMatch"


# --- De punta a punta, contra el internet falso -------------------------------------------


@pytest.fixture
def muestra(tmp_path: Path) -> Path:
    return tmp_path / "muestra"


async def test_ubicar_y_cosechar_con_reemplazos(internet, muestra, monkeypatch):
    # Estrato C con cupo 2: los dos primeros no sirven (anti-bot y sin OAI) y se reemplazan; la última
    # reserva no se cosecha porque el cupo ya se cumplió.
    monkeypatch.setattr("relevamiento.muestra.ESTRATOS_MUESTRA", (("C", 0, None, 2),))
    repos_falsos.armar(internet)
    internet.agregar_host("nada.org", lambda r: httpx.Response(200, text="<html>hola</html>") if r.url.path == "/" else None)
    registros = {
        "Detrás de Anubis": ("oai:bot.org:1", ["https://bot.org/handle/1"]),
        "Sin OAI": ("oai:nada.org:1", ["https://nada.org/handle/1"]),
        "DSpace 6": ("oai:repo6.org:123/2", ["https://repo6.org/handle/123/2"]),
        "Revista": ("oai:ojs.pkp.sfu.ca:article/45", ["https://revista.org/index.php/rev/article/view/45"]),
        "Reserva": ("oai:repo6.org:123/3", ["https://repo6.org/handle/123/3"]),
        "Fantasma": None,
    }

    def buscador(request: httpx.Request) -> httpx.Response | None:
        if request.url.path != "/api/v1/search":
            return None
        nombre = request.url.params["filter[]"].removeprefix('reponame_str:"').removesuffix('"')
        datos = registros.get(nombre)
        cuerpo = _buscador(nombre, *datos) if datos else json.dumps({"resultCount": 0, "records": []})
        return httpx.Response(200, headers={"content-type": "application/json"}, text=cuerpo)

    internet.agregar_host("buscador.lareferencia.info", buscador)
    nombres = list(registros)
    candidatas = [Candidata(f"m{i:04d}", i, "C", n, 5_000, reserva=i > 2) for i, n in enumerate(nombres, 1)]
    escribir_candidatos(muestra, candidatas, [(n, 5_000) for n in nombres])

    await ubicar_candidatas(candidatas, muestra, "prueba@example.org", CORTESIA_MUESTRA, transporte=internet.transporte)
    ubicaciones = leer_ubicaciones(muestra)
    assert ubicaciones["m0004"]["url_base"] == "https://revista.org/index.php/rev"
    assert ubicaciones["m0006"]["problema"] == "sin_registros"
    assert verificar(muestra / UBICACION, CORTESIA_MUESTRA).ok

    resultado = await cosechar(
        candidatas, ubicaciones, {}, muestra, "prueba@example.org", CORTESIA_MUESTRA,
        transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar,
    )  # fmt: skip
    assert resultado["utiles"] == {"C": 2}
    assert internet.max_por_host == 1
    assert verificar(muestra / COSECHA, CORTESIA_MUESTRA).ok

    resumen = resumir_muestra(muestra)
    import csv

    filas = {f["id"]: f for f in csv.DictReader(open(muestra / "repositorios.csv", encoding="utf-8"))}
    assert filas["m0001"]["estado"] == "antibot"
    assert filas["m0002"]["estado"] == "sin_oai"
    assert filas["m0003"]["estado"] == "ok" and filas["m0003"]["formato"] == "mets" and filas["m0003"]["coincide"] == "si"
    assert filas["m0004"]["estado"] == "ok" and filas["m0004"]["formato"] == "oai_dc"
    assert filas["m0005"]["estado"] == "no_cosechado"
    assert filas["m0006"]["estado"] == "sin_ubicacion"
    assert resumen["estratos"]["C"]["utiles"] == 2 and resumen["para_la_prueba"]["repositorios_utiles"] == 2
    items = list(csv.DictReader(open(muestra / "items.csv", encoding="utf-8")))
    assert {i["repo"] for i in items} == {"m0003", "m0004"}
    assert any(i["pagina"].startswith("https://revista.org/index.php/rev/article/view/") for i in items)
    # Al relanzar no se vuelve a pedir nada: los útiles ya cumplen el cupo.
    antes = len(internet.llamadas)
    await cosechar(candidatas, ubicaciones, {}, muestra, "prueba@example.org", CORTESIA_MUESTRA,
                   transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)
    assert len(internet.llamadas) == antes


async def test_un_repositorio_chico_se_cosecha_entero_siguiendo_el_token(internet, muestra, monkeypatch):
    monkeypatch.setattr("relevamiento.muestra.ESTRATOS_MUESTRA", (("A", 0, None, None),))
    paginas = {
        None: _pagina([_mets(f"oai:chico.org:{i}", [("ORIGINAL", 100)]) for i in range(3)], token="p2", total=5),
        "p2": _pagina([_mets(f"oai:chico.org:{i}", [("ORIGINAL", 100)]) for i in range(3, 5)], token=None, total=5),
    }

    def chico(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/":
            return httpx.Response(200, text="<html></html>")
        if request.url.path != "/oai/request":
            return None
        verbo = request.url.params.get("verb")
        if verbo == "Identify":
            cuerpo = repos_falsos.m.IDENTIFY_DSPACE.replace("repo6.org", "chico.org")
        elif verbo == "ListMetadataFormats":
            cuerpo = repos_falsos.m.FORMATOS_DSPACE
        else:
            cuerpo = paginas[request.url.params.get("resumptionToken")]
        return httpx.Response(200, headers={"content-type": "text/xml"}, text=cuerpo)

    internet.agregar_host("chico.org", chico)
    candidatas = [Candidata("m0001", 1, "A", "Chico", 5)]
    escribir_candidatos(muestra, candidatas, [("Chico", 5)])
    ubicaciones = {"m0001": {"url_base": "https://chico.org", "host": "chico.org", "namespace": "chico.org"}}
    await cosechar(candidatas, ubicaciones, {}, muestra, "prueba@example.org", CORTESIA_MUESTRA,
                   transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)
    (muestra / UBICACION).mkdir(exist_ok=True)
    resumen = resumir_muestra(muestra)
    assert resumen["para_la_prueba"]["items"] == 5 and resumen["para_la_prueba"]["archivos_con_tamano"] == 5
    pedidas = [ll.url for ll in internet.llamadas_a("chico.org") if "ListRecords" in ll.url or "resumptionToken" in ll.url]
    assert len(pedidas) == 2 and "from=" not in "".join(pedidas)  # sin ventanas de fechas
    assert resumen["ponderado_por_documentos"]["tamano_media"] == 100


async def test_handle_resuelto_y_ruta_oai_extra(internet, muestra, monkeypatch):
    # Un repositorio que solo trae un handle (como los de España) y tiene el OAI en /oai2d (Invenio).
    monkeypatch.setattr("relevamiento.muestra.ESTRATOS_MUESTRA", (("A", 0, None, None),))
    mets = _pagina([_mets("oai:inv.org:1", [("ORIGINAL", 300)])])

    def invenio(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/":
            return httpx.Response(200, text="<html>Invenio</html>")
        if request.url.path != "/oai2d":
            return None
        verbo = request.url.params.get("verb")
        cuerpo = {"Identify": repos_falsos.m.IDENTIFY_DSPACE.replace("repo6.org", "inv.org"),
                  "ListMetadataFormats": repos_falsos.m.formatos("oai_dc", "mets")}.get(verbo, mets)
        return httpx.Response(200, headers={"content-type": "text/xml"}, text=cuerpo)

    def buscador(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/api/v1/search":
            return httpx.Response(200, headers={"content-type": "application/json"},
                                  text=_buscador("Inv", "oai:dnet:inv::1", ["http://hdl.handle.net/999/1"], pais="ES"))
        return None

    def handle(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/api/handles/999/1":
            valor = {"values": [{"type": "URL", "data": {"value": "https://inv.org/record/1"}}]}
            return httpx.Response(200, headers={"content-type": "application/json"}, text=json.dumps(valor))
        return None

    internet.agregar_host("inv.org", invenio)
    internet.agregar_host("buscador.lareferencia.info", buscador)
    internet.agregar_host("hdl.handle.net", handle)
    candidatas = [Candidata("m0001", 1, "A", "Inv", 60_000)]
    escribir_candidatos(muestra, candidatas, [("Inv", 60_000)])
    await ubicar_candidatas(candidatas, muestra, "prueba@example.org", CORTESIA_MUESTRA, transporte=internet.transporte)
    ubicaciones = leer_ubicaciones(muestra)
    assert ubicaciones["m0001"]["url_base"] == "https://inv.org" and ubicaciones["m0001"]["pais"] == "ES"
    await cosechar(candidatas, ubicaciones, {}, muestra, "prueba@example.org", CORTESIA_MUESTRA,
                   transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)
    resumen = resumir_muestra(muestra)
    import csv

    (fila,) = csv.DictReader(open(muestra / "repositorios.csv", encoding="utf-8"))
    assert (fila["estado"], fila["oai_endpoint"], fila["oai_fuente"]) == ("ok", "https://inv.org/oai2d", "sondeo_extra")
    assert resumen["para_la_prueba"]["archivos_con_tamano"] == 1
    assert verificar(muestra / UBICACION, CORTESIA_MUESTRA).ok and verificar(muestra / COSECHA, CORTESIA_MUESTRA).ok
    # Reinterpretar lo guardado da lo mismo, sin pedidos (incluido el handle ya resuelto).
    from relevamiento.muestra import reprocesar_ubicaciones

    antes = len(internet.llamadas)
    assert reprocesar_ubicaciones(candidatas, muestra) == 1
    assert leer_ubicaciones(muestra)["m0001"]["url_base"] == "https://inv.org" and len(internet.llamadas) == antes


def test_items_de_pagina_marcxml_toma_el_856_con_tamano():
    registro = ('<record><header><identifier>oai:ddd.uab.cat:1</identifier></header><metadata>'
                '<record xmlns="http://www.loc.gov/MARC21/slim">'
                '<datafield tag="856" ind1="4" ind2=" "><subfield code="s">123456</subfield>'
                '<subfield code="u">https://ddd.uab.cat/pub/agenda/a2003.pdf</subfield></datafield>'
                '<datafield tag="856" ind1="4" ind2="1"><subfield code="u">https://ddd.uab.cat/record/1</subfield></datafield>'
                "</record></metadata></record>")  # fmt: skip
    (item,) = items_de_pagina(_respuesta(_pagina([registro])), "marcxml").items
    assert [(a.url, a.tamano) for a in item.archivos] == [("https://ddd.uab.cat/pub/agenda/a2003.pdf", 123456)]
    assert item.pagina == "https://ddd.uab.cat/record/1"


def test_una_respuesta_cortada_por_el_servidor_es_un_problema():
    # CONICET corta la respuesta METS después del encabezado, sin error OAI.
    cortada = ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
               "<responseDate>2026-11-10T22:03:34Z</responseDate>")  # fmt: skip
    assert items_de_pagina(_respuesta(cortada), "mets").problema == "sin_listrecords"


async def test_si_la_primera_pagina_viene_rota_prueba_el_formato_siguiente(internet, muestra, monkeypatch):
    monkeypatch.setattr("relevamiento.muestra.ESTRATOS_MUESTRA", (("A", 0, None, None),))
    xoai = repos_falsos.m.oai("<ListRecords>" + "<record><header><identifier>oai:roto.org:1</identifier></header>"
                              "<metadata><metadata/></metadata></record></ListRecords>")

    def roto(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/":
            return httpx.Response(200, text="<html></html>")
        if request.url.path != "/server/oai/request":
            return None
        verbo, prefijo = request.url.params.get("verb"), request.url.params.get("metadataPrefix")
        if verbo == "Identify":
            cuerpo = repos_falsos.m.IDENTIFY_DSPACE.replace("repo6.org", "roto.org")
        elif verbo == "ListMetadataFormats":
            cuerpo = repos_falsos.m.FORMATOS_DSPACE
        elif prefijo == "mets":
            cuerpo = '<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><responseDate>x</responseDate>'
        else:
            cuerpo = xoai
        return httpx.Response(200, headers={"content-type": "text/xml"}, text=cuerpo)

    internet.agregar_host("roto.org", roto)
    candidatas = [Candidata("m0001", 1, "A", "Roto", 60_000)]
    escribir_candidatos(muestra, candidatas, [("Roto", 60_000)])
    ubicaciones = {"m0001": {"url_base": "https://roto.org", "host": "roto.org", "namespace": "roto.org"}}
    conocidos = {"roto.org": "https://roto.org/server/oai/request"}
    await cosechar(candidatas, ubicaciones, conocidos, muestra, "prueba@example.org", CORTESIA_MUESTRA,
                   transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)
    (muestra / UBICACION).mkdir(exist_ok=True)
    resumir_muestra(muestra)
    import csv

    (fila,) = csv.DictReader(open(muestra / "repositorios.csv", encoding="utf-8"))
    assert (fila["estado"], fila["formato"], fila["items"]) == ("ok", "xoai", "1")


async def test_contexto_snrd_cuando_request_no_lista_nada_y_rehacer(internet, muestra, monkeypatch):
    # Como CONICET: el contexto `request` corta METS y no lista nada en xoai; `snrd` sí.
    monkeypatch.setattr("relevamiento.muestra.ESTRATOS_MUESTRA", (("A", 0, None, None),))
    snrd_activo = {"si": False}
    dc = ('<record><header><identifier>oai:ri.x.ar:11336/1</identifier></header><metadata>'
          '<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">'
          '<dc:identifier>http://hdl.handle.net/11336/1</dc:identifier></oai_dc:dc></metadata></record>')  # fmt: skip

    def conicet(request: httpx.Request) -> httpx.Response | None:
        ruta, verbo = request.url.path, request.url.params.get("verb")
        prefijo = request.url.params.get("metadataPrefix")
        if ruta == "/":
            return httpx.Response(200, text="<html></html>")
        if ruta == "/server/oai/request":
            if verbo == "Identify":
                cuerpo = repos_falsos.m.IDENTIFY_DSPACE.replace("repo6.org", "ri.x.ar")
            elif verbo == "ListMetadataFormats":
                cuerpo = repos_falsos.m.FORMATOS_DSPACE
            elif prefijo == "mets":
                cuerpo = '<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><responseDate>x</responseDate>'
            else:
                cuerpo = repos_falsos.m.oai('<error code="noRecordsMatch"/>')
        elif ruta == "/server/oai/snrd" and snrd_activo["si"]:
            cuerpo = repos_falsos.m.formatos("oai_dc") if verbo == "ListMetadataFormats" else _pagina([dc])
        else:
            return None
        return httpx.Response(200, headers={"content-type": "text/xml"}, text=cuerpo)

    internet.agregar_host("ri.x.ar", conicet)
    candidatas = [Candidata("m0001", 1, "A", "CONICET", 157_227)]
    escribir_candidatos(muestra, candidatas, [("CONICET", 157_227)])
    (muestra / UBICACION).mkdir(parents=True, exist_ok=True)
    ubicaciones = {"m0001": {"url_base": "https://ri.x.ar", "host": "ri.x.ar", "namespace": "ri.x.ar"}}
    conocidos = {"ri.x.ar": "https://ri.x.ar/server/oai/request"}
    kw = dict(transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)
    await cosechar(candidatas, ubicaciones, conocidos, muestra, "prueba@example.org", CORTESIA_MUESTRA, **kw)
    import csv

    (fila,) = csv.DictReader(open(muestra / "repositorios.csv", encoding="utf-8")) if resumir_muestra(muestra) else []
    assert fila["estado"] == "sin_items"
    # Sin --rehacer no se vuelve a pedir nada; con --rehacer se cosecha de nuevo y aparece el contexto snrd.
    snrd_activo["si"] = True
    antes = len(internet.llamadas)
    await cosechar(candidatas, ubicaciones, conocidos, muestra, "prueba@example.org", CORTESIA_MUESTRA, **kw)
    assert len(internet.llamadas) == antes
    await cosechar(candidatas, ubicaciones, conocidos, muestra, "prueba@example.org", CORTESIA_MUESTRA,
                   rehacer={"m0001"}, **kw)
    resumen = resumir_muestra(muestra)
    (fila,) = csv.DictReader(open(muestra / "repositorios.csv", encoding="utf-8"))
    assert (fila["estado"], fila["formato"], fila["items"]) == ("ok", "oai_dc", "1")
    assert resumen["para_la_prueba"]["items"] == 1
    assert verificar(muestra / COSECHA, CORTESIA_MUESTRA).ok


def test_endpoints_roar_por_host():
    from relevamiento.extractores import endpoints_roar

    rawlist = b"""<?xml version='1.0'?><eprints xmlns='http://eprints.org/ep2/data/2.0'>
      <eprint><home_page>http://digital.csic.es/</home_page>
        <oai_pmh><item>http://digital.csic.es/dspace-oai/request</item></oai_pmh></eprint>
      <eprint><home_page>https://www.repo.org/</home_page>
        <oai_pmh><item>http://oai.repo.org/request?verb=Identify</item><item>http://dspace.org</item></oai_pmh></eprint>
      <eprint><home_page>http://sin-oai.org/</home_page></eprint></eprints>"""
    por_host = endpoints_roar(rawlist)
    assert por_host["digital.csic.es"] == ["http://digital.csic.es/dspace-oai/request"]
    assert por_host["repo.org"] == por_host["oai.repo.org"] == ["http://oai.repo.org/request"]
    assert "sin-oai.org" not in por_host


async def test_cosecha_con_un_endpoint_de_roar(internet, muestra, monkeypatch):
    # Como el CSIC: el OAI está en /dspace-oai/request, que no se adivina, pero ROAR lo publica.
    monkeypatch.setattr("relevamiento.muestra.ESTRATOS_MUESTRA", (("A", 0, None, None),))

    def csic(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/":
            return httpx.Response(200, text="<html></html>")
        if request.url.path != "/dspace-oai/request":
            return None
        verbo = request.url.params.get("verb")
        cuerpo = {"Identify": repos_falsos.m.IDENTIFY_DSPACE.replace("repo6.org", "digital.csic.es"),
                  "ListMetadataFormats": repos_falsos.m.FORMATOS_DSPACE}.get(verbo, _pagina([_mets("oai:digital.csic.es:1", [("ORIGINAL", 9)])]))
        return httpx.Response(200, headers={"content-type": "text/xml"}, text=cuerpo)

    internet.agregar_host("digital.csic.es", csic)
    candidatas = [Candidata("m0001", 1, "A", "CSIC", 204_213)]
    escribir_candidatos(muestra, candidatas, [("CSIC", 204_213)])
    (muestra / UBICACION).mkdir(parents=True, exist_ok=True)
    ubicaciones = {"m0001": {"url_base": "https://digital.csic.es", "host": "digital.csic.es"}}
    await cosechar(candidatas, ubicaciones, {}, muestra, "prueba@example.org", CORTESIA_MUESTRA,
                   roar={"m0001": ["https://digital.csic.es/dspace-oai/request"]},
                   transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)
    resumir_muestra(muestra)
    import csv

    (fila,) = csv.DictReader(open(muestra / "repositorios.csv", encoding="utf-8"))
    assert (fila["estado"], fila["oai_endpoint"], fila["oai_fuente"]) == ("ok", "https://digital.csic.es/dspace-oai/request", "roar")
    assert verificar(muestra / COSECHA, CORTESIA_MUESTRA).ok


def test_ampliar_suma_sin_tocar_los_anteriores():
    from relevamiento.muestra import ampliar

    antes = elegir(_poblacion())
    despues = ampliar(antes, _poblacion(), {"C": 30, "D": 10})
    assert despues[: len(antes)] == antes
    nuevas = despues[len(antes):]
    assert [c.estrato for c in nuevas].count("C") == 30 and [c.estrato for c in nuevas].count("D") == 10
    assert not {c.repositorio for c in nuevas} & {c.repositorio for c in antes} and not any(c.reserva for c in nuevas)
    assert len({c.id for c in despues}) == len(despues)


async def test_rehacer_fallidos_olvida_el_abandono_e_ignora_robots(internet, muestra, monkeypatch):
    # Un repositorio que el primer día dio 503 (abandono) y cuyo robots.txt prohíbe el OAI.
    monkeypatch.setattr("relevamiento.muestra.ESTRATOS_MUESTRA", (("A", 0, None, None),))
    caido = {"si": True}

    def repo(request: httpx.Request) -> httpx.Response | None:
        if caido["si"]:
            return httpx.Response(503, text="mantenimiento")
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /oai/\n")
        if request.url.path == "/":
            return httpx.Response(200, text="<html></html>")
        if request.url.path != "/oai/request":
            return None
        verbo = request.url.params.get("verb")
        cuerpo = {"Identify": repos_falsos.m.IDENTIFY_DSPACE.replace("repo6.org", "r.org"),
                  "ListMetadataFormats": repos_falsos.m.FORMATOS_DSPACE}.get(verbo, _pagina([_mets("oai:r.org:1", [("ORIGINAL", 5)])]))
        return httpx.Response(200, headers={"content-type": "text/xml"}, text=cuerpo)

    from relevamiento.muestra import fallidos

    internet.agregar_host("r.org", repo)
    candidatas = [Candidata("m0001", 1, "A", "R", 60_000)]
    escribir_candidatos(muestra, candidatas, [("R", 60_000)])
    (muestra / UBICACION).mkdir(parents=True, exist_ok=True)
    ubicaciones = {"m0001": {"url_base": "https://r.org", "host": "r.org", "namespace": "r.org"}}
    kw = dict(transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)
    await cosechar(candidatas, ubicaciones, {}, muestra, "prueba@example.org", CORTESIA_MUESTRA, **kw)
    assert fallidos(muestra) == {"m0001"}
    caido["si"] = False
    await cosechar(candidatas, ubicaciones, {}, muestra, "prueba@example.org", CORTESIA_MUESTRA,
                   rehacer=fallidos(muestra), ignorar_robots=True, **kw)
    resumir_muestra(muestra)
    import csv

    (fila,) = csv.DictReader(open(muestra / "repositorios.csv", encoding="utf-8"))
    assert (fila["estado"], fila["items"]) == ("ok", "1") and fallidos(muestra) == set()
    assert verificar(muestra / COSECHA, CORTESIA_MUESTRA).ok


async def test_ampliar_sigue_el_token_en_cada_ventana(internet, muestra, monkeypatch):
    """Un repositorio grande cosechado por ventanas: `muestra-ampliar` sigue el resumptionToken de cada
    ventana, de a una página por ventana, hasta el tope; un repositorio chico (entero) no se amplía."""
    monkeypatch.setattr("relevamiento.muestra.ESTRATOS_MUESTRA", (("A", 0, None, None),))
    monkeypatch.setattr("relevamiento.muestra.ITEMS_POR_REPO_MUESTRA", 4)

    def items(clave: str, desde: int) -> list[str]:
        return [_mets(f"oai:grande.org:{clave}-{i}", [("ORIGINAL", 100)]) for i in range(desde, desde + 2)]

    def grande(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/":
            return httpx.Response(200, text="<html></html>")
        if request.url.path != "/oai/request":
            return None
        verbo, params = request.url.params.get("verb"), request.url.params
        if verbo == "Identify":
            cuerpo = repos_falsos.m.IDENTIFY_DSPACE.replace("repo6.org", "grande.org")
        elif verbo == "ListMetadataFormats":
            cuerpo = repos_falsos.m.FORMATOS_DSPACE
        elif token := params.get("resumptionToken"):
            # Cada ventana tiene 3 páginas de 2 ítems: el token es `<ventana>|<página>`.
            clave, n = token.rsplit("|", 1)
            siguiente = f"{clave}|{int(n) + 1}" if int(n) < 2 else None
            cuerpo = _pagina(items(clave, 2 * int(n)), token=siguiente, total=50)
        else:
            clave = f"{params.get('from', '')}_{params.get('until', '')}"
            cuerpo = _pagina(items(clave, 0), token=f"{clave}|1", total=50)
        return httpx.Response(200, headers={"content-type": "text/xml"}, text=cuerpo)

    internet.agregar_host("grande.org", grande)
    candidatas = [Candidata("m0001", 1, "A", "Grande", 50)]
    escribir_candidatos(muestra, candidatas, [("Grande", 50)])
    ubicaciones = {"m0001": {"url_base": "https://grande.org", "host": "grande.org", "namespace": "grande.org"}}
    await cosechar(candidatas, ubicaciones, {}, muestra, "prueba@example.org", CORTESIA_MUESTRA,
                   transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)
    (muestra / UBICACION).mkdir(exist_ok=True)
    assert resumir_muestra(muestra)["para_la_prueba"]["items"] == 4  # la cosecha corta en 4
    assert [r["repo"] for r in a_ampliar(muestra, tope=20)] == ["m0001"]

    resultado = await ampliar_items(muestra, "prueba@example.org", CORTESIA_AMPLIACION, tope=20,
                                    transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)
    assert resultado["repositorios"] == 1 and resultado["items_nuevos"] >= 16
    assert verificar(muestra / AMPLIACION, CORTESIA_AMPLIACION).ok
    resumen = resumir_muestra(muestra)
    assert 20 <= resumen["para_la_prueba"]["items"] <= 22
    import csv

    ids = [i["item"] for i in csv.DictReader(open(muestra / "items.csv", encoding="utf-8"))]
    assert len(set(ids)) == len(ids)
    # Repartido entre las ventanas, con páginas de token (no solo las primeras de cada ventana).
    assert len({i.rsplit("-", 1)[0] for i in ids}) >= 5 and any(i.endswith(("-2", "-3")) for i in ids)
    # Al relanzar no se vuelve a pedir nada: ya está ampliado.
    assert a_ampliar(muestra, tope=20) == []
    antes = len(internet.llamadas)
    await ampliar_items(muestra, "prueba@example.org", CORTESIA_AMPLIACION, tope=20,
                        transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar)
    assert len(internet.llamadas) == antes
