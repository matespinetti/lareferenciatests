"""Cinco repositorios simulados sobre el internet falso: DSpace 6, DSpace 7 con la API en otro host, OJS,
uno detrás de Anubis y un EPrints con certificado inválido."""

import httpx

import muestras as m
from relevamiento.modelos import Item, Repositorio


def _html(cuerpo: str, codigo: int = 200) -> httpx.Response:
    return httpx.Response(codigo, headers={"content-type": "text/html; charset=utf-8"}, text=cuerpo)


def _xml(cuerpo: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/xml; charset=utf-8"}, text=cuerpo)


def _json(cuerpo: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "application/json"}, text=cuerpo)


def _pdf(nombre: str = "file.pdf") -> httpx.Response:
    return httpx.Response(206, headers={
        "content-type": "application/pdf", "content-range": f"bytes 0-2047/{len(m.PDF)}",
        "content-disposition": f'attachment; filename="{nombre}"'}, content=m.PDF[:2048])


def _oai(request: httpx.Request, identify: str, formatos: str, listrecords: str, getrecord) -> httpx.Response | None:
    verbo = request.url.params.get("verb")
    if verbo == "Identify":
        return _xml(identify)
    if verbo == "ListMetadataFormats":
        return _xml(formatos)
    if verbo == "ListRecords":
        return _xml(listrecords)
    if verbo == "GetRecord":
        return _xml(getrecord(request.url.params["metadataPrefix"], request.url.params["identifier"]))
    return None


def dspace6(request: httpx.Request) -> httpx.Response | None:
    ruta, base = request.url.path, "https://repo6.org"
    if ruta == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nDisallow: /discover\n")
    if ruta == "/":
        return _html(m.DSPACE6_INICIO)
    if ruta == "/oai/request":
        def getrecord(formato: str, identificador: str) -> str:
            handle = identificador.rsplit(":", 1)[-1]
            return {"xoai": m.getrecord_xoai, "ore": m.getrecord_ore, "mets": m.getrecord_mets}[formato](base, handle)
        if request.url.params.get("verb") == "ListRecords" and "from" in request.url.params:
            return _xml(m.oai('<error code="noRecordsMatch">No matches for the query</error>'))
        return _oai(request, m.IDENTIFY_DSPACE, m.FORMATOS_DSPACE, m.LISTRECORDS_DSPACE6, getrecord)
    if ruta.startswith("/handle/"):
        return _html(m.DSPACE6_ITEM.replace("123/2", ruta.removeprefix("/handle/")))
    if ruta.startswith(("/bitstream/", "/rest/bitstreams/")):
        return _pdf()
    if ruta == "/rest/status":
        return _json(m.REST6_STATUS)
    if ruta.startswith("/rest/handle/"):
        return _json(m.rest6_handle(ruta.removeprefix("/rest/handle/")))
    return None


def doi(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/10.1234/abc":
        return httpx.Response(302, headers={"location": "https://repo6.org/handle/123/4"})
    return None


def dspace7_ui(request: httpx.Request) -> httpx.Response | None:
    ruta = request.url.path
    if ruta == "/":
        return _html(m.DSPACE7_INICIO)
    if ruta == f"/items/{m.UUID7}":
        return _html(m.DSPACE7_ITEM)
    if ruta.startswith("/bitstreams/"):
        return _pdf("tesis.pdf")
    return None


def dspace7_api(request: httpx.Request) -> httpx.Response | None:
    ruta = request.url.path
    if ruta == "/server/oai/request":
        return _oai(request, m.IDENTIFY_DSPACE7, m.formatos("oai_dc", "xoai"), m.oai("<ListRecords/>"),
                    lambda _f, _i: m.OAI_ID_INEXISTENTE)
    if ruta == "/server/api":
        return _json(m.REST7_RAIZ)
    if ruta == f"/server/api/core/items/{m.UUID7}/bundles" and request.url.params.get("embed") == "bitstreams":
        return _json(m.REST7_BUNDLES_EMBED)
    if ruta.startswith("/server/api/core/bitstreams/"):
        return _pdf("tesis.pdf")
    return None


def ojs(request: httpx.Request) -> httpx.Response | None:
    ruta = request.url.path
    if ruta == "/index.php/rev":
        return _html(m.OJS_INICIO)
    if ruta == "/index.php/rev/oai":
        registros = m.oai("<ListRecords>" + "".join(
            f'<record><header><identifier>oai:revista.org:article/{n}</identifier></header><metadata>'
            f'<oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:identifier>https://revista.org/index.php/rev/article/view/{n}</dc:identifier></oai_dc:dc></metadata></record>'
            for n in (45, 46)) + "</ListRecords>")
        return _oai(request, m.IDENTIFY_OJS, m.FORMATOS_OJS, registros, lambda _f, _i: m.OAI_ID_INEXISTENTE)
    if ruta.startswith("/index.php/rev/article/view/"):
        return _html(m.OJS_ARTICULO.replace("/45/", f"/{ruta.split('/')[-1]}/").replace("view/45", f"view/{ruta.split('/')[-1]}"))
    if ruta.endswith("/30"):
        return _pdf("articulo.pdf")
    if ruta.startswith("/index.php/rev/article/download/"):
        return _html("<html><body>Galerada HTML</body></html>")
    return None


def anubis(_request: httpx.Request) -> httpx.Response:
    return _html(m.ANUBIS)


def eprints(request: httpx.Request) -> httpx.Response | None:
    ruta = request.url.path
    if ruta == "/":
        return _html(m.EPRINTS_INICIO)
    if ruta == "/cgi/oai2":
        return _oai(request, m.IDENTIFY_EPRINTS, m.formatos("oai_dc"), m.oai("<ListRecords/>"), lambda _f, _i: "")
    if ruta == "/123/":
        return _html(m.EPRINTS_RESUMEN.replace("eprints.org", "tls.org"))
    if ruta.startswith("/123/"):
        return _pdf("paper.pdf")
    return None


REPOS = [
    Repositorio("r6", "Repositorio DSpace 6", "https://repo6.org/"),
    Repositorio("r7", "Repositorio DSpace 7", "https://repo7.org/"),
    Repositorio("ojs", "Revista OJS", "https://revista.org/index.php/rev"),
    Repositorio("bot", "Detrás de Anubis", "https://bot.org/"),
    Repositorio("tls", "EPrints con TLS inválido", "https://tls.org/", oai_endpoint="https://tls.org/cgi/oai2"),
]
ITEMS = {
    "r7": [Item("r7", 0, f"https://repo7.org/items/{m.UUID7}")],
    "tls": [Item("tls", 0, "https://tls.org/123/")],
}


def armar(internet) -> tuple[list[Repositorio], dict[str, list[Item]]]:
    for host, manejador in [
        ("repo6.org", dspace6), ("doi.org", doi), ("repo7.org", dspace7_ui), ("api.repo7.org", dspace7_api),
        ("revista.org", ojs), ("bot.org", anubis), ("tls.org", eprints),
    ]:
        internet.agregar_host(host, manejador)
    internet.tls_invalido.add("tls.org")
    return REPOS, ITEMS
