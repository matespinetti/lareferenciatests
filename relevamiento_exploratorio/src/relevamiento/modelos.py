"""Tipos de datos compartidos. Sin I/O."""

import re
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Contexto:
    """Etiqueta de cada pedido: la crea el orquestador y la registra la cortesía."""

    repo: str
    paso: str  # reconocimiento | metodo | verificacion
    metodo: str
    item: str | None = None
    item_idx: int | None = None


@dataclass(frozen=True, slots=True)
class Salto:
    url: str
    codigo: int | None
    location: str | None
    pedido_id: str


@dataclass(frozen=True, slots=True)
class Respuesta:
    url_inicial: str
    url: str  # la del último salto
    codigo: int | None
    encabezados: tuple[tuple[str, str], ...] = ()
    cuerpo: bytes = b""
    truncado: bool = False
    ms_encabezados: int | None = None
    error: str | None = None
    antibot: str | None = None
    tls_verificado: bool = True
    saltos: tuple[Salto, ...] = ()
    pedido_ids: tuple[str, ...] = ()

    def encabezado(self, nombre: str) -> str | None:
        nombre = nombre.lower()
        for clave, valor in self.encabezados:
            if clave.lower() == nombre:
                return valor
        return None

    @property
    def tipo_contenido(self) -> str:
        return tipo_medio(self.encabezado("content-type"))

    @property
    def content_length(self) -> int | None:
        return _entero(self.encabezado("content-length"))

    @property
    def total(self) -> int | None:
        """Tamaño completo del recurso: el total de Content-Range o, si no hay, Content-Length."""
        rango = self.encabezado("content-range")
        if rango:
            m = re.search(r"/(\d+)\s*$", rango)
            if m:
                return int(m.group(1))
        return self.content_length if self.codigo == 200 else None

    @property
    def exitosa(self) -> bool:
        return self.error is None and self.codigo is not None and 200 <= self.codigo < 300

    def texto(self) -> str:
        m = re.search(r"charset=([\w.-]+)", self.encabezado("content-type") or "", re.I)
        try:
            return self.cuerpo.decode(m.group(1) if m else "utf-8", errors="replace")
        except LookupError:
            return self.cuerpo.decode("utf-8", errors="replace")


def tipo_medio(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def parece_archivo(content_type: str | None, disposition: str | None) -> bool:
    """Decide por encabezados si una respuesta es un archivo y no una página o un documento de datos."""
    if disposition and "attachment" in disposition.lower():
        return True
    tipo = tipo_medio(content_type)
    if not tipo:
        return False
    textual = (
        tipo.startswith("text/")
        or tipo.endswith(("+xml", "+json", "/xml", "/json", "/html", "/javascript"))
    )
    return not textual


def _entero(valor: str | None) -> int | None:
    try:
        return int(valor) if valor is not None else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Repositorio:
    id: str
    nombre: str
    url_base: str
    oai_endpoint: str | None = None


@dataclass(frozen=True, slots=True)
class Item:
    repo: str
    idx: int
    url: str
    oai_id: str | None = None


@dataclass(frozen=True, slots=True)
class Deteccion:
    plataforma: str | None  # dspace | ojs | eprints
    version: str | None
    variante: str | None = None  # xmlui | jspui | angular
    fuente: str = ""
    crudo: str | None = None


@dataclass(frozen=True, slots=True)
class DatosIdentify:
    nombre: str | None
    base_url: str | None
    version_protocolo: str | None
    identificador_repositorio: str | None
    fecha_mas_antigua: str | None


@dataclass(frozen=True, slots=True)
class Candidato:
    url: str
    fuente: str  # citation_pdf_url | enlace | xoai | ore | mets | rest56 | rest7 | directo | ...
    bundle: str | None = None
    nombre: str | None = None
    mime: str | None = None
    tamano: int | None = None


@dataclass(frozen=True, slots=True)
class Extraccion:
    candidatos: tuple[Candidato, ...] = ()
    # Pedidos que el método todavía necesita: (url, etiqueta). Lo usa la REST 7 para los bitstreams por bundle.
    siguiente: tuple[tuple[str, str], ...] = ()
    datos: Mapping[str, str] = field(default_factory=dict)
    problema: str | None = None


@dataclass(slots=True)
class Reconocimiento:
    plataforma: str | None = None
    version: str | None = None
    variante: str | None = None
    fuente_deteccion: str | None = None
    detecciones: list[Deteccion] = field(default_factory=list)
    oai_endpoint: str | None = None
    oai_fuente: str | None = None  # csv | sondeo
    oai_formatos: list[str] | None = None
    identificador_oai: str | None = None
    rest: str | None = None  # v7 | v5_6
    rest_base: str | None = None
    antibot: str | None = None
    cdn: str | None = None
    no_probado: dict[str, str] = field(default_factory=dict)
