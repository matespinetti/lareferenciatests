"""verificar-cortesia: comprueba sobre pedidos.jsonl que se respetaron las reglas, sin confiar en la cortesía."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from relevamiento.config import ConfigCortesia
from relevamiento.detectores import parsear_robots
from relevamiento.registro import PEDIDOS, leer_jsonl

MAX_MENSAJES = 50


@dataclass
class ResultadoVerificacion:
    violaciones: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)
    estadisticas: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.violaciones


_TROZO_MAXIMO = 256 * 1024


def verificar(dir_salida: Path, cfg: ConfigCortesia = ConfigCortesia(), tolerancia: float = 0.002) -> ResultadoVerificacion:
    pedidos = list(leer_jsonl(dir_salida / PEDIDOS))
    return verificar_pedidos(pedidos, crawl_delays(dir_salida, pedidos, cfg), cfg, tolerancia)


def crawl_delays(dir_salida: Path, pedidos: list[dict], cfg: ConfigCortesia) -> dict[str, list[tuple[float, float]]]:
    """Crawl-delay por host, recalculado desde las capturas de robots.txt: (desde cuándo rige, segundos)."""
    host_de_cadena = {p["id"]: p["host"] for p in pedidos if p["id"] == p.get("cadena")}
    resultado: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for p in pedidos:
        if p["metodo"] != "robots" or p.get("codigo") != 200 or not p.get("captura"):
            continue
        ruta = dir_salida / p["captura"]
        if not ruta.exists():
            continue
        delay = parsear_robots(ruta.with_suffix(".cuerpo").read_bytes(), cfg.agente).crawl_delay
        if delay:
            host = host_de_cadena.get(p.get("cadena"), p["host"])
            resultado[host].append((_t(p["fin"]), delay))
    return resultado


def verificar_pedidos(
    pedidos: list[dict],
    delays: dict[str, list[tuple[float, float]]] | None = None,
    cfg: ConfigCortesia = ConfigCortesia(),
    tolerancia: float = 0.002,
) -> ResultadoVerificacion:
    res = ResultadoVerificacion()
    delays = delays or {}
    violar = _limitado(res.violaciones)
    avisar = _limitado(res.avisos)

    por_host: dict[str, list[dict]] = defaultdict(list)
    for p in pedidos:
        por_host[p["host"]].append(p)

    hueco_minimo: float | None = None
    for host, lista in por_host.items():
        lista.sort(key=lambda p: _t(p["inicio"]))
        for a, b in zip(lista, lista[1:]):
            ini_a, fin_a, ini_b = _t(a["inicio"]), _t(a["fin"]), _t(b["inicio"])
            hueco = ini_b - fin_a
            if hueco < -tolerancia:
                violar(f"{host}: pedidos simultáneos {a['id']} y {b['id']}")
                continue
            crawl = max((d for desde, d in delays.get(host, []) if desde <= ini_b), default=0.0)
            requerido = max(cfg.intervalo_minimo, cfg.factor_latencia * (fin_a - ini_a), crawl)
            if hueco < requerido - tolerancia:
                violar(f"{host}: {b['id']} empezó {hueco:.3f} s después de {a['id']}; se requerían {requerido:.3f} s")
            hueco_minimo = hueco if hueco_minimo is None else min(hueco_minimo, hueco)

        abandono = next((p for p in lista if p.get("abandono")), None)
        if abandono:
            despues = [p["id"] for p in lista if _t(p["inicio"]) >= _t(abandono["fin"]) and p is not abandono]
            if despues:
                violar(f"{host}: {len(despues)} pedidos después del abandono en {abandono['id']} ({despues[0]}…)")

    max_en_vuelo, max_hosts = _barrido(pedidos, cfg, tolerancia, violar)

    por_repo = Counter(p["repo"] for p in pedidos)
    for repo, n in por_repo.items():
        if n > cfg.pedidos_por_repo:
            violar(f"repositorio {repo}: {n} pedidos (tope {cfg.pedidos_por_repo})")

    por_cadena = Counter(p.get("cadena") for p in pedidos if not p.get("reintento_tls"))
    for cadena, n in por_cadena.items():
        if n > cfg.max_redirecciones + 1:
            violar(f"cadena {cadena}: {n} saltos (máximo {cfg.max_redirecciones + 1})")

    for p in pedidos:
        tope = {
            "archivo": cfg.bytes_archivo,
            # La medición cuenta lo leído y corta al pasar el tope: puede pasarse por un trozo de lectura.
            "medicion": cfg.bytes_medicion + _TROZO_MAXIMO,
        }.get(p.get("tipo"), cfg.bytes_pagina)
        if (p.get("bytes_cuerpo") or 0) > tope:
            violar(f"{p['id']}: se leyeron {p['bytes_cuerpo']} bytes (tope {tope})")

    repetidos = Counter(
        (p.get("corrida"), p["repo"], p["url"]) for p in pedidos if p["metodo"] != "robots" and not p.get("reintento_tls")
    )
    for (_corrida, repo, url), n in repetidos.items():
        if n > 1:
            avisar(f"repositorio {repo}: {url} se pidió {n} veces")

    if pedidos:
        inicios = [_t(p["inicio"]) for p in pedidos]
        fines = [_t(p["fin"]) for p in pedidos]
        res.estadisticas = {
            "pedidos": len(pedidos),
            "hosts": len(por_host),
            "repositorios": len(por_repo),
            "corridas": len({p.get("corrida") for p in pedidos}),
            "duracion_s": round(max(fines) - min(inicios), 1),
            "hueco_minimo_s": round(hueco_minimo, 3) if hueco_minimo is not None else None,
            "max_en_vuelo": max_en_vuelo,
            "max_hosts_en_vuelo": max_hosts,
            "max_pedidos_por_repo": max(por_repo.values()),
        }
    return res


def _barrido(pedidos: list[dict], cfg: ConfigCortesia, tolerancia: float, violar) -> tuple[int, int]:
    """Línea de barrido global: nunca más de N pedidos ni N hosts distintos en vuelo."""
    eventos = []
    for p in pedidos:
        eventos.append((_t(p["inicio"]), 1, p["host"], p["id"]))
        eventos.append((_t(p["fin"]) - tolerancia, -1, p["host"], p["id"]))
    eventos.sort(key=lambda e: (e[0], e[1]))  # los fines antes que los inicios en el mismo instante
    en_vuelo = 0
    hosts: Counter[str] = Counter()
    max_en_vuelo = max_hosts = 0
    for _t_evento, delta, host, pid in eventos:
        en_vuelo += delta
        hosts[host] += delta
        distintos = sum(1 for n in hosts.values() if n > 0)
        if delta > 0 and (en_vuelo > cfg.hosts_en_paralelo or distintos > cfg.hosts_en_paralelo):
            violar(f"{pid}: {en_vuelo} pedidos y {distintos} hosts en vuelo (máximo {cfg.hosts_en_paralelo})")
        max_en_vuelo = max(max_en_vuelo, en_vuelo)
        max_hosts = max(max_hosts, distintos)
    return max_en_vuelo, max_hosts


def _t(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _limitado(lista: list[str]):
    def agregar(mensaje: str) -> None:
        if len(lista) < MAX_MENSAJES:
            lista.append(mensaje)
        elif len(lista) == MAX_MENSAJES:
            lista.append("… (se omiten más mensajes)")

    return agregar
