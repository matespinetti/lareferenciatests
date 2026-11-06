"""Lectura de los CSV de entrada."""

import csv
from pathlib import Path

from relevamiento.modelos import Item, Repositorio


class EntradaInvalida(ValueError):
    pass


def leer_repositorios(ruta: Path) -> list[Repositorio]:
    repos = []
    with open(ruta, newline="", encoding="utf-8-sig") as f:
        lector = csv.DictReader(f)
        faltan = {"id", "nombre", "url_base"} - set(lector.fieldnames or [])
        if faltan:
            raise EntradaInvalida(f"{ruta}: faltan columnas {sorted(faltan)}")
        for n, fila in enumerate(lector, start=2):
            # Las planillas exportadas suelen dejar filas vacías (",,,") al final.
            if not any((v or "").strip() for v in fila.values() if isinstance(v, str)):
                continue
            url = (fila["url_base"] or "").strip()
            if not url.startswith(("http://", "https://")):
                raise EntradaInvalida(f"{ruta}:{n}: url_base inválida: {url!r}")
            repos.append(Repositorio(
                id=fila["id"].strip(),
                nombre=(fila["nombre"] or "").strip(),
                url_base=url,
                oai_endpoint=(fila.get("oai_endpoint") or "").strip() or None,
            ))
    ids = [r.id for r in repos]
    if len(ids) != len(set(ids)):
        raise EntradaInvalida(f"{ruta}: hay ids repetidos")
    return repos


def leer_items(ruta: Path | None) -> dict[str, list[Item]]:
    if ruta is None:
        return {}
    items: dict[str, list[Item]] = {}
    with open(ruta, newline="", encoding="utf-8-sig") as f:
        lector = csv.DictReader(f)
        faltan = {"repo_id", "url"} - set(lector.fieldnames or [])
        if faltan:
            raise EntradaInvalida(f"{ruta}: faltan columnas {sorted(faltan)}")
        for fila in lector:
            repo, url = fila["repo_id"].strip(), (fila["url"] or "").strip()
            if url:
                lista = items.setdefault(repo, [])
                lista.append(Item(repo=repo, idx=len(lista), url=url))
    return items
