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
