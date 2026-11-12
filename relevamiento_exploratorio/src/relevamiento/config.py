"""Constantes del relevamiento. Los valores de cortesía son los de la tabla de la spec."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConfigCortesia:
    intervalo_minimo: float = 5.0
    factor_latencia: float = 2.0
    hosts_en_paralelo: int = 3
    pedidos_por_repo: int = 40
    bytes_archivo: int = 2048
    bytes_pagina: int = 2 * 1024 * 1024
    timeout_conexion: float = 10.0
    timeout_lectura: float = 30.0
    # Desvío confirmado de la spec: tope total por pedido, para servidores que gotean bytes.
    timeout_total: float = 120.0
    max_redirecciones: int = 5
    abandono_403_seguidos: int = 2
    abandono_red_seguidos: int = 3
    agente: str = "pruebamateo"
    version_agente: str = "0.1"
    # Solo para `medir-velocidad`: cuánto se lee de un archivo para medir la velocidad por conexión.
    bytes_medicion: int = 8 * 1024 * 1024
    segundos_medicion: float = 30.0

    def user_agent(self, contacto: str) -> str:
        return f"{self.agente}/{self.version_agente} (contacto: {contacto})"


# Desvío pedido por el usuario (nov 2026): medir a qué velocidad entregan los repositorios un
# archivo, para la prueba de concurrencia. Es la única excepción a "archivos nunca completos", y
# por eso la cortesía es más estricta que la del relevamiento.
CORTESIA_MEDICION = ConfigCortesia(
    intervalo_minimo=30.0,
    hosts_en_paralelo=2,
    pedidos_por_repo=10,
)
ARCHIVOS_POR_REPO_MEDICION = 3
TAMANO_MINIMO_MEDICION = 1_000_000
ARCHIVOS_POR_REPO_TAMANOS = 5
"""Con `medir-velocidad --desde-tamanos`: archivos por repositorio, de distintos tamaños (entra en los
10 pedidos por repositorio de CORTESIA_MEDICION, con robots.txt)."""

# Pedido por el usuario (nov 2026): medir el tamaño de los archivos desde los metadatos OAI (METS o
# xoai), para calibrar el volumen de la prueba de concurrencia. Son páginas de ListRecords, no
# archivos: valen las reglas de páginas (tope de 2 MB). Cortesía más lenta que la del relevamiento.
CORTESIA_TAMANOS = ConfigCortesia(
    intervalo_minimo=10.0,
    hosts_en_paralelo=2,
    pedidos_por_repo=8,
)
# Ventanas de fechas (from, until) para repartir la muestra en el tiempo, además de la primera página.
VENTANAS_TAMANOS = (
    (None, "2012-12-31"),
    ("2013-01-01", "2016-12-31"),
    ("2017-01-01", "2019-12-31"),
    ("2020-01-01", "2022-12-31"),
    ("2023-01-01", None),
)

# Pedido por el usuario (nov 2026): una muestra de ~180 repositorios reales del listado de LA
# Referencia, con su endpoint OAI, su país y ítems candidatos a descargar, para repetir la prueba de
# concurrencia contra repositorios reales en vez del simulador. Solo metadatos: el buscador de LA
# Referencia y páginas de ListRecords (tope de 2 MB); ningún archivo. Cortesía por host, como el
# resto del relevamiento: 5 s entre pedidos (la de la spec), 8 hosts a la vez y 20 pedidos por
# repositorio (reconocimiento, robots y hasta ~11 páginas).
CORTESIA_MUESTRA = ConfigCortesia(
    hosts_en_paralelo=8,
    pedidos_por_repo=20,
)
BUSCADOR_LA_REFERENCIA = "https://buscador.lareferencia.info/api/v1/search"
REGISTROS_BUSCADOR = 10
"""Registros por consulta al buscador: se vota el host entre ellos, porque uno solo puede apuntar afuera."""
API_HANDLE = "https://hdl.handle.net/api/handles/"
# Rutas OAI que no prueba el reconocimiento de `relevar`: Invenio (/oai2d), SciELO y la genérica /oai.
RUTAS_OAI_EXTRA = ("/oai2d", "/oai/scielo-oai.php", "/oai")
CONTEXTOS_OAI = ("snrd", "openaire", "driver")
"""Otros contextos OAI de DSpace en la misma base (`…/oai/<contexto>`): si `request` viene vacío o roto, se
prueban estos. `snrd` es el que cosecha el Sistema Nacional de Repositorios Digitales de Argentina."""
# Estratos por cantidad de documentos: (nombre, desde, hasta, cupo de repositorios útiles).
# Cupo None: todos (censo). Pedido por el usuario: priorizar los grandes, que suelen andar; los de
# menos de 100 documentos suelen estar caídos y pesan el 0,3 % del total.
ESTRATOS_MUESTRA = (
    ("A", 50_000, None, None),
    ("B", 10_000, 50_000, None),
    ("C", 1_000, 10_000, 50),
    ("D", 100, 1_000, 25),
    ("E", 0, 100, 0),
)
RESERVAS_MUESTRA = 1.0
"""En los estratos con cupo se ubican además `cupo × RESERVAS_MUESTRA` repositorios de reserva."""
SEMILLA_MUESTRA = 1
ITEMS_POR_REPO_MUESTRA = 600
"""Si un repositorio tiene hasta esta cantidad de ítems se cosecha entero siguiendo el resumptionToken;
si tiene más, la primera página y una por ventana de fechas (`VENTANAS_TAMANOS`)."""
FORMATOS_MUESTRA = ("mets", "xoai", "ore", "marcxml", "oai_dc")
"""Formato OAI de la cosecha, del más rico (tamaño y URL de cada archivo) al más pobre. `marcxml`: Invenio
publica los archivos en el campo 856. Si la primera página de un formato viene rota, se prueba el siguiente."""


ITEMS_POR_REPO = 3
# Los ítems se muestrean entre los registros modificados en este período, para no quedarse con los más viejos.
DIAS_MUESTREO_RECIENTE = 365

# Rutas OAI-PMH típicas por plataforma, relativas a la raíz del host.
RUTAS_OAI = {
    "dspace7": ["/server/oai/request", "/oai/request"],
    "dspace": ["/oai/request", "/server/oai/request"],
    "eprints": ["/cgi/oai2"],
    "desconocida": ["/oai/request", "/server/oai/request", "/cgi/oai2"],
}
MAX_SONDEOS_OAI = 3

FORMATOS_OAI_METODO = {"oai_xoai": "xoai", "oai_ore": "ore", "oai_mets": "mets"}

# Bundles de DSpace que no son contenido: se registran pero no se verifican.
BUNDLES_DERIVADOS = frozenset({"THUMBNAIL", "TEXT", "LICENSE", "CC-LICENSE"})

METODOS = ("directo", "pagina", "doi_handle", "oai_xoai", "oai_ore", "oai_mets", "rest56", "rest7")

ACEPTAR_XML = "application/xml, text/xml;q=0.9, */*;q=0.1"
ACEPTAR_JSON = "application/json, */*;q=0.1"
