"""Tamaño de los archivos desde los metadatos OAI (`medir-tamanos`).

Pedido por el usuario para la prueba de concurrencia: los pilotos vieron 3 ítems por repositorio,
y el tamaño medio de archivo salía de 51 archivos. Acá se piden páginas enteras de ListRecords en
METS (o xoai) de los repositorios de los pilotos que publican el tamaño de cada archivo, sin
descargar ningún archivo: una página sin fechas, que trae además cuántos ítems tiene el repositorio
(`completeListSize`), y una por ventana de fechas (`VENTANAS_TAMANOS`), para repartir la muestra en
el tiempo.

Todo pasa por el componente de cortesía, con `CORTESIA_TAMANOS`. Lo extraído de cada página queda
en `eventos.jsonl`; `resumir_tamanos` deriva de ahí `tamanos.csv` y `tamanos.json`, con el total
ponderado por la cantidad de ítems de cada repositorio.
"""

import asyncio
import csv
import json
import statistics
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from relevamiento.config import ACEPTAR_XML, VENTANAS_TAMANOS, ConfigCortesia
from relevamiento.cortesia import Cortesia, SinPedido
from relevamiento.extractores import extraer_pagina_tamanos
from relevamiento.modelos import Contexto
from relevamiento.parseo import host_de
from relevamiento.registro import EVENTOS, PEDIDOS, Registro, leer_jsonl

TAMANOS = "tamanos.csv"
RESUMEN = "tamanos.json"
_PREFIJOS = {"mets": "mets", "xoai": "xoai"}


@dataclass(frozen=True, slots=True)
class Repositorio:
    repo: str
    plataforma: str
    endpoint: str
    formato: str  # mets | xoai


def candidatos(pilotos: list[Path]) -> list[Repositorio]:
    """Repositorios de los pilotos con OAI en METS o xoai, sin los de un host abandonado.

    Si un repositorio aparece en varios pilotos, vale el último."""
    abandonados: set[str] = set()
    por_repo: dict[str, Repositorio] = {}
    for piloto in pilotos:
        for p in leer_jsonl(piloto / PEDIDOS):
            if p.get("abandono"):
                abandonados.add(p["host"])
        ruta = piloto / "repositorios.csv"
        if not ruta.exists():
            continue
        with open(ruta, encoding="utf-8") as f:
            for fila in csv.DictReader(f):
                endpoint = (fila.get("oai_endpoint") or "").strip()
                formatos = (fila.get("oai_formatos") or "").split()
                formato = next((f for f in ("mets", "xoai") if f in formatos), None)
                if fila.get("repo_id") and endpoint.startswith("http") and formato:
                    por_repo[fila["repo_id"]] = Repositorio(fila["repo_id"], fila.get("plataforma") or "", endpoint, formato)
    return sorted((r for r in por_repo.values() if host_de(r.endpoint) not in abandonados), key=lambda r: r.repo)


def urls(repo: Repositorio) -> list[tuple[str, str]]:
    """(etiqueta, URL) de cada página a pedir: la primera sin fechas y una por ventana."""
    return urls_listrecords(repo.endpoint, _PREFIJOS[repo.formato])


def urls_listrecords(endpoint: str, prefijo: str) -> list[tuple[str, str]]:
    """(etiqueta, URL) de la primera página de ListRecords sin fechas (`completa`) y de una por ventana."""
    salida = []
    for desde, hasta in [(None, None), *VENTANAS_TAMANOS]:
        params = {"verb": "ListRecords", "metadataPrefix": prefijo}
        if desde:
            params["from"] = desde
        if hasta:
            params["until"] = hasta
        etiqueta = "completa" if desde is None and hasta is None else f"{desde or '…'}/{hasta or '…'}"
        salida.append((etiqueta, f"{endpoint}?{urlencode(params)}"))
    return salida


async def medir_tamanos(repos: list[Repositorio], dir_salida: Path, contacto: str, cfg: ConfigCortesia) -> str:
    """Pide las páginas de cada repositorio con la cortesía de `cfg`. Devuelve el id de la corrida."""
    corrida = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]
    registro = Registro(dir_salida, corrida)
    registro.evento("corrida", user_agent=cfg.user_agent(contacto), config=asdict(cfg), repositorios=len(repos))
    try:
        async with Cortesia(cfg, registro, contacto) as cortesia:

            async def un_repo(repo: Repositorio) -> None:
                for i, (etiqueta, url) in enumerate(urls(repo)):
                    if cortesia.robots_permitido(url) is False:
                        registro.evento("no_medido", repo=repo.repo, url=url, motivo="robots")
                        return
                    ctx = Contexto(repo=repo.repo, paso="tamanos", metodo=f"oai_{repo.formato}", item=etiqueta, item_idx=i)
                    try:
                        r = await cortesia.pedir(url, ctx, tipo="pagina", aceptar=ACEPTAR_XML)
                    except SinPedido as exc:
                        registro.evento("no_medido", repo=repo.repo, url=url, motivo=str(exc))
                        return
                    pagina = extraer_pagina_tamanos(r, repo.formato) if r.exitosa else None
                    registro.evento(
                        "pagina_tamanos", repo=repo.repo, plataforma=repo.plataforma, formato=repo.formato,
                        ventana=etiqueta, url=url, codigo=r.codigo, error=r.error, truncado=r.truncado,
                        problema=pagina.problema if pagina else (r.error or f"http_{r.codigo}"),
                        total=pagina.total if pagina else None,
                        registros=[[g.identificador, g.borrado, [list(a) for a in g.archivos]] for g in pagina.registros]
                        if pagina else [],
                    )  # fmt: skip

            await asyncio.gather(*(un_repo(repo) for repo in repos))
    finally:
        registro.cerrar()
    return corrida


def _percentil(valores: list[float], pesos: list[float], q: float) -> float:
    """Percentil ponderado (el valor donde el peso acumulado llega a q)."""
    pares = sorted(zip(valores, pesos, strict=True))
    total, acumulado = sum(pesos), 0.0
    for valor, peso in pares:
        acumulado += peso
        if acumulado >= q * total:
            return valor
    return pares[-1][0]


def resumir_tamanos(dir_salida: Path) -> dict[str, Any]:
    """Escribe tamanos.csv (un archivo por fila) y devuelve el resumen por repositorio y ponderado.

    Ponderación: cada repositorio pesa según cuántos ítems tiene (`completeListSize` de su página
    sin fechas); dentro de un repositorio, todos sus ítems y archivos de la muestra pesan igual."""
    items: dict[str, dict[str, list]] = {}
    totales: dict[str, int] = {}
    plataformas: dict[str, str] = {}
    eventos = list(leer_jsonl(dir_salida / EVENTOS))
    # robots.txt llega con el primer pedido: si no permite la ruta, se descarta lo que ya se pidió.
    sin_permiso = {e["repo"] for e in eventos if e.get("tipo") == "no_medido" and e.get("motivo") == "robots"}
    for e in eventos:
        if e.get("tipo") != "pagina_tamanos" or e["repo"] in sin_permiso:
            continue
        repo = e["repo"]
        plataformas[repo] = e.get("plataforma", "")
        if e["ventana"] == "completa" and e.get("total"):
            totales[repo] = e["total"]
        por_id = items.setdefault(repo, {})
        for identificador, borrado, archivos in e.get("registros", []):
            if not borrado and identificador:
                por_id[identificador] = archivos
    filas = [
        {"repo": repo, "item": ident, "tamano": tamano, "mime": mime}
        for repo, por_id in sorted(items.items())
        for ident, archivos in por_id.items()
        for tamano, mime in archivos
        if tamano
    ]
    with open(dir_salida / TAMANOS, "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=["repo", "item", "tamano", "mime"])
        escritor.writeheader()
        escritor.writerows(filas)

    por_repo = {}
    for repo, por_id in sorted(items.items()):
        if not por_id:
            continue
        cantidades = [len([a for a in archivos if a[0]]) for archivos in por_id.values()]
        tamanos = [a[0] for archivos in por_id.values() for a in archivos if a[0]]
        por_repo[repo] = {
            "plataforma": plataformas.get(repo, ""),
            "items_repositorio": totales.get(repo),
            "items_muestra": len(por_id),
            "sin_archivos": round(sum(1 for c in cantidades if c == 0) / len(cantidades), 3),
            "archivos_por_item": round(statistics.fmean(cantidades), 3),
            "archivos": len(tamanos),
            "tamano_p50": statistics.median(tamanos) if tamanos else None,
            "tamano_p95": _percentil(tamanos, [1.0] * len(tamanos), 0.95) if tamanos else None,
            "tamano_max": max(tamanos, default=None),
            "tamano_media": round(statistics.fmean(tamanos)) if tamanos else None,
            "_cantidades": cantidades,
            "_tamanos": tamanos,
        }

    def total(repos: dict[str, dict], ponderado: bool) -> dict[str, Any]:
        """Por ítem pesa el repositorio según sus ítems; por archivo, además, según sus archivos por ítem."""
        con_peso = {r: d for r, d in repos.items() if d["items_muestra"] and (not ponderado or d["items_repositorio"])}
        if not con_peso:
            return {}
        peso_repo = {r: (d["items_repositorio"] if ponderado else d["items_muestra"]) for r, d in con_peso.items()}
        suma = sum(peso_repo.values())
        cantidades, pesos_item = [], []
        tamanos, pesos_archivo = [], []
        for r, d in con_peso.items():
            w = peso_repo[r] / suma
            cantidades += d["_cantidades"]
            pesos_item += [w / len(d["_cantidades"])] * len(d["_cantidades"])
            if d["_tamanos"]:
                # La parte de los archivos de la población que son de este repositorio.
                peso = w * d["archivos_por_item"] / len(d["_tamanos"])
                tamanos += d["_tamanos"]
                pesos_archivo += [peso] * len(d["_tamanos"])
        distribucion = Counter()
        for c, p in zip(cantidades, pesos_item, strict=True):
            distribucion[min(c, 5)] += p
        archivos_por_item = sum(c * p for c, p in zip(cantidades, pesos_item, strict=True))
        media = sum(t * p for t, p in zip(tamanos, pesos_archivo, strict=True)) / sum(pesos_archivo) if tamanos else None
        return {
            "repositorios": len(con_peso),
            "items": len(cantidades),
            "archivos": len(tamanos),
            "archivos_por_item": round(archivos_por_item, 3),
            "distribucion_archivos_por_item": {str(k): round(v, 4) for k, v in sorted(distribucion.items())},
            "tamano_p50": _percentil(tamanos, pesos_archivo, 0.5) if tamanos else None,
            "tamano_p90": _percentil(tamanos, pesos_archivo, 0.9) if tamanos else None,
            "tamano_p95": _percentil(tamanos, pesos_archivo, 0.95) if tamanos else None,
            "tamano_p99": _percentil(tamanos, pesos_archivo, 0.99) if tamanos else None,
            "tamano_max": max(tamanos, default=None),
            "tamano_media": round(media) if media else None,
            "bytes_por_item": round(archivos_por_item * media) if media else None,
        }

    resumen = {
        "excluidos_por_robots": sorted(sin_permiso),
        "ponderado_por_items": total(por_repo, ponderado=True),
        "sin_ponderar": total(por_repo, ponderado=False),
        "por_repositorio": {r: {k: v for k, v in d.items() if not k.startswith("_")} for r, d in por_repo.items()},
    }
    (dir_salida / RESUMEN).write_text(json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")
    return resumen
