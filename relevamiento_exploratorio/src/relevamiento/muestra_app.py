"""Muestra para probar la app del descargador (pedida por el usuario, nov 2026).

La muestra de `muestra-*` sirvió para la prueba de concurrencia, pero solo tenía repositorios que
anduvieron y no seguía la distribución real de tamaños. Esta toma los candidatos ya sorteados (más los
que suma `muestra-elegir --ampliar` hasta el 20 % de cada estrato chico) y arma la entrada que va a
recibir la app: un listado de ítems por repositorio, proporcional a sus documentos.

- **Fallas reales sí, nuestras no.** Un repositorio que falló por anti-bot, caída, 403, 503 u OAI roto
  queda: la app los tiene que manejar. Uno que falló por cómo lo buscamos (no ubicado en el buscador,
  OAI no encontrado, URL base mal deducida) queda afuera, en `excluidos.csv`, para reemplazarlo o
  corregirlo con el endpoint del IBICT.
- **Ítems:** `objetivo_items` por repositorio. Primero los ya cosechados por OAI (repartidos en el
  tiempo); lo que falte, del buscador de LA Referencia, que tiene exactamente los documentos del listado
  (también de los repositorios que no responden).

`muestra-app` pide al buscador (un solo host, con la cortesía de siempre) y se puede cortar y relanzar;
`muestra-app-resumir` escribe `muestra_app/` sin pedidos.
"""

import csv
import json
import math
import traceback
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from relevamiento.config import (
    ACEPTAR_JSON,
    BUSCADOR_LA_REFERENCIA,
    ESTRATOS_MUESTRA,
    FRACCION_ITEMS_APP,
    PISO_ITEMS_APP,
    REGISTROS_PAGINA_BUSCADOR,
    ConfigCortesia,
)
from relevamiento.cortesia import Cortesia, SinPedido
from relevamiento.modelos import Contexto
from relevamiento.muestra import (
    COSECHA,
    POBLACION,
    Candidata,
    _corrida,
    _escribir_csv,
    leer_candidatos,
    leer_ibict,
    leer_ubicaciones,
)
from relevamiento.parseo import host_de, parsear_json
from relevamiento.registro import EVENTOS, PEDIDOS, Registro, leer_jsonl

BUSCADOR = "buscador"
"""Subdirectorio de la muestra con los pedidos al buscador de `muestra-app`."""
SALIDA_APP = "muestra_app"
_RESOLUTORES = frozenset({"hdl.handle.net", "doi.org", "dx.doi.org", "www.doi.org"})
_RED = frozenset({"red", "timeout_conexion", "timeout_lectura", "tls_protocolo", "tls_invalido"})

# Causas nuestras: la app nunca las vería, porque recibe el listado de LA Referencia con su endpoint.
NUESTRAS = frozenset({"no_ubicado", "oai_no_encontrado", "url_mal_deducida"})


# --- Clasificación de fallas -------------------------------------------------------------


def clasificar_falla(estado: str | None, motivo: str | None, pedidos: list[dict[str, Any]],
                     ubicacion: dict[str, Any], endpoint_ibict: str | None) -> str:
    """La causa de una cosecha que no dio ítems, a partir de su último intento (`pedidos`, en orden).

    Con el endpoint confirmado por el IBICT, una falla siempre es del repositorio."""
    motivo = motivo or ""
    codigos = Counter(str(p.get("codigo")) for p in pedidos if p.get("codigo"))
    errores = Counter(p.get("error") for p in pedidos if p.get("error"))
    probo_ibict = bool(endpoint_ibict) and any(str(p.get("url", "")).startswith(endpoint_ibict) for p in pedidos)
    if estado == "antibot" or "anti-bot" in motivo:
        return "antibot"
    if estado == "sin_ubicacion" or (estado is None and (not ubicacion or ubicacion.get("problema"))):
        return "no_ubicado"
    if estado == "sin_oai":
        if probo_ibict:
            return "oai_roto"
        if (codigos["403"] or codigos["468"]) and not codigos["200"]:
            return "rechazo_403"
        inicio_ok = any(p.get("metodo") == "inicio" and 200 <= int(p.get("codigo") or 0) < 400 for p in pedidos)
        return "oai_no_encontrado" if inicio_ok else "sitio_con_error"
    if estado == "abandono":
        if "503" in motivo:
            return "servicio_no_disponible"
        if "429" in motivo:
            return "pide_esperar"
        if "403" in motivo:
            return "rechazo_403"
        solo_red = bool(errores) and set(errores) <= _RED and not any(200 <= int(c) < 400 for c in codigos if c.isdigit())
        ejemplo = host_de(ubicacion.get("url_ejemplo") or "").removeprefix("www.")
        deducido = (ubicacion.get("host") or "").removeprefix("www.")
        if solo_red and not probo_ibict and ejemplo and ejemplo not in _RESOLUTORES and ejemplo != deducido:
            return "url_mal_deducida"
        # Sin pedidos propios: el servidor ya estaba abandonado por otro repositorio que comparte host.
        return "caido" if solo_red or errores or "red" in motivo else "abandono"
    return {"sin_items": "oai_vacio", "sin_formato": "sin_formato", "robots": "robots"}.get(estado or "", estado or "pendiente")


def estados(dir_base: Path) -> dict[str, dict[str, Any]]:
    """Por candidato: estado de su última cosecha (`ok`, una causa de falla o `pendiente`) y si es nuestra."""
    candidatas = leer_candidatos(dir_base)
    ubicaciones = leer_ubicaciones(dir_base)
    ibict = leer_ibict(dir_base)
    fin = {e["repo"]: e for e in leer_jsonl(dir_base / COSECHA / EVENTOS) if e.get("tipo") == "repo_terminado"}
    por_corrida: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for p in leer_jsonl(dir_base / COSECHA / PEDIDOS):
        por_corrida[(p["repo"].split(".")[0], p["corrida"])].append(p)
    salida = {}
    for c in candidatas:
        u, f = ubicaciones.get(c.id) or {}, fin.get(c.id)
        if f and f.get("estado") == "ok":
            causa = "ok"
        elif f is None and u and not u.get("problema"):
            causa = "pendiente"
        else:
            pedidos = por_corrida.get((c.id, f["corrida"]), []) if f else []
            causa = clasificar_falla(f.get("estado") if f else None, f.get("motivo") if f else None, pedidos, u,
                                     (ibict.get(c.id) or {}).get("endpoint"))  # fmt: skip
        salida[c.id] = {"estado": causa, "nuestra": causa in NUESTRAS, "motivo": (f or {}).get("motivo") or u.get("problema")}
    return salida


# --- Ítems por repositorio ---------------------------------------------------------------


def objetivo_items(documentos: int) -> int:
    """Ítems de un repositorio en la muestra: el 2 % de sus documentos, al menos 200 (o todos si tiene menos)."""
    return min(documentos, max(PISO_ITEMS_APP, math.ceil(FRACCION_ITEMS_APP * documentos)))


def repartir(items: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """`n` ítems repartidos a lo largo de la lista ordenada por fecha (no los primeros `n`)."""
    if len(items) <= n:
        return list(items)
    orden = sorted(items, key=lambda i: (i.get("datestamp") or "", i["item"]))
    return [orden[(k * len(orden)) // n] for k in range(n)]


def url_buscador_items(nombre: str, pagina: int, limite: int = REGISTROS_PAGINA_BUSCADOR) -> str:
    """Una página de los registros de un repositorio en el buscador de LA Referencia."""
    valor = nombre.replace("\\", "\\\\").replace('"', '\\"')
    params = [
        ("lookfor", ""), ("type", "AllFields"), ("limit", str(limite)), ("page", str(pagina)),
        ("field[]", "rawData"), ("filter[]", f'reponame_str:"{valor}"'),
    ]  # fmt: skip
    return f"{BUSCADOR_LA_REFERENCIA}?{urlencode(params)}"


def items_de_buscador(cuerpo: bytes) -> tuple[int | None, list[dict[str, Any]]]:
    """`resultCount` y los ítems (identificador OAI, URLs, país, fecha) de una respuesta del buscador."""
    datos = parsear_json(cuerpo)
    if not isinstance(datos, dict):
        return None, []
    items = []
    for registro in datos.get("records") or []:
        crudo = (registro or {}).get("rawData") or {}
        ident = crudo.get("oai_identifier_str")
        if isinstance(ident, list):
            ident = ident[0] if ident else None
        urls = crudo.get("url") or []
        urls = [urls] if isinstance(urls, str) else [u for u in urls if isinstance(u, str)]
        if ident:
            items.append({"item": ident, "urls": urls, "pais": crudo.get("network_acronym_str"),
                          "fecha": str(crudo.get("publishDateSort") or crudo.get("publishDate") or "")})  # fmt: skip
    total = datos.get("resultCount")
    return (int(total) if isinstance(total, int | str) and str(total).isdigit() else None), items


def paginas_repartidas(total: int, necesarias: int, limite: int = REGISTROS_PAGINA_BUSCADOR) -> list[int]:
    """`necesarias` números de página (desde 1) repartidos entre las `ceil(total / limite)` que hay."""
    hay = max(1, math.ceil(total / limite))
    necesarias = min(necesarias, hay)
    return sorted({1 + (k * hay) // necesarias for k in range(necesarias)})


# --- Plan y pedidos al buscador --------------------------------------------------------------


def _oai_por_repo(dir_base: Path) -> dict[str, list[dict[str, Any]]]:
    """Los ítems OAI ya cosechados (items.csv de `muestra-resumir`), por repositorio."""
    salida: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ruta = dir_base / "items.csv"
    if ruta.exists():
        with open(ruta, encoding="utf-8", newline="") as f:
            for fila in csv.DictReader(f):
                salida[fila["repo"]].append(fila)
    return salida


def _del_buscador(dir_base: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int | None]]:
    """Lo ya pedido al buscador: ítems por repositorio (sin repetir) y su `resultCount`."""
    items: dict[str, dict[str, Any]] = defaultdict(dict)
    totales: dict[str, int | None] = {}
    for e in leer_jsonl(dir_base / BUSCADOR / EVENTOS):
        if e.get("tipo") == "pagina_buscador":
            totales[e["repo"]] = e.get("total")
            for i in e.get("items", []):
                items[e["repo"]].setdefault(i["item"], i)
    return items, totales


def plan(dir_base: Path) -> list[dict[str, Any]]:
    """Por candidato (salvo pendientes): estado, objetivo de ítems y cuántos salen de OAI y del buscador."""
    est = estados(dir_base)
    oai = _oai_por_repo(dir_base)
    buscados, _ = _del_buscador(dir_base)
    filas = []
    for c in leer_candidatos(dir_base):
        e = est[c.id]
        if e["estado"] == "pendiente":
            continue
        objetivo = objetivo_items(c.documentos)
        de_oai = min(len(oai.get(c.id, [])), objetivo) if e["estado"] == "ok" else 0
        filas.append({"candidata": c, **e, "objetivo": objetivo, "de_oai": de_oai,
                      "faltan": 0 if e["nuestra"] else max(0, objetivo - de_oai - len(buscados.get(c.id, {})))})  # fmt: skip
    return filas


async def pedir_al_buscador(
    dir_base: Path,
    contacto: str,
    cfg: ConfigCortesia,
    *,
    transporte: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Pide al buscador los ítems que faltan, de a un repositorio (un solo host). Primero la página 1, que da
    el `resultCount`; después páginas repartidas en todo el resultado. Si el buscador no da una página
    profunda, sigue con las primeras. Se puede cortar y relanzar."""
    dir_salida = dir_base / BUSCADOR
    pendientes = [f for f in plan(dir_base) if f["faltan"] > 0]
    previos = list(leer_jsonl(dir_salida / PEDIDOS))
    intentos = Counter(e["repo"] for e in leer_jsonl(dir_salida / EVENTOS) if e.get("tipo") == "buscador_inicio")
    registro = Registro(dir_salida, _corrida())
    nuevos = 0
    try:
        async with Cortesia(cfg, registro, contacto, transporte=transporte) as cortesia:
            cortesia.sembrar(previos, {})
            registro.evento("corrida", user_agent=cfg.user_agent(contacto), config=asdict(cfg), repositorios=len(pendientes))
            for fila in pendientes:
                c: Candidata = fila["candidata"]
                try:
                    nuevos += await _un_repo(c, fila["faltan"], cortesia, registro, f"{c.id}.b{intentos[c.id]}")
                except SinPedido as exc:
                    registro.evento("buscador_corte", repo=c.id, motivo=exc.motivo)
                    if "abandonado" in exc.motivo:
                        break  # el buscador entero quedó abandonado
                except Exception:  # un error de programación no debe frenar al resto, pero queda registrado
                    registro.evento("error_interno", repo=c.id, detalle=traceback.format_exc())
    finally:
        registro.cerrar()
    return {"repositorios": len(pendientes), "items_nuevos": nuevos}


async def _un_repo(c: Candidata, faltan: int, cortesia: Cortesia, registro: Registro, cuenta: str) -> int:
    registro.evento("buscador_inicio", repo=c.id, cuenta=cuenta, faltan=faltan)
    vistos: set[str] = set()

    async def pagina(n: int) -> tuple[int | None, int]:
        url = url_buscador_items(c.repositorio, n)
        r = await cortesia.pedir(url, Contexto(repo=cuenta, paso="buscador", metodo="buscador_items", item=str(n)),
                                 tipo="pagina", aceptar=ACEPTAR_JSON)  # fmt: skip
        total, items = items_de_buscador(r.cuerpo) if r.exitosa else (None, [])
        nuevos = [i for i in items if i["item"] not in vistos]
        vistos.update(i["item"] for i in nuevos)
        registro.evento("pagina_buscador", repo=c.id, pagina=n, codigo=r.codigo, total=total, items=nuevos)
        return total, len(nuevos)

    total, _ = await pagina(1)
    if not total:
        return len(vistos)
    necesarias = math.ceil(faltan / REGISTROS_PAGINA_BUSCADOR) + 1  # una de más: las de OAI pueden repetirse
    resto = [p for p in paginas_repartidas(total, necesarias) if p != 1]
    siguiente_corrida = 2
    for p in resto:
        if len(vistos) >= faltan:
            break
        _, n = await pagina(p)
        if n == 0 and p > siguiente_corrida:
            # Sin paginación profunda: las páginas que siguen a la 1, en orden.
            while len(vistos) < faltan and siguiente_corrida <= math.ceil(total / REGISTROS_PAGINA_BUSCADOR):
                _, n = await pagina(siguiente_corrida)
                siguiente_corrida += 1
                if n == 0:
                    break
            break
    return len(vistos)


# --- Salida ------------------------------------------------------------------------------


def resumir_muestra_app(dir_base: Path, salida: Path) -> dict[str, Any]:
    """Escribe `salida/` (repositorios.csv, items.csv, archivos.csv, excluidos.csv, resumen.json y .md) sin pedidos."""
    poblacion = json.loads((dir_base / POBLACION).read_text(encoding="utf-8"))
    ubicaciones = leer_ubicaciones(dir_base)
    ibict = leer_ibict(dir_base)
    repos_previos = {}
    ruta = dir_base / "repositorios.csv"
    if ruta.exists():
        with open(ruta, encoding="utf-8", newline="") as f:
            repos_previos = {r["id"]: r for r in csv.DictReader(f)}
    archivos_oai: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    if (dir_base / "archivos.csv").exists():
        with open(dir_base / "archivos.csv", encoding="utf-8", newline="") as f:
            for a in csv.DictReader(f):
                archivos_oai[(a["repo"], a["item"])].append(a)
    oai = _oai_por_repo(dir_base)
    buscados, totales = _del_buscador(dir_base)
    filas_plan = plan(dir_base)

    incluidos = [f for f in filas_plan if not f["nuestra"]]
    excluidos = [f for f in filas_plan if f["nuestra"]]
    en_muestra = Counter(f["candidata"].estrato for f in incluidos)
    repos, items, archivos = [], [], []
    for f in incluidos:
        c: Candidata = f["candidata"]
        u, p, ib = ubicaciones.get(c.id) or {}, repos_previos.get(c.id) or {}, ibict.get(c.id) or {}
        elegidos = repartir(oai.get(c.id, []), f["objetivo"]) if f["estado"] == "ok" else []
        ya = {i["item"] for i in elegidos}
        for i in elegidos:
            items.append({"repo": c.id, "item": i["item"], "url": i["pagina"], "fuente": "oai", "fecha": i.get("datestamp", "")})
            for a in archivos_oai.get((c.id, i["item"]), []):
                archivos.append({"repo": c.id, "item": i["item"], "url": a["url"], "tamano": a["tamano"], "mime": a["mime"]})
        del_buscador = [i for k, i in buscados.get(c.id, {}).items() if k not in ya][: max(0, f["objetivo"] - len(elegidos))]
        for i in del_buscador:
            urls = [x for x in i["urls"] if host_de(x) not in _RESOLUTORES] or i["urls"]
            items.append({"repo": c.id, "item": i["item"], "url": urls[0] if urls else "", "fuente": "buscador", "fecha": i.get("fecha", "")})
        pais = u.get("pais") or next((i.get("pais") for i in buscados.get(c.id, {}).values() if i.get("pais")), None)
        repos.append({
            "id": c.id, "repositorio": c.repositorio, "estrato": c.estrato,
            "peso": round(poblacion[c.estrato]["repositorios"] / en_muestra[c.estrato], 3),
            "documentos": c.documentos, "pais": pais, "estado": f["estado"], "motivo": f["motivo"],
            "plataforma": p.get("plataforma"), "version": p.get("version"), "oai_endpoint": p.get("oai_endpoint"),
            "oai_fuente": p.get("oai_fuente"), "formato": p.get("formato"), "host": p.get("host") or u.get("host"),
            "endpoint_ibict": ib.get("endpoint"), "software_ibict": ib.get("software"),
            "items_objetivo": f["objetivo"], "items_oai": len(elegidos), "items_buscador": len(del_buscador),
            "resultados_buscador": totales.get(c.id),
        })  # fmt: skip
    salida.mkdir(parents=True, exist_ok=True)
    columnas = list(repos[0]) if repos else ["id"]
    _escribir_csv(salida / "repositorios.csv", columnas, repos)
    _escribir_csv(salida / "items.csv", ["repo", "item", "url", "fuente", "fecha"], items)
    _escribir_csv(salida / "archivos.csv", ["repo", "item", "url", "tamano", "mime"], archivos)
    _escribir_csv(salida / "excluidos.csv", ["id", "repositorio", "estrato", "documentos", "estado", "motivo"],
                  [{"id": f["candidata"].id, "repositorio": f["candidata"].repositorio, "estrato": f["candidata"].estrato,
                    "documentos": f["candidata"].documentos, "estado": f["estado"], "motivo": f["motivo"]} for f in excluidos])  # fmt: skip

    resumen = _resumen(repos, items, excluidos, poblacion)
    (salida / "resumen.json").write_text(json.dumps(resumen, indent=1, ensure_ascii=False), encoding="utf-8")
    (salida / "resumen.md").write_text(_markdown(resumen), encoding="utf-8")
    return resumen


def _resumen(repos: list[dict[str, Any]], items: list[dict[str, Any]], excluidos: list[dict[str, Any]],
             poblacion: dict[str, dict[str, int]]) -> dict[str, Any]:
    por_repo = Counter(i["repo"] for i in items)
    total_pob = sum(v["repositorios"] for v in poblacion.values())
    estratos = {}
    for nombre, *_ in ESTRATOS_MUESTRA:
        de_e = [r for r in repos if r["estrato"] == nombre]
        estratos[nombre] = {
            "poblacion": poblacion[nombre]["repositorios"], "pct_poblacion": round(100 * poblacion[nombre]["repositorios"] / total_pob, 1),
            "en_muestra": len(de_e), "peso": de_e[0]["peso"] if de_e else None,
            "items": sum(por_repo[r["id"]] for r in de_e),
            "excluidos": sum(1 for f in excluidos if f["candidata"].estrato == nombre),
        }  # fmt: skip
    peso_total = sum(r["peso"] for r in repos) or 1
    vals = sorted(por_repo.values()) or [0]
    return {
        "repositorios": len(repos), "items": len(items), "excluidos": len(excluidos),
        "items_por_fuente": dict(Counter(i["fuente"] for i in items)),
        "items_por_repo": {"mediana": vals[len(vals) // 2], "maximo": vals[-1], "minimo": vals[0]},
        "estratos": estratos,
        "estratos_ponderados_pct": {e: round(100 * sum(r["peso"] for r in repos if r["estrato"] == e) / peso_total, 1) for e in estratos},
        "estados": dict(Counter(r["estado"] for r in repos).most_common()),
        "paises": dict(Counter(r["pais"] or "?" for r in repos).most_common()),
        "plataformas": dict(Counter(r["plataforma"] or r["software_ibict"] or "?" for r in repos).most_common()),
        "con_endpoint_ibict": sum(1 for r in repos if r["endpoint_ibict"]),
    }  # fmt: skip


def _markdown(r: dict[str, Any]) -> str:
    lineas = [
        "# Muestra para la app",
        "",
        f"{r['repositorios']} repositorios y {r['items']:,} ítems ({', '.join(f'{k}: {v:,}' for k, v in r['items_por_fuente'].items())}). "
        f"Ítems por repositorio: mediana {r['items_por_repo']['mediana']}, máximo {r['items_por_repo']['maximo']}. "
        f"{r['excluidos']} candidatos excluidos por fallas nuestras (excluidos.csv). {r['con_endpoint_ibict']} con endpoint del IBICT.",
        "",
        "| Estrato | Población | % población | En la muestra | Peso | % ponderado | Ítems | Excluidos |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e, d in r["estratos"].items():
        lineas.append(f"| {e} | {d['poblacion']} | {d['pct_poblacion']} | {d['en_muestra']} | {d['peso']} | "
                      f"{r['estratos_ponderados_pct'][e]} | {d['items']:,} | {d['excluidos']} |")  # fmt: skip
    for titulo, clave in (("Estado conocido", "estados"), ("Países", "paises"), ("Plataformas", "plataformas")):
        lineas += ["", f"## {titulo}", "", *[f"- {k}: {v}" for k, v in r[clave].items()]]
    return "\n".join(lineas) + "\n"
