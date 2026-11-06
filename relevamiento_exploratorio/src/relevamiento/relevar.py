"""Orquestación: qué pedir, en qué orden y cuándo cortar. Todos los pedidos pasan por la cortesía."""

import asyncio
import traceback
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from relevamiento.config import (
    ACEPTAR_JSON,
    ACEPTAR_XML,
    FORMATOS_OAI_METODO,
    DIAS_MUESTREO_RECIENTE,
    ITEMS_POR_REPO,
    MAX_SONDEOS_OAI,
    RUTAS_OAI,
    ConfigCortesia,
)
from relevamiento.cortesia import Cortesia, SinPedido
from relevamiento.detectores import (
    clasificar_url,
    combinar,
    detectar_cdn,
    detectar_generator,
    detectar_por_html,
    detectar_por_identify,
    detectar_por_rest7,
    detectar_por_rest56,
    es_archivo,
    es_dspace7,
    extraer_rest_base_dspace7,
    magic,
    nombre_archivo,
    parsear_formatos_oai,
)
from relevamiento.extractores import (
    derivar_oai_id,
    es_derivado,
    extraer_directo,
    extraer_oai_mets,
    extraer_oai_ore,
    extraer_oai_xoai,
    extraer_pagina,
    extraer_resolucion,
    extraer_rest7_bitstreams,
    extraer_rest7_bundles,
    extraer_rest7_item,
    extraer_rest56,
    metodos_aplicables,
    muestrear_listrecords,
    url_pedible,
    url_rest7_bundles,
)
from relevamiento.modelos import Candidato, Contexto, Deteccion, Item, Reconocimiento, Repositorio, Respuesta
from relevamiento.parseo import host_de, normalizar_url, raiz
from relevamiento.registro import EVENTOS, PEDIDOS, Registro, leer_jsonl

_EXTRACTORES_OAI = {"oai_xoai": extraer_oai_xoai, "oai_ore": extraer_oai_ore, "oai_mets": extraer_oai_mets}


@dataclass
class ResultadoMetodo:
    resultado: str  # ok | sin archivos | error | anti-bot | no aplica | no probado
    nota: str | None = None
    pedidos: list[str] = field(default_factory=list)
    candidatos: list[Candidato] = field(default_factory=list)


# --- Rutas a sondear (puras) -----------------------------------------------------------


def rutas_oai(url_base: str, pista: Deteccion | None, rest7_base: str | None) -> list[str]:
    base, r = url_base.rstrip("/"), raiz(url_base)
    plataforma = pista.plataforma if pista else None
    if plataforma in ("ojs", "omp"):
        rutas = [f"{base}/oai", f"{r}/index.php/index/oai"] if "/index.php/" in base + "/" else [
            f"{r}/index.php/index/oai", f"{base}/oai"]
    elif plataforma == "eprints":
        rutas = [r + p for p in RUTAS_OAI["eprints"]]
    elif pista and es_dspace7(pista.plataforma, pista.version, pista.variante):
        rutas = ([f"{rest7_base}/oai/request"] if rest7_base else []) + [r + p for p in RUTAS_OAI["dspace7"]]
    elif plataforma == "dspace":
        rutas = [r + p for p in RUTAS_OAI["dspace"]]
    else:
        rutas = [r + p for p in RUTAS_OAI["desconocida"]]
    return list(dict.fromkeys(rutas))[:MAX_SONDEOS_OAI]


def rutas_rest(url_base: str, pista: Deteccion | None, rest7_base: str | None) -> list[tuple[str, str, str]]:
    """Sondeos de la REST de DSpace: (tipo, url a pedir, base de la REST)."""
    r = raiz(url_base)
    base7 = rest7_base or f"{r}/server"
    v7 = ("v7", f"{base7}/api", base7)
    v56 = ("v5_6", f"{r}/rest/status", f"{r}/rest")
    if pista is None or pista.plataforma is None:
        return [v7, v56]
    if pista.plataforma != "dspace":
        return []
    if es_dspace7(pista.plataforma, pista.version, pista.variante):
        return [v7]
    if pista.version or pista.variante in ("xmlui", "jspui"):
        return [v56]
    return [v7, v56]


def plan(repos: list[Repositorio], items: dict[str, list[Item]]) -> list[str]:
    """Simulacro sin pedidos: lo que se pediría en el reconocimiento si no se supiera nada de la plataforma."""
    lineas = []
    for repo in repos:
        lineas.append(f"[{repo.id}] {repo.nombre} — {repo.url_base}")
        lineas.append(f"  robots.txt: {raiz(repo.url_base)}/robots.txt")
        lineas.append(f"  inicio:     {repo.url_base}")
        for ruta in [repo.oai_endpoint] if repo.oai_endpoint else rutas_oai(repo.url_base, None, None):
            lineas.append(f"  OAI:        {ruta}?verb=Identify")
        for _tipo, url, _base in rutas_rest(repo.url_base, None, None):
            lineas.append(f"  REST:       {url}")
        if items.get(repo.id):
            lineas.extend(f"  ítem:       {i.url}" for i in items[repo.id])
        else:
            lineas.append(f"  ítems:      {ITEMS_POR_REPO} de ListRecords en oai_dc (último año; si no hay, sin filtro)")
    return lineas


# --- Un repositorio --------------------------------------------------------------------


class Relevamiento:
    def __init__(self, cortesia: Cortesia, registro: Registro):
        self.c = cortesia
        self.registro = registro

    async def _pedir(self, url: str, ctx: Contexto, **kw: Any) -> Respuesta | SinPedido:
        try:
            return await self.c.pedir(url, ctx, **kw)
        except SinPedido as exc:
            return exc

    async def repo(self, repo: Repositorio, items_csv: list[Item]) -> None:
        rec = await self.reconocer(repo)
        self.registro.evento("reconocimiento", repo=repo.id, **asdict(rec))
        if items_csv:
            items, fuente = items_csv, "csv"
        else:
            items, fuente = await self._muestrear(repo, rec)
        self.registro.evento("items", repo=repo.id, fuente=fuente, items=[asdict(i) for i in items])
        verificadas: dict[str, dict[str, Any]] = {}
        for item in items:
            await self.item(repo, rec, item, verificadas)
        usados = self.c.cfg.pedidos_por_repo - self.c.restantes(repo.id)
        if self.c.abandono(host_de(repo.url_base)):
            motivo = "abandono"
        elif not items:
            motivo = "sin_items"
        elif self.c.restantes(repo.id) <= 0:
            motivo = "presupuesto"
        else:
            motivo = "completo"
        self.registro.evento("repo_terminado", repo=repo.id, motivo_fin=motivo, pedidos=usados)

    # --- Reconocimiento ----------------------------------------------------------------

    async def reconocer(self, repo: Repositorio) -> Reconocimiento:
        rec = Reconocimiento()

        def ctx(metodo: str) -> Contexto:
            return Contexto(repo=repo.id, paso="reconocimiento", metodo=metodo)

        rest7_ssr = None
        r = await self._pedir(repo.url_base, ctx("inicio"))
        if isinstance(r, SinPedido):
            rec.no_probado["inicio"] = r.motivo
        else:
            rec.antibot, rec.cdn = r.antibot, detectar_cdn(r)
            rec.detecciones += [d for d in (detectar_generator(r), detectar_por_html(r)) if d]
            rest7_ssr = extraer_rest_base_dspace7(r)

        endpoints = [repo.oai_endpoint] if repo.oai_endpoint else rutas_oai(repo.url_base, combinar(rec.detecciones), rest7_ssr)
        for endpoint in endpoints:
            r = await self._pedir(f"{endpoint}?verb=Identify", ctx("oai_identify"), aceptar=ACEPTAR_XML)
            if isinstance(r, SinPedido):
                rec.no_probado["oai_identify"] = r.motivo
                break
            if identificado := detectar_por_identify(r):
                datos, det = identificado
                rec.oai_endpoint, rec.oai_fuente = endpoint, "csv" if repo.oai_endpoint else "sondeo"
                rec.identificador_oai = datos.identificador_repositorio
                if det:
                    rec.detecciones.append(det)
                break
        if rec.oai_endpoint:
            r = await self._pedir(f"{rec.oai_endpoint}?verb=ListMetadataFormats", ctx("oai_formatos"), aceptar=ACEPTAR_XML)
            if isinstance(r, SinPedido):
                rec.no_probado["oai_formatos"] = r.motivo
            else:
                rec.oai_formatos = parsear_formatos_oai(r)

        for tipo, url, base in rutas_rest(repo.url_base, combinar(rec.detecciones), rest7_ssr):
            r = await self._pedir(url, ctx(f"rest_{tipo}"), aceptar=ACEPTAR_JSON)
            if isinstance(r, SinPedido):
                rec.no_probado[f"rest_{tipo}"] = r.motivo
                break
            if det := (detectar_por_rest7(r) if tipo == "v7" else detectar_por_rest56(r)):
                rec.rest, rec.rest_base = tipo, base
                rec.detecciones.append(det)
                break

        if final := combinar(rec.detecciones):
            rec.plataforma, rec.version, rec.variante, rec.fuente_deteccion = (
                final.plataforma, final.version, final.variante, final.fuente)
        return rec

    async def _muestrear(self, repo: Repositorio, rec: Reconocimiento) -> tuple[list[Item], str]:
        """Ítems de ListRecords en oai_dc. Primero pide los modificados en el último año, porque la primera página
        sin filtro trae los registros más viejos (en el piloto, tesis sin archivos). Si no da ítems, repite sin filtro."""
        if not rec.oai_endpoint:
            return [], "ninguna"
        ctx = Contexto(repo=repo.id, paso="reconocimiento", metodo="oai_listrecords")
        desde = (self.c.reloj.ahora_utc() - timedelta(days=DIAS_MUESTREO_RECIENTE)).date().isoformat()
        base = f"{rec.oai_endpoint}?verb=ListRecords&metadataPrefix=oai_dc"
        for url, fuente in ((f"{base}&from={desde}", f"oai_listrecords_desde_{desde}"), (base, "oai_listrecords")):
            r = await self._pedir(url, ctx, aceptar=ACEPTAR_XML)
            if isinstance(r, SinPedido):
                rec.no_probado["oai_listrecords"] = r.motivo
                return [], "ninguna"
            if items := muestrear_listrecords(r, repo.id, repo.url_base, ITEMS_POR_REPO):
                return items, fuente
            if r.antibot:
                break
        return [], "ninguna"

    # --- Métodos por ítem --------------------------------------------------------------

    async def item(self, repo: Repositorio, rec: Reconocimiento, item: Item, verificadas: dict[str, dict]) -> None:
        def ctx(metodo: str, paso: str = "metodo") -> Contexto:
            return Contexto(repo=repo.id, paso=paso, metodo=metodo, item=item.url, item_idx=item.idx)

        tipo_url = clasificar_url(item.url)
        resolver = tipo_url in ("doi", "handle")
        url = url_pedible(item.url) or item.url
        r_item = await self._pedir(url, ctx("doi_handle" if resolver else "directo"))
        ids_item = list(r_item.pedido_ids)
        item_es_archivo = isinstance(r_item, Respuesta) and es_archivo(r_item)
        if tipo_url == "pagina" and item_es_archivo:
            tipo_url = "archivo"
        ext_pagina = extraer_pagina(r_item) if isinstance(r_item, Respuesta) and not item_es_archivo else None
        datos: dict[str, str] = dict(ext_pagina.datos) if ext_pagina else {}
        res: dict[str, ResultadoMetodo] = {}

        # Directo, página y DOI/handle comparten el pedido del ítem.
        falla = _falla(r_item)
        compartidos = ["doi_handle"] if resolver else ["directo", "pagina"]
        motivos = metodos_aplicables(rec, tipo_url, item_es_archivo, datos, derivar_oai_id(rec, item, datos))
        for metodo in ("directo", "pagina", "doi_handle"):
            if metodo not in compartidos:
                res[metodo] = ResultadoMetodo("no aplica", motivos[metodo])
            elif falla:
                res[metodo] = ResultadoMetodo(*falla, pedidos=ids_item)
            elif motivos[metodo]:
                res[metodo] = ResultadoMetodo("no aplica", motivos[metodo], pedidos=ids_item)
            else:
                assert isinstance(r_item, Respuesta)
                ext = {"directo": extraer_directo, "doi_handle": extraer_resolucion}.get(metodo)
                extraccion = ext(r_item) if ext else ext_pagina
                res[metodo] = _resultado_extraccion(extraccion, ids_item)
                if metodo == "doi_handle":
                    datos.update({k: v for k, v in extraccion.datos.items() if k in ("handle", "uuid", "doi")})

        oai_id = derivar_oai_id(rec, item, datos)
        motivos = metodos_aplicables(rec, tipo_url, item_es_archivo, datos, oai_id)
        for metodo, formato in FORMATOS_OAI_METODO.items():
            if motivos[metodo]:
                res[metodo] = ResultadoMetodo("no aplica", motivos[metodo])
                continue
            url_oai = f"{rec.oai_endpoint}?verb=GetRecord&metadataPrefix={formato}&identifier={quote(oai_id or '', safe=':/')}"
            r = await self._pedir(url_oai, ctx(metodo), aceptar=ACEPTAR_XML)
            if f := _falla(r):
                res[metodo] = ResultadoMetodo(*f, pedidos=list(r.pedido_ids))
                continue
            extraccion = _EXTRACTORES_OAI[metodo](r)
            res[metodo] = _resultado_extraccion(extraccion, list(r.pedido_ids))
            if "handle" not in datos and extraccion.datos.get("handle"):
                datos["handle"] = extraccion.datos["handle"]

        motivos = metodos_aplicables(rec, tipo_url, item_es_archivo, datos, oai_id)
        res["rest56"] = await self._rest56(rec, datos, ctx) if not motivos["rest56"] else ResultadoMetodo("no aplica", motivos["rest56"])
        res["rest7"] = await self._rest7(rec, datos, ctx) if not motivos["rest7"] else ResultadoMetodo("no aplica", motivos["rest7"])

        for metodo, resultado in res.items():
            self.registro.evento(
                "resultado", repo=repo.id, item_idx=item.idx, item_url=item.url, tipo_url=tipo_url, metodo=metodo,
                resultado=resultado.resultado, nota=resultado.nota, pedidos=resultado.pedidos,
                candidatos=[asdict(c) for c in resultado.candidatos],
            )
        await self._verificar(repo, item, res, r_item, verificadas, ctx)

    async def _rest56(self, rec: Reconocimiento, datos: dict[str, str], ctx) -> ResultadoMetodo:
        r = await self._pedir(f"{rec.rest_base}/handle/{datos['handle']}?expand=bitstreams", ctx("rest56"), aceptar=ACEPTAR_JSON)
        if f := _falla(r):
            return ResultadoMetodo(*f, pedidos=list(r.pedido_ids))
        return _resultado_extraccion(extraer_rest56(r), list(r.pedido_ids))

    async def _rest7(self, rec: Reconocimiento, datos: dict[str, str], ctx) -> ResultadoMetodo:
        pedidos: list[str] = []
        if datos.get("uuid"):
            url_bundles = url_rest7_bundles(rec.rest_base or "", datos["uuid"])
        else:
            r = await self._pedir(f"{rec.rest_base}/api/pid/find?id={quote(datos['handle'], safe='/')}", ctx("rest7"),
                                  aceptar=ACEPTAR_JSON)
            pedidos += r.pedido_ids
            if f := _falla(r):
                return ResultadoMetodo(*f, pedidos=pedidos)
            item = extraer_rest7_item(r)
            if item.problema:
                return ResultadoMetodo("error", item.problema, pedidos=pedidos)
            url_bundles = item.siguiente[0][0]
        r = await self._pedir(url_bundles, ctx("rest7"), aceptar=ACEPTAR_JSON)
        pedidos += r.pedido_ids
        if f := _falla(r):
            return ResultadoMetodo(*f, pedidos=pedidos)
        bundles = extraer_rest7_bundles(r)
        if bundles.problema:
            return ResultadoMetodo("error", bundles.problema, pedidos=pedidos)
        candidatos = list(bundles.candidatos)
        incompleto = None
        for url, bundle in bundles.siguiente:
            r = await self._pedir(url, ctx("rest7"), aceptar=ACEPTAR_JSON)
            pedidos += r.pedido_ids
            if f := _falla(r):
                incompleto = f"bitstreams de {bundle}: {f[1]}"
                if isinstance(r, SinPedido):
                    break
                continue
            candidatos += extraer_rest7_bitstreams(r, bundle).candidatos
        resultado = _resultado_extraccion(bundles, pedidos, candidatos)
        if incompleto:
            resultado.nota = f"incompleto: {incompleto}"
            if not candidatos:
                resultado.resultado = "no probado" if "agotado" in incompleto or "abandonado" in incompleto else "error"
        return resultado

    # --- Verificación ------------------------------------------------------------------

    async def _verificar(self, repo, item, res, r_item, verificadas, ctx) -> None:
        unidos: dict[str, dict[str, Any]] = {}
        for metodo, resultado in res.items():
            for c in resultado.candidatos:
                entrada = unidos.setdefault(normalizar_url(c.url), {"url": c.url, "metodos": set(), "bundle": None, "nombre": None})
                entrada["metodos"].add(metodo)
                entrada["bundle"] = entrada["bundle"] or c.bundle
                entrada["nombre"] = entrada["nombre"] or c.nombre
        orden = sorted(unidos.items(), key=lambda kv: (-len(kv[1]["metodos"]), kv[1]["bundle"] not in (None, "ORIGINAL")))
        urls_item = {normalizar_url(r_item.url), normalizar_url(r_item.url_inicial)} if isinstance(r_item, Respuesta) else set()
        ya_ok: dict[tuple[str, str | None], str] = {}
        for clave, entrada in orden:
            evento: dict[str, Any] = {
                "repo": repo.id, "item_idx": item.idx, "url": entrada["url"], "metodos": sorted(entrada["metodos"]),
                "bundle": entrada["bundle"], "nombre_candidato": entrada["nombre"], "verificado": False,
            }
            par = (entrada["nombre"], entrada["bundle"])
            if es_derivado(entrada["bundle"]):
                evento["motivo"] = "bundle derivado"
            elif entrada["nombre"] and par in ya_ok:
                evento["motivo"] = "mismo nombre y bundle que otro archivo verificado"
                evento["equivalente_a"] = ya_ok[par]
            elif clave in verificadas:
                evento.update(verificadas[clave], reutilizado=True)
            elif clave in urls_item and es_archivo(r_item):
                evento.update(_datos_verificacion(r_item), reutilizado=True)
            else:
                r = await self._pedir(entrada["url"], ctx("verificacion", "verificacion"), tipo="archivo")
                if isinstance(r, SinPedido):
                    evento.update(motivo=f"no probado: {r.motivo}", pedidos=list(r.pedido_ids))
                else:
                    evento.update(_datos_verificacion(r))
            if evento["verificado"]:
                verificadas[clave] = {k: evento[k] for k in _CAMPOS_VERIFICACION}
                if evento["ok"] and entrada["nombre"]:
                    ya_ok.setdefault(par, entrada["url"])
            self.registro.evento("verificacion", **evento)


_CAMPOS_VERIFICACION = (
    "verificado", "pedidos", "codigo", "error", "antibot", "content_type", "content_length", "total", "nombre", "magic", "ok",
)


def _datos_verificacion(r: Respuesta) -> dict[str, Any]:
    tipo = magic(r.cuerpo)
    return {
        "verificado": True,
        "pedidos": list(r.pedido_ids),
        "codigo": r.codigo,
        "error": r.error,
        "antibot": r.antibot,
        "content_type": r.tipo_contenido or None,
        "content_length": r.content_length,
        "total": r.total,
        "nombre": nombre_archivo(r),
        "magic": tipo,
        "ok": r.codigo in (200, 206) and not r.error and tipo not in (None, "html"),
    }


def _falla(r: Respuesta | SinPedido) -> tuple[str, str] | None:
    if isinstance(r, SinPedido):
        return "no probado", r.motivo
    if r.antibot:
        return "anti-bot", r.antibot
    if r.error:
        return "error", r.error
    if r.codigo is None or r.codigo >= 400:
        return "error", f"código {r.codigo}"
    return None


def _resultado_extraccion(extraccion, pedidos: list[str], candidatos: list[Candidato] | None = None) -> ResultadoMetodo:
    candidatos = list(extraccion.candidatos) if candidatos is None else candidatos
    if extraccion.problema and not candidatos:
        return ResultadoMetodo("error", extraccion.problema, pedidos=pedidos)
    return ResultadoMetodo("ok" if candidatos else "sin archivos", None, pedidos=pedidos, candidatos=candidatos)


# --- Corrida ---------------------------------------------------------------------------


def robots_previos(dir_salida: Path, pedidos: list[dict]) -> dict[str, tuple[int, bytes]]:
    """robots.txt ya resueltos en corridas anteriores, por host de origen de la cadena."""
    por_cadena: dict[str, list[dict]] = defaultdict(list)
    for p in pedidos:
        if p["metodo"] == "robots" and p.get("codigo") is not None:
            por_cadena[p["cadena"]].append(p)
    resultado = {}
    for saltos in por_cadena.values():
        saltos.sort(key=lambda p: p["salto"])
        ultimo = saltos[-1]
        ruta = dir_salida / ultimo["captura"] if ultimo.get("captura") else None
        cuerpo = ruta.with_suffix(".cuerpo").read_bytes() if ruta and ruta.with_suffix(".cuerpo").exists() else b""
        resultado[saltos[0]["host"]] = (ultimo["codigo"], cuerpo)
    return resultado


async def relevar(
    repos: list[Repositorio],
    items: dict[str, list[Item]],
    dir_salida: Path,
    contacto: str,
    cfg: ConfigCortesia = ConfigCortesia(),
    *,
    transporte: httpx.AsyncBaseTransport | None = None,
    transporte_sin_verificar: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    pedidos_previos = list(leer_jsonl(dir_salida / PEDIDOS))
    terminados = {e["repo"] for e in leer_jsonl(dir_salida / EVENTOS) if e["tipo"] == "repo_terminado"}
    pendientes = [r for r in repos if r.id not in terminados]
    corrida = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]
    registro = Registro(dir_salida, corrida)
    try:
        async with Cortesia(cfg, registro, contacto, transporte=transporte,
                            transporte_sin_verificar=transporte_sin_verificar) as cortesia:
            cortesia.sembrar(pedidos_previos, robots_previos(dir_salida, pedidos_previos))
            registro.evento("corrida", user_agent=cfg.user_agent(contacto), config=asdict(cfg),
                            pendientes=[r.id for r in pendientes], ya_terminados=sorted(terminados))
            relevamiento = Relevamiento(cortesia, registro)
            cola: asyncio.Queue[Repositorio] = asyncio.Queue()
            for r in pendientes:
                cola.put_nowait(r)

            async def trabajador() -> None:
                while not cola.empty():
                    repo = cola.get_nowait()
                    try:
                        await relevamiento.repo(repo, items.get(repo.id, []))
                    except Exception:  # un error de programación no debe frenar al resto, pero queda registrado
                        registro.evento("error_interno", repo=repo.id, detalle=traceback.format_exc())

            async with asyncio.TaskGroup() as grupo:
                for _ in range(min(cfg.hosts_en_paralelo, len(pendientes))):
                    grupo.create_task(trabajador())
    finally:
        registro.cerrar()
    return {"corrida": corrida, "relevados": len(pendientes), "ya_terminados": len(terminados)}
