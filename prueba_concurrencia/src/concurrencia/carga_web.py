"""Carga que ocupa el lugar de la API y el frontend en la validación de una máquina de producción.

Todavía no existen, así que este proceso reserva la memoria estimada para los dos (`MEMORIA_WEB_MB`,
~450 MB) y usa una fracción de un núcleo (`CPU_WEB`, ~10 %) en ráfagas cortas, como un servidor con
pocos usuarios. Así el motor, el subidor y PostgreSQL se miden compartiendo la máquina con ellos.
"""

import os
import time


def correr(memoria_mb: int, cpu: float, periodo: float = 0.1) -> None:
    # Memoria tocada página por página, para que cuente de verdad (no solo reservada).
    bloque = bytearray(memoria_mb * 1024 * 1024)
    for i in range(0, len(bloque), 4096):
        bloque[i] = 1
    while True:
        inicio = time.monotonic()
        while time.monotonic() - inicio < periodo * cpu:
            pass
        time.sleep(periodo * (1 - cpu))


def main() -> None:
    correr(int(os.environ.get("MEMORIA_WEB_MB", "450")), float(os.environ.get("CPU_WEB", "0.1")))


if __name__ == "__main__":
    main()
