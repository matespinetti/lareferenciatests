"""Arma datos/repos_medidos.json: los repositorios reales con su cantidad de ítems (de `relevamiento
medir-tamanos`) y su velocidad por conexión (de `relevamiento medir-velocidad --desde-tamanos`).

El modelo de duración los usa tal cual y sortea solo el resto de los repositorios.

Uso: python scripts/repos_medidos.py VELOCIDAD_JSON TAMANOS_JSON REGION > datos/repos_medidos.json
"""

import json
import sys
from pathlib import Path


def main() -> None:
    velocidad = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    tamanos = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    repos = {}
    for repo, datos in sorted(tamanos["por_repositorio"].items()):
        mb_s = velocidad["mediana_por_repositorio"].get(repo)
        if datos.get("items_repositorio") and mb_s:
            repos[repo] = {"items": datos["items_repositorio"], "mb_s": mb_s, "archivos_por_item": datos["archivos_por_item"]}
    salida = {
        "region": sys.argv[3],
        "fuente": "relevamiento medir-tamanos (ítems) y medir-velocidad --desde-tamanos (MB/s, mediana por repositorio)",
        "mb_s_por_repositorio": velocidad["mb_s_por_repositorio"],
        "repos": repos,
    }
    print(json.dumps(salida, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
