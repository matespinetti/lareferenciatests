"""Registro de resultados en PostgreSQL (`--db`) con asyncpg.

Dos maneras: una transacción por resultado, que el lugar espera (escenario 8), o en lotes
(`db_lotes`, como el descargador): el lugar encola el resultado y sigue, y una tarea aparte escribe
lo encolado cada `LOTE_DB_SEGUNDOS` en una sola transacción.
"""

import asyncio
import time

import asyncpg

from concurrencia import config
from concurrencia.metricas import Contadores
from concurrencia.planificador import Tarea

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS resultados (
    id bigserial PRIMARY KEY,
    tipo text NOT NULL,
    repo integer NOT NULL,
    item integer NOT NULL,
    archivo integer NOT NULL,
    codigo integer,
    bytes bigint NOT NULL,
    sha256 text,
    registrado timestamptz NOT NULL DEFAULT now()
)
"""
_INSERT = (
    "INSERT INTO resultados (tipo, repo, item, archivo, codigo, bytes, sha256) VALUES ($1, $2, $3, $4, $5, $6, $7)"
)

Fila = tuple[str, int, int, int, int | None, int, str | None]


class Registro:
    def __init__(self, pool: asyncpg.Pool, contadores: Contadores, lotes: bool = False) -> None:
        self._pool = pool
        self._contadores = contadores
        self._lote: list[Fila] | None = [] if lotes else None
        self._parar = asyncio.Event()
        self._escritor = asyncio.create_task(self._escribir_lotes()) if lotes else None

    @classmethod
    async def abrir(cls, dsn: str, tamano_pool: int, contadores: Contadores, lotes: bool = False) -> "Registro":
        pool = await asyncpg.create_pool(dsn, min_size=tamano_pool, max_size=tamano_pool)
        async with pool.acquire() as conexion:
            await conexion.execute(_ESQUEMA)
            await conexion.execute("TRUNCATE resultados")
        return cls(pool, contadores, lotes)

    async def registrar(self, tarea: Tarea, codigo: int | None, bytes_: int, sha256: str | None) -> None:
        fila: Fila = (tarea.tipo.value, tarea.repo, tarea.item, tarea.archivo, codigo, bytes_, sha256)
        if self._lote is not None:
            self._lote.append(fila)
            return
        t0 = time.perf_counter()
        async with self._pool.acquire() as conexion:
            t1 = time.perf_counter()
            async with conexion.transaction():
                await conexion.execute(_INSERT, *fila)
        self._contadores.db(espera=t1 - t0, commit=time.perf_counter() - t1)

    async def _escribir_lotes(self) -> None:
        while not self._parar.is_set():
            try:
                async with asyncio.timeout(config.LOTE_DB_SEGUNDOS):
                    await self._parar.wait()
            except TimeoutError:
                pass
            await self._vaciar()

    async def _vaciar(self) -> None:
        if not self._lote:
            return
        filas, self._lote = self._lote, []
        t0 = time.perf_counter()
        async with self._pool.acquire() as conexion:
            t1 = time.perf_counter()
            async with conexion.transaction():
                await conexion.executemany(_INSERT, filas)
        self._contadores.db(espera=t1 - t0, commit=time.perf_counter() - t1, filas=len(filas))

    async def cerrar(self) -> None:
        if self._escritor is not None:
            # El escritor escribe lo que quedó encolado antes de terminar.
            self._parar.set()
            await self._escritor
        await self._pool.close()
