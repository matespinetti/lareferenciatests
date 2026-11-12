"""Extractores puros: de una respuesta ya obtenida a candidatos de archivo. Uno por método."""

import re
from collections import Counter
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from relevamiento.config import BUNDLES_DERIVADOS, FORMATOS_OAI_METODO, METODOS
from relevamiento.detectores import clasificar_url, error_oai, es_angular_sin_ssr, es_archivo, magic, nombre_archivo
from relevamiento.modelos import (
    ArchivoOai,
    Candidato,
    Extraccion,
    Item,
    ItemOai,
    PaginaItems,
    PaginaOai,
    Reconocimiento,
    RegistroOai,
    Respuesta,
    Ubicacion,
)
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
    datos = {}
    for otros in (e for e in descendientes(registro, "element") if e.get("name") == "others"):
        if handle := _campo(otros, "handle"):
            datos["handle"] = handle
    return Extraccion(candidatos=tuple(_candidatos_xoai(registro)), datos=datos)


def _candidatos_xoai(registro) -> list[Candidato]:
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
    return candidatos


def _campo(elemento, nombre: str) -> str | None:
    for campo in hijos(elemento, "field"):
        if campo.get("name") == nombre and campo.text and campo.text.strip():
            return campo.text.strip()
    return None


def extraer_oai_ore(r: Respuesta) -> Extraccion:
    registro, problema = _registro_oai(r)
    if problema:
        return Extraccion(problema=problema)
    return Extraccion(candidatos=tuple(_candidatos_ore(registro)))


def _candidatos_ore(registro) -> list[Candidato]:
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
    return candidatos


def extraer_oai_mets(r: Respuesta) -> Extraccion:
    registro, problema = _registro_oai(r)
    if problema:
        return Extraccion(problema=problema)
    return Extraccion(candidatos=tuple(_candidatos_mets(registro)))


def _candidatos_mets(registro) -> list[Candidato]:
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
    return candidatos


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


def archivos_de_pagina(r: Respuesta, formato: str) -> list[tuple[str, int | None]]:
    """(URL, tamaño) de los archivos de contenido de una página de ListRecords en METS o xoai, para
    elegir qué archivos medir. Sin los registros borrados ni el último si la página quedó cortada."""
    if error_oai(r):
        return []
    doc = parsear_xml(r.cuerpo)
    if doc is None or not isinstance(doc.tag, str):
        return []
    registros = descendientes(doc, "record")
    if r.truncado and registros:
        registros = registros[:-1]
    salida = []
    for registro in registros:
        encabezado = hijos(registro, "header")
        if encabezado and encabezado[0].get("status") == "deleted":
            continue
        if formato == "mets":
            for grupo in descendientes(registro, "fileGrp"):
                if es_derivado(grupo.get("USE")):
                    continue
                for archivo in hijos(grupo, "file"):
                    for flocat in hijos(archivo, "FLocat"):
                        url = next((v for k, v in flocat.attrib.items() if k.endswith("href")), None)
                        if url:
                            salida.append((url, _entero(archivo.get("SIZE"))))
        else:
            for bundle in (e for e in descendientes(registro, "element") if e.get("name") == "bundle"):
                if es_derivado(_campo(bundle, "name")):
                    continue
                for bs in (e for e in descendientes(bundle, "element") if e.get("name") == "bitstream"):
                    if url := _campo(bs, "url"):
                        salida.append((url, _entero(_campo(bs, "size"))))
    return salida


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


def _candidatos_marc(registro) -> list[Candidato]:
    """Campo 856 de MARCXML (Invenio y otros): $u la URL, $s el tamaño en bytes, $q el tipo, $y el nombre."""
    candidatos = []
    for campo in (d for d in descendientes(registro, "datafield") if d.get("tag") == "856"):
        sub = {s.get("code"): (s.text or "").strip() for s in hijos(campo, "subfield")}
        if sub.get("u", "").startswith(("http://", "https://")):
            candidatos.append(Candidato(url=sub["u"], fuente="marc", nombre=sub.get("y") or None,
                                        mime=sub.get("q") or None, tamano=_entero(sub.get("s"))))
    return candidatos


# --- Muestra de LA Referencia (`muestra-ubicar` y `muestra-cosechar`) -----------------

_CANDIDATOS_POR_FORMATO = {"mets": _candidatos_mets, "xoai": _candidatos_xoai, "ore": _candidatos_ore}
_PARECE_ARCHIVO = re.compile(r"\.(pdf|epub|docx?|odt|zip|ps|djvu|mp4|mp3)$|/bitstream/|/bitstreams/|/article/download/", re.I)
_PARECE_PAGINA = re.compile(r"hdl\.handle\.net/|/handle/|/items/|/article/view/|/record/|/eprint/|/\d+/?$")


def items_de_pagina(r: Respuesta, formato: str) -> PaginaItems:
    """Todos los ítems de una página de ListRecords con su página y sus archivos de contenido (URL,
    tamaño, tipo y bundle), sin los bundles derivados, más `completeListSize` y el resumptionToken.

    En `mets`, `xoai` y `ore` los archivos salen del registro. En `oai_dc` (OJS y otros) solo se toman
    los `dc:identifier` que ya apuntan a un archivo; lo demás queda como página del ítem, que el
    descargador tendrá que resolver. Si la página quedó cortada, se descarta el último registro y el
    token (que estaría al final)."""
    if codigo := error_oai(r):
        return PaginaItems(problema=codigo)
    doc = parsear_xml(r.cuerpo)
    if doc is None or not isinstance(doc.tag, str):
        return PaginaItems(problema="xml_invalido")
    # Solo los registros OAI (con <header>): MARCXML trae además su propio <record>.
    registros = [x for x in descendientes(doc, "record") if hijos(x, "header")]
    if not registros and not descendientes(doc, "ListRecords"):
        return PaginaItems(problema="sin_listrecords")  # respuesta cortada por el servidor
    if r.truncado and registros:
        registros = registros[:-1]
    items = []
    for registro in registros:
        encabezado = hijos(registro, "header")
        cabeza = encabezado[0] if encabezado else None
        identificador = texto_hijo(cabeza, "identifier") if cabeza is not None else None
        datestamp = texto_hijo(cabeza, "datestamp") if cabeza is not None else None
        if cabeza is not None and cabeza.get("status") == "deleted":
            items.append(ItemOai(identificador, datestamp, borrado=True))
            continue
        urls = _urls_del_registro(registro)
        if formato == "marcxml":
            marc = _candidatos_marc(registro)
            archivos = [ArchivoOai(c.url, c.tamano, c.mime, None, c.nombre or _nombre_de_url(c.url))
                        for c in marc if c.tamano or _PARECE_ARCHIVO.search(c.url)]
            urls += [c.url for c in marc if not (c.tamano or _PARECE_ARCHIVO.search(c.url))]
        elif extraer := _CANDIDATOS_POR_FORMATO.get(formato):
            archivos = [
                ArchivoOai(c.url, c.tamano, c.mime, c.bundle, c.nombre)
                for c in extraer(registro)
                if not es_derivado(c.bundle)
            ]
        else:
            archivos = [ArchivoOai(u, nombre=_nombre_de_url(u)) for u in urls if _PARECE_ARCHIVO.search(u)]
        pagina = next((u for u in urls if _PARECE_PAGINA.search(u) and not _PARECE_ARCHIVO.search(u)), None)
        pagina = pagina or next((u for u in urls if not _PARECE_ARCHIVO.search(u)), None)
        items.append(ItemOai(identificador, datestamp, pagina=pagina, archivos=tuple(_sin_repetidos(archivos))))
    nodos_token = [] if r.truncado else descendientes(doc, "resumptionToken")
    token = (nodos_token[0].text or "").strip() or None if nodos_token else None
    total = _entero(nodos_token[0].get("completeListSize")) if nodos_token else None
    if total is None and not nodos_token and not r.truncado:
        total = len(items)
    return PaginaItems(items=tuple(items), total=total, token=token)


def _urls_del_registro(registro) -> list[str]:
    """URLs de los identificadores del registro: dc:identifier, mods:identifier y, en xoai, los valores
    del elemento `identifier`. Sin el encabezado (su identifier es el `oai:`)."""
    urls = []
    for nodo in descendientes(registro, "identifier"):
        texto = (nodo.text or "").strip()
        if texto.startswith(("http://", "https://")):
            urls.append(texto)
    for elemento in (e for e in descendientes(registro, "element") if e.get("name") == "identifier"):
        for campo in descendientes(elemento, "field"):
            texto = (campo.text or "").strip()
            if campo.get("name") == "value" and texto.startswith(("http://", "https://")):
                urls.append(texto)
    return list(dict.fromkeys(urls))


def _sin_repetidos(archivos: list[ArchivoOai]) -> list[ArchivoOai]:
    vistos, salida = set(), []
    for a in archivos:
        if normalizar_url(a.url) not in vistos:
            vistos.add(normalizar_url(a.url))
            salida.append(a)
    return salida


_HOSTS_RESOLUTORES = frozenset({"hdl.handle.net", "doi.org", "dx.doi.org", "www.doi.org", "n2t.net", "purl.org"})
_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
_NAMESPACES_GENERICOS = frozenset({"ojs.pkp.sfu.ca", "localhost"})
_OJS_RUTA = re.compile(r"^(?P<base>.*?)/(?:index\.php/)?(?P<revista>[^/]+)/(?:article|issue)/(?:view|download)/", re.I)
# Página de un ítem en el propio repositorio (DSpace, Invenio, EPrints): su host es el del repositorio.
_PAGINA_DE_ITEM = re.compile(r"/handle/|/items/|/entities/|/record/|/eprint/|/jspui/|/xmlui/|^https?://[^/]+/\d+/?$", re.I)
_NAMESPACE_OJS = re.compile(r"^ojs\d*\.")
_HANDLE = re.compile(r"^https?://hdl\.handle\.net/(?P<handle>[^?#]+)", re.I)


def ubicar(r: Respuesta, nombre: str) -> Ubicacion:
    """Interpreta la respuesta de la API del buscador de LA Referencia (varios registros de un
    repositorio del listado): país, namespace OAI, una URL de ítem, el host y la URL base desde la que
    se busca el endpoint OAI. Vota entre los registros, porque uno solo puede apuntar afuera.

    La URL base sale, en este orden:
    - de las URLs con forma de revista OJS (la revista tiene su propio endpoint);
    - del host de las páginas de ítem del propio repositorio (`/handle/`, `/items/`, `/record/`…);
    - del namespace OAI si es un host (DSpace usa `oai:<host>:<handle>`);
    - de un host que se repita entre las URLs.
    La otra opción, si difiere, queda como `alternativa`. Si solo hay handles (los de España llegan
    con `oai:dnet:…`), queda el handle para resolverlo (`problema="solo_handle"`)."""
    datos = parsear_json(r.cuerpo)
    if not isinstance(datos, dict):
        return Ubicacion(problema="respuesta_invalida")
    todos = [reg.get("rawData") or {} for reg in datos.get("records") or []]
    if not todos:
        return Ubicacion(problema="sin_registros")
    crudos = [c for c in todos if (_primero(c.get("reponame_str")) or "").strip() == nombre.strip()]
    if not crudos:
        return Ubicacion(problema="otro_repositorio")
    pais = Counter(p for c in crudos if (p := _primero(c.get("network_acronym_str")))).most_common(1)
    pais_top = pais[0][0] if pais else None
    namespaces: Counter[str] = Counter()
    hosts_item: Counter[str] = Counter()
    hosts: Counter[str] = Counter()
    revistas: Counter[str] = Counter()
    handles: list[str] = []
    ejemplos: list[str] = []
    for c in crudos:
        partes = (_primero(c.get("oai_identifier_str")) or "").split(":")
        if len(partes) >= 3 and partes[0].lower() == "oai":
            ns = partes[1].strip().lower()
            if _HOST.match(ns) and ns not in _NAMESPACES_GENERICOS:
                namespaces[ns] += 1
        for u in _lista(c.get("url")):
            if not isinstance(u, str) or not u.startswith(("http://", "https://")):
                continue
            ejemplos.append(u)
            if m := _HANDLE.match(u):
                handles.append(m.group("handle"))
            host = host_de(u)
            if host in _HOSTS_RESOLUTORES:
                continue
            hosts[host] += 1
            if m := _OJS_RUTA.match(u):
                revistas[m.group("base") + ("/index.php/" if "/index.php/" in u else "/") + m.group("revista")] += 1
            elif _PAGINA_DE_ITEM.search(u):
                hosts_item[host] += 1
    ns_top = namespaces.most_common(1)[0][0] if namespaces else None
    ns_ojs = bool(ns_top and _NAMESPACE_OJS.match(ns_top))  # `ojs.<host>`, `ojs2.<host>`: el de fábrica de OJS
    propios = [u for u in ejemplos if host_de(u) not in _HOSTS_RESOLUTORES]
    url_ejemplo = (propios or ejemplos or [None])[0]
    minimo = 1 if len(crudos) == 1 else 2  # con varios registros, un host que aparece una sola vez no alcanza
    item_top = hosts_item.most_common(1)[0] if hosts_item else None

    def con(host: str, base: str, plataforma: str | None = None, alternativa: str | None = None) -> Ubicacion:
        if alternativa and _sin_www(host_de(alternativa)) == _sin_www(host):
            alternativa = None
        return Ubicacion(pais_top, ns_top, url_ejemplo, host, base, plataforma, alternativa=alternativa,
                         registros=len(crudos))

    ojs = _ojs(revistas, minimo)
    host_ojs = host_de(ojs) if ojs else None
    # 1. Namespace OAI que es un host. Si coincide con el de las revistas, es una revista o un portal
    #    OJS. Si las páginas de ítem están en otro host, mandan ellas (el namespace puede ser el de un
    #    agregador). Lo otro queda de alternativa, por si el primero no tiene OAI.
    if ns_top and not ns_ojs:
        if ojs and _sin_www(ns_top) == _sin_www(host_ojs or ""):
            return con(host_ojs or "", ojs, "ojs")
        if item_top and item_top[1] >= minimo and _sin_www(item_top[0]) != _sin_www(ns_top):
            return con(item_top[0], f"https://{item_top[0]}", alternativa=f"https://{ns_top}")
        otro = f"https://{item_top[0]}" if item_top and item_top[1] >= minimo else ojs
        return con(ns_top, f"https://{ns_top}", alternativa=otro)
    # 2. Páginas de ítem del propio repositorio (un DSpace que además enlaza artículos de revistas).
    if item_top and item_top[1] >= minimo:
        return con(item_top[0], f"https://{item_top[0]}", alternativa=ojs)
    # 3. Revistas OJS: la revista, o el sitio si son varias.
    if ojs:
        return con(host_ojs or "", ojs, "ojs")
    # 4. Un host que aparezca en la mitad de los registros o más (si no, son enlaces a otros sitios).
    if hosts:
        host, votos = hosts.most_common(1)[0]
        if votos >= max(minimo, (len(crudos) + 1) // 2):
            return con(host, f"https://{host}")
    if ns_top and ns_ojs:
        host = ns_top.split(".", 1)[1]
        return con(host, f"https://{host}")
    if handles:
        return Ubicacion(pais_top, ns_top, url_ejemplo, handle=handles[0], registros=len(crudos), problema="solo_handle")
    return Ubicacion(pais_top, ns_top, url_ejemplo, registros=len(crudos), problema="sin_host")


def _ojs(revistas: Counter[str], minimo: int) -> str | None:
    """La URL base OJS de las revistas votadas: la revista si una tiene la mayoría y el sitio
    (`/index.php/index`) si son varias. None si no alcanzan los votos o ningún host se repite."""
    total = sum(revistas.values())
    if not revistas or total < minimo:
        return None
    base, votos = revistas.most_common(1)[0]
    if len(revistas) == 1 or votos * 2 > total:
        return base
    por_host = Counter(host_de(b) for b in revistas.elements())
    host, votos_host = por_host.most_common(1)[0]
    if votos_host < minimo:
        return None  # artículos de revistas de distintos sitios: enlaces de otro repositorio
    base = next(b for b in revistas if host_de(b) == host)
    return base[: base.index("/index.php/")] + "/index.php/index" if "/index.php/" in base else raiz(base)


def endpoints_roar(cuerpo: bytes) -> dict[str, list[str]]:
    """Host → endpoints OAI-PMH del listado de ROAR (`rawlist.xml`, EPrints). Cada registro aparece con el
    host de su página y el de cada endpoint (sin `www.`). Se limpian los `?verb=…` y lo que no es URL."""
    doc = parsear_xml(cuerpo)
    salida: dict[str, list[str]] = {}
    if doc is None:
        return salida
    for registro in hijos(doc, "eprint"):
        oai = hijos(registro, "oai_pmh")
        endpoints = []
        for item in hijos(oai[0], "item") if oai else []:
            url = (item.text or "").strip().split("?")[0]
            if url.startswith(("http://", "https://")) and host_de(url) not in ("dspace.org", "localhost"):
                endpoints.append(url)
        if not endpoints:
            continue
        pagina = texto_hijo(registro, "home_page") or ""
        for host in {host_de(pagina), *(host_de(e) for e in endpoints)} - {""}:
            lista = salida.setdefault(host.removeprefix("www."), [])
            lista.extend(e for e in endpoints if e not in lista)
    return salida


def url_de_handle(r: Respuesta) -> str | None:
    """La URL a la que apunta un handle, según la API de handle.net (`/api/handles/<handle>?type=URL`)."""
    datos = parsear_json(r.cuerpo)
    if not isinstance(datos, dict):
        return None
    for valor in datos.get("values") or []:
        if (valor.get("type") or "").upper() == "URL":
            url = (valor.get("data") or {}).get("value")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
    return None


def ubicacion_desde_handle(previa: Ubicacion, url: str) -> Ubicacion:
    """Completa una ubicación `solo_handle` con la URL resuelta del handle."""
    host = host_de(url)
    if m := _OJS_RUTA.match(url):
        base = m.group("base") + ("/index.php/" if "/index.php/" in url else "/") + m.group("revista")
        return Ubicacion(previa.pais, previa.namespace, url, host, base, "ojs", handle=previa.handle, registros=previa.registros)
    return Ubicacion(previa.pais, previa.namespace, url, host, raiz(url), handle=previa.handle, registros=previa.registros)


def _sin_www(host: str) -> str:
    return host.removeprefix("www.")


def _lista(valor: Any) -> list[Any]:
    if valor is None:
        return []
    return valor if isinstance(valor, list) else [valor]


def _primero(valor: Any) -> str | None:
    lista = _lista(valor)
    return str(lista[0]) if lista else None


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
