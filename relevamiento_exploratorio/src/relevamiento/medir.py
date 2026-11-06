"""Medición de la velocidad de descarga por conexión (`medir-velocidad`).

Desvío pedido por el usuario para la prueba de concurrencia: los pilotos leyeron solo 2 KB de cada
archivo, así que nunca se midió a qué velocidad entregan un archivo los repositorios. Se vuelve a
pedir una muestra de archivos ya verificados en los pilotos, leyendo hasta 8 MB o 30 s de cada uno,
con una cortesía más estricta que la del relevamiento (`CORTESIA_MEDICION`).

Todo pasa por el componente de cortesía. La velocidad sale de `pedidos.jsonl`: bytes leídos sobre
el tiempo entre los encabezados y el último byte.
"""

import asyncio
import csv
import json
import re
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from relevamiento.config import ARCHIVOS_POR_REPO_MEDICION, TAMANO_MINIMO_MEDICION, ConfigCortesia
from relevamiento.cortesia import Cortesia, SinPedido
from relevamiento.modelos import Contexto
from relevamiento.registro import PEDIDOS, Registro, leer_jsonl

VELOCIDAD = "velocidad.csv"
MB = 1024 * 1024
_TOTAL = re.compile(r"/(\d+)$")


@dataclass(frozen=True, slots=True)
class Candidato:
    repo: str
    host: str
    url: str
    tamano: int


def _tamano(pedido: dict[str, Any]) -> int | None:
    coincide = _TOTAL.search(pedido.get("content_range") or "")
    if coincide:
        return int(coincide.group(1))
    largo = pedido.get("content_length") or ""
    return int(largo) if largo.isdigit() else None


def candidatos(
    pilotos: list[Path], por_repo: int = ARCHIVOS_POR_REPO_MEDICION, minimo: int = TAMANO_MINIMO_MEDICION
) -> list[Candidato]:
    """Archivos ya verificados en los pilotos: de al menos `minimo` bytes, permitidos por robots.txt
    y de hosts que no se abandonaron. Los más grandes de cada repositorio, hasta `por_repo`."""
    abandonados: set[str] = set()
    por_url: dict[str, Candidato] = {}
    for piloto in pilotos:
        for p in leer_jsonl(piloto / PEDIDOS):
            if p.get("abandono"):
                abandonados.add(p["host"])
            tamano = _tamano(p)
            if (
                p.get("tipo") == "archivo"
                and p.get("codigo") in (200, 206)
                and p.get("robots_permitido") is not False
                and tamano is not None
                and tamano >= minimo
            ):
                por_url[p["url"]] = Candidato(p["repo"], p["host"], p["url"], tamano)
    por_repo_: dict[str, list[Candidato]] = defaultdict(list)
    for c in por_url.values():
        if c.host not in abandonados:
            por_repo_[c.repo].append(c)
    elegidos = []
    for repo in sorted(por_repo_):
        elegidos += sorted(por_repo_[repo], key=lambda c: -c.tamano)[:por_repo]
    return elegidos


async def medir(elegidos: list[Candidato], dir_salida: Path, contacto: str, cfg: ConfigCortesia) -> str:
    """Pide cada candidato con la cortesía de medición. Devuelve el id de la corrida."""
    corrida = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]
    registro = Registro(dir_salida, corrida)
    registro.evento("corrida", user_agent=cfg.user_agent(contacto), config=asdict(cfg), archivos=len(elegidos))
    por_repo: dict[str, list[Candidato]] = defaultdict(list)
    for c in elegidos:
        por_repo[c.repo].append(c)
    try:
        async with Cortesia(cfg, registro, contacto) as cortesia:

            async def un_repo(repo: str, archivos: list[Candidato]) -> None:
                for i, c in enumerate(archivos):
                    ctx = Contexto(repo=repo, paso="medicion", metodo="medicion", item=c.url, item_idx=i)
                    try:
                        await cortesia.pedir(c.url, ctx, tipo="medicion")
                    except SinPedido as exc:
                        registro.evento("no_medido", repo=repo, url=c.url, motivo=str(exc))
                        return

            await asyncio.gather(*(un_repo(repo, archivos) for repo, archivos in por_repo.items()))
    finally:
        registro.cerrar()
    return corrida


def velocidades(dir_salida: Path) -> list[dict[str, Any]]:
    """Una fila por archivo medido: bytes leídos y MB/s desde los encabezados hasta el último byte."""
    filas = []
    for p in leer_jsonl(dir_salida / PEDIDOS):
        if p.get("tipo") != "medicion" or p.get("codigo") not in (200, 206) or p.get("ms_encabezados") is None:
            continue
        total = (datetime.fromisoformat(p["fin"]) - datetime.fromisoformat(p["inicio"])).total_seconds()
        transferencia = total - p["ms_encabezados"] / 1000
        if transferencia <= 0 or not p.get("bytes_cuerpo"):
            continue
        filas.append(
            {
                "repo": p["repo"],
                "host": p["host"],
                "url": p["url"],
                "bytes": p["bytes_cuerpo"],
                "completo": not p["truncado"],
                "ms_encabezados": p["ms_encabezados"],
                "segundos_transferencia": round(transferencia, 3),
                "mb_s": round(p["bytes_cuerpo"] / transferencia / MB, 3),
            }
        )
    return filas


def resumir_medicion(dir_salida: Path) -> dict[str, Any]:
    """Escribe velocidad.csv y devuelve los percentiles de MB/s por archivo y por repositorio.

    Los archivos de menos de 1 MB leídos se excluyen del resumen: el arranque de TCP pesa demasiado.
    """
    filas = velocidades(dir_salida)
    with open(dir_salida / VELOCIDAD, "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(
            f, fieldnames=["repo", "host", "url", "bytes", "completo", "ms_encabezados", "segundos_transferencia", "mb_s"]
        )
        escritor.writeheader()
        escritor.writerows(filas)
    utiles = [f for f in filas if f["bytes"] >= MB]
    por_repo: dict[str, list[float]] = defaultdict(list)
    for f in utiles:
        por_repo[f["repo"]].append(f["mb_s"])
    medianas = sorted(statistics.median(v) for v in por_repo.values())

    def cuantiles(valores: list[float]) -> dict[str, float]:
        if len(valores) < 2:
            return {"p50": valores[0]} if valores else {}
        q = statistics.quantiles(valores, n=4)
        return {"p25": round(q[0], 2), "p50": round(q[1], 2), "p75": round(q[2], 2)}

    resumen = {
        "archivos_medidos": len(filas),
        "archivos_de_1mb_o_mas": len(utiles),
        "repositorios": len(por_repo),
        "mb_s_por_archivo": cuantiles(sorted(f["mb_s"] for f in utiles)),
        "mb_s_por_repositorio": cuantiles(medianas),
        "mediana_por_repositorio": {repo: round(statistics.median(v), 2) for repo, v in sorted(por_repo.items())},
    }
    (dir_salida / "velocidad.json").write_text(json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")
    return resumen
