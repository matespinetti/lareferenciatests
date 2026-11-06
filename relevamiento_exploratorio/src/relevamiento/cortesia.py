"""Único componente que hace pedidos HTTP. Toda la cortesía de la spec vive acá.

Cada pedido, incluidos los saltos de una redirección y robots.txt, pasa por el turno de su host:
un lock más el instante en que terminó el último pedido a ese host.
"""

import asyncio
import ssl
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal
from urllib.parse import urljoin, urlsplit

import httpx

from relevamiento.config import ConfigCortesia
from relevamiento.detectores import Robots, detectar_antibot, parsear_robots
from relevamiento.modelos import Contexto, Respuesta, Salto, parece_archivo
from relevamiento.parseo import es_host_publico, host_de, normalizar_url
from relevamiento.registro import Registro

TipoPedido = Literal["pagina", "archivo", "medicion"]
"""`medicion` solo la usa `medir-velocidad`: lee hasta `bytes_medicion` o `segundos_medicion` de un
archivo, sin `Range`, y guarda solo los primeros bytes."""
_REDIRECCIONES = (301, 302, 303, 307, 308)


class SinPedido(Exception):
    """El pedido no salió a la red. Lo que falta se marca como no probado."""

    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo
        self.pedido_ids: tuple[str, ...] = ()


class PresupuestoAgotado(SinPedido):
    def __init__(self, repo: str):
        super().__init__("presupuesto agotado")
        self.repo = repo


class HostAbandonado(SinPedido):
    def __init__(self, host: str, motivo: str):
        super().__init__(f"host abandonado ({host}): {motivo}")
        self.host = host


class HostNoPublico(SinPedido):
    """Loopback, red privada o intranet: una URL así en un repo mal configurado no debe salir de la máquina."""

    def __init__(self, host: str):
        super().__init__(f"host no público ({host})")
        self.host = host


class Reloj:
    """Tiempo del event loop, que es monótono; en los tests lo adelanta looptime."""

    def __init__(self) -> None:
        self._ancla: tuple[datetime, float] | None = None

    def ahora(self) -> float:
        return asyncio.get_running_loop().time()

    def ahora_utc(self) -> datetime:
        if self._ancla is None:
            self._ancla = (datetime.now(timezone.utc), self.ahora())
        utc, mono = self._ancla
        return utc + timedelta(seconds=self.ahora() - mono)

    async def dormir(self, segundos: float) -> None:
        await asyncio.sleep(segundos)


@dataclass
class EstadoHost:
    """Reglas de abandono. Una vez abandonado, el host queda así por el resto de la corrida."""

    abandonado: str | None = None
    seguidos_403: int = 0
    seguidos_red: int = 0

    def registrar(self, codigo: int | None, error: str | None, antibot: str | None, cfg: ConfigCortesia) -> str | None:
        """Actualiza los contadores con un pedido terminado; devuelve el motivo si el host queda abandonado."""
        if self.abandonado or error in ("cancelado", "tls_invalido"):
            return None
        motivo = None
        if antibot:
            motivo = f"anti-bot: {antibot}"
        elif codigo in (429, 503):
            motivo = f"código {codigo}"
        elif error or codigo in (502, 504):
            self.seguidos_red += 1
            self.seguidos_403 = 0
            if self.seguidos_red >= cfg.abandono_red_seguidos:
                motivo = f"{self.seguidos_red} errores de red seguidos"
        elif codigo == 403:
            self.seguidos_403 += 1
            self.seguidos_red = 0
            if self.seguidos_403 >= cfg.abandono_403_seguidos:
                motivo = f"{self.seguidos_403} respuestas 403 seguidas"
        else:
            self.seguidos_403 = self.seguidos_red = 0
        self.abandonado = motivo
        return motivo


@dataclass
class TurnoHost:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ultimo_fin: float | None = None
    ultima_duracion: float = 0.0
    crawl_delay: float = 0.0
    robots: Robots | None = None
    robots_resuelto: bool = False
    robots_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tls_invalido: bool = False
    estado: EstadoHost = field(default_factory=EstadoHost)


def espera_necesaria(turno: TurnoHost, ahora: float, cfg: ConfigCortesia) -> float:
    if turno.ultimo_fin is None:
        return 0.0
    intervalo = max(cfg.intervalo_minimo, cfg.factor_latencia * turno.ultima_duracion, turno.crawl_delay)
    return max(0.0, turno.ultimo_fin + intervalo - ahora)


def es_error_certificado(exc: BaseException) -> bool:
    visto: BaseException | None = exc
    while visto is not None:
        if isinstance(visto, ssl.SSLCertVerificationError):
            return True
        visto = visto.__cause__ or visto.__context__
    return False


def _es_error_ssl(exc: BaseException) -> bool:
    visto: BaseException | None = exc
    while visto is not None:
        if isinstance(visto, ssl.SSLError):
            return True
        visto = visto.__cause__ or visto.__context__
    return False


@dataclass(frozen=True)
class _Intercambio:
    pedido_id: str
    url: str
    codigo: int | None
    encabezados: tuple[tuple[str, str], ...]
    cuerpo: bytes
    truncado: bool
    ms_encabezados: int | None
    error: str | None
    antibot: str | None
    tls_verificado: bool
    ids_previos: tuple[str, ...] = ()  # el pedido con TLS inválido que precedió a un reintento

    @property
    def pedido_ids(self) -> tuple[str, ...]:
        return self.ids_previos + (self.pedido_id,)

    def location(self) -> str | None:
        for clave, valor in self.encabezados:
            if clave.lower() == "location":
                return valor
        return None


class Cortesia:
    def __init__(
        self,
        cfg: ConfigCortesia,
        registro: Registro,
        contacto: str,
        *,
        reloj: Reloj | None = None,
        transporte: httpx.AsyncBaseTransport | None = None,
        transporte_sin_verificar: httpx.AsyncBaseTransport | None = None,
    ):
        if not contacto.strip():
            raise ValueError("Falta el contacto para el User-Agent")
        self.cfg = cfg
        self.registro = registro
        self.reloj = reloj or Reloj()
        self._turnos: dict[str, TurnoHost] = defaultdict(TurnoHost)
        self._usados: dict[str, int] = defaultdict(int)
        self._en_vuelo = asyncio.Semaphore(cfg.hosts_en_paralelo)
        self._contador = 0
        timeout = httpx.Timeout(cfg.timeout_lectura, connect=cfg.timeout_conexion)
        comunes: dict[str, Any] = dict(
            headers={"User-Agent": cfg.user_agent(contacto)},
            timeout=timeout,
            follow_redirects=False,
        )
        self._cliente = httpx.AsyncClient(
            transport=transporte or httpx.AsyncHTTPTransport(verify=True, http1=True, http2=False, retries=0),
            **comunes,
        )
        self._cliente_sin_verificar = httpx.AsyncClient(
            transport=transporte_sin_verificar
            or httpx.AsyncHTTPTransport(verify=False, http1=True, http2=False, retries=0),
            **comunes,
        )

    async def __aenter__(self) -> "Cortesia":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._cliente.aclose()
        await self._cliente_sin_verificar.aclose()

    # --- Consultas ---------------------------------------------------------------------

    def restantes(self, repo: str) -> int:
        return self.cfg.pedidos_por_repo - self._usados[repo]

    def abandono(self, host: str) -> str | None:
        return self._turnos[host.lower()].estado.abandonado

    def robots_permitido(self, url: str) -> bool | None:
        turno = self._turnos.get(host_de(url))
        return turno.robots.permitido(url) if turno and turno.robots else None

    # --- Reanudación -------------------------------------------------------------------

    def sembrar(self, pedidos_previos: Iterable[dict[str, Any]], robots_previos: dict[str, tuple[int, bytes]]) -> None:
        """Restaura presupuesto, intervalos, abandonos y robots de corridas anteriores."""
        ahora, ahora_utc = self.reloj.ahora(), self.reloj.ahora_utc()
        for p in pedidos_previos:
            self._usados[p["repo"]] += 1
            turno = self._turnos[p["host"]]
            fin = datetime.fromisoformat(p["fin"])
            duracion = (fin - datetime.fromisoformat(p["inicio"])).total_seconds()
            fin_mono = ahora - (ahora_utc - fin).total_seconds()
            if turno.ultimo_fin is None or fin_mono > turno.ultimo_fin:
                turno.ultimo_fin, turno.ultima_duracion = fin_mono, duracion
            if p.get("tls_verificado") is False:
                turno.tls_invalido = True
            if p.get("abandono"):
                turno.estado.abandonado = p["abandono"]
        for host, (codigo, cuerpo) in robots_previos.items():
            self._aplicar_robots(self._turnos[host], codigo, cuerpo)

    # --- Pedidos -----------------------------------------------------------------------

    async def pedir(
        self, url: str, ctx: Contexto, *, tipo: TipoPedido = "pagina", aceptar: str | None = None
    ) -> Respuesta:
        """Pide una URL siguiendo redirecciones a mano. Lanza SinPedido si no puede salir ningún pedido más."""
        saltos: list[Salto] = []
        ids: list[str] = []
        visitadas = {normalizar_url(url)}
        actual = url
        cadena: str | None = None
        error_final: str | None = None
        try:
            for numero in range(self.cfg.max_redirecciones + 1):
                host = host_de(actual)
                if not es_host_publico(host):
                    raise HostNoPublico(host)
                if ctx.metodo != "robots":
                    await self._asegurar_robots(actual, ctx)
                ix = await self._un_pedido(actual, host, ctx, tipo, aceptar, numero, cadena)
                cadena = cadena or ix.pedido_ids[0]
                ids.extend(ix.pedido_ids)
                location = ix.location()
                saltos.append(Salto(actual, ix.codigo, location, ix.pedido_id))
                if ix.codigo not in _REDIRECCIONES or not location:
                    break
                siguiente = urljoin(actual, location.strip()).split("#")[0]
                if urlsplit(siguiente).scheme not in ("http", "https") or normalizar_url(siguiente) in visitadas:
                    error_final = "redireccion_invalida"
                    break
                if numero == self.cfg.max_redirecciones:
                    error_final = "demasiadas_redirecciones"
                    break
                visitadas.add(normalizar_url(siguiente))
                actual = siguiente
        except SinPedido as exc:
            exc.pedido_ids = tuple(ids) + exc.pedido_ids
            raise
        return Respuesta(
            url_inicial=url,
            url=actual,
            codigo=ix.codigo,
            encabezados=ix.encabezados,
            cuerpo=ix.cuerpo,
            truncado=ix.truncado,
            ms_encabezados=ix.ms_encabezados,
            error=ix.error or error_final,
            antibot=ix.antibot,
            tls_verificado=ix.tls_verificado,
            saltos=tuple(saltos),
            pedido_ids=tuple(ids),
        )

    async def _asegurar_robots(self, url: str, ctx: Contexto) -> None:
        host = host_de(url)
        turno = self._turnos[host]
        if turno.robots_resuelto:
            return
        async with turno.robots_lock:
            if turno.robots_resuelto:
                return
            esquema = urlsplit(url).scheme
            ctx_robots = Contexto(repo=ctx.repo, paso=ctx.paso, metodo="robots")
            r = await self.pedir(f"{esquema}://{urlsplit(url).netloc}/robots.txt", ctx_robots)
            self._aplicar_robots(turno, r.codigo, r.cuerpo)

    def _aplicar_robots(self, turno: TurnoHost, codigo: int | None, cuerpo: bytes) -> None:
        turno.robots_resuelto = True
        if codigo == 200:
            turno.robots = parsear_robots(cuerpo, self.cfg.agente)
            turno.crawl_delay = turno.robots.crawl_delay

    async def _un_pedido(
        self,
        url: str,
        host: str,
        ctx: Contexto,
        tipo: TipoPedido,
        aceptar: str | None,
        salto: int,
        cadena: str | None,
        reintento_tls: bool = False,
    ) -> _Intercambio:
        turno = self._turnos[host]
        async with turno.lock:
            if turno.estado.abandonado:
                raise HostAbandonado(host, turno.estado.abandonado)
            if self.restantes(ctx.repo) <= 0:
                raise PresupuestoAgotado(ctx.repo)
            espera = espera_necesaria(turno, self.reloj.ahora(), self.cfg)
            if espera > 0:
                await self.reloj.dormir(espera)
            ix = await self._intercambio(url, host, turno, ctx, tipo, aceptar, salto, cadena, espera, reintento_tls)
        if ix.error == "tls_invalido" and not reintento_tls:
            # Única excepción a la regla de no reintentar: una vez, sin verificar, respetando el turno.
            turno.tls_invalido = True
            try:
                reintento = await self._un_pedido(url, host, ctx, tipo, aceptar, salto, cadena or ix.pedido_id, True)
            except SinPedido:
                return ix
            return replace(reintento, ids_previos=(ix.pedido_id,))
        return ix

    async def _intercambio(
        self,
        url: str,
        host: str,
        turno: TurnoHost,
        ctx: Contexto,
        tipo: TipoPedido,
        aceptar: str | None,
        salto: int,
        cadena: str | None,
        espera: float,
        reintento_tls: bool,
    ) -> _Intercambio:
        cfg = self.cfg
        async with self._en_vuelo:
            self._usados[ctx.repo] += 1
            self._contador += 1
            pedido_id = f"{self.registro.corrida}-{self._contador:05d}"
            sin_verificar = turno.tls_invalido
            cliente = self._cliente_sin_verificar if sin_verificar else self._cliente
            encabezados_pedido: dict[str, str] = {}
            medicion = tipo == "medicion"
            if tipo == "archivo":
                encabezados_pedido["Range"] = f"bytes=0-{cfg.bytes_archivo - 1}"
            if tipo in ("archivo", "medicion"):
                encabezados_pedido["Accept-Encoding"] = "identity"
            if aceptar:
                encabezados_pedido["Accept"] = aceptar

            codigo: int | None = None
            encabezados: tuple[tuple[str, str], ...] = ()
            cuerpo = bytearray()
            truncado = False
            ms: int | None = None
            error: str | None = None
            bytes_red = 0
            leidos = 0
            limite = {"archivo": cfg.bytes_archivo, "medicion": cfg.bytes_medicion}.get(tipo, cfg.bytes_pagina)
            inicio = self.reloj.ahora()
            inicio_utc = self.reloj.ahora_utc()
            try:
                async with asyncio.timeout(cfg.timeout_total):
                    async with cliente.stream("GET", url, headers=encabezados_pedido) as resp:
                        ms = round((self.reloj.ahora() - inicio) * 1000)
                        codigo = resp.status_code
                        encabezados = tuple(resp.headers.multi_items())
                        if not medicion and parece_archivo(
                            resp.headers.get("content-type"), resp.headers.get("content-disposition")
                        ):
                            limite = cfg.bytes_archivo
                        encabezados_en = self.reloj.ahora()
                        async for trozo in resp.aiter_bytes():
                            if medicion:
                                # Se cuentan los bytes y se guardan solo los primeros (tipo y anti-bot).
                                leidos += len(trozo)
                                cuerpo += trozo[: max(0, cfg.bytes_archivo - len(cuerpo))]
                                agotado = self.reloj.ahora() - encabezados_en >= cfg.segundos_medicion
                                if leidos >= limite or agotado:
                                    largo = resp.headers.get("content-length")
                                    truncado = largo is None or not largo.isdigit() or int(largo) > leidos
                                    break
                                continue
                            falta = limite - len(cuerpo)
                            cuerpo += trozo[:falta]
                            if len(trozo) > falta:
                                truncado = True
                            if len(cuerpo) >= limite:
                                largo = resp.headers.get("content-length")
                                truncado = truncado or largo is None or not largo.isdigit() or int(largo) > limite
                                break
                        bytes_red = resp.num_bytes_downloaded
            except TimeoutError:
                error = "timeout_total"
            except httpx.ConnectTimeout:
                error = "timeout_conexion"
            except httpx.TimeoutException:
                error = "timeout_lectura"
            except httpx.TransportError as exc:
                if es_error_certificado(exc) and not sin_verificar:
                    error = "tls_invalido"
                elif _es_error_ssl(exc):
                    error = "tls_protocolo"
                else:
                    error = "red"
            except httpx.InvalidURL:
                error = "url_invalida"
            except asyncio.CancelledError:
                error = "cancelado"
                raise
            finally:
                fin = self.reloj.ahora()
                turno.ultimo_fin = fin
                turno.ultima_duracion = fin - inicio
                antibot = detectar_antibot(codigo, encabezados, bytes(cuerpo)) if codigo is not None else None
                motivo = turno.estado.registrar(codigo, error, antibot, cfg)
                captura = None
                if codigo is not None:
                    captura = self.registro.captura(
                        pedido_id, host, url=url, codigo=codigo, encabezados=encabezados,
                        cuerpo=bytes(cuerpo), truncado=truncado, tipo=tipo,
                    )
                cabecera = dict((k.lower(), v) for k, v in encabezados)
                self.registro.pedido({
                    "id": pedido_id,
                    "repo": ctx.repo,
                    "item": ctx.item,
                    "item_idx": ctx.item_idx,
                    "paso": ctx.paso,
                    "metodo": ctx.metodo,
                    "url": url,
                    "host": host,
                    "salto": salto,
                    "cadena": cadena or pedido_id,
                    "reintento_tls": reintento_tls,
                    "inicio": inicio_utc.isoformat(),
                    "fin": (inicio_utc + timedelta(seconds=fin - inicio)).isoformat(),
                    "espera_s": round(espera, 3),
                    "codigo": codigo,
                    "ms_encabezados": ms,
                    "bytes": bytes_red,
                    "bytes_cuerpo": leidos if medicion else len(cuerpo),
                    "truncado": truncado,
                    "tipo": tipo,
                    "content_type": cabecera.get("content-type"),
                    "content_length": cabecera.get("content-length"),
                    "content_range": cabecera.get("content-range"),
                    "location": cabecera.get("location"),
                    "tls_verificado": not sin_verificar,
                    "error": error,
                    "antibot": antibot,
                    "robots_permitido": turno.robots.permitido(url) if turno.robots else None,
                    "abandono": motivo,
                    "captura": captura,
                })
                if motivo:
                    self.registro.evento("abandono", repo=ctx.repo, host=host, motivo=motivo, pedido=pedido_id)
        return _Intercambio(
            pedido_id, url, codigo, encabezados, bytes(cuerpo), truncado, ms, error, antibot, not sin_verificar
        )
