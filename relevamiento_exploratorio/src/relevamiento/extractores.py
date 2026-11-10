"""Extractores puros: de una respuesta ya obtenida a candidatos de archivo. Uno por método."""

import re
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from relevamiento.config import BUNDLES_DERIVADOS, FORMATOS_OAI_METODO, METODOS
from relevamiento.detectores import clasificar_url, error_oai, es_angular_sin_ssr, es_archivo, magic, nombre_archivo
from relevamiento.modelos import Candidato, Extraccion, Item, PaginaOai, Reconocimiento, RegistroOai, Respuesta
from relevamiento.parseo import (
    descendientes,
    hijos,
    host_de,
    normalizar_url,
    parsear_html,
    parsear_json,
    parsear_xml,
    raiz,
    texto_hijo,
)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ENLACE_DSPACE = re.compile(rf"/bitstream/|/bitstreams/{_UUID}/download|/core/bitstreams/{_UUID}/content")
_OJS_DESCARGA = re.compile(r"/article/download/\d+/\d+")
_OJS_GALERADA = re.compile(r"/article/view/(\d+)/(\d+)")
_EPRINTS_DOCUMENTO = re.compile(r"^/\d+/\d+/[^/?#]+\.\w+$")


# --- Directo, página y resolución ------------------------------------------------------


def extraer_directo(r: Respuesta) -> Extraccion:
    if not es_archivo(r):
        return Extraccion()
    candidato = Candidato(
        url=r.url, fuente="directo", nombre=nombre_archivo(r), mime=r.tipo_contenido or None, tamano=r.total
    )
    return Extraccion(candidatos=(candidato,))


def extraer_pagina(r: Respuesta) -> Extraccion:
    doc = parsear_html(r.cuerpo, base_url=r.url)
    if doc is None or magic(r.cuerpo) not in ("html", "xml", "texto"):
        return Extraccion(problema="no_es_html")
    if es_angular_sin_ssr(r.cuerpo):
        return Extraccion(problema="angular_sin_ssr")
    candidatos: dict[str, Candidato] = {}

    def agregar(url: str | None, fuente: str) -> None:
        if url and url.startswith(("http://", "https://")):
            candidatos.setdefault(normalizar_url(url), Candidato(url=url, fuente=fuente, nombre=_nombre_de_url(url)))

    metas: dict[str, list[str]] = {}
    for meta in doc.iter("meta"):
        nombre_meta = (meta.get("name") or meta.get("property") or "").strip().lower()
        if nombre_meta and meta.get("content"):
            metas.setdefault(nombre_meta, []).append(meta.get("content").strip())
    for url in metas.get("citation_pdf_url", []):
        agregar(urljoin(r.url, url), "citation_pdf_url")
    for url in metas.get("eprints.document_url", []):
        agregar(urljoin(r.url, url), "eprints_meta")

    host_pagina = host_de(r.url)
    for a in doc.iter("a"):
        href = (a.get("href") or "").strip()
        if not href.startswith(("http://", "https://")):
            continue
        ruta = urlsplit(href).path
        if _ENLACE_DSPACE.search(ruta) or _OJS_DESCARGA.search(ruta):
            agregar(href, "enlace")
        elif m := _OJS_GALERADA.search(ruta):
            # Las galeradas de OJS se ven en /article/view/ID/GALERADA y se descargan en /article/download/.
            agregar(href[: href.index("/article/view/")] + f"/article/download/{m.group(1)}/{m.group(2)}", "enlace_ojs")
        elif host_de(href) == host_pagina and (_EPRINTS_DOCUMENTO.match(ruta) or "ep_document_link" in (a.get("class") or "")):
            agregar(href, "enlace_eprints")

    datos: dict[str, str] = {}
    identificadores = metas.get("dc.identifier", []) + metas.get("citation_doi", []) + [r.url]
    for valor in identificadores:
        if "handle" not in datos and (h := extraer_handle(valor)):
            datos["handle"] = h
        if "doi" not in datos and (d := extraer_doi(valor)):
            datos["doi"] = d
    if m := re.search(rf"/(?:items|entities/[\w-]+)/({_UUID})", urlsplit(r.url).path):
        datos["uuid"] = m.group(1).lower()
    return Extraccion(candidatos=tuple(candidatos.values()), datos=datos)


def _nombre_de_url(url: str) -> str | None:
    segmento = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    return segmento if re.search(r"\.\w{2,5}$", segmento) else None


def extraer_resolucion(r: Respuesta) -> Extraccion:
    """DOI o handle: registra el destino y aplica directo o página allí."""
    destino = extraer_directo(r) if es_archivo(r) else extraer_pagina(r)
    datos = {**destino.datos, "destino": r.url, "saltos": str(len(r.saltos))}
    return Extraccion(candidatos=destino.candidatos, datos=datos, problema=destino.problema)


# --- OAI-PMH ---------------------------------------------------------------------------


def _registro_oai(r: Respuesta) -> tuple[Any, str | None]:
    if codigo := error_oai(r):
        return None, codigo
    doc = parsear_xml(r.cuerpo)
    if doc is None or not isinstance(doc.tag, str):
        return None, "xml_invalido"
    registros = descendientes(doc, "record")
    if not registros:
        return None, "sin_registro"
    encabezado = hijos(registros[0], "header")
    if encabezado and encabezado[0].get("status") == "deleted":
        return None, "registro_borrado"
    return registros[0], None


def extraer_oai_xoai(r: Respuesta) -> Extraccion:
    registro, problema = _registro_oai(r)
    if problema:
        return Extraccion(problema=problema)
    candidatos = []
    for bundles in (e for e in descendientes(registro, "element") if e.get("name") == "bundles"):
        for bundle in (e for e in hijos(bundles, "element") if e.get("name") == "bundle"):
            nombre_bundle = _campo(bundle, "name")
            for bitstreams in (e for e in hijos(bundle, "element") if e.get("name") == "bitstreams"):
                for bs in (e for e in hijos(bitstreams, "element") if e.get("name") == "bitstream"):
                    url = _campo(bs, "url")
                    if url:
                        candidatos.append(Candidato(
                            url=url, fuente="xoai", bundle=nombre_bundle, nombre=_campo(bs, "name"),
                            mime=_campo(bs, "format"), tamano=_entero(_campo(bs, "size")),
                        ))
    datos = {}
    for otros in (e for e in descendientes(registro, "element") if e.get("name") == "others"):
        if handle := _campo(otros, "handle"):
            datos["handle"] = handle
    return Extraccion(candidatos=tuple(candidatos), datos=datos)


def _campo(elemento, nombre: str) -> str | None:
    for campo in hijos(elemento, "field"):
        if campo.get("name") == nombre and campo.text and campo.text.strip():
            return campo.text.strip()
    return None


def extraer_oai_ore(r: Respuesta) -> Extraccion:
    registro, problema = _registro_oai(r)
    if problema:
        return Extraccion(problema=problema)
    # Las tripletas RDF de DSpace describen cada bitstream con su bundle en dcterms:description.
    bundles = {}
    for desc in descendientes(registro, "Description"):
        about = next((v for k, v in desc.attrib.items() if k.endswith("about")), None)
        if about and (d := texto_hijo(desc, "description")):
            bundles[normalizar_url(about)] = d
    candidatos = []
    for link in descendientes(registro, "link"):
        if not (link.get("rel") or "").endswith("/ore/terms/aggregates"):
            continue
        url = link.get("href")
        if url:
            candidatos.append(Candidato(
                url=url, fuente="ore", bundle=bundles.get(normalizar_url(url)), nombre=link.get("title"),
                mime=link.get("type"), tamano=_entero(link.get("length")),
            ))
    return Extraccion(candidatos=tuple(candidatos))


def extraer_oai_mets(r: Respuesta) -> Extraccion:
    registro, problema = _registro_oai(r)
    if problema:
        return Extraccion(problema=problema)
    candidatos = []
    for grupo in descendientes(registro, "fileGrp"):
        uso = grupo.get("USE")
        for archivo in hijos(grupo, "file"):
            for flocat in hijos(archivo, "FLocat"):
                url = next((v for k, v in flocat.attrib.items() if k.endswith("href")), None)
                titulo = next((v for k, v in flocat.attrib.items() if k.endswith("title")), None)
                if url:
                    candidatos.append(Candidato(
                        url=url, fuente="mets", bundle=uso, nombre=titulo,
                        mime=archivo.get("MIMETYPE"), tamano=_entero(archivo.get("SIZE")),
                    ))
    return Extraccion(candidatos=tuple(candidatos))


def extraer_pagina_tamanos(r: Respuesta, formato: str) -> PaginaOai:
    """Todos los registros de una página de ListRecords en METS (`mets`) o xoai (`xoai`), con el
    tamaño de sus archivos de contenido (sin los bundles derivados: miniaturas, texto, licencias).

    Si la página quedó cortada por el tope de bytes, el último registro puede estar incompleto y se
    descarta."""
    if codigo := error_oai(r):
        return PaginaOai(problema=codigo)
    doc = parsear_xml(r.cuerpo)
    if doc is None or not isinstance(doc.tag, str):
        return PaginaOai(problema="xml_invalido")
    registros = descendientes(doc, "record")
    if r.truncado and registros:
        registros = registros[:-1]
    salida = []
    for registro in registros:
        encabezado = hijos(registro, "header")
        identificador = texto_hijo(encabezado[0], "identifier") if encabezado else None
        if encabezado and encabezado[0].get("status") == "deleted":
            salida.append(RegistroOai(identificador, borrado=True))
            continue
        archivos = _archivos_mets(registro) if formato == "mets" else _archivos_xoai(registro)
        salida.append(RegistroOai(identificador, archivos=tuple(archivos)))
    token = descendientes(doc, "resumptionToken")
    total = _entero(token[0].get("completeListSize")) if token else None
    if total is None and not token and not r.truncado:
        # Sin resumptionToken, la lista entra entera en esta página.
        total = len(salida)
    return PaginaOai(registros=tuple(salida), total=total)


def _archivos_mets(registro) -> list[tuple[int | None, str | None]]:
    return [
        (_entero(archivo.get("SIZE")), archivo.get("MIMETYPE"))
        for grupo in descendientes(registro, "fileGrp")
        if not es_derivado(grupo.get("USE"))
        for archivo in hijos(grupo, "file")
    ]


def _archivos_xoai(registro) -> list[tuple[int | None, str | None]]:
    archivos = []
    for bundles in (e for e in descendientes(registro, "element") if e.get("name") == "bundles"):
        for bundle in (e for e in hijos(bundles, "element") if e.get("name") == "bundle"):
            if es_derivado(_campo(bundle, "name")):
                continue
            for bitstreams in (e for e in hijos(bundle, "element") if e.get("name") == "bitstreams"):
                for bs in (e for e in hijos(bitstreams, "element") if e.get("name") == "bitstream"):
                    archivos.append((_entero(_campo(bs, "size")), _campo(bs, "format")))
    return archivos


def muestrear_listrecords(r: Respuesta, repo: str, url_base: str, n: int) -> list[Item]:
    """Toma los primeros n registros no borrados con una URL usable en dc:identifier."""
    doc = parsear_xml(r.cuerpo)
    if doc is None or not isinstance(doc.tag, str):
        return []
    host_repo = host_de(url_base)
    items: list[Item] = []
    for registro in descendientes(doc, "record"):
        encabezado = hijos(registro, "header")
        if not encabezado or encabezado[0].get("status") == "deleted":
            continue
        oai_id = texto_hijo(encabezado[0], "identifier")
        identificadores = [i.text.strip() for i in descendientes(registro, "identifier") if i.text and i.text.strip()]
        urls = [u for u in (url_pedible(i) for i in identificadores if i != oai_id) if u]
        if not urls:
            continue
        propia = next((u for u in urls if host_de(u) == host_repo and clasificar_url(u) == "pagina"), None)
        items.append(Item(repo=repo, idx=len(items), url=propia or urls[0], oai_id=oai_id))
        if len(items) == n:
            break
    return items


# --- REST de DSpace --------------------------------------------------------------------


def extraer_rest56(r: Respuesta) -> Extraccion:
    datos = parsear_json(r.cuerpo)
    if not isinstance(datos, dict):
        return Extraccion(problema="json_invalido")
    candidatos = []
    for bs in datos.get("bitstreams") or []:
        enlace = bs.get("retrieveLink")
        if not enlace:
            continue
        # En DSpace 6 el enlace es relativo al host (/rest/...); en 5, relativo a la base /rest.
        url = urljoin(raiz(r.url), enlace) if enlace.startswith("/rest/") else urljoin(raiz(r.url), "/rest" + enlace)
        candidatos.append(Candidato(
            url=url, fuente="rest56", bundle=bs.get("bundleName"), nombre=bs.get("name"),
            mime=bs.get("mimeType"), tamano=_entero(bs.get("sizeBytes")),
        ))
    return Extraccion(candidatos=tuple(candidatos), datos={"handle": datos["handle"]} if datos.get("handle") else {})


def extraer_rest7_item(r: Respuesta) -> Extraccion:
    datos = parsear_json(r.cuerpo)
    if not isinstance(datos, dict) or "uuid" not in datos and "id" not in datos:
        return Extraccion(problema="json_invalido")
    uuid = datos.get("uuid") or datos["id"]
    bundles = (datos.get("_links") or {}).get("bundles", {}).get("href")
    if not bundles:
        return Extraccion(problema="sin_enlace_bundles", datos={"uuid": uuid})
    return Extraccion(siguiente=((_con_embed(bundles), "bundles"),), datos={"uuid": uuid})


def url_rest7_bundles(rest_base: str, uuid: str) -> str:
    return _con_embed(f"{rest_base}/api/core/items/{uuid}/bundles")


def _con_embed(url: str) -> str:
    return url + ("&" if "?" in url else "?") + "embed=bitstreams"


def extraer_rest7_bundles(r: Respuesta) -> Extraccion:
    datos = parsear_json(r.cuerpo)
    if not isinstance(datos, dict):
        return Extraccion(problema="json_invalido")
    candidatos: list[Candidato] = []
    siguiente: list[tuple[str, str]] = []
    for bundle in (datos.get("_embedded") or {}).get("bundles") or []:
        nombre = bundle.get("name")
        incrustados = (bundle.get("_embedded") or {}).get("bitstreams")
        if incrustados is not None:
            candidatos.extend(_bitstreams7(incrustados, nombre))
        elif (enlace := (bundle.get("_links") or {}).get("bitstreams", {}).get("href")) and (
            (nombre or "").upper() not in BUNDLES_DERIVADOS
        ):
            siguiente.append((enlace, nombre or ""))
    return Extraccion(candidatos=tuple(candidatos), siguiente=tuple(siguiente))


def extraer_rest7_bitstreams(r: Respuesta, bundle: str | None) -> Extraccion:
    datos = parsear_json(r.cuerpo)
    if not isinstance(datos, dict):
        return Extraccion(problema="json_invalido")
    return Extraccion(candidatos=tuple(_bitstreams7(datos, bundle)))


def _bitstreams7(pagina: dict, bundle: str | None) -> list[Candidato]:
    lista = (pagina.get("_embedded") or {}).get("bitstreams") if isinstance(pagina, dict) else None
    candidatos = []
    for bs in lista or []:
        url = (bs.get("_links") or {}).get("content", {}).get("href")
        if url:
            candidatos.append(Candidato(
                url=url, fuente="rest7", bundle=bundle, nombre=bs.get("name"), tamano=_entero(bs.get("sizeBytes")),
            ))
    return candidatos


# --- Identificadores y aplicabilidad ---------------------------------------------------


def extraer_handle(valor: str) -> str | None:
    m = re.search(r"(?:hdl\.handle\.net/|/handle/|^hdl:|^info:hdl/)(\d[\d.]*/[\w.\-]+)", valor.strip())
    return m.group(1) if m else None


def extraer_doi(valor: str) -> str | None:
    m = re.search(r"(10\.\d{4,9}/[^\s\"<>]+)", unquote(valor))
    return m.group(1).rstrip(".,;") if m else None


def url_pedible(valor: str) -> str | None:
    """Convierte un identificador (URL, doi:, hdl:) en una URL que se pueda pedir."""
    valor = valor.strip()
    if valor.startswith(("http://", "https://")):
        return valor
    minus = valor.lower()
    if minus.startswith(("doi:", "info:doi/")) or re.match(r"^10\.\d{4,}/", valor):
        return f"https://doi.org/{extraer_doi(valor)}" if extraer_doi(valor) else None
    if minus.startswith(("hdl:", "info:hdl/")):
        h = extraer_handle(valor)
        return f"https://hdl.handle.net/{h}" if h else None
    return None


def derivar_oai_id(recon: Reconocimiento, item: Item, datos: dict[str, str]) -> str | None:
    if item.oai_id:
        return item.oai_id
    rid = recon.identificador_oai
    if not rid:
        return None
    if recon.plataforma == "dspace" and datos.get("handle"):
        return f"oai:{rid}:{datos['handle']}"
    if recon.plataforma == "ojs" and (m := re.search(r"/article/view/(\d+)", item.url)):
        return f"oai:{rid}:article/{m.group(1)}"
    if recon.plataforma == "eprints" and (m := re.search(r"^/(\d+)/?$", urlsplit(item.url).path)):
        return f"oai:{rid}:{m.group(1)}"
    return None


def metodos_aplicables(
    recon: Reconocimiento, tipo_url: str, item_es_archivo: bool, datos: dict[str, str], oai_id: str | None
) -> dict[str, str | None]:
    """Para cada método, None si aplica o el motivo por el que no aplica."""
    resolver = tipo_url in ("doi", "handle")
    motivos: dict[str, str | None] = {m: None for m in METODOS}
    motivos["directo"] = "la URL es un resolver (lo cubre doi_handle)" if resolver else (
        None if item_es_archivo else "la URL devuelve una página")
    motivos["pagina"] = "la URL es un resolver (lo cubre doi_handle)" if resolver else (
        "la URL devuelve un archivo" if item_es_archivo else None)
    motivos["doi_handle"] = None if resolver else "la URL no es un DOI ni un handle"
    for metodo, formato in FORMATOS_OAI_METODO.items():
        if not recon.oai_endpoint:
            motivos[metodo] = "sin endpoint OAI"
        elif formato not in (recon.oai_formatos or []):
            motivos[metodo] = f"el repositorio no ofrece {formato}"
        elif not oai_id:
            motivos[metodo] = "no se pudo derivar el identificador OAI"
    if recon.rest != "v5_6":
        motivos["rest56"] = "sin REST de DSpace 5/6"
    elif not datos.get("handle"):
        motivos["rest56"] = "sin handle del ítem"
    if recon.rest != "v7":
        motivos["rest7"] = "sin REST de DSpace 7+"
    elif not (datos.get("uuid") or datos.get("handle")):
        motivos["rest7"] = "sin handle ni uuid del ítem"
    return motivos


def es_derivado(bundle: str | None) -> bool:
    return (bundle or "").upper() in BUNDLES_DERIVADOS


def _entero(valor: Any) -> int | None:
    try:
        return int(valor) if valor not in (None, "") else None
    except (TypeError, ValueError):
        return None
