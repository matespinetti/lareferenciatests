"""resumir: deriva repositorios.csv, resultados.csv y resumen.md de pedidos.jsonl y eventos.jsonl."""

import csv
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from relevamiento.config import METODOS, ConfigCortesia
from relevamiento.extractores import es_derivado
from relevamiento.modelos import Repositorio
from relevamiento.parseo import normalizar_url
from relevamiento.registro import EVENTOS, PEDIDOS, leer_jsonl
from relevamiento.verificar import verificar_pedidos, crawl_delays

COLUMNAS_REPOS = [
    "repo_id", "nombre", "url_base", "plataforma", "version", "fuente_deteccion", "detecciones", "oai_endpoint",
    "oai_fuente", "oai_formatos", "rest", "rest_base", "antibot", "cdn", "tls_invalido", "latencia_mediana_ms",
    "latencia_p90_ms", "hosts_alcanzados", "pedidos", "n_403", "n_429", "errores_red", "motivo_abandono", "terminado",
]
COLUMNAS_RESULTADOS = [
    "repo_id", "item_idx", "item_url", "tipo_url", "metodo", "resultado", "nota", "pedidos", "archivos_encontrados",
    "archivos_verificados", "archivos_equivalentes", "tipos", "bundles",
]


class Datos:
    """Pedidos y eventos vigentes: de cada repo, los eventos de su último intento."""

    def __init__(self, dir_salida: Path, repos: list[Repositorio] | None):
        self.pedidos = list(leer_jsonl(dir_salida / PEDIDOS))
        eventos = list(leer_jsonl(dir_salida / EVENTOS))
        vigente = {e["repo"]: e["corrida"] for e in eventos if e["tipo"] == "reconocimiento"}
        self.eventos = [e for e in eventos if "repo" not in e or e["tipo"] == "abandono" or vigente.get(e["repo"]) == e["corrida"]]
        ids = [r.id for r in repos] if repos else list(vigente)
        self.repos = {r.id: r for r in repos or []}
        self.ids = ids
        self.recon = {e["repo"]: e for e in self._tipo("reconocimiento")}
        self.terminado = {e["repo"]: e for e in self._tipo("repo_terminado")}
        self.pedidos_repo: dict[str, list[dict]] = defaultdict(list)
        for p in self.pedidos:
            self.pedidos_repo[p["repo"]].append(p)
        self.resultados = list(self._tipo("resultado"))
        self.verificaciones: dict[tuple[str, int], dict[str, dict]] = defaultdict(dict)
        for v in self._tipo("verificacion"):
            self.verificaciones[(v["repo"], v["item_idx"])][normalizar_url(v["url"])] = v
        self.items: dict[str, list[dict]] = {e["repo"]: e["items"] for e in self._tipo("items")}

    def _tipo(self, tipo: str) -> Iterable[dict]:
        return (e for e in self.eventos if e["tipo"] == tipo)

    def verificacion(self, repo: str, item_idx: int, url: str) -> dict | None:
        return self.verificaciones[(repo, item_idx)].get(normalizar_url(url))


def resumir(dir_salida: Path, repos: list[Repositorio] | None = None, cfg: ConfigCortesia = ConfigCortesia()) -> list[Path]:
    datos = Datos(dir_salida, repos)
    filas_repos = [fila_repositorio(datos, r) for r in datos.ids]
    filas_resultados = [fila_resultado(datos, e) for e in datos.resultados]
    rutas = [
        _csv(dir_salida / "repositorios.csv", COLUMNAS_REPOS, filas_repos),
        _csv(dir_salida / "resultados.csv", COLUMNAS_RESULTADOS, filas_resultados),
    ]
    verificacion = verificar_pedidos(datos.pedidos, crawl_delays(dir_salida, datos.pedidos, cfg), cfg)
    resumen = dir_salida / "resumen.md"
    resumen.write_text(markdown(datos, filas_repos, filas_resultados, verificacion), encoding="utf-8")
    return rutas + [resumen]


# --- Filas -----------------------------------------------------------------------------


def fila_repositorio(datos: Datos, repo_id: str) -> dict[str, Any]:
    repo = datos.repos.get(repo_id)
    rec = datos.recon.get(repo_id, {})
    pedidos = datos.pedidos_repo.get(repo_id, [])
    mediana, p90 = cuantiles([p["ms_encabezados"] for p in pedidos if p.get("ms_encabezados") is not None])
    terminado = datos.terminado.get(repo_id)
    return {
        "repo_id": repo_id,
        "nombre": repo.nombre if repo else "",
        "url_base": repo.url_base if repo else "",
        "plataforma": rec.get("plataforma"),
        "version": rec.get("version"),
        "fuente_deteccion": rec.get("fuente_deteccion"),
        "detecciones": "; ".join(
            f"{d['fuente']}={d['plataforma']}{' ' + d['version'] if d.get('version') else ''}" for d in rec.get("detecciones", [])
        ),
        "oai_endpoint": rec.get("oai_endpoint"),
        "oai_fuente": rec.get("oai_fuente"),
        "oai_formatos": " ".join(rec.get("oai_formatos") or []),
        "rest": rec.get("rest") or ("no probado" if any(k.startswith("rest_") for k in rec.get("no_probado", {})) else "no"),
        "rest_base": rec.get("rest_base"),
        "antibot": "; ".join(sorted({f"{p['antibot']} ({p['host']})" for p in pedidos if p.get("antibot")})),
        "cdn": rec.get("cdn"),
        "tls_invalido": " ".join(sorted({p["host"] for p in pedidos if p.get("error") == "tls_invalido"})),
        "latencia_mediana_ms": mediana,
        "latencia_p90_ms": p90,
        "hosts_alcanzados": " ".join(sorted({p["host"] for p in pedidos if p.get("codigo") is not None})),
        "pedidos": len(pedidos),
        "n_403": sum(1 for p in pedidos if p.get("codigo") == 403),
        "n_429": sum(1 for p in pedidos if p.get("codigo") == 429),
        "errores_red": sum(1 for p in pedidos if p.get("error") and p["error"] != "tls_invalido"),
        "motivo_abandono": "; ".join(f"{p['host']}: {p['abandono']}" for p in pedidos if p.get("abandono")),
        "terminado": terminado["motivo_fin"] if terminado else "no",
    }


def fila_resultado(datos: Datos, e: dict) -> dict[str, Any]:
    verificados, equivalentes, tipos = 0, 0, set()
    for c in e["candidatos"]:
        v = datos.verificacion(e["repo"], e["item_idx"], c["url"]) or {}
        if v.get("ok"):
            verificados += 1
            tipos.add(v.get("magic"))
        elif v.get("equivalente_a"):
            equivalente = datos.verificacion(e["repo"], e["item_idx"], v["equivalente_a"]) or {}
            if equivalente.get("ok"):
                equivalentes += 1
                tipos.add(equivalente.get("magic"))
    return {
        "repo_id": e["repo"],
        "item_idx": e["item_idx"],
        "item_url": e["item_url"],
        "tipo_url": e["tipo_url"],
        "metodo": e["metodo"],
        "resultado": e["resultado"],
        "nota": e.get("nota"),
        "pedidos": len(e["pedidos"]),
        "archivos_encontrados": len(e["candidatos"]),
        "archivos_verificados": verificados,
        "archivos_equivalentes": equivalentes,
        "tipos": " ".join(sorted(t for t in tipos if t)),
        "bundles": " ".join(sorted({c["bundle"] for c in e["candidatos"] if c.get("bundle")})),
    }


def cuantiles(valores: list[int]) -> tuple[int | None, int | None]:
    if not valores:
        return None, None
    if len(valores) == 1:
        return valores[0], valores[0]
    return round(statistics.median(valores)), round(statistics.quantiles(valores, n=10, method="inclusive")[8])


# --- resumen.md ------------------------------------------------------------------------


def markdown(datos: Datos, repos: list[dict], resultados: list[dict], verificacion) -> str:
    partes = ["# Resumen del relevamiento exploratorio", ""]
    no_terminados = [r["repo_id"] for r in repos if r["terminado"] == "no"]
    partes.append(f"{len(repos)} repositorios, {len(datos.pedidos)} pedidos.")
    if no_terminados:
        partes.append(f"**Sin terminar:** {', '.join(no_terminados)}. Sus datos están incompletos.")
    partes += ["", "## 1. Plataforma y versión", ""]
    partes.append(_tabla(
        ["repo", "plataforma", "versión", "fuente", "todas las detecciones"],
        [[r["repo_id"], r["plataforma"], r["version"], r["fuente_deteccion"], r["detecciones"]] for r in repos],
    ))
    con_plataforma = sum(1 for r in repos if r["plataforma"])
    con_version = sum(1 for r in repos if r["version"])
    partes.append(f"\nPlataforma detectada en {con_plataforma} de {len(repos)}; versión en {con_version}.")
    fuentes = Counter(r["fuente_deteccion"] or "sin detección" for r in repos)
    partes += ["", _tabla(["fuente", "repositorios"], fuentes.most_common())]

    partes += ["", "## 2. Métodos por plataforma y versión", "",
               "Cada celda: ítems con resultado ok sobre ítems donde el método aplicaba, y mediana de pedidos de los ok.", ""]
    grupo_de = {r["repo_id"]: _grupo(r) for r in repos}
    celdas: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for fila in resultados:
        celdas[(grupo_de.get(fila["repo_id"], "?"), fila["metodo"])].append(fila)
    grupos = sorted({g for g, _ in celdas})
    partes.append(_tabla(["plataforma"] + list(METODOS), [[g] + [_celda(celdas.get((g, m), [])) for m in METODOS] for g in grupos]))

    partes += ["", "## 3. Archivos por ítem", ""]
    filas_items, dspace_original, dspace_total, otros_bundles = [], 0, 0, Counter()
    for (repo, idx), filas in sorted(_por_item(resultados).items()):
        candidatos = [c for e in datos.resultados if e["repo"] == repo and e["item_idx"] == idx for c in e["candidatos"]]
        contenido = [c for c in candidatos if not es_derivado(c.get("bundle"))]
        nombres = {c.get("nombre") or normalizar_url(c["url"]) for c in contenido}
        ok = [v for v in datos.verificaciones[(repo, idx)].values() if v.get("ok")]
        bundles = Counter(c["bundle"] for c in candidatos if c.get("bundle"))
        filas_items.append([repo, idx, len(nombres), len(ok), " ".join(sorted({v["magic"] for v in ok})),
                            " ".join(f"{b}×{n}" for b, n in sorted(bundles.items()))])
        con_bundle = [c for c in contenido if c.get("bundle")]
        if grupo_de.get(repo, "").startswith("dspace") and con_bundle:
            dspace_total += 1
            if all(c["bundle"] == "ORIGINAL" for c in con_bundle):
                dspace_original += 1
            otros_bundles.update(c["bundle"] for c in con_bundle if c["bundle"] != "ORIGINAL")
    partes.append(_tabla(["repo", "ítem", "archivos de contenido", "verificados ok", "tipos", "bundles vistos"], filas_items))
    partes.append(f"\nDSpace: en {dspace_original} de {dspace_total} ítems con bundles conocidos, todo el contenido está en ORIGINAL."
                  + (f" Otros bundles con contenido: {', '.join(f'{b} ({n})' for b, n in otros_bundles.most_common())}." if otros_bundles else ""))

    partes += ["", "## 4. Tipos de URL", ""]
    tipos = Counter(filas[0]["tipo_url"] for filas in _por_item(resultados).values())
    total = sum(tipos.values()) or 1
    partes.append(_tabla(["tipo", "ítems", "%"], [[t, n, f"{100 * n / total:.0f}"] for t, n in tipos.most_common()]))

    partes += ["", "## 5. Anti-bot, 403, 429 y TLS", ""]
    problemas = [r for r in repos if r["antibot"] or r["n_403"] or r["n_429"] or r["tls_invalido"] or r["motivo_abandono"]]
    partes.append(_tabla(["repo", "anti-bot", "403", "429", "TLS inválido", "abandono"],
                         [[r["repo_id"], r["antibot"], r["n_403"], r["n_429"], r["tls_invalido"], r["motivo_abandono"]] for r in problemas])
                  if problemas else "Ningún repositorio con problemas.")
    partes.append(f"\nCon anti-bot: {sum(1 for r in repos if r['antibot'])} · con 403: {sum(1 for r in repos if r['n_403'])} · "
                  f"con 429: {sum(1 for r in repos if r['n_429'])} · con TLS inválido: {sum(1 for r in repos if r['tls_invalido'])} "
                  f"(de {len(repos)}).")

    partes += ["", "## 6. Latencias (ms hasta los encabezados)", ""]
    partes.append(_tabla(["repo", "mediana", "p90"], [[r["repo_id"], r["latencia_mediana_ms"], r["latencia_p90_ms"]] for r in repos]))
    mediana, p90 = cuantiles([p["ms_encabezados"] for p in datos.pedidos if p.get("ms_encabezados") is not None])
    partes.append(f"\nGlobal: mediana {mediana} ms, p90 {p90} ms.")

    partes += ["", "## Cortesía", ""]
    est = verificacion.estadisticas
    partes.append("verificar-cortesia: " + ("**OK**" if verificacion.ok else f"**{len(verificacion.violaciones)} violaciones**"))
    if est:
        partes.append(f"\n{est['pedidos']} pedidos a {est['hosts']} hosts en {est['corridas']} corrida(s), {est['duracion_s']} s. "
                      f"Hueco mínimo entre pedidos al mismo host: {est['hueco_minimo_s']} s. "
                      f"Máximo en vuelo: {est['max_en_vuelo']}.")
    partes += ["", "Desvíos de la spec: tope total de 120 s por pedido; eventos.jsonl como registro adicional; "
               "los bundles THUMBNAIL, TEXT, LICENSE y CC-LICENSE no se verifican.", ""]
    return "\n".join(partes)


def _grupo(fila: dict) -> str:
    if not fila["plataforma"]:
        return "desconocida"
    m = re.match(r"(\d+)", fila["version"] or "")
    return f"{fila['plataforma']} {m.group(1)}" if m else fila["plataforma"]


def _celda(filas: list[dict]) -> str:
    aplicables = [f for f in filas if f["resultado"] != "no aplica"]
    if not aplicables:
        return "—"
    ok = [f for f in aplicables if f["resultado"] == "ok"]
    texto = f"{len(ok)}/{len(aplicables)}"
    if ok:
        texto += f" · {statistics.median(f['pedidos'] for f in ok):g} ped."
    sin_probar = sum(1 for f in aplicables if f["resultado"] == "no probado")
    return texto + (f" ({sin_probar} sin probar)" if sin_probar else "")


def _por_item(resultados: list[dict]) -> dict[tuple[str, int], list[dict]]:
    por_item: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for fila in resultados:
        por_item[(fila["repo_id"], fila["item_idx"])].append(fila)
    return por_item


def _tabla(encabezados: list[str], filas: Iterable[Iterable[Any]]) -> str:
    def celda(v: Any) -> str:
        return "" if v is None else str(v).replace("|", "\\|")

    lineas = ["| " + " | ".join(encabezados) + " |", "| " + " | ".join("---" for _ in encabezados) + " |"]
    lineas += ["| " + " | ".join(celda(v) for v in fila) + " |" for fila in filas]
    return "\n".join(lineas)


def _csv(ruta: Path, columnas: list[str], filas: list[dict]) -> Path:
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        escritor = csv.DictWriter(f, fieldnames=columnas)
        escritor.writeheader()
        escritor.writerows(filas)
    return ruta
