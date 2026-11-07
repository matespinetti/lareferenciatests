"""Modo hilos: la misma prueba con `ThreadPoolExecutor` y requests, solo para comparar.

Es el único módulo con E/S bloqueante, y el código de corrutinas no lo importa. Para que la
comparación sea justa repite el diseño del motor: el mismo planificador (protegido con un lock),
un despachador que entrega tareas cuando hay un lugar libre, un cliente compartido, el mismo
parseo y el mismo SHA-256 por chunk.
"""

import contextlib
import hashlib
import platform
import queue
import socket
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from concurrencia import config, metricas, resolucion
from concurrencia.config import Escenario
from concurrencia.distribucion import items_por_repo
from concurrencia.planificador import Planificador, SeguimientoItems, Tarea, Tipo

_TIMEOUT = (30, 60)
_LOCALES = {"127.0.0.1", "::1", "localhost", None}


@contextlib.contextmanager
def resolver_local() -> Iterator[None]:
    """`*.sim.localhost` a `IP_SERVIDOR` sin DNS, como el resolver del motor; requests no acepta uno.

    Rechaza cualquier host de afuera, así ningún pedido sale de la máquina.
    """
    original = socket.getaddrinfo

    def getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(host, str) and host.endswith("." + config.DOMINIO):
            return original(config.IP_SERVIDOR, port, *args, **kwargs)
        if host in _LOCALES:
            return original(host, port, *args, **kwargs)
        raise socket.gaierror(f"host fuera de la prueba: {host}")

    socket.getaddrinfo = getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = original


def _sesion(escenario: Escenario, certs: Path) -> requests.Session:
    sesion = requests.Session()
    # Un pool por repositorio, como el conector de aiohttp; el tope de 10 por defecto
    # obligaría a reconectar todo el tiempo.
    adaptador = HTTPAdapter(pool_connections=escenario.repos, pool_maxsize=2, max_retries=0)
    sesion.mount("https://", adaptador)
    sesion.verify = str(certs / "ca.pem")
    return sesion


class MotorHilos:
    def __init__(
        self, escenario: Escenario, sesion: requests.Session, puerto: int, semilla: int, procesos: int = 1
    ) -> None:
        self.e = escenario
        self._sesion = sesion
        self._puerto = puerto
        self._procesos = procesos
        self.contadores = metricas.Contadores(con_lock=True)
        items = items_por_repo(escenario.repos, escenario.items, semilla)
        self.plan = Planificador(items, config.INTERVALO, time.monotonic())
        self.items = SeguimientoItems()
        self.cond = threading.Condition()
        self._libres = threading.Semaphore(escenario.lugares)
        self._cola: queue.SimpleQueue[tuple[Tarea, float] | None] = queue.SimpleQueue()
        self.parar = threading.Event()
        self.terminado_solo = False

    def despachar(self) -> None:
        while not self.parar.is_set():
            if not self._libres.acquire(timeout=0.5):
                continue
            tomada = None
            with self.cond:
                while not self.parar.is_set():
                    ahora = time.monotonic()
                    tomada = self.plan.siguiente(ahora)
                    if tomada is not None:
                        break
                    if self.plan.terminado():
                        self.terminado_solo = True
                        self.parar.set()
                        break
                    proximo = self.plan.proximo_listo()
                    espera = 0.5 if proximo is None else min(0.5, max(0.0, proximo - ahora))
                    self.cond.wait(espera)
            if tomada is None:
                self._libres.release()
                return
            self._cola.put(tomada)

    def lugar(self) -> None:
        while (tomada := self._cola.get()) is not None:
            tarea, listo_en = tomada
            despacho = time.monotonic() - listo_en
            try:
                self._ejecutar(tarea, despacho)
            finally:
                self._libres.release()
                with self.cond:
                    self.cond.notify()

    def liberar_lugares(self) -> None:
        for _ in range(self.e.lugares):
            self._cola.put(None)

    def _ejecutar(self, tarea: Tarea, despacho: float) -> None:
        t0 = time.monotonic()
        latencia = servicio = None
        recibidos = 0
        sha: str | None = None
        try:
            with self._sesion.get(resolucion.url(tarea, self._puerto, self._procesos), stream=True, timeout=_TIMEOUT) as r:
                latencia = time.monotonic() - t0
                servicio = float(r.headers.get("X-Tiempo-Servicio", "nan"))
                if r.status_code in (429, 503):
                    recibidos = len(r.content)
                    self.contadores.recibidos(recibidos)
                    espera = float(r.headers.get("Retry-After", config.RETRY_AFTER))
                    with self.cond:
                        self.plan.pausar(tarea.repo, time.monotonic() + espera)
                        self.plan.agregar(tarea)
                    self.contadores.estado(r.status_code)
                elif r.status_code != 200:
                    recibidos = len(r.content)
                    self.contadores.recibidos(recibidos)
                    self.contadores.error(f"http_{r.status_code}")
                elif tarea.tipo is Tipo.RESOLUCION:
                    cuerpo = r.content
                    recibidos = len(cuerpo)
                    self.contadores.recibidos(recibidos)
                    descargas = resolucion.descargas(cuerpo)
                    with self.cond:
                        for descarga in descargas:
                            self.plan.agregar(descarga)
                        completo = self.items.resuelto(tarea.repo, tarea.item, len(descargas))
                    if completo:
                        self.contadores.item_completo()
                else:
                    h = hashlib.sha256()
                    for chunk in r.iter_content(config.CHUNK):
                        h.update(chunk)
                        recibidos += len(chunk)
                        self.contadores.recibidos(len(chunk), hasheados=True)
                        if self.parar.is_set():
                            break
                    else:
                        largo = r.headers.get("Content-Length")
                        if largo is not None and recibidos != int(largo):
                            self.contadores.error("archivo_incompleto")
                        else:
                            sha = h.hexdigest()
                            with self.cond:
                                completo = self.items.descargado(tarea.repo, tarea.item)
                            if completo:
                                self.contadores.item_completo()
        except (requests.RequestException, OSError) as error:
            self.contadores.error(type(error).__name__)
        except Exception as error:
            self.contadores.error(f"inesperado_{type(error).__name__}")
        fin = time.monotonic()
        with self.cond:
            self.plan.terminada(tarea, fin)
            self.cond.notify()
        if self.parar.is_set() and sha is None and tarea.tipo is Tipo.DESCARGA:
            return  # descarga cortada por el fin de la corrida: no es un resultado
        self.contadores.pedido(
            tarea.tipo, fin - t0, latencia, servicio, despacho, descarga_ok=sha is not None,
        )  # fmt: skip


def _medir_retraso(motor: MotorHilos) -> None:
    """Equivalente al retraso del bucle: cuánto se pasa un hilo que duerme 100 ms."""
    while not motor.parar.is_set():
        t = time.monotonic()
        time.sleep(0.1)
        motor.contadores.bucle(time.monotonic() - t - 0.1)


def _muestrear(motor: MotorHilos, muestreador: metricas.Muestreador) -> None:
    # Si el hilo se atrasa, la fila cubre más de un segundo; no se recuperan filas perdidas.
    while not motor.parar.wait(1.0):
        with motor.cond:
            en_curso, listos = motor.plan.en_curso, motor.plan.listos_sin_atender(time.monotonic())
        muestreador.muestra(en_curso, listos)


def _control(
    sesion: requests.Session, puerto: int, ruta: str, metodo: str = "GET", procesos: int = 1
) -> dict[str, Any]:
    estados = []
    for i in range(procesos):
        respuesta = sesion.request(metodo, resolucion.url_control(puerto + i, ruta), timeout=_TIMEOUT)
        respuesta.raise_for_status()
        estados.append(respuesta.json())
    return metricas.combinar_estados(estados) if ruta == "estado" else estados[0]


def correr(
    escenario: Escenario, puerto: int, certs: Path, salida: Path, semilla: int = config.SEMILLA, procesos: int = 1
) -> dict[str, Any]:
    """Corre el escenario en modo hilos y escribe CSV y JSON en `salida`."""
    from concurrent.futures import ThreadPoolExecutor

    with resolver_local(), _sesion(escenario, certs) as sesion:
        _control(sesion, puerto, "reiniciar", "POST", procesos)
        motor = MotorHilos(escenario, sesion, puerto, semilla, procesos)
        muestreador = metricas.Muestreador(motor.contadores, escenario.calentamiento, escenario.lugares)
        motor.contadores.instalar_gc()
        auxiliares = [
            threading.Thread(target=_medir_retraso, args=(motor,), daemon=True),
            threading.Thread(target=_muestrear, args=(motor, muestreador), daemon=True),
            threading.Thread(target=motor.despachar, daemon=True),
        ]
        with ThreadPoolExecutor(max_workers=escenario.lugares, thread_name_prefix="lugar") as pool:
            for _ in range(escenario.lugares):
                pool.submit(motor.lugar)
            for hilo in auxiliares:
                hilo.start()
            motor.parar.wait(escenario.calentamiento + escenario.duracion)
            motor.parar.set()
            with motor.cond:
                motor.cond.notify_all()
            motor.liberar_lugares()
        for hilo in auxiliares:
            hilo.join()
        motor.contadores.quitar_gc()
        servidor = _control(sesion, puerto, "estado", procesos=procesos)
    resumen = {
        "escenario": asdict(escenario),
        "semilla": semilla,
        "puerto": puerto,
        "terminado_antes_de_tiempo": motor.terminado_solo,
        "errores_por_tipo": dict(motor.contadores.errores_por_tipo),
        "servidor": servidor,
        "versiones": {
            "python": sys.version.split()[0],
            "requests": requests.__version__,
            "plataforma": platform.platform(),
        },
        "supuestos": {
            "ancho_por_conexion_mb_s": config.ANCHO_POR_CONEXION / config.MB,
            "ancho_sigma": config.ANCHO_SIGMA,
            "descargas_por_item": config.DESCARGAS_POR_ITEM,
            "intervalo_s": config.INTERVALO,
        },
        "bucle": muestreador.resumen_bucle(),
    }
    metricas.escribir(salida, muestreador.filas, resumen)
    return resumen
