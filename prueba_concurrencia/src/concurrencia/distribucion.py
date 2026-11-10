"""Distribuciones de la prueba: latencias, tamaños e ítems por repositorio.

Funciones puras: sin I/O y sin async. Todo el azar sale de un `random.Random` con semilla.
"""

import json
import math
import os
import random
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist

from concurrencia import config

_NORMAL = NormalDist()
_Z95 = _NORMAL.inv_cdf(0.95)


@dataclass(frozen=True, slots=True)
class LogNormal:
    """Log-normal definida por su mediana y su p95, opcionalmente cortada en `maximo`."""

    mu: float
    sigma: float
    maximo: float | None = None

    @classmethod
    def desde_percentiles(cls, p50: float, p95: float, maximo: float | None = None) -> "LogNormal":
        return cls(math.log(p50), math.log(p95 / p50) / _Z95, maximo)

    def muestra(self, rng: random.Random) -> float:
        valor = rng.lognormvariate(self.mu, self.sigma)
        return valor if self.maximo is None else min(valor, self.maximo)

    def percentil(self, q: float) -> float:
        valor = math.exp(self.mu + self.sigma * _NORMAL.inv_cdf(q))
        return valor if self.maximo is None else min(valor, self.maximo)

    def media(self) -> float:
        """Media de min(X, maximo)."""
        s = self.sigma
        if self.maximo is None:
            return math.exp(self.mu + s * s / 2)
        z = (math.log(self.maximo) - self.mu) / s
        return math.exp(self.mu + s * s / 2) * _NORMAL.cdf(z - s) + self.maximo * (1 - _NORMAL.cdf(z))


LATENCIA = LogNormal.desde_percentiles(config.LATENCIA_P50, config.LATENCIA_P95)
PAGINA = LogNormal.desde_percentiles(config.PAGINA_P50, config.PAGINA_P95, config.PAGINA_MAXIMO)
ARCHIVO = LogNormal.desde_percentiles(config.ARCHIVO_P50, config.ARCHIVO_P95, config.ARCHIVO_MAXIMO)


def items_por_repo(repos: int, total: int, semilla: int) -> list[int]:
    """Ítems de cada repositorio: dos tercios quedan en el 4 % de los repositorios, como en la red.

    Es la regla de `LAR_descargador/simulator/src/lar_simulator/generate.py:_sizes`, copiada para
    que este proyecto no dependa de aquel.
    """
    rng = random.Random(semilla)
    grandes = max(1, repos // 25)
    pesos = [rng.lognormvariate(0, 1) * (48 if n < grandes else 1) for n in range(repos)]
    escala = total / sum(pesos)
    tamanos = [int(peso * escala) for peso in pesos]
    tamanos[0] += total - sum(tamanos)
    rng.shuffle(tamanos)
    return tamanos


REPOS_MEDIDOS_RUTA = Path(os.environ.get("REPOS_MEDIDOS", Path(__file__).resolve().parents[2] / "datos" / "repos_medidos.json"))
"""Repositorios reales con su cantidad de ítems y su velocidad medida (scripts/repos_medidos.py)."""


Medido = tuple[int, float, float | None]
"""(ítems, bytes/s por conexión, archivos por ítem) de un repositorio medido."""


def repos_medidos(ruta: Path = REPOS_MEDIDOS_RUTA) -> dict[str, Medido]:
    """{repo: (ítems, bytes/s, archivos por ítem)} de los repositorios medidos; vacío si no hay archivo."""
    if not ruta.exists():
        return {}
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    return {
        repo: (int(r["items"]), float(r["mb_s"]) * config.MB, r.get("archivos_por_item"))
        for repo, r in datos["repos"].items()
    }


def items_del_modelo(semilla: int = config.SEMILLA, medidos: dict[str, Medido] | None = None) -> list[int]:
    """Ítems por repositorio del problema real para el modelo de duración: primero los repositorios
    medidos, con su cantidad real; después el resto, con la regla de `items_por_repo`, hasta
    completar los repositorios y los ítems del problema."""
    medidos = repos_medidos() if medidos is None else medidos
    conocidos = [n for n, _, _ in medidos.values()]
    resto = items_por_repo(config.REPOS_REALES - len(conocidos), config.ITEMS_REALES - sum(conocidos), semilla)
    return conocidos + resto


def rng_de(semilla: int, *partes: object) -> random.Random:
    """Un generador determinista para una entidad, como un ítem o un archivo."""
    return random.Random(":".join(str(p) for p in (semilla, *partes)))


def descargas_del_item(semilla: int, repo: int, item: int) -> int:
    """Archivos del ítem, con la frecuencia de los pilotos (0 a 4; casi siempre 1)."""
    cantidades = list(config.ARCHIVOS_POR_ITEM)
    return rng_de(semilla, "descargas", repo, item).choices(cantidades, list(config.ARCHIVOS_POR_ITEM.values()))[0]


def ancho_repo(
    semilla: int, repo: int, mediana: float = config.ANCHO_POR_CONEXION, sigma: float = config.ANCHO_SIGMA
) -> float:
    """Bytes por segundo que entrega una conexión con el repositorio: log-normal alrededor de
    `mediana`, fija para cada repositorio (unos son lentos y otros rápidos, como en la medición)."""
    if sigma <= 0:
        return mediana
    factor = math.exp(sigma * rng_de(semilla, "ancho", repo).gauss(0.0, 1.0))
    return mediana * min(max(factor, config.ANCHO_RANGO[0]), config.ANCHO_RANGO[1])


def tam_pagina(semilla: int, repo: int, item: int) -> int:
    return int(PAGINA.muestra(rng_de(semilla, "pagina", repo, item)))


def tam_archivo(semilla: int, repo: int, item: int, archivo: int, fijo: int | None = None) -> int:
    if fijo is not None:
        return fijo
    return max(1, int(ARCHIVO.muestra(rng_de(semilla, "archivo", repo, item, archivo))))


def cierra_conexion(semilla: int, repo: int) -> bool:
    return rng_de(semilla, "cierre", repo).random() < config.PROB_CIERRE
