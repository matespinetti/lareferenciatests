"""Calibración de los pilotos, parámetros fijos de la prueba y tabla de escenarios.

Los percentiles salen de `relevamiento_exploratorio/piloto*/pedidos.jsonl` (596 pedidos con
latencia, 537 páginas y 51 archivos con tamaño conocido por `Content-Range`).
"""

import os
from dataclasses import dataclass, replace
from enum import StrEnum

MB = 1024 * 1024

# Latencia hasta los encabezados (pilotos: p50 469 ms, p95 1,88 s, p99 4,24 s).
LATENCIA_P50 = 0.47
LATENCIA_P95 = 1.9
# Página de ítem (pilotos: p50 9,9 KB, p95 451 KB). Se corta a 2 MB, como en el relevamiento.
PAGINA_P50 = 10_000
PAGINA_P95 = 450_000
PAGINA_MAXIMO = 2 * MB
# Archivo (pilotos: p50 0,53 MB, p95 38 MB, máximo 200 MB, media 11,5 MB).
ARCHIVO_P50 = 529_207
ARCHIVO_P95 = 37_961_393
ARCHIVO_MAXIMO = 200_605_784

ANCHO_POR_CONEXION = float(os.environ.get("ANCHO_MB_S", "2")) * MB
"""Bytes por segundo que el servidor entrega por conexión. Supuesto: los pilotos no lo midieron
(`relevamiento medir-velocidad` lo mide). Se cambia con la variable ANCHO_MB_S."""
ANCHOS_SENSIBILIDAD = (0.5, 1.0, 2.0, 5.0)
"""MB/s por conexión con los que el informe recalcula la duración."""

INTERVALO = 1.0
"""Segundos entre el fin de un pedido y el inicio del siguiente al mismo repositorio."""

TOLERANCIA = 0.001
"""Margen del servidor al comparar intervalos, para no contar como violación un redondeo."""

CHUNK = 64 * 1024
DESCARGAS_MIN = 1
DESCARGAS_MAX = 3
PROB_429 = 0.005
PROB_503 = 0.005
RETRY_AFTER = 10
PROB_CIERRE = 0.2
"""Fracción de repositorios que cierran la conexión después de cada respuesta."""

DOMINIO = "sim.localhost"
PUERTO = 8443
SEMILLA = 1

# Problema real.
REPOS_REALES = 2400
ITEMS_REALES = 6_000_000
ITEMS_POR_REPO_SINTETICO = ITEMS_REALES // REPOS_REALES
PEDIDOS_POR_ITEM = 1 + (DESCARGAS_MIN + DESCARGAS_MAX) / 2



@dataclass(frozen=True, slots=True)
class Instancia:
    """Una instancia candidata de EC2: lo que el motor tendría si corriera ahí.

    Red de la documentación de AWS (instance types, network specifications); precio on-demand de
    Linux en us-east-1 según instances.vantage.sh (noviembre de 2026). Verificar antes de decidir.
    """

    nombre: str
    vcpu: int
    memoria_gb: float
    red_base_gbps: float
    red_rafaga_gbps: float
    precio_hora: float
    linea_base_cpu: float | None = None
    """Fracción de cada vCPU que se usa sin gastar créditos (familia t3); None, sin créditos."""

    @property
    def memoria(self) -> float:
        return self.memoria_gb * 1024 * MB

    @property
    def red_base(self) -> float:
        """Bytes por segundo que la red sostiene todo el tiempo."""
        return self.red_base_gbps * 1e9 / 8

    @property
    def red_rafaga(self) -> float:
        return self.red_rafaga_gbps * 1e9 / 8

    @property
    def cpu_disponible(self) -> float:
        """% de un núcleo que el motor puede usar sostenido: un proceso de Python usa como mucho
        uno, y sin créditos se deja un 20 % de margen para Postgres y el sistema."""
        if self.linea_base_cpu is not None:
            return min(100.0, 100 * self.vcpu * self.linea_base_cpu)
        return 80.0


INSTANCIAS: dict[str, Instancia] = {
    i.nombre: i
    for i in [
        Instancia("t3.small", 2, 2, 0.128, 5.0, 0.0208, linea_base_cpu=0.20),
        Instancia("t3.medium", 2, 4, 0.256, 5.0, 0.0416, linea_base_cpu=0.20),
        Instancia("t3.large", 2, 8, 0.512, 5.0, 0.0832, linea_base_cpu=0.30),
        Instancia("t3.xlarge", 4, 16, 1.024, 5.0, 0.1664, linea_base_cpu=0.40),
        Instancia("c7g.medium", 1, 2, 0.52, 12.5, 0.0363),
        Instancia("c7g.large", 2, 4, 0.937, 12.5, 0.0725),
        Instancia("c6i.large", 2, 4, 0.781, 12.5, 0.085),
        Instancia("c6gn.medium", 1, 2, 1.6, 16.0, 0.0432),
        Instancia("c6gn.large", 2, 4, 3.0, 25.0, 0.0864),
        Instancia("c7gn.medium", 1, 2, 3.125, 25.0, 0.0624),
    ]
}
INSTANCIA_POR_DEFECTO = "c6gn.medium"


def instancia(nombre: str | None = None) -> Instancia:
    return INSTANCIAS[nombre or INSTANCIA_POR_DEFECTO]


class Modo(StrEnum):
    CORRUTINAS = "corrutinas"
    HILOS = "hilos"


@dataclass(frozen=True, slots=True)
class Escenario:
    id: str
    lugares: int
    repos: int
    items: int
    descripcion: str
    modo: Modo = Modo.CORRUTINAS
    db: bool = False
    duracion: float = 180.0
    calentamiento: float = 30.0
    tam_archivo: int | None = None
    """Tamaño fijo de los archivos; None sigue la distribución de los pilotos."""
    con_red: bool = False
    """Si el servidor limita el total a la red base de la instancia que se simula."""
    red: float | None = None
    """Tope de bytes por segundo de todo el servidor, ya resuelto para la instancia simulada.
    None: sin tope, para medir cuánto aguanta un proceso."""
    instancia: str | None = None
    """Instancia de EC2 que se simula; la fija `escenario()`."""
    grupo: str | None = None
    """Barrido al que pertenece el escenario, para agruparlo en el informe."""

    @property
    def real(self) -> bool:
        return self.repos == REPOS_REALES and self.items == ITEMS_REALES


def _sintetico(id: str, lugares: int, descripcion: str, **extra: object) -> Escenario:
    # Al menos 10 repositorios por lugar, para que la cortesía no limite la concurrencia.
    repos = 10 * lugares
    return Escenario(id, lugares, repos, repos * ITEMS_POR_REPO_SINTETICO, descripcion, **extra)  # type: ignore[arg-type]


def _real(id: str, descripcion: str, lugares: int = REPOS_REALES, **extra: object) -> Escenario:
    # Por loopback no hay placa de red: sin tope, 2.400 repositorios a 2 MB/s piden unos 3 GB/s,
    # algo que la instancia real nunca recibiría. Se limita a la red base de la instancia simulada.
    extra.setdefault("con_red", True)
    return Escenario(id, lugares, REPOS_REALES, ITEMS_REALES, descripcion, **extra)  # type: ignore[arg-type]


BARRIDO_LUGARES = (2, 5, 10, 25, 50, 100, 250, 500, 1000, 2400)
"""Lugares de ejecución del barrido: dónde deja de rendir sumar tareas concurrentes."""
BARRIDO_RED = "barrido_red"
BARRIDO_SIN_TOPE = "barrido_sin_tope"


def _barrido(prefijo: str, grupo: str, con_red: bool, descripcion: str) -> list[Escenario]:
    return [
        _real(f"{prefijo}{n}", f"{descripcion}, {n} lugares", lugares=n, con_red=con_red, grupo=grupo,
              duracion=30.0, calentamiento=10.0)
        for n in BARRIDO_LUGARES
    ]  # fmt: skip


ESCENARIOS: dict[str, Escenario] = {
    e.id: e
    for e in [
        _sintetico("1", 50, "Línea base"),
        _sintetico("2", 200, "La configuración prevista"),
        _sintetico("3", 1000, "Margen"),
        _real("4", "El pico real: todos los repositorios activos, con la red de la instancia"),
        _sintetico("5", 5000, "Buscar el límite"),
        _real("6", "Capacidad de un proceso: sin tope de red, archivos de 38 MB", tam_archivo=ARCHIVO_P95, con_red=False),
        _real("7", "Comparación con hilos en el pico real", modo=Modo.HILOS),
        _real("8", "Costo de registrar en la base", db=True),
        _real("9", "Resistencia: memoria estable y créditos de CPU", duracion=3600.0),
        _sintetico("1h", 50, "Línea base con hilos", modo=Modo.HILOS),
        _sintetico("2h", 200, "Configuración prevista con hilos", modo=Modo.HILOS),
        _sintetico("3h", 1000, "Margen con hilos", modo=Modo.HILOS),
        *_barrido("b", BARRIDO_RED, True, "Barrido con la red de la instancia"),
        *_barrido("bs", BARRIDO_SIN_TOPE, False, "Barrido sin tope de red"),
    ]
}


def escenario(
    id: str, duracion: float | None = None, calentamiento: float | None = None, instancia_: str | None = None
) -> Escenario:
    """El escenario `id` para la instancia simulada, con la duración y el calentamiento acortados
    si se piden (ensayos)."""
    base = ESCENARIOS[id]
    simulada = instancia(instancia_)
    cambios: dict[str, object] = {"instancia": simulada.nombre, "red": simulada.red_base if base.con_red else None}
    if duracion is not None:
        cambios["duracion"] = duracion
    if calentamiento is not None:
        cambios["calentamiento"] = calentamiento
    return replace(base, **cambios)  # type: ignore[arg-type]
