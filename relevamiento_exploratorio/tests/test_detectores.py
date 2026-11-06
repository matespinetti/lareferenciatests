import pytest

import muestras as m
from relevamiento import detectores as d
from relevamiento.modelos import Deteccion


@pytest.mark.parametrize(
    "cuerpo, plataforma, version, variante",
    [
        (m.DSPACE6_INICIO, "dspace", "6.3", None),
        (m.DSPACE7_INICIO, "dspace", "7.6.1", "angular"),
        (m.OJS_INICIO, "ojs", "3.3.0.14", None),
        (m.EPRINTS_INICIO, "eprints", "3.4.5", None),
        (m.OJS_INICIO.replace("Open Journal Systems 3.3.0.14", "Open Monograph Press 1.1.1.1"), "omp", "1.1.1.1", None),
    ],
)
def test_detectar_generator(cuerpo, plataforma, version, variante):
    det = d.detectar_generator(m.respuesta(cuerpo))
    assert (det.plataforma, det.version, det.variante, det.fuente) == (plataforma, version, variante, "generator")


@pytest.mark.parametrize(
    "cuerpo, esperado",
    [
        (m.DSPACE6_INICIO, ("dspace", "xmlui")),
        (m.DSPACE7_INICIO, ("dspace", "angular")),
        (m.OJS_SIN_GENERATOR, ("ojs", None)),
        (m.EPRINTS_INICIO, ("eprints", None)),
        (m.SIN_PISTAS, None),
    ],
)
def test_detectar_por_html(cuerpo, esperado):
    det = d.detectar_por_html(m.respuesta(cuerpo))
    assert (det and (det.plataforma, det.variante)) == esperado


def test_sin_generator_no_detecta():
    assert d.detectar_generator(m.respuesta(m.SIN_PISTAS)) is None
    assert d.detectar_generator(m.respuesta(b"")) is None


@pytest.mark.parametrize(
    "cuerpo, plataforma, version, rid",
    [
        (m.IDENTIFY_DSPACE, "dspace", None, "repo6.org"),
        (m.IDENTIFY_OJS, "ojs", "3.3.0.14", "revista.org"),
    ],
)
def test_detectar_por_identify(cuerpo, plataforma, version, rid):
    datos, det = d.detectar_por_identify(m.respuesta(cuerpo, tipo="text/xml"))
    assert datos.identificador_repositorio == rid and datos.version_protocolo == "2.0"
    assert (det.plataforma, det.version) == (plataforma, version)


def test_la_descripcion_eprints_de_oai_no_identifica_la_plataforma():
    _datos, det = d.detectar_por_identify(m.respuesta(m.IDENTIFY_EPRINTS, tipo="text/xml"))
    assert det is None


def test_identify_rechaza_html_con_200():
    assert d.detectar_por_identify(m.respuesta(m.DSPACE7_INICIO)) is None


def test_rest():
    assert d.detectar_por_rest7(m.respuesta(m.REST7_RAIZ, tipo="application/json")).version == "7.6.1"
    assert d.detectar_por_rest56(m.respuesta(m.REST6_STATUS, tipo="application/json")).version == "6.3"
    assert d.detectar_por_rest56(m.respuesta(m.REST5_STATUS, tipo="application/json")).version == "5"
    assert d.detectar_por_rest7(m.respuesta(m.DSPACE7_INICIO)) is None
    assert d.detectar_por_rest56(m.respuesta("<html>no</html>")) is None


def test_rest_base_desde_el_estado_de_angular():
    assert d.extraer_rest_base_dspace7(m.respuesta(m.DSPACE7_INICIO)) == "https://api.repo7.org/server"
    assert d.extraer_rest_base_dspace7(m.respuesta(m.DSPACE6_INICIO)) is None


def test_combinar_prioriza_rest_y_completa_la_version():
    combinada = d.combinar([
        Deteccion("dspace", None, "angular", "html"),
        Deteccion("dspace", "7.6", None, "generator"),
        Deteccion("dspace", "7+", "angular", "rest7"),
    ])
    assert (combinada.plataforma, combinada.version, combinada.variante, combinada.fuente) == (
        "dspace", "7.6", "angular", "rest7+generator")
    assert d.combinar([]) is None


def test_oai():
    assert d.parsear_formatos_oai(m.respuesta(m.FORMATOS_DSPACE)) == ["oai_dc", "xoai", "ore", "mets", "qdc"]
    assert d.parsear_formatos_oai(m.respuesta(m.SIN_PISTAS)) is None
    assert d.error_oai(m.respuesta(m.OAI_ID_INEXISTENTE)) == "idDoesNotExist"
    assert d.es_oai(m.respuesta(m.IDENTIFY_DSPACE)) and not d.es_oai(m.respuesta(m.SIN_PISTAS))


@pytest.mark.parametrize(
    "codigo, encabezados, cuerpo, esperado",
    [
        (403, {"server": "cloudflare", "cf-mitigated": "challenge"}, m.CLOUDFLARE_DESAFIO, "cloudflare"),
        (503, {"server": "cloudflare"}, m.CLOUDFLARE_DESAFIO, "cloudflare"),
        (200, {}, m.ANUBIS, "anubis"),
        (200, {"server": "cloudflare", "cf-ray": "abc"}, m.DSPACE6_INICIO, None),
        (404, {"server": "cloudflare"}, m.SIN_PISTAS, None),
        (200, {"content-type": "application/pdf"}, "anubis /.within.website/", None),
        (200, {}, m.DSPACE6_ITEM + '<form><div class="g-recaptcha"></div></form>' + "x" * 6000, None),
        (429, {}, '<div class="g-recaptcha"></div>', "captcha"),
        (200, {}, m.UFSC_TURNSTILE, "turnstile"),
        (302, {"location": "/challenge?next=%2Fhandle%2F10495%2F46151"}, "", "bunkerweb"),
        (302, {"location": "/challenge"}, "", "bunkerweb"),
        (302, {"location": "/challenge-results/1"}, "", None),
        (200, {}, m.BUNKERWEB, "bunkerweb"),
        (200, {}, m.REQUEST_REJECTED, "request-rejected"),
        (200, {}, "<title>Request Rejected</title><p>Pedido de alta rechazado</p>", None),
        (200, {}, m.DSPACE6_ITEM + '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>'
         + "x" * 30_000, None),
    ],
)
def test_detectar_antibot(codigo, encabezados, cuerpo, esperado):
    cabecera = tuple({"content-type": "text/html", **encabezados}.items())
    assert d.detectar_antibot(codigo, cabecera, cuerpo.encode()) == esperado


def test_detectar_cdn_es_informativo():
    r = m.respuesta(m.DSPACE6_INICIO, encabezados={"server": "cloudflare", "cf-ray": "x"})
    assert d.detectar_cdn(r) == "cloudflare" and d.detectar_antibot(200, r.encabezados, r.cuerpo) is None


@pytest.mark.parametrize(
    "url, tipo",
    [
        ("http://hdl.handle.net/123/2", "handle"),
        ("hdl:123/2", "handle"),
        ("https://doi.org/10.1234/abc", "doi"),
        ("doi:10.1234/abc", "doi"),
        ("10.1234/abc", "doi"),
        ("https://repo6.org/handle/123/2", "pagina"),
        ("https://repo6.org/bitstream/handle/123/2/file.pdf?sequence=1", "archivo"),
        (f"https://repo7.org/bitstreams/{m.BS7}/download", "archivo"),
        ("https://revista.org/index.php/rev/article/download/45/30", "archivo"),
        ("https://revista.org/index.php/rev/article/view/45", "pagina"),
        ("https://eprints.org/123/1/paper.pdf", "archivo"),
        ("https://eprints.org/123/", "pagina"),
        ("https://x.org/docs/informe.PDF", "archivo"),
    ],
)
def test_clasificar_url(url, tipo):
    assert d.clasificar_url(url) == tipo


def test_robots():
    robots = d.parsear_robots(b"User-agent: *\nDisallow: /discover*\nCrawl-delay: 10\n", "pruebamateo")
    assert not robots.permitido("https://a.org/discover?q=1")
    assert robots.permitido("https://a.org/handle/1/2")
    assert robots.crawl_delay == 10


@pytest.mark.parametrize(
    "cuerpo, esperado",
    [
        (m.PDF, "pdf"),
        (b"PK\x03\x04....[Content_Types].xml", "ooxml"),
        (b"PK\x03\x04\x00\x00mimetypeapplication/epub+zip", "epub"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1", "ole"),
        (b"\x89PNG\r\n", "png"),
        (b"\xff\xd8\xff\xe0", "jpeg"),
        (b"\n  <!DOCTYPE html><html>", "html"),
        (b'<?xml version="1.0"?><a/>', "xml"),
        (b"hola mundo", "texto"),
        (b"", None),
    ],
)
def test_magic(cuerpo, esperado):
    assert d.magic(cuerpo) == esperado


def test_es_archivo():
    assert d.es_archivo(m.respuesta(m.PDF, tipo="application/pdf"))
    assert d.es_archivo(m.respuesta(m.PDF, tipo=""))  # sin Content-Type, decide por los primeros bytes
    assert not d.es_archivo(m.respuesta("<html>login</html>", tipo="application/octet-stream"))
    assert not d.es_archivo(m.respuesta(m.DSPACE6_ITEM))
    assert not d.es_archivo(m.respuesta(m.PDF, codigo=404, tipo="application/pdf"))


@pytest.mark.parametrize(
    "disposicion, url, esperado",
    [
        ("attachment; filename=\"tesis final.pdf\"", "https://a/x", "tesis final.pdf"),
        ("attachment; filename*=UTF-8''tesis%C3%B1.pdf", "https://a/x", "tesisñ.pdf"),
        (None, "https://a/bitstream/1/2/file.pdf?sequence=1", "file.pdf"),
        (None, f"https://a/bitstreams/{m.BS7}/download", None),
    ],
)
def test_nombre_archivo(disposicion, url, esperado):
    encabezados = {"content-disposition": disposicion} if disposicion else {}
    assert d.nombre_archivo(m.respuesta(m.PDF, url=url, tipo="application/pdf", encabezados=encabezados)) == esperado
