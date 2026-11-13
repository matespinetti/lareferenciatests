"""Muestra real de repositorios del listado de LA Referencia, pedida por el usuario (nov 2026) para
repetir la prueba de concurrencia contra repositorios reales en vez del simulador.

Cuatro pasos, cada uno un comando:

1. `muestra-elegir` (sin pedidos): estratos por cantidad de documentos y sorteo con semilla. Los
   repositorios grandes van todos (censo); de los medianos y chicos, un cupo con reservas.
2. `muestra-ubicar`: una consulta por repositorio a la API del buscador de LA Referencia, que da el
   país, el namespace OAI y una URL de ítem; de ahí sale la URL base (`extractores.ubicar`).
3. `muestra-cosechar`: reconocimiento (`Relevamiento.reconocer`, que encuentra el endpoint OAI y sus
   formatos) y páginas de ListRecords en el formato más rico, con los archivos de cada ítem. En los
   estratos con cupo, un repositorio que no sirve se reemplaza por el siguiente.
4. `muestra-resumir`: repositorios.csv, items.csv, archivos.csv y un resumen ponderado.

Solo metadatos, nunca archivos. Todos los pedidos pasan por la cortesía (`CORTESIA_MUESTRA`).
"""

import asyncio
import csv
import json
import random
import re
import statistics
import traceback
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from relevamiento.config import (
    ACEPTAR_JSON,
    ACEPTAR_XML,
    API_HANDLE,
    BUSCADOR_LA_REFERENCIA,
    CONTEXTOS_OAI,
    REGISTROS_BUSCADOR,
    RUTAS_OAI_EXTRA,
    ESTRATOS_MUESTRA,
    FORMATOS_MUESTRA,
    ITEMS_POR_REPO_AMPLIADA,
    ITEMS_POR_REPO_MUESTRA,
    PAGINAS_SIN_NUEVOS,
    RESERVAS_MUESTRA,
    SEMILLA_MUESTRA,
    ConfigCortesia,
)
from relevamiento.cortesia import Cortesia, SinPedido
from relevamiento.detectores import detectar_por_identify, parsear_formatos_oai
from relevamiento.extractores import endpoints_roar, items_de_pagina, ubicacion_desde_handle, ubicar, url_de_handle
from relevamiento.modelos import Contexto, PaginaItems, Repositorio, Ubicacion
from relevamiento.parseo import host_de, raiz
from relevamiento.registro import EVENTOS, PEDIDOS, Registro, leer_jsonl, respuesta_desde_captura
from relevamiento.relevar import Relevamiento, robots_previos
from relevamiento.tamanos import _percentil, urls_listrecords

CANDIDATOS = "candidatos.csv"
_PAGINA_ROTA = frozenset({"sin_listrecords", "xml_invalido", "cannotDisseminateFormat"}) | {f"http_{c}" for c in (500, 502, 504)}
POBLACION = "poblacion.json"
UBICACION = "ubicacion"
COSECHA = "cosecha"
AMPLIACION = "ampliacion"


# --- 1. Elegir ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidata:
    id: str
    orden: int
    estrato: str
    repositorio: str
    documentos: int
    reserva: bool = False


def leer_poblacion(ruta: Path) -> list[tuple[str, int]]:
    """El listado de LA Referencia: (repositorio, documentos). Un nombre repetido vale una vez."""
    vistos: dict[str, int] = {}
    with open(ruta, encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            nombre = (fila.get("repositorio") or "").strip()
            if nombre and nombre not in vistos:
                vistos[nombre] = int(fila.get("documentos") or 0)
    return list(vistos.items())


def estrato_de(documentos: int) -> str:
    for nombre, desde, hasta, _cupo in ESTRATOS_MUESTRA:
        if documentos >= desde and (hasta is None or documentos < hasta):
            return nombre
    return ESTRATOS_MUESTRA[-1][0]


def cupos() -> dict[str, int | None]:
    return {nombre: cupo for nombre, _d, _h, cupo in ESTRATOS_MUESTRA}


def elegir(poblacion: list[tuple[str, int]], semilla: int = SEMILLA_MUESTRA) -> list[Candidata]:
    """Todos los de los estratos sin cupo (de mayor a menor) y, en los demás, un sorteo de
    `cupo × (1 + RESERVAS_MUESTRA)`: los primeros `cupo` son titulares y el resto, reservas en orden."""
    por_estrato: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for nombre, documentos in poblacion:
        por_estrato[estrato_de(documentos)].append((nombre, documentos))
    salida: list[Candidata] = []
    for estrato, _desde, _hasta, cupo in ESTRATOS_MUESTRA:
        repos = sorted(por_estrato[estrato], key=lambda p: (-p[1], p[0]))
        if cupo is None:
            elegidos, titulares = repos, len(repos)
        elif cupo <= 0:
            continue
        else:
            n = min(len(repos), round(cupo * (1 + RESERVAS_MUESTRA)))
            elegidos, titulares = random.Random(f"{semilla}:{estrato}").sample(repos, n), cupo
        for i, (nombre, documentos) in enumerate(elegidos):
            orden = len(salida) + 1
            salida.append(Candidata(f"m{orden:04d}", orden, estrato, nombre, documentos, reserva=i >= titulares))
    return salida


def ampliar(candidatas: list[Candidata], poblacion: list[tuple[str, int]], extra: dict[str, int],
            semilla: int = SEMILLA_MUESTRA) -> list[Candidata]:
    """Agrega `extra[estrato]` candidatos nuevos a cada estrato, sorteados entre los que todavía no están,
    sin tocar los que ya están (ni sus ids). Entran como titulares: el cupo crece en la misma cantidad."""
    ya = {c.repositorio for c in candidatas}
    nuevas = list(candidatas)
    for estrato, cantidad in extra.items():
        libres = sorted(((n, d) for n, d in poblacion if estrato_de(d) == estrato and n not in ya), key=lambda p: (-p[1], p[0]))
        elegidos = random.Random(f"{semilla}:{estrato}:ampliar{len(nuevas)}").sample(libres, min(cantidad, len(libres)))
        for nombre, documentos in elegidos:
            orden = len(nuevas) + 1
            nuevas.append(Candidata(f"m{orden:04d}", orden, estrato, nombre, documentos))
    return nuevas


def resumen_poblacion(poblacion: list[tuple[str, int]]) -> dict[str, dict[str, int]]:
    resumen: dict[str, dict[str, int]] = {e[0]: {"repositorios": 0, "documentos": 0} for e in ESTRATOS_MUESTRA}
    for _nombre, documentos in poblacion:
        e = resumen[estrato_de(documentos)]
        e["repositorios"] += 1
        e["documentos"] += documentos
    return resumen


_COLUMNAS_CANDIDATOS = ["id", "orden", "estrato", "repositorio", "documentos", "reserva"]


def escribir_candidatos(dir_salida: Path, candidatas: list[Candidata], poblacion: list[tuple[str, int]]) -> None:
    dir_salida.mkdir(parents=True, exist_ok=True)
    with open(dir_salida / CANDIDATOS, "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=_COLUMNAS_CANDIDATOS)
        escritor.writeheader()
        for c in candidatas:
            escritor.writerow({**asdict(c), "reserva": "si" if c.reserva else ""})
    (dir_salida / POBLACION).write_text(json.dumps(resumen_poblacion(poblacion), indent=2), encoding="utf-8")


def leer_candidatos(dir_salida: Path) -> list[Candidata]:
    with open(dir_salida / CANDIDATOS, encoding="utf-8") as f:
        return [
            Candidata(fila["id"], int(fila["orden"]), fila["estrato"], fila["repositorio"], int(fila["documentos"]),
                      fila["reserva"] == "si")
            for fila in csv.DictReader(f)
        ]  # fmt: skip


# --- 2. Ubicar ---------------------------------------------------------------------------


def url_buscador(nombre: str) -> str:
    """Registros del repositorio en la API del buscador de LA Referencia, con todos sus campos."""
    valor = nombre.replace("\\", "\\\\").replace('"', '\\"')
    params = [
        ("lookfor", ""), ("type", "AllFields"), ("limit", str(REGISTROS_BUSCADOR)),
        ("field[]", "rawData"), ("filter[]", f'reponame_str:"{valor}"'),
    ]  # fmt: skip
    return f"{BUSCADOR_LA_REFERENCIA}?{urlencode(params)}"


async def ubicar_candidatas(
    candidatas: list[Candidata],
    dir_base: Path,
    contacto: str,
    cfg: ConfigCortesia,
    *,
    transporte: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Consulta el buscador de a un repositorio por vez (un solo host). Se puede cortar y relanzar."""
    dir_salida = dir_base / UBICACION
    previos = list(leer_jsonl(dir_salida / PEDIDOS))
    ubicadas = {e["repo"]: e for e in leer_jsonl(dir_salida / EVENTOS) if e.get("tipo") == "ubicacion"}
    con_handle = {p["repo"] for p in previos if p.get("metodo") == "handle"}
    # Pendientes: las que no se consultaron y las que quedaron con un handle sin resolver (por `--reprocesar`).
    pendientes = [c for c in candidatas if c.id not in ubicadas
                  or (ubicadas[c.id].get("problema") == "solo_handle" and c.id not in con_handle)]
    registro = Registro(dir_salida, _corrida())
    cortado = None
    try:
        async with Cortesia(cfg, registro, contacto, transporte=transporte) as cortesia:
            cortesia.sembrar(previos, robots_previos(dir_salida, previos))
            registro.evento("corrida", user_agent=cfg.user_agent(contacto), config=asdict(cfg), pendientes=len(pendientes))
            for c in pendientes:
                if (previa := ubicadas.get(c.id)) is not None:
                    campos = {k: previa.get(k) for k in Ubicacion.__dataclass_fields__}
                    u = await _resolver_handle(Ubicacion(**campos), c, cortesia)
                    registro.evento("ubicacion", repo=c.id, repositorio=c.repositorio, **asdict(u))
                    continue
                url = url_buscador(c.repositorio)
                if cortesia.robots_permitido(url) is False:
                    cortado = "robots"
                    break
                ctx = Contexto(repo=c.id, paso="ubicacion", metodo="buscador")
                try:
                    r = await cortesia.pedir(url, ctx, tipo="pagina", aceptar=ACEPTAR_JSON)
                except SinPedido as exc:
                    cortado = exc.motivo
                    break
                if cortesia.robots_permitido(url) is False:
                    # robots.txt llegó con este primer pedido: no se usa la respuesta y se corta.
                    cortado = "robots"
                    break
                u = ubicar(r, c.repositorio) if r.exitosa else Ubicacion(problema=r.error or f"http_{r.codigo}")
                if u.problema == "solo_handle" and u.handle:
                    u = await _resolver_handle(u, c, cortesia)
                registro.evento("ubicacion", repo=c.id, repositorio=c.repositorio, **asdict(u))
            if cortado:
                registro.evento("ubicacion_cortada", motivo=cortado)
    finally:
        registro.cerrar()
    return {"pendientes": len(pendientes), "cortado": cortado}


async def _resolver_handle(u: Ubicacion, c: Candidata, cortesia: Cortesia) -> Ubicacion:
    """Pregunta a handle.net a qué URL apunta el handle (un pedido, por la cortesía)."""
    url = f"{API_HANDLE}{quote(u.handle or '', safe='/')}?type=URL"
    if cortesia.robots_permitido(url) is False:
        return u
    try:
        r = await cortesia.pedir(url, Contexto(repo=c.id, paso="ubicacion", metodo="handle"), tipo="pagina", aceptar=ACEPTAR_JSON)
    except SinPedido:
        return u
    destino = url_de_handle(r) if r.exitosa else None
    return ubicacion_desde_handle(u, destino) if destino else u


def reprocesar_ubicaciones(candidatas: list[Candidata], dir_base: Path) -> int:
    """Vuelve a interpretar las respuestas guardadas del buscador (y de handle.net) con `ubicar`, sin
    hacer pedidos: agrega un evento `ubicacion` por candidato, que reemplaza al anterior. Los handles
    que antes no hacía falta resolver quedan `solo_handle` hasta relanzar `muestra-ubicar`."""
    dir_salida = dir_base / UBICACION
    capturas: dict[tuple[str, str], Path] = {}
    for p in leer_jsonl(dir_salida / PEDIDOS):
        if p.get("captura") and p.get("codigo") == 200 and p.get("metodo") in ("buscador", "handle"):
            capturas[(p["repo"], p["metodo"])] = dir_salida / p["captura"]
    registro = Registro(dir_salida, _corrida())
    n = 0
    try:
        for c in candidatas:
            if (c.id, "buscador") not in capturas:
                continue
            u = ubicar(respuesta_desde_captura(capturas[(c.id, "buscador")]), c.repositorio)
            if u.problema == "solo_handle" and (c.id, "handle") in capturas:
                destino = url_de_handle(respuesta_desde_captura(capturas[(c.id, "handle")]))
                if destino:
                    u = ubicacion_desde_handle(u, destino)
            registro.evento("ubicacion", repo=c.id, repositorio=c.repositorio, reprocesado=True, **asdict(u))
            n += 1
    finally:
        registro.cerrar()
    return n


ROAR = "endpoints_roar.json"


def endpoints_de_roar(rawlist: Path, candidatas: list[Candidata], ubicaciones: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Cruza el listado completo de ROAR (`roar.eprints.org/rawlist.xml`, bajado a mano) con los
    candidatos por host (URL base, alternativa y URL de ejemplo): los endpoints OAI que ROAR publica
    para cada uno. Sin pedidos."""
    por_host = endpoints_roar(rawlist.read_bytes())
    salida = {}
    for c in candidatas:
        u = ubicaciones.get(c.id) or {}
        hosts = {host_de(x).removeprefix("www.") for x in (u.get("url_base"), u.get("alternativa"), u.get("url_ejemplo")) if x}
        hosts -= {"hdl.handle.net", "doi.org", "dx.doi.org"}
        encontrados = list(dict.fromkeys(e for h in sorted(hosts) for e in por_host.get(h, [])))
        if encontrados:
            salida[c.id] = encontrados
    return salida


def fallidos(dir_base: Path) -> set[str]:
    """Repositorios cuya última cosecha falló por algo que puede cambiar en otro intento: todos salvo
    los que terminaron bien y los de anti-bot (un desafío no se resuelve)."""
    ultimo = {e["repo"]: e for e in leer_jsonl(dir_base / COSECHA / EVENTOS) if e.get("tipo") == "repo_terminado"}
    return {r for r, e in ultimo.items() if e.get("estado") not in ("ok", "antibot")}


def leer_roar(dir_base: Path) -> dict[str, list[str]]:
    ruta = dir_base / ROAR
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}


def leer_ubicaciones(dir_base: Path) -> dict[str, dict[str, Any]]:
    return {e["repo"]: e for e in leer_jsonl(dir_base / UBICACION / EVENTOS) if e.get("tipo") == "ubicacion"}


# --- 3. Cosechar -------------------------------------------------------------------------


def endpoints_conocidos(rutas: list[Path]) -> dict[str, str]:
    """Host → endpoint OAI ya verificado (entradas/repos.csv y repositorios.csv de los pilotos)."""
    conocidos: dict[str, str] = {}
    for ruta in rutas:
        if not ruta.exists():
            continue
        with open(ruta, encoding="utf-8") as f:
            for fila in csv.DictReader(f):
                endpoint = (fila.get("oai_endpoint") or "").strip()
                if endpoint.startswith(("http://", "https://")):
                    conocidos.setdefault(host_de(endpoint), endpoint)
    return conocidos


def coincide(identificador_oai: str | None, endpoint: str | None, namespace: str | None) -> bool | None:
    """Si el endpoint encontrado es el del repositorio: su `repositoryIdentifier` o su host coinciden
    con el namespace OAI de los registros de LA Referencia. None si no hay con qué comparar."""
    if not namespace or not endpoint or "." not in namespace or re.match(r"ojs\d*\.", namespace):
        return None  # el namespace de fábrica de OJS (`ojs.<algo>`) no dice de qué host es
    host = host_de(endpoint)
    return (identificador_oai or "").lower() == namespace or host == namespace or host == namespace.removeprefix("ojs.")


class _Turnos:
    """Reparte repositorios a los trabajadores respetando el cupo de cada estrato: en los estratos con
    cupo, el siguiente se toma solo si los útiles más los que están en curso no llegan al cupo."""

    def __init__(self, colas: dict[str, list[tuple[Candidata, dict]]], utiles: Counter[str]):
        self.colas = colas
        self.utiles = utiles
        self.en_curso: Counter[str] = Counter()
        self.cupos = cupos()
        self.cambio = asyncio.Condition()

    def _siguiente(self) -> tuple[Candidata, dict] | None:
        for estrato, cola in self.colas.items():
            cupo = self.cupos.get(estrato)
            if cola and (cupo is None or self.utiles[estrato] + self.en_curso[estrato] < cupo):
                self.en_curso[estrato] += 1
                return cola.pop(0)
        return None

    async def tomar(self) -> tuple[Candidata, dict] | None:
        async with self.cambio:
            while True:
                if tarea := self._siguiente():
                    return tarea
                if not sum(self.en_curso.values()):
                    return None
                await self.cambio.wait()

    async def terminar(self, estrato: str, util: bool) -> None:
        async with self.cambio:
            self.en_curso[estrato] -= 1
            if util:
                self.utiles[estrato] += 1
            self.cambio.notify_all()


async def cosechar(
    candidatas: list[Candidata],
    ubicaciones: dict[str, dict[str, Any]],
    conocidos: dict[str, str],
    dir_base: Path,
    contacto: str,
    cfg: ConfigCortesia,
    *,
    solo: set[str] | None = None,
    rehacer: set[str] | None = None,
    roar: dict[str, list[str]] | None = None,
    ignorar_robots: bool = False,
    transporte: httpx.AsyncBaseTransport | None = None,
    transporte_sin_verificar: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Cosecha los candidatos ubicados. Con `rehacer`, esos repositorios se vuelven a cosechar aunque ya
    hayan terminado, con su presupuesto de pedidos de nuevo entero (es otra corrida para ellos)."""
    dir_salida = dir_base / COSECHA
    rehacer = rehacer or set()
    previos = list(leer_jsonl(dir_salida / PEDIDOS))
    todos_terminados = [e for e in leer_jsonl(dir_salida / EVENTOS) if e.get("tipo") == "repo_terminado"]
    terminados = {e["repo"]: e for e in todos_terminados if e["repo"] not in rehacer}
    # Cada nuevo intento de un repositorio tiene su propio presupuesto de pedidos, con otro id en la
    # cortesía (`m0003.r1`); el turno por host sigue siendo uno solo.
    intentos = Counter(e["repo"] for e in todos_terminados)
    cuenta = {r: f"{r}.r{intentos[r]}" for r in rehacer if intentos[r]}
    utiles = Counter(
        c.estrato for c in candidatas if terminados.get(c.id, {}).get("estado") == "ok"
    )
    colas: dict[str, list[tuple[Candidata, dict]]] = {e[0]: [] for e in ESTRATOS_MUESTRA}
    for c in sorted(candidatas, key=lambda c: c.orden):
        u = ubicaciones.get(c.id)
        if c.id in terminados or not u or not u.get("url_base") or (solo is not None and c.id not in solo):
            continue
        colas[c.estrato].append((c, u))
    turnos = _Turnos(colas, utiles)
    for estrato, cupo in list(turnos.cupos.items()):
        if cupo is not None:
            titulares = sum(1 for c in candidatas if c.estrato == estrato and not c.reserva)
            turnos.cupos[estrato] = max(cupo, titulares)
    registro = Registro(dir_salida, _corrida())
    cosechados = 0
    try:
        async with Cortesia(cfg, registro, contacto, transporte=transporte,
                            transporte_sin_verificar=transporte_sin_verificar) as cortesia:
            # Un nuevo intento olvida los abandonos anteriores de los hosts de esos repositorios.
            olvidar = {p["host"] for p in previos if p.get("repo", "").split(".r")[0] in rehacer}
            cortesia.sembrar(previos, robots_previos(dir_salida, previos), olvidar_abandonos=olvidar)
            registro.evento("corrida", user_agent=cfg.user_agent(contacto), config=asdict(cfg),
                            ya_terminados=len(terminados), utiles_previos=dict(utiles),
                            rehacer=sorted(rehacer), ignorar_robots=ignorar_robots)
            relevamiento = Relevamiento(cortesia, registro)

            async def trabajador() -> None:
                nonlocal cosechados
                while tarea := await turnos.tomar():
                    c, u = tarea
                    estado = "error"
                    try:
                        estado = await _un_repo(c, u, conocidos, cortesia, relevamiento, registro, cuenta.get(c.id, c.id),
                                                (roar or {}).get(c.id), ignorar_robots)
                    except Exception:  # un error de programación no debe frenar al resto, pero queda registrado
                        registro.evento("error_interno", repo=c.id, detalle=traceback.format_exc())
                        registro.evento("repo_terminado", repo=c.id, estado="error", motivo="error_interno")
                    finally:
                        cosechados += 1
                        await turnos.terminar(c.estrato, estado == "ok")

            async with asyncio.TaskGroup() as grupo:
                for _ in range(cfg.hosts_en_paralelo):
                    grupo.create_task(trabajador())
    finally:
        registro.cerrar()
    return {"cosechados": cosechados, "ya_terminados": len(terminados), "utiles": dict(turnos.utiles)}


async def _un_repo(
    c: Candidata, u: dict[str, Any], conocidos: dict[str, str], cortesia: Cortesia,
    relevamiento: Relevamiento, registro: Registro, cuenta: str | None = None, de_roar: list[str] | None = None,
    ignorar_robots: bool = False,
) -> str:
    """Reconoce y cosecha un repositorio. Devuelve su estado: ok si quedó al menos un ítem. `cuenta` es el
    id con el que la cortesía lleva el presupuesto de pedidos (otro en cada nuevo intento)."""
    cuenta = cuenta or c.id
    conocido = conocidos.get(u.get("host") or "")
    if not conocido and u.get("plataforma") == "ojs":
        # Revista o portal OJS: el endpoint es `<base>/oai`. Muchos responden 200 con HTML a cualquier
        # ruta, así que el sondeo genérico no lo encuentra.
        conocido = u["url_base"].rstrip("/") + "/oai"
    repo = Repositorio(cuenta, c.repositorio, u["url_base"], conocido)
    rec = await relevamiento.reconocer(repo)
    if rec.oai_endpoint and conocido and conocido not in conocidos.values():
        rec.oai_fuente = "ojs"
    abandono = cortesia.abandono(host_de(repo.url_base))
    extras = [e for e in (de_roar or []) if e.rstrip("/") != (rec.oai_endpoint or "").rstrip("/")]
    if not rec.oai_endpoint and not rec.antibot and not abandono:
        # Los endpoints que publica ROAR; después, rutas que el reconocimiento no prueba (Invenio,
        # SciELO) y, si no aparece, la otra URL base.
        if await _probar(rec, cuenta, extras[:3], cortesia, "roar"):
            extras = []
        elif not await _sondear_extra(rec, cuenta, raiz(repo.url_base), cortesia) and u.get("alternativa"):
            alterna = await relevamiento.reconocer(Repositorio(cuenta, c.repositorio, u["alternativa"]))
            if alterna.oai_endpoint:
                rec = alterna
        abandono = cortesia.abandono(host_de(repo.url_base))
    registro.evento("reconocimiento", repo=c.id, **asdict(rec))

    def fin(estado: str, motivo: str | None = None, **datos: Any) -> str:
        registro.evento("repo_terminado", repo=c.id, estado=estado, motivo=motivo,
                        pedidos=cortesia.cfg.pedidos_por_repo - cortesia.restantes(cuenta), **datos)
        return estado

    if rec.antibot or (abandono or "").startswith("anti-bot"):
        return fin("antibot", rec.antibot or abandono)
    if not rec.oai_endpoint:
        return fin("abandono" if abandono else "sin_oai", abandono or next(iter(rec.no_probado.values()), None))
    es_ojs = u.get("plataforma") == "ojs" or rec.plataforma in ("ojs", "omp")
    # En OJS el 856 de MARCXML es la página del artículo, como el dc:identifier: mejor oai_dc.
    formatos = [f for f in FORMATOS_MUESTRA if f in (rec.oai_formatos or []) and not (es_ojs and f == "marcxml")]
    if not formatos:
        return fin("sin_formato", " ".join(rec.oai_formatos or []) or None, endpoint=rec.oai_endpoint)
    formato = formatos[0]
    datos = {"endpoint": rec.oai_endpoint, "coincide": coincide(rec.identificador_oai, rec.oai_endpoint, u.get("namespace"))}

    vistos: set[str] = set()
    corte: str | None = None

    async def pagina(etiqueta: str, url: str) -> PaginaItems | None:
        nonlocal corte
        if cortesia.robots_permitido(url) is False and not ignorar_robots:
            corte = "robots"
            return None
        ctx = Contexto(repo=cuenta, paso="cosecha", metodo=f"oai_{formato}", item=etiqueta)
        try:
            r = await cortesia.pedir(url, ctx, tipo="pagina", aceptar=ACEPTAR_XML)
        except SinPedido as exc:
            corte = exc.motivo
            return None
        p = items_de_pagina(r, formato) if r.exitosa else PaginaItems(problema=r.error or f"http_{r.codigo}")
        registro.evento(
            "pagina_muestra", repo=c.id, formato=formato, etiqueta=etiqueta, url=url, codigo=r.codigo,
            truncado=r.truncado, problema=p.problema, total=p.total, hay_token=bool(p.token),
            items=[[i.identificador, i.datestamp, i.borrado, i.pagina, [list(asdict(a).values()) for a in i.archivos]]
                   for i in p.items],
        )  # fmt: skip
        vistos.update(i.identificador for i in p.items if i.identificador and not i.borrado)
        return p

    endpoint = rec.oai_endpoint
    # La primera página decide el formato: si viene rota (o el formato no se puede servir), el siguiente.
    for formato in formatos[:3]:
        paginas = urls_listrecords(endpoint, formato)
        primera = await pagina(*paginas[0])
        if primera is None or primera.items or primera.problema not in _PAGINA_ROTA:
            break
    if primera is not None and not primera.items and endpoint.rstrip("/").endswith("/request"):
        # El contexto `request` de DSpace no lista nada: otros contextos de la misma base (snrd, openaire, driver).
        for contexto in CONTEXTOS_OAI:
            otro = endpoint.rstrip("/").removesuffix("request") + contexto
            ctx = Contexto(repo=cuenta, paso="cosecha", metodo="oai_formatos", item=contexto)
            try:
                r = await cortesia.pedir(f"{otro}?verb=ListMetadataFormats", ctx, aceptar=ACEPTAR_XML)
            except SinPedido as exc:
                corte = exc.motivo
                break
            de_otro = [f for f in FORMATOS_MUESTRA if f in (parsear_formatos_oai(r) if r.exitosa else [])]
            if not de_otro:
                continue
            formato = de_otro[0]
            paginas = urls_listrecords(otro, formato)
            prueba = await pagina(*paginas[0])
            if prueba is not None and prueba.items:
                endpoint, primera = otro, prueba
                break
            if corte:
                break
    if primera is not None and not primera.items and extras and not corte:
        # El endpoint encontrado no lista nada: los que publica ROAR para este repositorio.
        for otro in extras[:2]:
            prueba_rec = type(rec)()
            if not await _probar(prueba_rec, cuenta, [otro], cortesia, "roar"):
                continue
            de_otro = [f for f in FORMATOS_MUESTRA if f in (prueba_rec.oai_formatos or [])]
            if not de_otro:
                continue
            formato = de_otro[0]
            paginas = urls_listrecords(otro, formato)
            prueba = await pagina(*paginas[0])
            if prueba is not None and prueba.items:
                endpoint, primera = otro, prueba
                break
            if corte:
                break
    datos["endpoint"], datos["formato"] = endpoint, formato
    total = primera.total if primera else None
    if primera and total is not None and total <= ITEMS_POR_REPO_MUESTRA:
        # Chico: se cosecha entero siguiendo el token.
        token, n = primera.token, 0
        while token and corte is None and len(vistos) < ITEMS_POR_REPO_MUESTRA:
            n += 1
            url = f"{endpoint}?verb=ListRecords&resumptionToken={quote(token, safe='')}"
            siguiente = await pagina(f"token{n}", url)
            token = siguiente.token if siguiente else None
    elif primera is not None and (primera.items or not primera.problema):
        # Grande: una página por ventana de fechas, para repartir la muestra en el tiempo. Si la página
        # sin fechas ya vino vacía o con error, el repositorio no lista nada por OAI: no se insiste.
        for etiqueta, url in paginas[1:]:
            if corte or len(vistos) >= ITEMS_POR_REPO_MUESTRA:
                break
            await pagina(etiqueta, url)
    datos |= {"items_repositorio": total, "items": len(vistos), "corte": corte}
    if vistos:
        return fin("ok", None, **datos)
    if corte == "robots":
        return fin("robots", None, **datos)
    if corte:
        return fin("abandono", corte, **datos)
    return fin("sin_items", primera.problema if primera else None, **datos)


async def _sondear_extra(rec: Any, repo: str, base: str, cortesia: Cortesia) -> bool:
    """Prueba `Identify` en las rutas extra de `base` (Invenio, SciELO, /oai)."""
    return await _probar(rec, repo, [base.rstrip("/") + ruta for ruta in RUTAS_OAI_EXTRA], cortesia, "sondeo_extra")


async def _probar(rec: Any, repo: str, endpoints: list[str], cortesia: Cortesia, fuente: str) -> bool:
    """Prueba `Identify` en cada endpoint; con el primero que responde completa el reconocimiento con
    ese endpoint y sus formatos."""
    for endpoint in endpoints:
        ctx = Contexto(repo=repo, paso="reconocimiento", metodo="oai_identify")
        try:
            r = await cortesia.pedir(f"{endpoint}?verb=Identify", ctx, aceptar=ACEPTAR_XML)
        except SinPedido:
            return False
        if identificado := detectar_por_identify(r):
            rec.oai_endpoint, rec.oai_fuente = endpoint, fuente
            rec.identificador_oai = identificado[0].identificador_repositorio
            try:
                f = await cortesia.pedir(f"{endpoint}?verb=ListMetadataFormats",
                                         Contexto(repo=repo, paso="reconocimiento", metodo="oai_formatos"), aceptar=ACEPTAR_XML)
                rec.oai_formatos = parsear_formatos_oai(f)
            except SinPedido:
                pass
            return True
    return False


# --- 4. Resumir --------------------------------------------------------------------------

_COLUMNAS_REPOS = [
    "id", "estrato", "reserva", "repositorio", "documentos", "pais", "host", "url_base", "plataforma", "version",
    "oai_endpoint", "oai_fuente", "formato", "identificador_oai", "coincide", "items_repositorio", "items",
    "items_con_archivos", "archivos", "archivos_con_tamano", "estado", "motivo",
]  # fmt: skip


# --- 3b. Ampliar ---------------------------------------------------------------------------


def _cosechados(dir_base: Path) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    """De la cosecha: el `repo_terminado` vigente de cada repositorio y los ítems de esa corrida."""
    eventos = list(leer_jsonl(dir_base / COSECHA / EVENTOS))
    fin = {e["repo"]: e for e in eventos if e.get("tipo") == "repo_terminado"}
    items: dict[str, set[str]] = defaultdict(set)
    for e in eventos:
        if e.get("tipo") == "pagina_muestra" and e.get("corrida") == fin.get(e["repo"], {}).get("corrida"):
            items[e["repo"]].update(i[0] for i in e.get("items", []) if i[0] and not i[2])
    return fin, items


def a_ampliar(dir_base: Path, tope: int = ITEMS_POR_REPO_AMPLIADA) -> list[dict[str, Any]]:
    """Los repositorios útiles que se cosecharon por ventanas (más de `ITEMS_POR_REPO_MUESTRA` ítems o
    sin total) y tienen menos de `tope` ítems: endpoint, formato e ítems ya vistos. Los chicos ya se
    cosecharon enteros siguiendo el token."""
    fin, items = _cosechados(dir_base)
    hechos = {e["repo"] for e in leer_jsonl(dir_base / AMPLIACION / EVENTOS) if e.get("tipo") == "repo_ampliado"}
    salida = []
    for repo, f in sorted(fin.items()):
        total = f.get("items_repositorio")
        if f.get("estado") != "ok" or repo in hechos or not f.get("endpoint") or not f.get("formato"):
            continue
        if total is not None and total <= ITEMS_POR_REPO_MUESTRA:
            continue
        if len(items[repo]) >= min(tope, total or tope):
            continue
        salida.append({"repo": repo, "endpoint": f["endpoint"], "formato": f["formato"], "total": total,
                       "vistos": items[repo]})
    return salida


async def ampliar_items(
    dir_base: Path,
    contacto: str,
    cfg: ConfigCortesia,
    *,
    tope: int = ITEMS_POR_REPO_AMPLIADA,
    solo: set[str] | None = None,
    ignorar_robots: bool = False,
    transporte: httpx.AsyncBaseTransport | None = None,
    transporte_sin_verificar: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Más ítems para los repositorios de `a_ampliar`: por cada ventana de fechas (y la página sin fechas),
    la primera página y después el resumptionToken, de a una página por ventana por turno, así la muestra
    sigue repartida en el tiempo. Para en `tope` ítems, sin presupuesto o cuando el host se abandona.
    Queda en `ampliacion/` (sus propios pedidos y eventos); `muestra-resumir` suma esos ítems. Se puede
    cortar y relanzar: los repositorios terminados no se repiten y los otros empiezan de nuevo, con otro
    presupuesto (`m0003.a1`)."""
    dir_salida = dir_base / AMPLIACION
    pendientes = [r for r in a_ampliar(dir_base, tope) if solo is None or r["repo"] in solo]
    previos = list(leer_jsonl(dir_salida / PEDIDOS))
    intentos = Counter(e["repo"] for e in leer_jsonl(dir_salida / EVENTOS) if e.get("tipo") == "ampliacion_inicio")
    cola: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for r in pendientes:
        cola.put_nowait(r)
    registro = Registro(dir_salida, _corrida())
    nuevos_total = 0
    try:
        async with Cortesia(cfg, registro, contacto, transporte=transporte,
                            transporte_sin_verificar=transporte_sin_verificar) as cortesia:
            cortesia.sembrar(previos, robots_previos(dir_salida, previos))
            registro.evento("corrida", user_agent=cfg.user_agent(contacto), config=asdict(cfg), tope=tope,
                            repositorios=len(pendientes), ignorar_robots=ignorar_robots)

            async def trabajador() -> None:
                nonlocal nuevos_total
                while not cola.empty():
                    r = cola.get_nowait()
                    try:
                        nuevos_total += await _ampliar_repo(r, cortesia, registro, f"{r['repo']}.a{intentos[r['repo']]}",
                                                            tope, ignorar_robots)
                    except Exception:  # un error de programación no debe frenar al resto, pero queda registrado
                        registro.evento("error_interno", repo=r["repo"], detalle=traceback.format_exc())

            async with asyncio.TaskGroup() as grupo:
                for _ in range(cfg.hosts_en_paralelo):
                    grupo.create_task(trabajador())
    finally:
        registro.cerrar()
    return {"repositorios": len(pendientes), "items_nuevos": nuevos_total}


async def _ampliar_repo(r: dict[str, Any], cortesia: Cortesia, registro: Registro, cuenta: str, tope: int,
                        ignorar_robots: bool) -> int:
    """Amplía un repositorio. Devuelve cuántos ítems nuevos encontró."""
    repo, formato, endpoint = r["repo"], r["formato"], r["endpoint"]
    vistos: set[str] = set(r["vistos"])
    antes = len(vistos)
    objetivo = min(tope, r["total"] or tope)
    registro.evento("ampliacion_inicio", repo=repo, cuenta=cuenta, endpoint=endpoint, formato=formato, ya=antes)
    # Una "ventana" por rango de fechas: [etiqueta, url de la próxima página, páginas seguidas sin nuevos].
    ventanas = deque([list(v) + [0] for v in urls_listrecords(endpoint, formato)])
    corte: str | None = None
    paginas = 0
    while ventanas and len(vistos) < objetivo:
        ventana = ventanas.popleft()
        etiqueta, url, sin_nuevos = ventana
        if cortesia.robots_permitido(url) is False and not ignorar_robots:
            corte = "robots"
            break
        ctx = Contexto(repo=cuenta, paso="ampliacion", metodo=f"oai_{formato}", item=etiqueta)
        try:
            resp = await cortesia.pedir(url, ctx, tipo="pagina", aceptar=ACEPTAR_XML)
        except SinPedido as exc:
            corte = exc.motivo
            break
        paginas += 1
        p = items_de_pagina(resp, formato) if resp.exitosa else PaginaItems(problema=resp.error or f"http_{resp.codigo}")
        registro.evento(
            "pagina_muestra", repo=repo, formato=formato, etiqueta=etiqueta, url=url, codigo=resp.codigo,
            truncado=resp.truncado, problema=p.problema, total=p.total, hay_token=bool(p.token),
            items=[[i.identificador, i.datestamp, i.borrado, i.pagina, [list(asdict(a).values()) for a in i.archivos]]
                   for i in p.items],
        )  # fmt: skip
        nuevos = {i.identificador for i in p.items if i.identificador and not i.borrado} - vistos
        vistos |= nuevos
        sin_nuevos = 0 if nuevos else sin_nuevos + 1
        if p.token and sin_nuevos < PAGINAS_SIN_NUEVOS:
            ventanas.append([etiqueta, f"{endpoint}?verb=ListRecords&resumptionToken={quote(p.token, safe='')}", sin_nuevos])
    encontrados = len(vistos) - antes
    registro.evento("repo_ampliado", repo=repo, paginas=paginas, ya=antes, nuevos=encontrados, items=len(vistos),
                    corte=corte, pedidos=cortesia.cfg.pedidos_por_repo - cortesia.restantes(cuenta))
    return encontrados


def resumir_muestra(dir_base: Path) -> dict[str, Any]:
    """Escribe repositorios.csv, items.csv, archivos.csv, resumen.json y resumen.md sin hacer pedidos.

    Ponderación: cada repositorio útil representa a `población del estrato ÷ útiles del estrato`
    repositorios; por documento, además, pesa su cantidad de documentos del listado. Los tamaños y
    archivos por ítem salen solo de los repositorios cosechados en METS, xoai u ORE (en oai_dc no se
    sabe qué archivos tiene un ítem)."""
    candidatas = leer_candidatos(dir_base)
    poblacion = json.loads((dir_base / POBLACION).read_text(encoding="utf-8"))
    ubicaciones = leer_ubicaciones(dir_base)
    eventos = list(leer_jsonl(dir_base / COSECHA / EVENTOS))
    recon = {e["repo"]: e for e in eventos if e.get("tipo") == "reconocimiento"}
    fin = {e["repo"]: e for e in eventos if e.get("tipo") == "repo_terminado"}
    items: dict[str, dict[str, tuple]] = defaultdict(dict)
    ultima = {e["repo"]: e["corrida"] for e in eventos if e.get("tipo") == "repo_terminado"}

    def fin_ok(repo: str) -> bool:
        return fin.get(repo, {}).get("estado") == "ok"

    for e in eventos:
        if e.get("tipo") == "pagina_muestra" and e.get("corrida") == ultima.get(e["repo"], e.get("corrida")):
            for ident, datestamp, borrado, pagina, archivos in e.get("items", []):
                if ident and not borrado:
                    items[e["repo"]][ident] = (datestamp, pagina, archivos)
    # Los ítems de `muestra-ampliar`, de los repositorios útiles, después de los de la cosecha.
    for e in leer_jsonl(dir_base / AMPLIACION / EVENTOS):
        if e.get("tipo") == "pagina_muestra" and fin_ok(e["repo"]):
            for ident, datestamp, borrado, pagina, archivos in e.get("items", []):
                if ident and not borrado and ident not in items[e["repo"]]:
                    items[e["repo"]][ident] = (datestamp, pagina, archivos)

    filas_repos, filas_items, filas_archivos = [], [], []
    por_repo: dict[str, dict[str, Any]] = {}
    for c in candidatas:
        u, r, f = ubicaciones.get(c.id, {}), recon.get(c.id, {}), fin.get(c.id)
        if f:
            estado, motivo = f.get("estado"), f.get("motivo")
        elif not u:
            estado, motivo = "sin_ubicar", None
        elif u.get("problema"):
            estado, motivo = "sin_ubicacion", u["problema"]
        else:
            estado, motivo = "no_cosechado", "reserva" if c.reserva else None
        del_repo = items.get(c.id, {})
        cantidades = [len(a) for _d, _p, a in del_repo.values()]
        tamanos = [a[1] for _d, _p, archivos in del_repo.values() for a in archivos if a[1]]
        formato = (f or {}).get("formato")
        filas_repos.append({
            "id": c.id, "estrato": c.estrato, "reserva": "si" if c.reserva else "", "repositorio": c.repositorio,
            "documentos": c.documentos, "pais": u.get("pais"), "host": u.get("host"), "url_base": u.get("url_base"),
            "plataforma": r.get("plataforma"), "version": r.get("version"), "oai_endpoint": r.get("oai_endpoint"),
            "oai_fuente": r.get("oai_fuente"), "formato": formato, "identificador_oai": r.get("identificador_oai"),
            "coincide": _si_no((f or {}).get("coincide")),
            "items_repositorio": (f or {}).get("items_repositorio"), "items": len(del_repo),
            "items_con_archivos": sum(1 for n in cantidades if n), "archivos": sum(cantidades),
            "archivos_con_tamano": len(tamanos), "estado": estado, "motivo": motivo,
        })  # fmt: skip
        for ident, (datestamp, pagina, archivos) in del_repo.items():
            filas_items.append({"repo": c.id, "item": ident, "datestamp": datestamp, "pagina": pagina, "archivos": len(archivos)})
            for url, tamano, mime, bundle, nombre in archivos:
                filas_archivos.append({"repo": c.id, "item": ident, "url": url, "tamano": tamano, "mime": mime,
                                       "bundle": bundle, "nombre": nombre})
        if estado == "ok":
            por_repo[c.id] = {"estrato": c.estrato, "documentos": c.documentos, "pais": u.get("pais"),
                              "plataforma": r.get("plataforma"), "formato": formato,
                              "con_tamanos": formato in ("mets", "xoai", "ore"),
                              "_cantidades": cantidades, "_tamanos": tamanos}

    _escribir_csv(dir_base / "repositorios.csv", _COLUMNAS_REPOS, filas_repos)
    _escribir_csv(dir_base / "items.csv", ["repo", "item", "datestamp", "pagina", "archivos"], filas_items)
    _escribir_csv(dir_base / "archivos.csv", ["repo", "item", "url", "tamano", "mime", "bundle", "nombre"], filas_archivos)

    utiles_estrato = Counter(d["estrato"] for d in por_repo.values())
    con_tamanos = {k: d for k, d in por_repo.items() if d["con_tamanos"] and d["_cantidades"]}
    utiles_tamano = Counter(d["estrato"] for d in con_tamanos.values())
    expansion = {k: poblacion[d["estrato"]]["repositorios"] / utiles_tamano[d["estrato"]] for k, d in con_tamanos.items()}

    estratos = {}
    for nombre, _desde, _hasta, cupo in ESTRATOS_MUESTRA:
        intentados = [f for c in candidatas if c.estrato == nombre and (f := fin.get(c.id))]
        estratos[nombre] = {
            "poblacion_repositorios": poblacion[nombre]["repositorios"],
            "poblacion_documentos": poblacion[nombre]["documentos"],
            "cupo": cupo if cupo is None else max(cupo, sum(1 for c in candidatas if c.estrato == nombre and not c.reserva)),
            "candidatos": sum(1 for c in candidatas if c.estrato == nombre),
            "sin_ubicacion": sum(1 for x in filas_repos if x["estrato"] == nombre and x["estado"] == "sin_ubicacion"),
            "cosechados": len(intentados),
            "utiles": utiles_estrato[nombre],
            "utiles_con_tamanos": utiles_tamano[nombre],
            "estados": dict(Counter(f["estado"] for f in intentados)),
        }
    resumen = {
        "estratos": estratos,
        "ponderado_por_documentos": _totales(con_tamanos, {k: expansion[k] * d["documentos"] for k, d in con_tamanos.items()}),
        "ponderado_por_repositorio": _totales(con_tamanos, expansion),
        "sin_ponderar": _totales(con_tamanos, {k: 1.0 for k in con_tamanos}),
        "paises": dict(Counter(d["pais"] or "?" for d in por_repo.values()).most_common()),
        "plataformas": dict(Counter(d["plataforma"] or "?" for d in por_repo.values()).most_common()),
        "formatos": dict(Counter(d["formato"] or "?" for d in por_repo.values()).most_common()),
        "para_la_prueba": {
            "repositorios_utiles": len(por_repo),
            "hosts_distintos": len({x["host"] for x in filas_repos if x["estado"] == "ok"}),
            "items": len(filas_items),
            "items_con_archivos": sum(1 for x in filas_items if x["archivos"]),
            "archivos": len(filas_archivos),
            "archivos_con_tamano": sum(1 for x in filas_archivos if x["tamano"]),
            "bytes_conocidos": sum(x["tamano"] or 0 for x in filas_archivos),
        },
    }
    (dir_base / "resumen.json").write_text(json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")
    (dir_base / "resumen.md").write_text(_markdown(resumen), encoding="utf-8")
    return resumen


def _totales(repos: dict[str, dict[str, Any]], pesos: dict[str, float]) -> dict[str, Any]:
    """Archivos por ítem y tamaño de archivo con un peso por repositorio. Dentro de un repositorio,
    sus ítems pesan igual; por archivo, el repositorio pesa además según sus archivos por ítem."""
    repos = {k: d for k, d in repos.items() if pesos.get(k)}
    if not repos:
        return {}
    suma = sum(pesos[k] for k in repos)
    cantidades, pesos_item, tamanos, pesos_archivo = [], [], [], []
    for k, d in repos.items():
        w = pesos[k] / suma
        n = len(d["_cantidades"])
        cantidades += d["_cantidades"]
        pesos_item += [w / n] * n
        if d["_tamanos"]:
            por_item = statistics.fmean(d["_cantidades"])
            tamanos += d["_tamanos"]
            pesos_archivo += [w * por_item / len(d["_tamanos"])] * len(d["_tamanos"])
    distribucion: Counter[int] = Counter()
    for c, p in zip(cantidades, pesos_item, strict=True):
        distribucion[min(c, 5)] += p
    archivos_por_item = sum(c * p for c, p in zip(cantidades, pesos_item, strict=True))
    media = sum(t * p for t, p in zip(tamanos, pesos_archivo, strict=True)) / sum(pesos_archivo) if tamanos else None
    return {
        "repositorios": len(repos),
        "items": len(cantidades),
        "archivos": len(tamanos),
        "archivos_por_item": round(archivos_por_item, 3),
        "sin_archivos": round(distribucion[0], 3),
        "distribucion_archivos_por_item": {str(k): round(v, 4) for k, v in sorted(distribucion.items())},
        "tamano_p50": _percentil(tamanos, pesos_archivo, 0.5) if tamanos else None,
        "tamano_p90": _percentil(tamanos, pesos_archivo, 0.9) if tamanos else None,
        "tamano_p95": _percentil(tamanos, pesos_archivo, 0.95) if tamanos else None,
        "tamano_p99": _percentil(tamanos, pesos_archivo, 0.99) if tamanos else None,
        "tamano_max": max(tamanos, default=None),
        "tamano_media": round(media) if media else None,
        "bytes_por_item": round(archivos_por_item * media) if media else None,
    }


def _markdown(resumen: dict[str, Any]) -> str:
    def mb(valor: Any) -> str:
        return f"{valor / 1e6:.2f} MB" if valor else "—"

    lineas = ["# Muestra de repositorios de LA Referencia", "", "## Por estrato", "",
              "| Estrato | Población | Documentos | Cupo | Cosechados | Útiles | Con tamaños | Estados |",
              "|---|---|---|---|---|---|---|---|"]
    for nombre, e in resumen["estratos"].items():
        estados = ", ".join(f"{k} {v}" for k, v in sorted(e["estados"].items()))
        lineas.append(f"| {nombre} | {e['poblacion_repositorios']} | {e['poblacion_documentos']:,} | "
                      f"{'todos' if e['cupo'] is None else e['cupo']} | {e['cosechados']} | {e['utiles']} | "
                      f"{e['utiles_con_tamanos']} | {estados} |")
    lineas += ["", "## Archivos (repositorios en METS, xoai u ORE)", "",
               "| | Archivos por ítem | Sin archivos | Mediana | p95 | Media | Bytes por ítem |", "|---|---|---|---|---|---|---|"]
    for clave, titulo in (("ponderado_por_documentos", "Por documento"), ("ponderado_por_repositorio", "Por repositorio"),
                          ("sin_ponderar", "Sin ponderar")):
        t = resumen[clave]
        if t:
            lineas.append(f"| {titulo} | {t['archivos_por_item']} | {t['sin_archivos']:.0%} | {mb(t['tamano_p50'])} | "
                          f"{mb(t['tamano_p95'])} | {mb(t['tamano_media'])} | {mb(t['bytes_por_item'])} |")
    p = resumen["para_la_prueba"]
    lineas += ["", "## Para la prueba de descarga", "",
               f"- {p['repositorios_utiles']} repositorios útiles en {p['hosts_distintos']} hosts distintos.",
               f"- {p['items']:,} ítems ({p['items_con_archivos']:,} con archivos), {p['archivos']:,} archivos "
               f"({p['archivos_con_tamano']:,} con tamaño, {p['bytes_conocidos'] / 1e9:.1f} GB conocidos).",
               "", "## Países, plataformas y formatos (útiles)", "",
               "- Países: " + ", ".join(f"{k} {v}" for k, v in resumen["paises"].items()),
               "- Plataformas: " + ", ".join(f"{k} {v}" for k, v in resumen["plataformas"].items()),
               "- Formatos: " + ", ".join(f"{k} {v}" for k, v in resumen["formatos"].items()), ""]
    return "\n".join(lineas)


def _si_no(valor: Any) -> str:
    return "si" if valor is True else "no" if valor is False else ""


def _escribir_csv(ruta: Path, columnas: list[str], filas: list[dict[str, Any]]) -> None:
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=columnas)
        escritor.writeheader()
        escritor.writerows(filas)


def _corrida() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3]
