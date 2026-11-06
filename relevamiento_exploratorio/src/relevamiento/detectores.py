"""Detectores puros sobre respuestas ya obtenidas: plataforma, versión, anti-bot, tipos de URL y archivos."""

import re
from dataclasses import dataclass
from typing import Literal, Sequence
from urllib.parse import unquote, urlsplit

from protego import Protego

from relevamiento.modelos import DatosIdentify, Deteccion, Respuesta, parece_archivo
from relevamiento.parseo import descendientes, hijos, parsear_html, parsear_json, parsear_xml, texto_hijo

# --- Plataforma y versión ---------------------------------------------------------------

_GENERADORES = [
    (re.compile(r"dspace(?:[\s-]*cris)?\s*v?([\d][\w.-]*)?", re.I), "dspace"),
    (re.compile(r"open journal systems\s*([\d][\w.-]*)?", re.I), "ojs"),
    (re.compile(r"open monograph press\s*([\d][\w.-]*)?", re.I), "omp"),
    (re.compile(r"eprints\s*([\d][\w.-]*)?", re.I), "eprints"),
]


def _plataforma_de_texto(texto: str) -> tuple[str, str | None] | None:
    for patron, plataforma in _GENERADORES:
        m = patron.search(texto)
        if m:
            return plataforma, m.group(1)
    return None


def detectar_generator(r: Respuesta) -> Deteccion | None:
    doc = parsear_html(r.cuerpo)
    if doc is None:
        return None
    for meta in doc.iter("meta"):
        if (meta.get("name") or "").lower() != "generator":
            continue
        contenido = meta.get("content") or ""
        hallado = _plataforma_de_texto(contenido)
        if hallado:
            plataforma, version = hallado
            variante = "angular" if plataforma == "dspace" and _es_dspace_angular(r.cuerpo) else None
            return Deteccion(plataforma, version, variante, "generator", contenido)
    return None


def _es_dspace_angular(cuerpo: bytes) -> bool:
    return b"<ds-app" in cuerpo or b"<ds-root" in cuerpo or b"dspace-angular" in cuerpo


_CASCARA_ANGULAR = re.compile(rb"<ds-app>\s*</ds-app>")


def es_angular_sin_ssr(cuerpo: bytes) -> bool:
    """DSpace 7+ que sirve el index.html vacío: el contenido lo arma el JS en el navegador (caso filo-uba)."""
    return len(cuerpo) < 20_000 and _CASCARA_ANGULAR.search(cuerpo) is not None


def detectar_por_html(r: Respuesta) -> Deteccion | None:
    """Heurísticas de marcado cuando no hay meta generator."""
    c = r.cuerpo
    if _es_dspace_angular(c):
        return Deteccion("dspace", None, "angular", "html")
    if b"/xmlui/" in c or (b"xmlui" in c.lower() and b"/themes/" in c):
        return Deteccion("dspace", None, "xmlui", "html")
    if b"/jspui/" in c or b"dspace-jspui" in c:
        return Deteccion("dspace", None, "jspui", "html")
    if b"pkp_structure" in c or b"pkp_page" in c or b"/lib/pkp/" in c:
        return Deteccion("ojs", None, None, "html")
    if b"ep_tm_" in c or b'name="eprints.' in c or (b"/cgi/search" in c and b"eprints" in c.lower()):
        return Deteccion("eprints", None, None, "html")
    return None


def detectar_por_identify(r: Respuesta) -> tuple[DatosIdentify, Deteccion | None] | None:
    raiz = parsear_xml(r.cuerpo)
    if raiz is None or not isinstance(raiz.tag, str):
        return None
    identify = descendientes(raiz, "Identify")
    if not identify:
        return None
    nodo = identify[0]
    rid = None
    for oai_id in descendientes(nodo, "oai-identifier"):
        rid = texto_hijo(oai_id, "repositoryIdentifier")
    datos = DatosIdentify(
        nombre=texto_hijo(nodo, "repositoryName"),
        base_url=texto_hijo(nodo, "baseURL"),
        version_protocolo=texto_hijo(nodo, "protocolVersion"),
        identificador_repositorio=rid,
        fecha_mas_antigua=texto_hijo(nodo, "earliestDatestamp"),
    )
    deteccion = None
    for descripcion in hijos(nodo, "description"):
        if descendientes(descripcion, "oai-identifier"):
            continue  # el identificador del repositorio puede contener "dspace" o "eprints" sin decir nada del software
        texto = " ".join(t.strip() for t in descripcion.itertext() if t.strip())
        hallado = _plataforma_de_texto(texto)
        if hallado:
            deteccion = Deteccion(hallado[0], hallado[1], None, "oai_identify", texto[:200])
            break
        # La descripción <eprints> es un estándar de OAI que también usan DSpace y otros: no identifica la plataforma.
        if "xoai" in texto.lower():
            deteccion = Deteccion("dspace", None, None, "oai_identify", texto[:200])
    return datos, deteccion


def detectar_por_rest7(r: Respuesta) -> Deteccion | None:
    datos = parsear_json(r.cuerpo)
    if not isinstance(datos, dict) or not ("_links" in datos or "dspaceVersion" in datos):
        return None
    crudo = datos.get("dspaceVersion")
    version = None
    if isinstance(crudo, str):
        m = re.search(r"(\d[\w.-]*)", crudo)
        version = m.group(1) if m else None
    return Deteccion("dspace", version or "7+", "angular", "rest7", crudo if isinstance(crudo, str) else None)


def detectar_por_rest56(r: Respuesta) -> Deteccion | None:
    datos = parsear_json(r.cuerpo)
    if not isinstance(datos, dict):
        raiz = parsear_xml(r.cuerpo)
        if raiz is None or not isinstance(raiz.tag, str) or not descendientes(raiz, "okay"):
            return None
        datos = {h.tag: h.text for h in raiz if isinstance(h.tag, str)}
    if "okay" not in datos:
        return None
    version = datos.get("sourceVersion")
    if version:
        return Deteccion("dspace", str(version), None, "rest56", str(version))
    # La /rest/status de DSpace 5 no informa la versión.
    return Deteccion("dspace", "5", None, "rest56", "sin sourceVersion: 5 (inferida)")


def extraer_rest_base_dspace7(r: Respuesta) -> str | None:
    """Base de la REST según el estado que el SSR de Angular incrusta en la página (puede estar en otro host)."""
    texto = r.texto().replace("&q;", '"')
    m = re.search(r'"rest"\s*:\s*\{[^{}]*?"baseUrl"\s*:\s*"([^"]+)"', texto)
    return m.group(1).rstrip("/") if m else None


_PRIORIDAD_FUENTES = ["rest7", "rest56", "generator", "oai_identify", "html"]


def combinar(detecciones: Sequence[Deteccion]) -> Deteccion | None:
    """Plataforma de la fuente más confiable; versión de la mejor fuente que coincide en plataforma."""
    ordenadas = sorted(
        (d for d in detecciones if d.plataforma),
        key=lambda d: _PRIORIDAD_FUENTES.index(d.fuente) if d.fuente in _PRIORIDAD_FUENTES else 99,
    )
    if not ordenadas:
        return None
    base = ordenadas[0]
    afines = [d for d in ordenadas if d.plataforma == base.plataforma]
    con_version = next((d for d in afines if d.version and d.version != "7+"), None) or next(
        (d for d in afines if d.version), None
    )
    variante = next((d.variante for d in afines if d.variante), None)
    fuente = base.fuente if not con_version or con_version.fuente == base.fuente else f"{base.fuente}+{con_version.fuente}"
    return Deteccion(base.plataforma, con_version.version if con_version else None, variante, fuente)


def es_dspace7(plataforma: str | None, version: str | None, variante: str | None) -> bool:
    if plataforma != "dspace":
        return False
    if variante == "angular":
        return True
    m = re.match(r"(\d+)", version or "")
    return bool(m) and int(m.group(1)) >= 7


# --- OAI-PMH ----------------------------------------------------------------------------


def es_oai(r: Respuesta) -> bool:
    raiz = parsear_xml(r.cuerpo)
    return raiz is not None and isinstance(raiz.tag, str) and raiz.tag.endswith("OAI-PMH")


def error_oai(r: Respuesta) -> str | None:
    raiz = parsear_xml(r.cuerpo)
    if raiz is None or not isinstance(raiz.tag, str):
        return None
    for error in descendientes(raiz, "error"):
        return error.get("code") or "error"
    return None


def parsear_formatos_oai(r: Respuesta) -> list[str] | None:
    raiz = parsear_xml(r.cuerpo)
    if raiz is None or not isinstance(raiz.tag, str):
        return None
    nodos = descendientes(raiz, "ListMetadataFormats")
    if not nodos:
        return None
    return [p.text.strip() for p in descendientes(nodos[0], "metadataPrefix") if p.text]


# --- Anti-bot y CDN ---------------------------------------------------------------------


def detectar_antibot(codigo: int | None, encabezados: tuple[tuple[str, str], ...], cuerpo: bytes) -> str | None:
    """Desafíos que impiden ver el contenido. Ojo: Anubis responde 200."""
    h = {k.lower(): v.lower() for k, v in encabezados}
    if h.get("cf-mitigated") == "challenge":
        return "cloudflare"
    if "x-amzn-waf-action" in h:
        return "aws-waf"
    if codigo in (301, 302, 303, 307, 308) and urlsplit(h.get("location", "")).path == "/challenge":
        # BunkerWeb manda todo a /challenge (prueba de trabajo en JS); se corta antes de seguir el salto.
        return "bunkerweb"
    if parece_archivo(h.get("content-type"), h.get("content-disposition")):
        return None
    c = cuerpo[:200_000].lower()
    if b"/cdn-cgi/challenge-platform" in c or b"cf_chl_opt" in c or (
        "cloudflare" in h.get("server", "") and b"<title>just a moment" in c
    ):
        return "cloudflare"
    if b"anubis" in c and (b"/.within.website/" in c or b"not a bot" in c or b"anubis_challenge" in c):
        return "anubis"
    if b"bunkerweb" in c and (b"proof of work" in c or b"<title>bot detection" in c):
        return "bunkerweb"
    if b"<title>request rejected</title>" in c and (
        b"support id" in c or b"url was rejected" in c or b"url fue rechazada" in c
    ):
        # Página de bloqueo de un WAF que responde 200 a todo (caso UNA).
        return "request-rejected"
    if b"_incapsula_resource" in c or b"incapsula incident id" in c:
        return "incapsula"
    if "ddos-guard" in h.get("server", "") and codigo in (403, 503):
        return "ddos-guard"
    if b"sucuri website firewall" in c:
        return "sucuri"
    if b"awswafintegration" in c or b"aws-waf-token" in c:
        return "aws-waf"
    if b"challenges.cloudflare.com/turnstile" in c and len(cuerpo) < 20_000:
        # Página propia que solo muestra un desafío Turnstile, aunque responda 200 (caso UFSC).
        return "turnstile"
    captcha = b"g-recaptcha" in c or b"h-captcha" in c or b"cf-turnstile" in c or b"checking your browser" in c
    if captcha and ((codigo is not None and codigo >= 400) or len(cuerpo) < 5_000):
        return "captcha"
    return None


def detectar_cdn(r: Respuesta) -> str | None:
    """Solo informativo: un CDN no es motivo de abandono."""
    servidor = (r.encabezado("server") or "").lower()
    if "cloudflare" in servidor or r.encabezado("cf-ray"):
        return "cloudflare"
    if "akamai" in servidor:
        return "akamai"
    if r.encabezado("x-amz-cf-id"):
        return "cloudfront"
    if "fastly" in (r.encabezado("x-served-by") or "").lower() or r.encabezado("x-fastly-request-id"):
        return "fastly"
    return None


# --- URLs y archivos --------------------------------------------------------------------

TipoUrl = Literal["handle", "doi", "archivo", "pagina"]

_EXTENSIONES_ARCHIVO = (
    ".pdf", ".doc", ".docx", ".odt", ".rtf", ".epub", ".zip", ".ppt", ".pptx", ".xls", ".xlsx",
    ".ods", ".csv", ".mp3", ".mp4", ".avi", ".jpg", ".jpeg", ".png", ".tif", ".tiff",
)
_RUTA_ARCHIVO = re.compile(
    r"/bitstream/|/bitstreams/[0-9a-f-]{36}/(download|content)|/core/bitstreams/[0-9a-f-]{36}/content"
    r"|/article/download/|^/\d+/\d+/[^/]+\.\w+$",
    re.I,
)


def clasificar_url(url: str) -> TipoUrl:
    texto = url.strip()
    minus = texto.lower()
    if minus.startswith(("doi:", "info:doi/")) or re.match(r"^10\.\d{4,}/", minus):
        return "doi"
    if minus.startswith(("hdl:", "info:hdl/")):
        return "handle"
    partes = urlsplit(texto)
    host = (partes.hostname or "").lower()
    if host in ("doi.org", "dx.doi.org", "www.doi.org"):
        return "doi"
    if host in ("hdl.handle.net", "handle.net", "www.handle.net"):
        return "handle"
    ruta = unquote(partes.path)
    if _RUTA_ARCHIVO.search(ruta) or ruta.lower().endswith(_EXTENSIONES_ARCHIVO):
        return "archivo"
    return "pagina"


@dataclass(frozen=True)
class Robots:
    _parser: Protego
    agente: str

    def permitido(self, url: str) -> bool:
        return bool(self._parser.can_fetch(url, self.agente))

    @property
    def crawl_delay(self) -> float:
        return float(self._parser.crawl_delay(self.agente) or 0.0)


def parsear_robots(cuerpo: bytes, agente: str) -> Robots:
    return Robots(Protego.parse(cuerpo.decode("utf-8", errors="replace")), agente)


_BINARIOS = {"pdf", "zip", "ooxml", "epub", "odf", "ole", "png", "jpeg", "gif", "tiff", "rtf", "mp3", "mp4"}


def magic(cuerpo: bytes) -> str | None:
    if not cuerpo:
        return None
    if cuerpo.startswith(b"%PDF"):
        return "pdf"
    if cuerpo.startswith(b"PK\x03\x04"):
        if b"[Content_Types].xml" in cuerpo[:2048] or b"word/" in cuerpo[:2048]:
            return "ooxml"
        if b"application/epub+zip" in cuerpo[:200]:
            return "epub"
        if b"application/vnd.oasis.opendocument" in cuerpo[:200]:
            return "odf"
        return "zip"
    if cuerpo.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"
    if cuerpo.startswith(b"\x89PNG"):
        return "png"
    if cuerpo.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if cuerpo.startswith(b"GIF8"):
        return "gif"
    if cuerpo.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if cuerpo.startswith(b"{\\rtf"):
        return "rtf"
    if cuerpo.startswith(b"ID3"):
        return "mp3"
    if cuerpo[4:8] == b"ftyp":
        return "mp4"
    inicio = cuerpo[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if inicio.startswith((b"<!doctype html", b"<html")) or b"<html" in inicio[:200]:
        return "html"
    if inicio.startswith(b"<?xml") or inicio.startswith(b"<"):
        return "xml"
    try:
        cuerpo[:512].decode("utf-8")
        return "texto"
    except UnicodeDecodeError:
        return "binario"


def es_archivo(r: Respuesta) -> bool:
    """La respuesta es un archivo: por encabezados o, si no lo dicen, por los primeros bytes."""
    if r.codigo not in (200, 206) or r.error:
        return False
    if parece_archivo(r.encabezado("content-type"), r.encabezado("content-disposition")):
        return magic(r.cuerpo) != "html"
    return magic(r.cuerpo) in _BINARIOS


def nombre_archivo(r: Respuesta) -> str | None:
    disposicion = r.encabezado("content-disposition") or ""
    m = re.search(r"filename\*\s*=\s*([\w-]+)''([^;]+)", disposicion, re.I)
    if m:
        return unquote(m.group(2).strip().strip('"'), encoding=m.group(1) or "utf-8", errors="replace")
    m = re.search(r'filename\s*=\s*"([^"]+)"', disposicion, re.I) or re.search(
        r"filename\s*=\s*([^;]+)", disposicion, re.I
    )
    if m:
        return m.group(1).strip()
    segmento = unquote(urlsplit(r.url).path.rstrip("/").rsplit("/", 1)[-1])
    if segmento and "." in segmento:
        return segmento
    return None
