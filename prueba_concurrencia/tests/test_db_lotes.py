"""El registro en lotes, con un pool falso: no hace falta PostgreSQL."""

import asyncio
import contextlib

from concurrencia import config, metricas
from concurrencia.db import Registro
from concurrencia.planificador import Tarea, Tipo


class _Conexion:
    def __init__(self, lotes: list[list[tuple]]) -> None:
        self.lotes = lotes

    def transaction(self) -> contextlib.AbstractAsyncContextManager[None]:
        return contextlib.nullcontext()  # type: ignore[return-value]

    async def executemany(self, sql: str, filas: list[tuple]) -> None:
        await asyncio.sleep(0.01)
        self.lotes.append(list(filas))


class _Pool:
    def __init__(self) -> None:
        self.lotes: list[list[tuple]] = []
        self.cerrado = False

    @contextlib.asynccontextmanager
    async def acquire(self):  # type: ignore[no-untyped-def]
        yield _Conexion(self.lotes)

    async def close(self) -> None:
        self.cerrado = True


async def test_en_lotes_el_lugar_no_espera_y_se_escribe_todo(monkeypatch):
    monkeypatch.setattr(config, "LOTE_DB_SEGUNDOS", 0.05)
    pool, contadores = _Pool(), metricas.Contadores(con_lock=False)
    registro = Registro(pool, contadores, lotes=True)  # type: ignore[arg-type]
    for i in range(5):
        # Encolar no espera a la base.
        async with asyncio.timeout(0.001):
            await registro.registrar(Tarea(Tipo.DESCARGA, 1, i, 0), 200, 100, "abc")
    await asyncio.sleep(0.1)
    for i in range(5, 8):
        await registro.registrar(Tarea(Tipo.RESOLUCION, 2, i, 0), 200, 10, None)
    await registro.cerrar()
    filas = [f for lote in pool.lotes for f in lote]
    assert len(filas) == 8 and len(pool.lotes) >= 2 and pool.cerrado
    assert contadores.cortar().db_filas == 8
