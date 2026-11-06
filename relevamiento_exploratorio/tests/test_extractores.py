import pytest

import muestras as m
from relevamiento import extractores as e
from relevamiento.modelos import Item, Reconocimiento


def urls(extraccion):
    return [c.url for c in extraccion.candidatos]


def test_directo():
    r = m.respuesta(m.PDF, url="https://a/f.pdf", tipo="application/pdf", encabezados={"content-length": "3010"})
    (c,) = e.extraer_directo(r).candidatos
    assert (c.url, c.nombre, c.mime, c.tamano) == ("https://a/f.pdf", "f.pdf", "application/pdf", 3010)
    assert e.extraer_directo(m.respuesta(m.DSPACE6_ITEM)).candidatos == ()


def test_pagina_dspace6_deduplica_citation_y_enlace_y_saca_el_handle():
    ex = e.extraer_pagina(m.respuesta(m.DSPACE6_ITEM, url="https://repo6.org/handle/123/2"))
    assert urls(ex) == ["https://repo6.org/bitstream/handle/123/2/file.pdf?sequence=1&isAllowed=y"]
    assert ex.candidatos[0].fuente == "citation_pdf_url"
    assert ex.datos["handle"] == "123/2"


def test_pagina_dspace7_saca_el_uuid_de_la_url():
    ex = e.extraer_pagina(m.respuesta(m.DSPACE7_ITEM, url=f"https://repo7.org/items/{m.UUID7}"))
    assert urls(ex) == [f"https://repo7.org/bitstreams/{m.BS7}/download"]
    assert ex.datos["uuid"] == m.UUID7


def test_pagina_ojs_reescribe_galeradas_a_descarga():
    ex = e.extraer_pagina(m.respuesta(m.OJS_ARTICULO, url="https://revista.org/index.php/rev/article/view/45"))
    assert urls(ex) == [
        "https://revista.org/index.php/rev/article/download/45/30",
        "https://revista.org/index.php/rev/article/download/45/31",
    ]
    assert ex.datos["doi"] == "10.5555/rev.45"


def test_pagina_eprints_solo_documentos_del_mismo_host():
    ex = e.extraer_pagina(m.respuesta(m.EPRINTS_RESUMEN, url="https://eprints.org/123/"))
    assert urls(ex) == ["https://eprints.org/123/1/paper.pdf", "https://eprints.org/123/2/datos.csv"]


def test_pagina_angular_sin_ssr():
    ex = e.extraer_pagina(m.respuesta(m.DSPACE7_SIN_SSR, url="https://repo7.org/handle/1/2"))
    assert ex.problema == "angular_sin_ssr" and not ex.candidatos
    # Con SSR, <ds-app> trae el contenido renderizado y la página sí sirve.
    assert e.extraer_pagina(m.respuesta(m.DSPACE7_ITEM, url=f"https://repo7.org/items/{m.UUID7}")).problema is None


def test_pagina_que_no_es_html():
    assert e.extraer_pagina(m.respuesta(m.PDF, tipo="application/pdf")).problema == "no_es_html"


def test_resolucion_registra_destino():
    r = m.respuesta(m.DSPACE6_ITEM, url="https://repo6.org/handle/123/2")
    ex = e.extraer_resolucion(r)
    assert ex.datos["destino"] == "https://repo6.org/handle/123/2" and len(ex.candidatos) == 1


@pytest.mark.parametrize("extractor, cuerpo, fuente", [
    (e.extraer_oai_xoai, m.getrecord_xoai("https://repo6.org", "123/2"), "xoai"),
    (e.extraer_oai_ore, m.getrecord_ore("https://repo6.org", "123/2"), "ore"),
    (e.extraer_oai_mets, m.getrecord_mets("https://repo6.org", "123/2"), "mets"),
])
def test_oai_indica_el_bundle_de_cada_archivo(extractor, cuerpo, fuente):
    ex = extractor(m.respuesta(cuerpo, tipo="text/xml"))
    original = ex.candidatos[0]
    assert original.url == "https://repo6.org/bitstream/123/2/1/file.pdf"
    assert (original.bundle, original.nombre, original.fuente) == ("ORIGINAL", "file.pdf", fuente)
    assert "THUMBNAIL" in {c.bundle for c in ex.candidatos}


def test_xoai_trae_tamano_mime_y_handle():
    ex = e.extraer_oai_xoai(m.respuesta(m.getrecord_xoai("https://repo6.org", "123/2")))
    assert [c.bundle for c in ex.candidatos] == ["ORIGINAL", "THUMBNAIL", "LICENSE"]
    assert (ex.candidatos[0].mime, ex.candidatos[0].tamano) == ("application/pdf", 3000)
    assert ex.datos == {"handle": "123/2"}


@pytest.mark.parametrize("cuerpo, problema", [
    (m.OAI_ID_INEXISTENTE, "idDoesNotExist"),
    (m.oai('<GetRecord><record><header status="deleted"><identifier>x</identifier></header></record></GetRecord>'),
     "registro_borrado"),
    (m.SIN_PISTAS, "sin_registro"),
    ("", "xml_invalido"),
])
def test_oai_problemas(cuerpo, problema):
    assert e.extraer_oai_xoai(m.respuesta(cuerpo)).problema == problema


def test_muestrear_listrecords():
    r = m.respuesta(m.LISTRECORDS_DSPACE6, tipo="text/xml")
    items = e.muestrear_listrecords(r, "r6", "https://repo6.org/", 3)
    assert [(i.idx, i.url, i.oai_id) for i in items] == [
        (0, "https://repo6.org/handle/123/2", "oai:repo6.org:123/2"),
        (1, "https://repo6.org/handle/123/3", "oai:repo6.org:123/3"),
        (2, "https://doi.org/10.1234/abc", "oai:repo6.org:123/4"),
    ]


def test_muestrear_listrecords_truncado_no_falla():
    r = m.respuesta(m.LISTRECORDS_DSPACE6[:1500], tipo="text/xml")
    assert len(e.muestrear_listrecords(r, "r6", "https://repo6.org/", 3)) <= 3


def test_rest56():
    ex = e.extraer_rest56(m.respuesta(m.rest6_handle("123/2"), url="https://repo6.org/rest/handle/123/2?expand=bitstreams"))
    assert urls(ex) == ["https://repo6.org/rest/bitstreams/b-1/retrieve", "https://repo6.org/rest/bitstreams/b-3/retrieve"]
    assert [c.bundle for c in ex.candidatos] == ["ORIGINAL", "THUMBNAIL"]


def test_rest5_enlace_relativo_a_la_base():
    cuerpo = '{"bitstreams": [{"name": "a.pdf", "bundleName": "ORIGINAL", "retrieveLink": "/bitstreams/77/retrieve"}]}'
    ex = e.extraer_rest56(m.respuesta(cuerpo, url="https://r5.org/rest/handle/1/2?expand=bitstreams"))
    assert urls(ex) == ["https://r5.org/rest/bitstreams/77/retrieve"]


def test_rest7():
    item = e.extraer_rest7_item(m.respuesta(m.REST7_ITEM, tipo="application/json"))
    assert item.datos["uuid"] == m.UUID7
    assert item.siguiente == ((f"https://api.repo7.org/server/api/core/items/{m.UUID7}/bundles?embed=bitstreams", "bundles"),)

    con_embed = e.extraer_rest7_bundles(m.respuesta(m.REST7_BUNDLES_EMBED, tipo="application/json"))
    assert [(c.bundle, c.nombre) for c in con_embed.candidatos] == [("ORIGINAL", "tesis.pdf"), ("THUMBNAIL", "tesis.pdf.jpg")]
    assert con_embed.siguiente == ()

    sin_embed = e.extraer_rest7_bundles(m.respuesta(m.REST7_BUNDLES_SIN_EMBED, tipo="application/json"))
    assert sin_embed.candidatos == ()
    assert sin_embed.siguiente == (("https://api.repo7.org/server/api/core/bundles/b-o/bitstreams", "ORIGINAL"),)

    bitstreams = e.extraer_rest7_bitstreams(m.respuesta(m.REST7_BITSTREAMS), "ORIGINAL")
    assert urls(bitstreams) == [f"https://api.repo7.org/server/api/core/bitstreams/{m.BS7}/content"]


@pytest.mark.parametrize("valor, handle, doi, pedible", [
    ("http://hdl.handle.net/123/2", "123/2", None, "http://hdl.handle.net/123/2"),
    ("hdl:10915/1234", "10915/1234", None, "https://hdl.handle.net/10915/1234"),
    ("https://repo/handle/123/45?locale=es", "123/45", None, "https://repo/handle/123/45?locale=es"),
    ("doi:10.1234/abc.1", None, "10.1234/abc.1", "https://doi.org/10.1234/abc.1"),
    ("Cita sin URL", None, None, None),
])
def test_identificadores(valor, handle, doi, pedible):
    assert e.extraer_handle(valor) == handle
    assert e.extraer_doi(valor) == doi
    assert e.url_pedible(valor) == pedible


def test_derivar_oai_id():
    recon = Reconocimiento(plataforma="dspace", identificador_oai="repo6.org")
    item = Item("r", 0, "https://repo6.org/handle/123/2")
    assert e.derivar_oai_id(recon, item, {"handle": "123/2"}) == "oai:repo6.org:123/2"
    assert e.derivar_oai_id(recon, Item("r", 0, "x", oai_id="oai:dado"), {}) == "oai:dado"
    ojs = Reconocimiento(plataforma="ojs", identificador_oai="revista.org")
    assert e.derivar_oai_id(ojs, Item("r", 0, "https://revista.org/index.php/rev/article/view/45"), {}) == "oai:revista.org:article/45"
    eprints = Reconocimiento(plataforma="eprints", identificador_oai="eprints.org")
    assert e.derivar_oai_id(eprints, Item("r", 0, "https://eprints.org/123/"), {}) == "oai:eprints.org:123"
    assert e.derivar_oai_id(Reconocimiento(plataforma="dspace"), item, {"handle": "1/2"}) is None


def test_metodos_aplicables():
    recon = Reconocimiento(plataforma="dspace", version="6.3", oai_endpoint="https://repo6.org/oai/request",
                           oai_formatos=["oai_dc", "xoai", "ore"], rest="v5_6")
    motivos = e.metodos_aplicables(recon, "pagina", False, {"handle": "123/2"}, "oai:x:123/2")
    aplican = sorted(k for k, v in motivos.items() if v is None)
    assert aplican == ["oai_ore", "oai_xoai", "pagina", "rest56"]
    assert motivos["oai_mets"] == "el repositorio no ofrece mets"

    resolver = e.metodos_aplicables(recon, "doi", False, {}, None)
    assert resolver["doi_handle"] is None and resolver["directo"] and resolver["pagina"]
    assert resolver["oai_xoai"] == "no se pudo derivar el identificador OAI"
    assert resolver["rest56"] == "sin handle del ítem"
