"""Planificador en memoria: un turno por repositorio.

Cada repositorio recibe un pedido a la vez y, entre el fin de uno y el inicio del siguiente,
pasa al menos `intervalo` segundos. La clave de cortesía es el id del repositorio: con el
simulador todos los hosts resuelven a 127.0.0.1, así que la IP no distingue nada; con la muestra
real es el repositorio de LA Referencia (varias revistas pueden compartir un servidor).

Sin I/O, sin async y sin reloj propio: quien lo usa pasa `ahora`. Tampoco es thread-safe; el modo
hilos lo protege con un lock. Las resoluciones de cada repositorio se generan a medida que hacen
falta desde un contador, así la memoria medida es la del motor y no la de un listado de millones
de ítems.

Entre los repositorios habilitados, primero los que tienen descargas pendientes: terminar lo
empezado antes de empezar otro ítem. Sin esto, con pocos lugares el motor pasaría el arranque
haciendo la primera resolución de los 2.400 repositorios sin descargar casi nada.

Con `vuelta_minima` (modo muestra, corrida larga): un repositorio que termina sus ítems vuelve a
empezar, para que la muestra alcance para una hora a carga plena. Cada vuelta dura al menos
`vuelta_minima` segundos desde que empezó la anterior: un repositorio con pocos ítems espera en vez
de pedir el mismo archivo una y otra vez.
"""

import heapq
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum


class Tipo(StrEnum):
    RESOLUCION = "resolucion"
    DESCARGA = "descarga"


@dataclass(slots=True)
class Tarea:
    tipo: Tipo
    repo: int
    item: int
    archivo: int = 0
    """Índice del archivo dentro del ítem; solo para descargas."""


@dataclass(slots=True)
class _Repo:
    id: int
    items: int
    siguiente_item: int = 0
    descargas: deque[Tarea] = field(default_factory=deque)
    reencoladas: deque[Tarea] = field(default_factory=deque)
    ocupado: bool = False
    listo_en: float = 0.0
    pausa_hasta: float = 0.0
    en_heap: bool = False
    abandonado: bool = False
    primer_item: int = 0
    """Con vueltas: dónde arranca el contador de ítems (después de los ya resueltos)."""
    originales: tuple[Tarea, ...] = ()
    """Con vueltas: las descargas precargadas, para volver a encolarlas en cada vuelta."""
    vuelta: int = 0
    inicio_vuelta: float = 0.0

    def empezado(self) -> bool:
        return bool(self.descargas or self.reencoladas)

    def tiene_trabajo(self) -> bool:
        if self.abandonado:
            return False
        return bool(self.descargas or self.reencoladas or self.siguiente_item < self.items)

    def tomar(self) -> Tarea:
        # Primero las descargas de lo ya resuelto, como el motor real; después lo pendiente.
        if self.descargas:
            return self.descargas.popleft()
        if self.reencoladas:
            return self.reencoladas.popleft()
        tarea = Tarea(Tipo.RESOLUCION, self.id, self.siguiente_item)
        self.siguiente_item += 1
        return tarea


class Planificador:
    def __init__(
        self,
        items_por_repo: list[int],
        intervalo: float,
        inicio: float,
        precargadas: list[list[Tarea]] | None = None,
        vuelta_minima: float | None = None,
        al_reiniciar: Callable[[int], None] | None = None,
    ) -> None:
        """`precargadas` (modo muestra): por repositorio, las descargas de los ítems que ya vienen
        resueltos (los primeros); sus resoluciones no se piden y el contador de ítems arranca después.

        `vuelta_minima`: con un valor, los repositorios que terminan vuelven a empezar (ver arriba) y
        `al_reiniciar(repo)` avisa de cada vuelta nueva."""
        self._intervalo = intervalo
        self._vuelta_minima = vuelta_minima
        self._al_reiniciar = al_reiniciar
        self._repos = [_Repo(id, items, inicio_vuelta=inicio) for id, items in enumerate(items_por_repo)]
        for repo, tareas in zip(self._repos, precargadas or [], strict=False):
            repo.descargas.extend(tareas)
            repo.siguiente_item = repo.primer_item = len({t.item for t in tareas})
            repo.originales = tuple(tareas)
        # Repositorios esperando su turno: con trabajo empezado y con solo ítems nuevos.
        self._empezados: list[tuple[float, int, int]] = []
        self._nuevos: list[tuple[float, int, int]] = []
        self._secuencia = 0
        self._en_curso = 0
        for repo in self._repos:
            if repo.tiene_trabajo():
                repo.listo_en = inicio
                self._encolar(repo)

    @property
    def en_curso(self) -> int:
        return self._en_curso

    def siguiente(self, ahora: float) -> tuple[Tarea, float] | None:
        """La próxima tarea que puede salir ya y el instante en que quedó habilitada, o None.

        El repositorio de la tarea queda ocupado hasta `terminada`.
        """
        for heap in (self._empezados, self._nuevos):
            while heap and heap[0][0] <= ahora:
                listo_en, _, id = heapq.heappop(heap)
                repo = self._repos[id]
                repo.en_heap = False
                if repo.ocupado or not repo.tiene_trabajo():
                    continue
                repo.ocupado = True
                self._en_curso += 1
                return repo.tomar(), listo_en
        return None

    def proximo_listo(self) -> float | None:
        """Cuándo puede quedar habilitada la próxima tarea, o None si no hay nada esperando."""
        topes = [heap[0][0] for heap in (self._empezados, self._nuevos) if heap]
        return min(topes) if topes else None

    def agregar(self, tarea: Tarea) -> None:
        """Agrega una descarga, o vuelve a encolar una tarea que no se pudo hacer."""
        repo = self._repos[tarea.repo]
        if tarea.tipo is Tipo.DESCARGA:
            repo.descargas.append(tarea)
        else:
            repo.reencoladas.append(tarea)
        if not repo.ocupado and not repo.en_heap:
            self._encolar(repo)

    def pausar(self, repo: int, hasta: float) -> None:
        """Ningún pedido a `repo` antes de `hasta`, como pide un `Retry-After`."""
        estado = self._repos[repo]
        estado.pausa_hasta = max(estado.pausa_hasta, hasta)

    def terminada(self, tarea: Tarea, fin: float) -> None:
        """Libera el repositorio de `tarea`; `fin` es cuando terminó de leerse la respuesta."""
        repo = self._repos[tarea.repo]
        repo.ocupado = False
        self._en_curso -= 1
        repo.listo_en = max(fin + self._intervalo, repo.pausa_hasta)
        if not repo.tiene_trabajo() and not repo.abandonado and self._vuelta_minima is not None:
            self._reiniciar(repo)
        if repo.tiene_trabajo():
            self._encolar(repo)

    def _reiniciar(self, repo: _Repo) -> None:
        """Una vuelta nueva: las mismas descargas precargadas y las mismas resoluciones, no antes de
        `vuelta_minima` desde el inicio de la vuelta anterior."""
        assert self._vuelta_minima is not None
        repo.descargas.extend(repo.originales)
        repo.siguiente_item = repo.primer_item
        repo.vuelta += 1
        repo.listo_en = max(repo.listo_en, repo.inicio_vuelta + self._vuelta_minima)
        repo.inicio_vuelta = repo.listo_en
        if self._al_reiniciar is not None:
            self._al_reiniciar(repo.id)

    def vuelta(self, repo: int) -> int:
        """La vuelta en curso de `repo` (0: la primera)."""
        return self._repos[repo].vuelta

    def vueltas(self) -> list[int]:
        """Por repositorio, cuántas vueltas completas terminó."""
        return [repo.vuelta for repo in self._repos]

    def abandonar(self, repo: int) -> None:
        """No le da más tareas a `repo` en esta corrida (anti-bot, 403, red caída, Retry-After eterno)."""
        estado = self._repos[repo]
        estado.abandonado = True
        estado.descargas.clear()
        estado.reencoladas.clear()

    def con_trabajo(self) -> int:
        """Repositorios a los que todavía les queda algo: con pocos, la cortesía limita el paralelismo."""
        return sum(1 for repo in self._repos if repo.tiene_trabajo() or repo.ocupado)

    def listos_sin_atender(self, ahora: float) -> int:
        """Repositorios que podrían recibir un pedido ya y esperan un lugar libre."""
        return sum(1 for heap in (self._empezados, self._nuevos) for listo_en, _, _ in heap if listo_en <= ahora)

    def terminado(self) -> bool:
        return not self._empezados and not self._nuevos and self._en_curso == 0

    def _encolar(self, repo: _Repo) -> None:
        self._secuencia += 1
        repo.en_heap = True
        heap = self._empezados if repo.empezado() else self._nuevos
        heapq.heappush(heap, (repo.listo_en, self._secuencia, repo.id))


class SeguimientoItems:
    """Cuenta los ítems completos: resueltos y con todos sus archivos descargados bien.

    Una descarga cortada o con error no completa el ítem: es trabajo que no sirvió.
    """

    def __init__(self) -> None:
        self._faltan: dict[tuple[int, int], int] = {}

    def resuelto(self, repo: int, item: int, descargas: int) -> bool:
        """Registra cuántas descargas tiene el ítem. Devuelve True si ya está completo."""
        if descargas == 0:
            return True
        self._faltan[(repo, item)] = descargas
        return False

    def descargado(self, repo: int, item: int) -> bool:
        """Descuenta una descarga correcta. Devuelve True cuando el ítem queda completo."""
        clave = (repo, item)
        faltan = self._faltan.get(clave)
        if faltan is None:
            return False
        if faltan == 1:
            del self._faltan[clave]
            return True
        self._faltan[clave] = faltan - 1
        return False
