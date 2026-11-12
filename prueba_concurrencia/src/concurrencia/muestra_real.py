"""Catálogo de la muestra real de LA Referencia para el modo muestra (escenarios `r*`).

Pedido por el usuario (nov 2026): repetir la prueba contra repositorios reales en vez del simulador.
La muestra la arma `relevamiento muestra-*` (relevamiento_exploratorio/muestra/): repositorios útiles,
sus ítems y, cuando el OAI los publica (METS, xoai, ORE, MARCXML), sus archivos con URL y tamaño.

Todo el motor trabaja con ids enteros (planificador, spool, base, claves de S3), así que acá se
numeran: el repositorio i es el `m0002` de la muestra y el ítem j de ese repositorio, su
identificador OAI. Dos clases de ítem:

- **Ya resueltos**: con archivos en `archivos.csv`. Son descargas directas, sin pedido de resolución
  (la resolución ya la hizo el OAI, de a 100 ítems por página). Van primero: ítems 0..d-1.
- **Por resolver**: sin archivos, con la página de un artículo OJS (`/article/view/N`). El motor pide
  la página y saca el PDF (`resolucion_real`). Ítems d..d+r-1.

Los demás ítems sin archivos se descartan: no hay nada que bajar. Cada paso del barrido baja una
parte distinta (el ítem k de cada repositorio va a la parte k % partes) y, de lo de tamaño conocido,
hasta `tope` bytes por repositorio.

Se lee antes de arrancar el bucle: el `open()` de este módulo nunca corre durante la medición.
"""

import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from concurrencia.planificador import Tarea, Tipo

_OJS_ARTICULO = re.compile(r"/article/view/\d+/?$")
_RESOLUTORES = frozenset({"hdl.handle.net", "doi.org", "dx.doi.org", "www.doi.org"})


def host_de(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


@dataclass(slots=True)
class Catalogo:
    repos: list[str] = field(default_factory=list)
    """Índice → id de la muestra (`m0002`)."""
    hosts: list[str] = field(default_factory=list)
    items: list[list[str]] = field(default_factory=list)
    """Repositorio → ítem → identificador OAI."""
    precargadas: list[list[Tarea]] = field(default_factory=list)
    """Repositorio → descargas de los ítems ya resueltos."""
    resueltos: list[int] = field(default_factory=list)
    """Repositorio → cantidad de ítems ya resueltos (los primeros)."""
    paginas: dict[tuple[int, int], str] = field(default_factory=dict)
    archivos: dict[tuple[int, int, int], str] = field(default_factory=dict)
    archivos_por_item: dict[tuple[int, int], int] = field(default_factory=dict)
    bytes_conocidos: list[int] = field(default_factory=list)

    def items_por_repo(self) -> list[int]:
        return [len(lista) for lista in self.items]

    def url(self, tarea: Tarea) -> str:
        if tarea.tipo is Tipo.RESOLUCION:
            return self.paginas[(tarea.repo, tarea.item)]
        return self.archivos[(tarea.repo, tarea.item, tarea.archivo)]

    def registrar(self, repo: int, item: int, urls: list[str]) -> list[Tarea]:
        """Las descargas que salieron de resolver un ítem: les da índices de archivo."""
        tareas = []
        for i, url in enumerate(urls):
            self.archivos[(repo, item, i)] = url
            tareas.append(Tarea(Tipo.DESCARGA, repo, item, i))
        return tareas

    def resumen(self) -> dict[str, Any]:
        return {
            "repositorios": len(self.repos),
            "hosts": len(set(self.hosts)),
            "items": sum(len(lista) for lista in self.items),
            "items_resueltos": sum(self.resueltos),
            "items_por_resolver": sum(len(lista) for lista in self.items) - sum(self.resueltos),
            "archivos_conocidos": sum(len(p) for p in self.precargadas),
            "bytes_conocidos": sum(self.bytes_conocidos),
        }

    def indice(self) -> dict[str, Any]:
        """Para rastrear cada archivo: índice de repositorio → id y host, e ítems por índice."""
        return {
            str(i): {"id": self.repos[i], "host": self.hosts[i], "items": self.items[i]} for i in range(len(self.repos))
        }


def _leer(ruta: Path) -> list[dict[str, str]]:
    with open(ruta, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def cargar(directorio: Path, parte: int = 0, partes: int = 1, tope: int | None = None) -> Catalogo:
    """La parte `parte` de `partes` de la muestra en `directorio`, con hasta `tope` bytes conocidos
    por repositorio."""
    utiles = [f for f in _leer(directorio / "repositorios.csv") if f["estado"] == "ok"]
    archivos: dict[tuple[str, str], list[tuple[str, int | None]]] = defaultdict(list)
    for f in _leer(directorio / "archivos.csv"):
        if f["url"].startswith(("http://", "https://")):
            archivos[(f["repo"], f["item"])].append((f["url"], int(f["tamano"]) if f["tamano"] else None))
    por_repo: dict[str, list[dict[str, str]]] = defaultdict(list)
    for f in _leer(directorio / "items.csv"):
        por_repo[f["repo"]].append(f)

    c = Catalogo()
    for fila in sorted(utiles, key=lambda f: f["id"]):
        resueltos: list[tuple[str, list[tuple[str, int | None]]]] = []
        por_resolver: list[tuple[str, str]] = []
        for k, item in enumerate(por_repo.get(fila["id"], [])):
            if k % partes != parte:
                continue
            de_item = archivos.get((fila["id"], item["item"]))
            pagina = item["pagina"] or ""
            if de_item:
                resueltos.append((item["item"], de_item))
            elif _OJS_ARTICULO.search(pagina) and host_de(pagina) not in _RESOLUTORES:
                por_resolver.append((item["item"], pagina))
        # Tope de bytes conocidos por repositorio y paso: se cortan los ítems ya resueltos.
        total, recortados = 0, []
        for ident, de_item in resueltos:
            tamano = sum(t or 0 for _, t in de_item)
            if tope is not None and recortados and total + tamano > tope:
                break
            recortados.append((ident, de_item))
            total += tamano
        if not recortados and not por_resolver:
            continue
        r = len(c.repos)
        c.repos.append(fila["id"])
        c.hosts.append(fila["host"] or host_de(fila["url_base"]))
        c.items.append([ident for ident, _ in recortados] + [ident for ident, _ in por_resolver])
        c.resueltos.append(len(recortados))
        c.bytes_conocidos.append(total)
        tareas = []
        for j, (_ident, de_item) in enumerate(recortados):
            c.archivos_por_item[(r, j)] = len(de_item)
            for a, (url, _t) in enumerate(de_item):
                c.archivos[(r, j, a)] = url
                tareas.append(Tarea(Tipo.DESCARGA, r, j, a))
        c.precargadas.append(tareas)
        for j, (_ident, pagina) in enumerate(por_resolver, start=len(recortados)):
            c.paginas[(r, j)] = pagina
    return c


def verificar_cortesia(pedidos: list[tuple[Any, ...]], intervalo: float, tolerancia: float = 0.01) -> dict[str, Any]:
    """La cortesía que con el simulador contaba el servidor, contada sobre el registro del motor
    (`Motor.pedidos`: repo, host, tipo, inicio, fin, código, bytes, error, reintento_tls).

    Por repositorio: nunca dos pedidos a la vez y, entre el fin de uno y el inicio del siguiente, al
    menos `intervalo`. El reintento sin verificar de un certificado inválido es parte del mismo turno."""
    por_repo: dict[Any, list[tuple[float, float]]] = defaultdict(list)
    for p in pedidos:
        if not p[8]:
            por_repo[p[0]].append((p[3], p[4]))
    simultaneos = cortos = 0
    ejemplos: list[dict[str, Any]] = []
    for repo, lista in por_repo.items():
        lista.sort()
        for (_i0, f0), (i1, _f1) in zip(lista, lista[1:], strict=False):
            if i1 < f0:
                simultaneos += 1
            elif i1 - f0 < intervalo - tolerancia:
                cortos += 1
            else:
                continue
            if len(ejemplos) < 20:
                ejemplos.append({"repo": repo, "hueco_s": round(i1 - f0, 3)})
    return {
        "pedidos": len(pedidos),
        "violaciones": simultaneos + cortos,
        "simultaneos": simultaneos,
        "intervalos_cortos": cortos,
        "ejemplos": ejemplos,
    }


def escribir_indice(ruta: Path, catalogo: Catalogo) -> None:
    """Se escribe al terminar la corrida, como el CSV de métricas."""
    ruta.write_text(json.dumps(catalogo.indice(), ensure_ascii=False), encoding="utf-8")
