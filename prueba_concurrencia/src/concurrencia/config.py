"""Calibración de los pilotos, parámetros fijos de la prueba y tabla de escenarios.

Los percentiles salen de `relevamiento_exploratorio/piloto*/pedidos.jsonl` (596 pedidos con
latencia, 537 páginas y 51 archivos con tamaño conocido por `Content-Range`).
"""

import math
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
# Archivo: medido con `relevamiento medir-tamanos` (nov 2026) en los metadatos OAI de 11 repositorios
# reales (2.306 ítems, 1.676 archivos), ponderado por la cantidad de ítems de cada repositorio:
# mediana 1,20 MB, p95 16,3 MB, p99 135 MB, máximo 1,88 GB y media 6,80 MB. La log-normal conserva
# la mediana y la media (las que fijan el tiempo y el volumen), así que su p95 queda en ~26 MB.
ARCHIVO_P50 = 1_204_370
ARCHIVO_P95 = 25_860_645
ARCHIVO_MAXIMO = 1_876_593_140
ARCHIVO_GRANDE = 37_961_393
"""Tamaño fijo del escenario 6 (el p95 de los pilotos), para medir la capacidad de un proceso con archivos grandes."""

ANCHO_POR_CONEXION = float(os.environ.get("ANCHO_MB_S", "3.30")) * MB
"""Mediana entre repositorios de los bytes por segundo que entrega una conexión, medida desde São Paulo
(sa-east-1, la región elegida) con `relevamiento medir-velocidad` sobre 15 repositorios: los 11 grandes de
`medir-tamanos` y los de los pilotos (scripts/combinar_velocidades.py): 3,30 MB/s. Desde Virginia era 2,15.
Cada repositorio tiene su propia velocidad alrededor de esta mediana (`ANCHO_SIGMA`). Se cambia con ANCHO_MB_S."""
ANCHO_CUARTILES_MEDIDOS = (float(os.environ.get("ANCHO_P25", "1.43")), float(os.environ.get("ANCHO_P75", "14.76")))
"""Cuartiles inferior y superior medidos de la velocidad por repositorio (MB/s), los que fijan la dispersión.
Desde São Paulo: 1,43 y 14,76 (desde Virginia, 0,97 y 3,81). Para otra región, ANCHO_MB_S, ANCHO_P25 y
ANCHO_P75 salen de la medición desde ahí (scripts/combinar_velocidades.py)."""
ANCHO_SIGMA = float(
    os.environ.get("ANCHO_SIGMA", round(math.log(ANCHO_CUARTILES_MEDIDOS[1] / ANCHO_CUARTILES_MEDIDOS[0]) / (2 * 0.6745), 3))
)
"""Dispersión de la velocidad entre repositorios: desvío del logaritmo de una log-normal cuyos cuartiles
son los medidos (~1,0). 0: todos los repositorios a la mediana. Se cambia con ANCHO_SIGMA."""
ANCHO_RANGO = (float(os.environ.get("ANCHO_REL_MIN", "0.18")), float(os.environ.get("ANCHO_REL_MAX", "14.6")))
"""La velocidad de un repositorio se corta entre estos múltiplos de la mediana: lo observado. Desde São
Paulo, 0,18 y 14,6 (0,59 a 48 MB/s con mediana 3,30); desde Virginia eran 0,3 y 6. Para otra región,
ANCHO_REL_MIN y ANCHO_REL_MAX (los imprime scripts/combinar_velocidades.py). La duración es muy sensible al mínimo."""
ANCHOS_SENSIBILIDAD = (1.0, 2.15, 3.30, 6.0)
"""Medianas de MB/s por conexión con las que el informe recalcula la duración."""

INTERVALO = 1.0
"""Segundos entre el fin de un pedido y el inicio del siguiente al mismo repositorio."""

TOLERANCIA = 0.001
"""Margen del servidor al comparar intervalos, para no contar como violación un redondeo."""

CHUNK = 64 * 1024
ARCHIVOS_POR_ITEM = {0: 3767, 1: 5899, 2: 241, 3: 26, 4: 37, 5: 30}
"""Archivos de contenido por ítem, medidos con `relevamiento medir-tamanos` (11 repositorios, ponderado por
sus ítems), por cada 10.000 ítems: uno en la mayoría y ninguno en ~38 % (embargados, restringidos, solo
metadatos o con el archivo afuera del repositorio). 5 son 5 o más. Media: 0,68."""
DESCARGAS_POR_ITEM = sum(k * n for k, n in ARCHIVOS_POR_ITEM.items()) / sum(ARCHIVOS_POR_ITEM.values())
PROB_429 = 0.005
PROB_503 = 0.005
RETRY_AFTER = 10
PROB_CIERRE = 0.2
"""Fracción de repositorios que cierran la conexión después de cada respuesta."""

DOMINIO = "sim.localhost"
PUERTO = 8443
IP_SERVIDOR = os.environ.get("IP_SERVIDOR", "127.0.0.1")
"""A dónde resuelve `*.sim.localhost`. Por defecto, esta misma máquina; en la validación en dos
máquinas, la IP privada de la que corre el servidor simulado."""

# Subida a S3 (escenarios con `subida`): el motor guarda cada archivo mientras lo baja y, al
# terminar, lo deja listo para un proceso subidor aparte que lo sube a un S3 falso local.
PUERTO_S3 = 9443
BUCKET = "descargas"
S3_BUCKET = os.environ.get("S3_BUCKET") or None
"""Bucket de S3 real. Si está, el subidor sube ahí (con las credenciales del rol de la instancia) en
vez de al S3 falso. Solo para la validación en una instancia real."""
S3_REGION = os.environ.get("AWS_REGION", "us-east-1")
S3_PREFIJO = os.environ.get("S3_PREFIJO", "prueba-concurrencia")
"""Todo lo que sube la prueba queda bajo este prefijo, para borrarlo de una vez."""
UMBRAL_DISCO = 8 * MB
"""Los archivos de este tamaño o más van al spool en disco; los más chicos, al spool en RAM."""
PARTE_S3 = 8 * MB
"""Tamaño de parte del multipart; los archivos más chicos van en un solo PUT."""
SUBIDAS_EN_PARALELO = int(os.environ.get("SUBIDA_PARALELO", "16"))
"""Subidas a la vez del subidor, en cualquiera de los modos. Se cambia con SUBIDA_PARALELO."""
MODOS_SUBIDA = ("proceso", "hilos", "corrutinas")
SUBIDOR_CLIENTE = os.environ.get("SUBIDOR_CLIENTE", "boto3")
"""Cliente de S3 del subidor: `boto3` (s3transfer en Python) o `crt` (el cliente de AWS en C).
CRT solo contra S3 real: resuelve los nombres por su cuenta y no pasa por el resolver de la prueba."""
CLIENTES_SUBIDA = ("boto3", "crt")
LOTE_DB_SEGUNDOS = 1.0
"""Con `db_lotes`: cada cuánto se escribe el lote pendiente."""
"""Dónde corre la subida: proceso aparte, pool de hilos en el motor o pool de corrutinas en el bucle."""
LIMITE_PENDIENTE_RAM = 256 * MB
LIMITE_PENDIENTE_DISCO = 4 * 1024 * MB
"""Si lo pendiente de subir pasa estos límites, el motor deja de repartir tareas nuevas."""
BLOQUE_DISCO = 1 * MB
"""El spool en disco se escribe de a bloques de este tamaño, fuera del bucle de eventos."""
SEMILLA = 1

# Problema real.
REPOS_REALES = 2400
ITEMS_REALES = 6_000_000
ITEMS_POR_REPO_SINTETICO = ITEMS_REALES // REPOS_REALES
PEDIDOS_POR_ITEM = 1 + DESCARGAS_POR_ITEM



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
    """Fracción de cada vCPU que se usa sin gastar créditos (familias t3 y t4g); None, sin créditos."""

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
        Instancia("t4g.medium", 2, 4, 0.256, 5.0, 0.0336, linea_base_cpu=0.20),
        Instancia("t4g.large", 2, 8, 0.512, 5.0, 0.0672, linea_base_cpu=0.30),
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
    MULTIPROCESO = "multiproceso"
    """Un proceso planificador y `workers` procesos workers con corrutinas, conectados por TCP."""


@dataclass(frozen=True, slots=True)
class Escenario:
    id: str
    lugares: int
    repos: int
    items: int
    descripcion: str
    modo: Modo = Modo.CORRUTINAS
    db: bool = False
    db_lotes: bool = False
    """Con `db`: el resultado se encola y una tarea aparte lo escribe en lotes (como el descargador),
    en vez de una transacción por resultado que el lugar espera."""
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
    subida: bool = False
    """Si el motor guarda los archivos en el spool y un subidor los sube a un S3 falso."""
    workers: int = int(os.environ.get("WORKERS", "2"))
    """Procesos workers del modo multiproceso; los lugares se reparten entre ellos."""
    modo_subida: str = "proceso"
    """Dónde corre el subidor (`MODOS_SUBIDA`); se cambia con MODO_SUBIDA."""
    subidor_mismo_nucleo: bool = False
    """El subidor en proceso aparte comparte el núcleo del motor, como en una instancia de 1 vCPU."""

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
        _real("6", "Capacidad de un proceso: sin tope de red, archivos de 38 MB", tam_archivo=ARCHIVO_GRANDE, con_red=False),
        _real("7", "Comparación con hilos en el pico real", modo=Modo.HILOS),
        _real("8", "Costo de registrar en la base", db=True),
        _real("9", "Resistencia: memoria estable y créditos de CPU", duracion=3600.0),
        _sintetico("1h", 50, "Línea base con hilos", modo=Modo.HILOS),
        _sintetico("2h", 200, "Configuración prevista con hilos", modo=Modo.HILOS),
        _sintetico("3h", 1000, "Margen con hilos", modo=Modo.HILOS),
        _real("s250", "Subida a S3 con un proceso aparte, 250 lugares, red de la instancia", lugares=250, subida=True),
        # Una c6gn.medium: 1 vCPU para el motor y el subidor, ~120 lugares.
        _real("b120", "Una c6gn.medium sin subida: 120 lugares", lugares=120),
        _real("s120", "Una c6gn.medium con subida: motor y subidor en el mismo núcleo, 120 lugares", lugares=120,
              subida=True, subidor_mismo_nucleo=True),
        # Validación en una c6gn.medium real (GUIA_DOS_MAQUINAS.md): lo mismo que s120, registrando cada
        # resultado en PostgreSQL y subiendo a S3 real.
        _real("v120", "Validación en una c6gn.medium: subida y base (en lotes), 120 lugares", lugares=120,
              subida=True, subidor_mismo_nucleo=True, db=True, db_lotes=True),
        _real("v120h", "Validación en una c6gn.medium durante 1 hora: memoria, disco y base estables", lugares=120,
              subida=True, subidor_mismo_nucleo=True, db=True, db_lotes=True, duracion=3600.0),
        # Producción en una t4g (con ráfagas): todo en la máquina (motor, subidor, PostgreSQL en lotes y la
        # carga de la API y el frontend) y un barrido de corrutinas, con la red de INSTANCIA (t4g.large).
        *[
            _real(f"t{n}", f"Producción en una t4g: {n} corrutinas, subida y base en lotes", lugares=n,
                  subida=True, db=True, db_lotes=True)
            for n in (25, 50, 75, 120)
        ],
        _real("sh250", "Subida a S3 con un pool de hilos en el motor, 250 lugares, red de la instancia", lugares=250,
              subida=True, modo_subida="hilos"),
        _real("sc250", "Subida a S3 con un pool de corrutinas en el motor, 250 lugares, red de la instancia",
              lugares=250, subida=True, modo_subida="corrutinas"),
        _real("ss250", "Subida a S3 sin tope de red: capacidad con spool y subida, 250 lugares", lugares=250,
              subida=True, con_red=False),
        _real("m250", "Multiproceso: un planificador y 2 workers, 250 lugares, red de la instancia", lugares=250,
              modo=Modo.MULTIPROCESO),
        _real("ms500", "Multiproceso sin tope de red, 500 lugares: ¿pasa el techo de un núcleo?", lugares=500,
              modo=Modo.MULTIPROCESO, con_red=False),
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
