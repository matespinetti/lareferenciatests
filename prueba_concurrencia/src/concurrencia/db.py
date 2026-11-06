"""Registro de resultados en PostgreSQL (`--db`): una transacción por resultado, con asyncpg."""

import time

import asyncpg

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


class Registro:
    def __init__(self, pool: asyncpg.Pool, contadores: Contadores) -> None:
        self._pool = pool
        self._contadores = contadores

    @classmethod
    async def abrir(cls, dsn: str, tamano_pool: int, contadores: Contadores) -> "Registro":
        pool = await asyncpg.create_pool(dsn, min_size=tamano_pool, max_size=tamano_pool)
        async with pool.acquire() as conexion:
            await conexion.execute(_ESQUEMA)
            await conexion.execute("TRUNCATE resultados")
        return cls(pool, contadores)

    async def registrar(self, tarea: Tarea, codigo: int | None, bytes_: int, sha256: str | None) -> None:
        t0 = time.perf_counter()
        async with self._pool.acquire() as conexion:
            t1 = time.perf_counter()
            async with conexion.transaction():
                await conexion.execute(
                    "INSERT INTO resultados (tipo, repo, item, archivo, codigo, bytes, sha256)"
                    " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    tarea.tipo.value, tarea.repo, tarea.item, tarea.archivo, codigo, bytes_, sha256,
                )
        self._contadores.db(espera=t1 - t0, commit=time.perf_counter() - t1)

    async def cerrar(self) -> None:
        await self._pool.close()
