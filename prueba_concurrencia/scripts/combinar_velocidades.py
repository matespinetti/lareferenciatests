"""Combina mediciones de velocidad (`velocidad.json` de `relevamiento medir-velocidad`) en la mediana y
los cuartiles por repositorio que usa el simulador para los repositorios no medidos.

Junta las medianas por repositorio de todas las mediciones. Si un repositorio está en varias, vale
la del primer archivo: pasá primero la de `--desde-tamanos` (5 archivos por repositorio).

Uso: python3 scripts/combinar_velocidades.py PRINCIPAL.json [OTRA.json ...]
Imprime la línea `export ANCHO_MB_S=… ANCHO_P25=… ANCHO_P75=…`; el detalle va a stderr.
"""

import json
import statistics
import sys
from pathlib import Path


def combinar(rutas: list[Path]) -> dict[str, float]:
    medianas: dict[str, float] = {}
    for ruta in rutas:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        for repo, mb_s in datos["mediana_por_repositorio"].items():
            medianas.setdefault(repo, mb_s)
    return medianas


def main() -> None:
    rutas = [Path(r) for r in sys.argv[1:]]
    if not rutas:
        sys.exit(__doc__)
    medianas = combinar(rutas)
    p25, p50, p75 = statistics.quantiles(sorted(medianas.values()), n=4)
    for repo, mb_s in sorted(medianas.items(), key=lambda par: par[1]):
        print(f"  {repo:20} {mb_s:6.2f} MB/s", file=sys.stderr)
    print(f"{len(medianas)} repositorios: p25 {p25:.2f}, mediana {p50:.2f}, p75 {p75:.2f} MB/s", file=sys.stderr)
    print(f"export ANCHO_MB_S={p50:.2f} ANCHO_P25={p25:.2f} ANCHO_P75={p75:.2f}")


if __name__ == "__main__":
    main()
