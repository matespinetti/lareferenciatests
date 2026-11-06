"""Respuestas sintéticas escritas a mano. Reemplazarlas o complementarlas con capturas reales del piloto."""

import json

UUID7 = "1b2c3d4e-0000-4000-8000-000000000007"
BS7 = "9f8e7d6c-0000-4000-8000-0000000000b7"

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n" + b"0" * 3000

# --- Páginas de inicio -----------------------------------------------------------------

DSPACE6_INICIO = """<!DOCTYPE html><html><head><meta name="Generator" content="DSpace 6.3">
<link rel="stylesheet" href="/xmlui/themes/Mirage2/styles/main.css"></head>
<body><h1>Repositorio 6</h1></body></html>"""

DSPACE7_INICIO = """<!DOCTYPE html><html><head><meta name="Generator" content="DSpace 7.6.1"></head>
<body><ds-app></ds-app>
<script id="dspace-angular-state" type="application/json">{&q;APP_CONFIG_STATE&q;:{&q;rest&q;:{&q;ssl&q;:true,&q;host&q;:&q;api.repo7.org&q;,&q;port&q;:443,&q;nameSpace&q;:&q;/server&q;,&q;baseUrl&q;:&q;https://api.repo7.org/server&q;}}}</script>
</body></html>"""

OJS_INICIO = """<!DOCTYPE html><html><head><meta name="generator" content="Open Journal Systems 3.3.0.14"></head>
<body><div class="pkp_structure_page">Revista</div></body></html>"""

OJS_SIN_GENERATOR = """<html><head><title>Revista</title></head>
<body><div class="pkp_structure_page"><script src="/lib/pkp/js/lib/jquery.js"></script></div></body></html>"""

EPRINTS_INICIO = """<html><head><meta name="generator" content="EPrints 3.4.5"></head>
<body><div class="ep_tm_page_content">EPrints</div></body></html>"""

SIN_PISTAS = "<html><head><title>Hola</title></head><body>Nada por acá</body></html>"

CLOUDFLARE_DESAFIO = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script></body></html>"""

ANUBIS = """<!doctype html><html><head><title>Making sure you&#39;re not a bot!</title></head>
<body><script id="anubis_challenge" type="application/json">{}</script>
<script src="/.within.website/x/cmd/anubis/static/js/main.mjs"></script></body></html>"""

# Página de validación propia con Cloudflare Turnstile, servida con 200 en cualquier ruta (capturada en el piloto, UFSC).
# Recortes de capturas reales de piloto3 (co-udea, cr-una, ar-filo-uba).
BUNKERWEB = """<!doctype html><html lang="en"><head><title>Bot Detection</title></head><body>
<div>BUNKERWEB &middot; SECURITY CHECK</div><h1>PROOF-OF-WORK</h1><p>Computing proof of work</p>
<p>Your browser is solving a small puzzle. No action needed, you will be redirected.</p>
<noscript>JavaScript needs to be enabled to complete this challenge.</noscript></body></html>"""

REQUEST_REJECTED = """<html><head><title>Request Rejected</title></head><body><br><img src='data:image/png;base64,AAAA'/>
<br><br>La solicitud URL fue rechazada. Por favor contacte con el administrador del sistema.<br><br>
Su ID para recibir asistencia es: <b>BOT-CGT-<16452133683496980332></b><br><br>
<a href='javascript:history.back();'>Regresar</a></body></html>"""

DSPACE7_SIN_SSR = """<!DOCTYPE html>
<html data-critters-container>
<head><meta charset="UTF-8"><base href="/"><title>DSpace</title></head>
<body>
  <ds-app></ds-app>
<script src="runtime.f91636efb6c29309.js" type="module"></script><script src="main.03b32622eefeb6ac.js" type="module"></script></body>
</html>"""

UFSC_TURNSTILE = """<html><head><script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
</head><body><h3>Sistema de Prevenção de Ataques da RedeUFSC</h3><form action="" method="post">
<div class="cf-turnstile" data-sitekey="x"></div><input type="submit" value="Acessar o site protegido" name="valida_">
</form></body></html>"""

# --- OAI-PMH ---------------------------------------------------------------------------

_OAI = '<?xml version="1.0" encoding="UTF-8"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><responseDate>2026-10-28T22:00:00Z</responseDate>{}</OAI-PMH>'


def oai(cuerpo: str) -> str:
    return _OAI.format(cuerpo)


def _identify(nombre: str, rid: str, descripcion_extra: str) -> str:
    return oai(f"""<request verb="Identify">https://x/oai</request><Identify>
<repositoryName>{nombre}</repositoryName><baseURL>https://{rid}/oai/request</baseURL><protocolVersion>2.0</protocolVersion>
<earliestDatestamp>2010-01-01T00:00:00Z</earliestDatestamp>
<description><oai-identifier xmlns="http://www.openarchives.org/OAI/2.0/oai-identifier"><scheme>oai</scheme>
<repositoryIdentifier>{rid}</repositoryIdentifier><delimiter>:</delimiter></oai-identifier></description>
{descripcion_extra}</Identify>""")


IDENTIFY_DSPACE = _identify("Repo 6", "repo6.org", "<description><XOAIDescription>XOAI: OAI-PMH Java Toolkit</XOAIDescription></description>")
IDENTIFY_DSPACE7 = _identify("Repo 7", "repo7.org", "<description><XOAIDescription>XOAI: OAI-PMH Java Toolkit</XOAIDescription></description>")
IDENTIFY_OJS = _identify("Revista", "revista.org", """<description><toolkit xmlns="http://oai.dlib.vt.edu/OAI/metadata/toolkit">
<title>Open Journal Systems</title><version>3.3.0.14</version></toolkit></description>""")
IDENTIFY_EPRINTS = _identify("EPrints", "eprints.org", '<description><eprints xmlns="http://www.openarchives.org/OAI/1.1/eprints"><content><text>x</text></content></eprints></description>')


def formatos(*prefijos: str) -> str:
    return oai("<ListMetadataFormats>" + "".join(
        f"<metadataFormat><metadataPrefix>{p}</metadataPrefix><schema>x</schema><metadataNamespace>y</metadataNamespace></metadataFormat>"
        for p in prefijos) + "</ListMetadataFormats>")


FORMATOS_DSPACE = formatos("oai_dc", "xoai", "ore", "mets", "qdc")
FORMATOS_OJS = formatos("oai_dc", "oai_marc", "marcxml", "rfc1807")


def _registro(oai_id: str, *identificadores: str, borrado: bool = False) -> str:
    if borrado:
        return f'<record><header status="deleted"><identifier>{oai_id}</identifier></header></record>'
    dc = "".join(f"<dc:identifier>{i}</dc:identifier>" for i in identificadores)
    return f"""<record><header><identifier>{oai_id}</identifier><datestamp>2020-01-01</datestamp></header>
<metadata><oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>T</dc:title>{dc}</oai_dc:dc></metadata></record>"""


LISTRECORDS_DSPACE6 = oai("<ListRecords>" + "".join([
    _registro("oai:repo6.org:123/1", borrado=True),
    _registro("oai:repo6.org:123/2", "http://hdl.handle.net/123/2", "https://repo6.org/handle/123/2"),
    _registro("oai:repo6.org:123/3", "Cita sin URL", "https://repo6.org/handle/123/3"),
    _registro("oai:repo6.org:123/4", "doi:10.1234/abc"),
    _registro("oai:repo6.org:123/5", "Sin identificadores útiles"),
    _registro("oai:repo6.org:123/6", "https://repo6.org/handle/123/6"),
]) + '<resumptionToken cursor="0">tok</resumptionToken></ListRecords>')


def getrecord_xoai(base: str, handle: str) -> str:
    def bitstream(nombre: str, formato: str, tam: int, seq: int) -> str:
        return f"""<element name="bitstream"><field name="name">{nombre}</field><field name="format">{formato}</field>
<field name="size">{tam}</field><field name="url">{base}/bitstream/{handle}/{seq}/{nombre}</field></element>"""

    return oai(f"""<GetRecord><record><header><identifier>oai:x:{handle}</identifier></header><metadata>
<metadata xmlns="http://www.lyncode.com/xoai"><element name="dc"/>
<element name="bundles">
 <element name="bundle"><field name="name">ORIGINAL</field><element name="bitstreams">{bitstream("file.pdf", "application/pdf", 3000, 1)}</element></element>
 <element name="bundle"><field name="name">THUMBNAIL</field><element name="bitstreams">{bitstream("file.pdf.jpg", "image/jpeg", 900, 3)}</element></element>
 <element name="bundle"><field name="name">LICENSE</field><element name="bitstreams">{bitstream("license.txt", "text/plain", 1700, 2)}</element></element>
</element>
<element name="others"><field name="handle">{handle}</field></element>
</metadata></metadata></record></GetRecord>""")


def getrecord_ore(base: str, handle: str) -> str:
    url = f"{base}/bitstream/{handle}/1/file.pdf"
    return oai(f"""<GetRecord><record><header><identifier>oai:x:{handle}</identifier></header><metadata>
<atom:entry xmlns:atom="http://www.w3.org/2005/Atom" xmlns:oreatom="http://www.openarchives.org/ore/atom/"
 xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns:dcterms="http://purl.org/dc/terms/">
<atom:id>{base}/handle/{handle}</atom:id>
<atom:link rel="http://www.openarchives.org/ore/terms/aggregates" href="{url}" title="file.pdf" type="application/pdf" length="3000"/>
<atom:link rel="http://www.openarchives.org/ore/terms/aggregates" href="{url}.jpg" title="file.pdf.jpg" type="image/jpeg" length="900"/>
<oreatom:triples>
 <rdf:Description rdf:about="{url}"><dcterms:description>ORIGINAL</dcterms:description></rdf:Description>
 <rdf:Description rdf:about="{url}.jpg"><dcterms:description>THUMBNAIL</dcterms:description></rdf:Description>
</oreatom:triples></atom:entry></metadata></record></GetRecord>""")


def getrecord_mets(base: str, handle: str) -> str:
    return oai(f"""<GetRecord><record><header><identifier>oai:x:{handle}</identifier></header><metadata>
<mets xmlns="http://www.loc.gov/METS/" xmlns:xlink="http://www.w3.org/1999/xlink"><fileSec>
<fileGrp USE="ORIGINAL"><file ID="f1" MIMETYPE="application/pdf" SIZE="3000">
 <FLocat LOCTYPE="URL" xlink:href="{base}/bitstream/{handle}/1/file.pdf" xlink:title="file.pdf"/></file></fileGrp>
<fileGrp USE="THUMBNAIL"><file ID="f3" MIMETYPE="image/jpeg" SIZE="900">
 <FLocat LOCTYPE="URL" xlink:href="{base}/bitstream/{handle}/3/file.pdf.jpg" xlink:title="file.pdf.jpg"/></file></fileGrp>
</fileSec></mets></metadata></record></GetRecord>""")


OAI_ID_INEXISTENTE = oai('<request verb="GetRecord">x</request><error code="idDoesNotExist">No matching identifier</error>')

# --- REST de DSpace --------------------------------------------------------------------

REST6_STATUS = json.dumps({"okay": True, "authenticated": False, "email": None, "fullname": None,
                           "sourceVersion": "6.3", "apiVersion": "6"})
REST5_STATUS = json.dumps({"okay": True, "authenticated": False, "email": None, "fullname": None, "token": None})


def rest6_handle(handle: str) -> str:
    return json.dumps({"uuid": "u-1", "handle": handle, "bitstreams": [
        {"name": "file.pdf", "bundleName": "ORIGINAL", "mimeType": "application/pdf", "sizeBytes": 3000,
         "retrieveLink": "/rest/bitstreams/b-1/retrieve"},
        {"name": "file.pdf.jpg", "bundleName": "THUMBNAIL", "mimeType": "image/jpeg", "sizeBytes": 900,
         "retrieveLink": "/rest/bitstreams/b-3/retrieve"},
    ]})


REST7_RAIZ = json.dumps({"dspaceUI": "https://repo7.org", "dspaceServer": "https://api.repo7.org/server",
                         "dspaceVersion": "DSpace 7.6.1", "type": "root", "_links": {"self": {"href": "x"}}})
REST7_ITEM = json.dumps({"id": UUID7, "uuid": UUID7, "handle": "123/7", "_links": {
    "bundles": {"href": f"https://api.repo7.org/server/api/core/items/{UUID7}/bundles"}}})


def _bitstream7(nombre: str, uuid: str) -> dict:
    return {"name": nombre, "sizeBytes": 5000,
            "_links": {"content": {"href": f"https://api.repo7.org/server/api/core/bitstreams/{uuid}/content"}}}


REST7_BUNDLES_EMBED = json.dumps({"_embedded": {"bundles": [
    {"name": "ORIGINAL", "_embedded": {"bitstreams": {"_embedded": {"bitstreams": [_bitstream7("tesis.pdf", BS7)]}}}},
    {"name": "THUMBNAIL", "_embedded": {"bitstreams": {"_embedded": {"bitstreams": [_bitstream7("tesis.pdf.jpg", "t")]}}}},
]}})
REST7_BUNDLES_SIN_EMBED = json.dumps({"_embedded": {"bundles": [
    {"name": "ORIGINAL", "_links": {"bitstreams": {"href": "https://api.repo7.org/server/api/core/bundles/b-o/bitstreams"}}},
    {"name": "THUMBNAIL", "_links": {"bitstreams": {"href": "https://api.repo7.org/server/api/core/bundles/b-t/bitstreams"}}},
]}})
REST7_BITSTREAMS = json.dumps({"_embedded": {"bitstreams": [_bitstream7("tesis.pdf", BS7)]}})

# --- Páginas de ítem -------------------------------------------------------------------

DSPACE6_ITEM = """<html><head><meta name="Generator" content="DSpace 6.3">
<meta name="DC.identifier" content="http://hdl.handle.net/123/2">
<meta name="citation_pdf_url" content="https://repo6.org/bitstream/handle/123/2/file.pdf?sequence=1&amp;isAllowed=y">
</head><body>
<a href="/bitstream/handle/123/2/file.pdf?sequence=1&amp;isAllowed=y"><img src="/bitstream/handle/123/2/file.pdf.jpg?sequence=3"></a>
<a href="/handle/123/1">Colección</a></body></html>"""

DSPACE7_ITEM = f"""<html><head><meta name="citation_pdf_url" content="https://repo7.org/bitstreams/{BS7}/download">
</head><body><ds-app><a href="/bitstreams/{BS7}/download">tesis.pdf</a></ds-app></body></html>"""

OJS_ARTICULO = """<html><head><meta name="citation_pdf_url" content="https://revista.org/index.php/rev/article/download/45/30">
<meta name="citation_doi" content="10.5555/rev.45"></head><body><div class="pkp_structure_page">
<a class="obj_galley_link pdf" href="https://revista.org/index.php/rev/article/view/45/30">PDF</a>
<a class="obj_galley_link" href="https://revista.org/index.php/rev/article/view/45/31">HTML</a>
</div></body></html>"""

EPRINTS_RESUMEN = """<html><head><meta name="eprints.document_url" content="https://eprints.org/123/1/paper.pdf"></head>
<body><a class="ep_document_link" href="https://eprints.org/123/1/paper.pdf">paper.pdf</a>
<a href="https://eprints.org/123/2/datos.csv">datos</a><a href="https://otro.org/9/1/x.pdf">externo</a></body></html>"""


def respuesta(cuerpo, url: str = "https://repo.org/", codigo: int = 200, tipo: str = "text/html; charset=utf-8",
              encabezados: dict[str, str] | None = None):
    from relevamiento.modelos import Respuesta

    datos = cuerpo.encode() if isinstance(cuerpo, str) else cuerpo
    cabecera = tuple({"content-type": tipo, **(encabezados or {})}.items())
    return Respuesta(url_inicial=url, url=url, codigo=codigo, encabezados=cabecera, cuerpo=datos)
