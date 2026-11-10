"""Informe: tablas por escenario, gráficos, proyección al problema real y criterio de decisión.

Lee `results/<escenario>.csv` y `.json`, descarta el calentamiento y escribe `report.md`. Las
cuentas de la proyección quedan escritas en el informe para que se puedan revisar.
"""

import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from concurrencia import config
from concurrencia.config import Modo
from concurrencia.distribucion import ARCHIVO, LATENCIA, PAGINA, ancho_repo, items_por_repo
from concurrencia.metricas import percentil

# Criterio de decisión de la spec (propuesto, a acordar).
DESPACHO_P99_MAX_MS = 100.0
BUCLE_P99_MAX_MS = 50.0
RSS_MAX_FRACCION = 0.5
DERIVA_RSS_MAX = 0.10
"""Crecimiento de la RSS en la hora del escenario 9, como fracción de la media."""
CPU_SERVIDOR_MAX = 90.0
"""Si el servidor pasa este % de un núcleo, el escenario no es válido: el límite fue él."""


@dataclass
class Resultado:
    id: str
    escenario: dict[str, Any]
    filas: list[dict[str, float]]
    datos: dict[str, Any]

    def col(self, nombre: str) -> list[float]:
        return [f.get(nombre, 0.0) for f in self.filas]

    def media(self, nombre: str) -> float:
        """Media pesada por lo que duró cada fila: si el bucle se atrasa, una fila cubre más de un
        segundo, y la media simple de las filas les daría menos peso a esos segundos lentos."""
        total = sum(f["dt"] for f in self.filas)
        return sum(f.get(nombre, 0.0) * f["dt"] for f in self.filas) / total if total else 0.0

    def integral(self, nombre: str) -> float:
        """Lo acumulado de una columna por segundo (MB, pedidos, segundos de CPU × 100)."""
        return sum(f.get(nombre, 0.0) * f["dt"] for f in self.filas)


def cargar(resultados: Path) -> list[Resultado]:
    salida = []
    for ruta in sorted(resultados.glob("*.json")):
        datos = json.loads(ruta.read_text())
        if "escenario" not in datos or not ruta.with_suffix(".csv").exists():
            continue
        with ruta.with_suffix(".csv").open() as f:
            filas = [{k: float(v) for k, v in fila.items()} for fila in csv.DictReader(f)]
        anterior = 0.0
        for fila in filas:
            fila["dt"], anterior = fila["t"] - anterior, fila["t"]
        medicion = [f for f in filas if not f["calentamiento"]]
        # El nombre del archivo: el id del escenario, o con un sufijo si se repitió con otra variante (`b120-host`).
        salida.append(Resultado(ruta.stem, datos["escenario"], medicion, datos))
    return sorted(salida, key=lambda r: (len(r.id), r.id))


def _pendiente(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(set(xs)) < 2:
        return 0.0
    return statistics.linear_regression(xs, ys).slope


def resumir(r: Resultado) -> dict[str, Any]:
    e, servidor = r.escenario, r.datos["servidor"]
    cpu = r.col("cpu")
    pedidos = r.integral("pedidos")
    mb = r.integral("mb_recibidos")
    cpu_seg = r.integral("cpu") / 100
    calentamiento = int(e["calentamiento"])
    cpu_srv = servidor.get("cpu_servidor", [])[calentamiento:] or [0.0]
    subida = r.datos.get("subida", {})
    cpu_s3 = subida.get("s3", {}).get("cpu", [])[calentamiento:] or [0.0]
    descargas = r.col("duracion_descarga_s")
    resoluciones = r.col("duracion_resolucion_s")
    rss = r.col("rss_mb")
    t = r.col("t")
    return {
        "id": r.id,
        "descripcion": e["descripcion"],
        "modo": e["modo"],
        "lugares": e["lugares"],
        "repos": e["repos"],
        "db": e["db"],
        "red": e.get("red"),
        "grupo": e.get("grupo"),
        "instancia": e.get("instancia"),
        "segundos": len(r.filas),
        "items_s": r.media("items_ok"),
        "ocupacion": r.media("ocupacion"),
        "pedidos_s": r.media("pedidos"),
        "mb_s": r.media("mb_recibidos"),
        "cpu_media": r.media("cpu"),
        "cpu_max": max(cpu, default=0.0),
        "cpu_sistema": r.media("cpu_sistema"),
        "rss_max": max(rss, default=0.0),
        "deriva_rss_mb_h": _pendiente(t, rss) * 3600,
        "rss_media": statistics.fmean(rss) if rss else 0.0,
        "hilos_max": max(r.col("hilos"), default=0),
        "descriptores_max": max(r.col("descriptores"), default=0),
        "conexiones_max": max(r.col("conexiones_tcp"), default=0),
        "bucle_p99": r.datos.get("bucle", {}).get("p99_ms", percentil(r.col("bucle_p99_ms"), 0.99)),
        "bucle_max": r.datos.get("bucle", {}).get("max_ms", max(r.col("bucle_max_ms"), default=0.0)),
        # Conservador: percentil 99 de los p99 de cada segundo.
        "despacho_p99": percentil(r.col("despacho_p99_ms"), 0.99),
        "listos_sin_atender": r.media("listos_sin_atender"),
        "lat_p50": statistics.median(r.col("lat_p50_ms")) if r.filas else 0.0,
        "lat_p95": statistics.median(r.col("lat_p95_ms")) if r.filas else 0.0,
        "agregada_p95": statistics.median(r.col("agregada_p95_ms")) if r.filas else 0.0,
        "gc_ms_s": r.media("gc_ms"),
        "steal": r.media("steal"),
        "cpu_ms_por_pedido": 1000 * cpu_seg / pedidos if pedidos else 0.0,
        "cpu_ms_por_mb": 1000 * cpu_seg / mb if mb else 0.0,
        "t_resolucion": statistics.fmean([x for x in resoluciones if x]) if any(resoluciones) else 0.0,
        "t_descarga": statistics.fmean([x for x in descargas if x]) if any(descargas) else 0.0,
        "db_tx": r.media("db_tx"),
        "db_commit_p95": statistics.median(r.col("db_commit_p95_ms")) if r.filas else 0.0,
        "errores": sum(r.datos["errores_por_tipo"].values()),
        "errores_por_tipo": r.datos["errores_por_tipo"],
        "violaciones": servidor["violaciones"],
        "cpu_servidor_media": statistics.fmean(cpu_srv),
        "cpu_servidor_max": max(cpu_srv),
        "valido": max(cpu_srv) < CPU_SERVIDOR_MAX and max(cpu_s3) < CPU_SERVIDOR_MAX,
        "subida": e.get("subida", False),
        "modo_subida": subida.get("modo", e.get("modo_subida", "proceso")),
        "subidor_mismo_nucleo": subida.get("mismo_nucleo", e.get("subidor_mismo_nucleo", False)),
        "subidor_rss_max": max(r.col("subidor_rss_mb"), default=0.0),
        "subida_paralelo": subida.get("paralelo"),
        "workers": e.get("workers", 0) if e["modo"] == Modo.MULTIPROCESO else 0,
        "cpu_workers": r.media("cpu_workers"),
        "cpu_workers_max": max(r.col("cpu_workers"), default=0.0),
        "rss_workers_max": max(r.col("rss_workers_mb"), default=0.0),
        "bucle_workers_max": max(r.col("bucle_workers_max_ms"), default=0.0),
        "bucle_workers_p99": max((w.get("bucle_p99_ms", 0.0) for w in r.datos.get("multiproceso", {}).get("bucle_workers", [])), default=0.0),
        "subida_mb_s": r.media("subida_mb_s"),
        "pendiente_ram_max": max(r.col("pendiente_ram_mb"), default=0.0),
        "pendiente_disco_max": max(r.col("pendiente_disco_mb"), default=0.0),
        "frenado_s": sum(r.col("frenado_s")),
        "disco_escritura_mb_s": r.media("disco_escritura_mb_s"),
        "subidor_cpu": r.media("subidor_cpu"),
        "subidor_cpu_max": max(r.col("subidor_cpu"), default=0.0),
        "s3_cpu_max": max(cpu_s3),
        "subida_errores": subida.get("subidor", {}).get("errores", 0),
        "pendiente_final_mb": sum(subida.get("pendiente_final_bytes", {}).values()) / config.MB,
        "subida_multipart": subida.get("s3", {}).get("multipart", 0),
        "subida_objetos": subida.get("s3", {}).get("objetos", 0),
        "terminado_antes": r.datos.get("terminado_antes_de_tiempo", False),
        "dos_maquinas": r.datos.get("despliegue", {}).get("dos_maquinas", False),
        "s3_real": r.datos.get("despliegue", {}).get("s3") == "real",
        "red_docker": r.datos.get("despliegue", {}).get("red_docker"),
        "db_lotes": e.get("db_lotes", False),
        # Los resultados de antes de los lotes no tienen la columna: ahí cada transacción era una fila.
        "db_filas": r.media("db_filas") if r.filas and "db_filas" in r.filas[0] else r.media("db_tx"),
        "cliente_subida": subida.get("cliente", "boto3"),
        "destino_subida": subida.get("destino", "s3falso"),
        "maquina_cpu": r.media("maquina_cpu"),
        "maquina_cpu_max": max(r.col("maquina_cpu"), default=0.0),
        "memoria_disponible_min": min(r.col("memoria_disponible_mb"), default=0.0),
        "deriva_memoria_mb_h": _pendiente(t, r.col("memoria_disponible_mb")) * 3600,
        "disco_escritura_max": max(r.col("disco_escritura_mb_s"), default=0.0),
    }


def _interpolar(puntos: list[tuple[float, float]], x: float) -> float | None:
    """Interpolación lineal en `puntos` (x creciente); fuera del rango, el extremo más cercano."""
    if not puntos:
        return None
    if x <= puntos[0][0]:
        return puntos[0][1]
    for (x0, y0), (x1, y1) in zip(puntos, puntos[1:], strict=False):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return puntos[-1][1]


def modelo_item(ancho: float) -> dict[str, float]:
    """Cuánto ocupa un ítem a su repositorio con `ancho` bytes/s por conexión (modelo calibrado:
    latencia de los pilotos; la página llega de una vez; cada archivo, a `ancho`; los archivos por
    ítem, con la frecuencia de los pilotos)."""
    descargas = config.DESCARGAS_POR_ITEM
    t_res = LATENCIA.media()
    t_desc = LATENCIA.media() + ARCHIVO.media() / ancho
    return {
        "descargas": descargas,
        "t_res": t_res,
        "t_desc": t_desc,
        "ciclo": (t_res + config.INTERVALO) + descargas * (t_desc + config.INTERVALO),
        "ocupado": t_res + descargas * t_desc,
        "bytes_item": PAGINA.media() + descargas * ARCHIVO.media(),
    }


def modelo_repos(mediana: float, semilla: int = config.SEMILLA, sigma: float = config.ANCHO_SIGMA) -> dict[str, Any]:
    """El modelo del ítem para cada repositorio, con su propia velocidad por conexión."""
    anchos = [ancho_repo(semilla, repo, mediana, sigma) for repo in range(config.REPOS_REALES)]
    modelos = [modelo_item(a) for a in anchos]
    return {
        "anchos": anchos,
        "ciclos": [m["ciclo"] for m in modelos],
        "ocupados": [m["ocupado"] for m in modelos],
        "bytes_item": modelos[0]["bytes_item"],
    }


def sensibilidad(simulada: config.Instancia, anchos: tuple[float, ...] = config.ANCHOS_SENSIBILIDAD) -> list[dict[str, Any]]:
    """La duración y las corrutinas según la mediana de la velocidad por conexión, con la red de `simulada`."""
    items = items_por_repo(config.REPOS_REALES, config.ITEMS_REALES, config.SEMILLA)
    filas = []
    for mb_s in anchos:
        m = modelo_repos(mb_s * config.MB)
        c = curva(items, m["ciclos"], m["ocupados"], m["bytes_item"], simulada.red_base)
        filas.append(
            {
                "mb_s": mb_s,
                "ciclo": modelo_item(mb_s * config.MB)["ciclo"],
                "duracion_cortesia_dias": _mas_lento(items, m)["dias"],
                "duracion_dias": c["duracion_dias"],
                "lugares": c["lugares_red"],
            }
        )
    return filas


def _mas_lento(items: list[int], m: dict[str, Any]) -> dict[str, float]:
    """El repositorio que más tarda solo, por la cortesía: sus ítems por su ciclo."""
    repo = max(range(len(items)), key=lambda r: items[r] * m["ciclos"][r])
    return {"items": items[repo], "mb_s": m["anchos"][repo] / config.MB, "dias": items[repo] * m["ciclos"][repo] / 86400}


DURACION_MUESTRAS = 30
"""Sorteos de velocidades con los que el informe da el rango de duración."""


def rango_duracion(
    items: list[int], mediana: float, red: float | None, muestras: int = DURACION_MUESTRAS
) -> dict[str, float]:
    """La duración con distintos sorteos de qué repositorio es rápido o lento: percentiles 10, 50 y 90."""
    duraciones = []
    for semilla in range(1, muestras + 1):
        m = modelo_repos(mediana, semilla)
        duraciones.append(curva(items, m["ciclos"], m["ocupados"], m["bytes_item"], red)["duracion_dias"])
    q = statistics.quantiles(duraciones, n=10)
    return {"p10": q[0], "p50": statistics.median(duraciones), "p90": q[-1], "muestras": muestras}


def proyeccion(
    resumenes: list[dict[str, Any]], simulada: config.Instancia | None = None, ancho: float | None = None
) -> dict[str, Any]:
    """El problema real (2.400 repositorios, 6 M de ítems) con lo calibrado y lo medido."""
    items = items_por_repo(config.REPOS_REALES, config.ITEMS_REALES, config.SEMILLA)
    mediana = ancho or config.ANCHO_POR_CONEXION
    # Un ítem típico, con la velocidad mediana; cada repositorio, con la suya.
    tipico = modelo_item(mediana)
    m = modelo_repos(mediana)
    descargas, bytes_item = tipico["descargas"], m["bytes_item"]
    total_bytes = config.ITEMS_REALES * bytes_item
    pico_items_s = sum(1 / c for c in m["ciclos"])
    pico_mb_s = pico_items_s * bytes_item / config.MB
    # La capacidad de un proceso sale de los escenarios sin tope de red.
    corrutinas = [r for r in resumenes if r["modo"] == Modo.CORRUTINAS and r["mb_s"] > 0 and not r["red"]]
    validos = [r for r in corrutinas if r["valido"] and r["violaciones"] == 0 and r["errores"] == 0]
    capacidad = max(validos or corrutinas, key=lambda r: r["mb_s"], default=None)
    referencia = next((r for r in resumenes if r["id"] == "4"), capacidad)
    # La CPU por MB sale del escenario sin tope que más bajó; si no hay, del que más bajó con tope
    # (sobreestima un poco: con poco tráfico pesa más el costo fijo de las conexiones).
    fuente_cpu = capacidad or max(
        (r for r in resumenes if r["modo"] == Modo.CORRUTINAS and r["mb_s"] > 0), key=lambda r: r["mb_s"], default=None
    )
    cpu_ms_por_mb = fuente_cpu["cpu_ms_por_mb"] if fuente_cpu else 0.0
    simulada = simulada or config.instancia()
    redes = {
        f"{simulada.nombre}, línea base": simulada.red_base,
        f"{simulada.nombre}, ráfaga": simulada.red_rafaga,
    }
    # Memoria según los lugares, medida en el barrido con red (si se corrió).
    barrido = sorted((r for r in resumenes if r["grupo"] == config.BARRIDO_RED), key=lambda r: r["lugares"])
    rss_por_lugares = [(float(r["lugares"]), r["rss_max"]) for r in barrido]
    candidatas = []
    for inst in config.INSTANCIAS.values():
        c = curva(items, m["ciclos"], m["ocupados"], bytes_item, inst.red_base)
        lugares = c["lugares_pico"]
        mb_s = min(inst.red_base, pico_mb_s * config.MB) / config.MB
        cpu = 100 * cpu_ms_por_mb / 1000 * mb_s if cpu_ms_por_mb else None
        candidatas.append(
            {
                "instancia": inst,
                "duracion_dias": c["duracion_dias"],
                "lugares": lugares,
                "mb_s": mb_s,
                "cpu": cpu,
                "cpu_alcanza": None if cpu is None else cpu <= inst.cpu_disponible,
                "rss_mb": _interpolar(rss_por_lugares, lugares),
                "costo": inst.precio_hora * c["duracion_dias"] * 24,
            }
        )
    mas_lento = _mas_lento(items, m)
    return {
        "simulada": simulada,
        "candidatas": candidatas,
        "items_max": max(items),
        "items_mediana": statistics.median(items),
        "items_top4": sum(sorted(items, reverse=True)[: config.REPOS_REALES // 25]) / config.ITEMS_REALES,
        "descargas_por_item": descargas,
        "ancho_mediana_mb_s": mediana / config.MB,
        "ancho_cuartiles_mb_s": [round(x / config.MB, 2) for x in statistics.quantiles(m["anchos"], n=4)[::2]],
        "t_res": tipico["t_res"],
        "t_desc": tipico["t_desc"],
        "t_res_medido": referencia["t_resolucion"] if referencia else None,
        "t_desc_medido": referencia["t_descarga"] if referencia else None,
        "ciclo_item": tipico["ciclo"],
        "ocupado_item": tipico["ocupado"],
        "curvas": {
            nombre: curva(items, m["ciclos"], m["ocupados"], bytes_item, red)
            for nombre, red in {**redes, "sin tope de red": None}.items()
        },
        "rango": {
            nombre: rango_duracion(items, mediana, red)
            for nombre, red in {f"{simulada.nombre}, línea base": simulada.red_base, "sin tope de red": None}.items()
        },
        "bytes_item": bytes_item,
        "total_tb": total_bytes / 1e12,
        "pico_pedidos_s": pico_items_s * (1 + descargas),
        "pico_mb_s": pico_mb_s,
        "pico_lugares": sum(o / c for o, c in zip(m["ocupados"], m["ciclos"], strict=True)),
        "mas_lento": mas_lento,
        "duracion_cortesia_dias": mas_lento["dias"],
        "duracion_red_dias": {nombre: total_bytes / red / 86400 for nombre, red in redes.items()},
        "red_mb_s": {nombre: red / config.MB for nombre, red in redes.items()},
        "cpu_nucleos_para_red": {nombre: cpu_ms_por_mb / 1000 * red / config.MB for nombre, red in redes.items()},
        "cpu_ms_por_mb": cpu_ms_por_mb,
        "cpu_mb_escenario": fuente_cpu["id"] if fuente_cpu else None,
        "capacidad_mb_s": capacidad["mb_s"] if capacidad else None,
        "capacidad_escenario": capacidad["id"] if capacidad else None,
        "capacidad_es_cota_inferior": bool(capacidad and not capacidad["valido"]),
    }


FRACCION_RODILLA = 0.95


def rodilla(barrido: list[dict[str, Any]], clave: str) -> dict[str, Any] | None:
    """El escenario con menos lugares que llega al 95 % del máximo de `clave` en el barrido."""
    maximo = max((r[clave] for r in barrido), default=0.0)
    if maximo <= 0:
        return None
    return next(r for r in sorted(barrido, key=lambda r: r["lugares"]) if r[clave] >= FRACCION_RODILLA * maximo)


def barridos(resumenes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Por grupo de barrido: los escenarios en orden, la rodilla y el costo de sobrar."""
    salida = {}
    for grupo in (config.BARRIDO_RED, config.BARRIDO_SIN_TOPE):
        filas = sorted((r for r in resumenes if r["grupo"] == grupo), key=lambda r: r["lugares"])
        if len(filas) < 2:
            continue
        por_mb = rodilla(filas, "mb_s")
        ultimo = filas[-1]
        sobran = ultimo["lugares"] - por_mb["lugares"] if por_mb else 0
        salida[grupo] = {
            "filas": filas,
            "rodilla_mb": por_mb,
            "sobran": sobran,
            # Lo que cuesta cada lugar de más después de la rodilla.
            "rss_kb_por_lugar": 1024 * (ultimo["rss_max"] - por_mb["rss_max"]) / sobran if sobran > 0 else 0.0,
            "cpu_por_lugar": (ultimo["cpu_media"] - por_mb["cpu_media"]) / sobran if sobran > 0 else 0.0,
        }
    return salida


def curva(
    items: list[int], ciclos: float | list[float], ocupados: float | list[float], bytes_item: float, red: float | None
) -> dict[str, Any]:
    """Modelo de fluido de toda la descarga: repositorios activos y lugares necesarios en el tiempo.

    Cada repositorio avanza a su ritmo: el de la cortesía (un ítem cada `ciclos[r]` segundos, que
    depende de su velocidad) o, si la red no alcanza para todos, la parte que le toca. La red se
    reparte en partes iguales y el que pide menos que su parte se lleva lo que pide (max-min).
    Lugares necesarios: cada repositorio ocupa `ocupados[r]` segundos de cada ítem.
    `ciclos` y `ocupados` pueden ser un número, igual para todos.
    """
    import numpy as np

    n = np.asarray(items, dtype=float)
    activos = n > 0
    restantes = n[activos]
    ciclo = np.broadcast_to(np.asarray(ciclos, dtype=float), n.shape)[activos]
    ocupado = np.broadcast_to(np.asarray(ocupados, dtype=float), n.shape)[activos]
    maximo = 1 / ciclo
    t, puntos = 0.0, []

    def punto(t: float, ritmo: Any, ocupado: Any) -> dict[str, Any]:
        return {
            "t": t,
            "activos": int(ritmo.size),
            "lugares": float((ritmo * ocupado).sum()),
            "items_s": float(ritmo.sum()),
            "mb_s": float(ritmo.sum() * bytes_item / config.MB),
        }

    while restantes.size and red is not None and (maximo.sum() * bytes_item) > red:
        # La red no alcanza: cada repositorio recibe lo que pide o el nivel común, lo que sea menor.
        ritmo = np.minimum(maximo * bytes_item, _nivel(maximo * bytes_item, red)) / bytes_item
        puntos.append(punto(t, ritmo, ocupado))
        dt = float((restantes / ritmo).min())
        t += dt
        restantes = restantes - ritmo * dt
        quedan = restantes > 1e-6
        restantes, maximo, ocupado = restantes[quedan], maximo[quedan], ocupado[quedan]
    # Desde acá la red alcanza (y seguirá alcanzando: terminar repositorios solo baja la demanda):
    # cada repositorio termina a su ritmo de cortesía.
    if restantes.size:
        orden = np.argsort(t + restantes / maximo)
        fin = (t + restantes / maximo)[orden]
        maximo, ocupado = maximo[orden], ocupado[orden]
        items_s = np.cumsum(maximo[::-1])[::-1]
        lugares = np.cumsum((maximo * ocupado)[::-1])[::-1]
        inicios = np.concatenate([[t], fin[:-1]])
        for k in range(fin.size):
            puntos.append(
                {
                    "t": float(inicios[k]),
                    "activos": int(fin.size - k),
                    "lugares": float(lugares[k]),
                    "items_s": float(items_s[k]),
                    "mb_s": float(items_s[k] * bytes_item / config.MB),
                }
            )
        t = float(fin[-1])
    puntos.append({"t": t, "activos": 0, "lugares": 0.0, "items_s": 0.0, "mb_s": 0.0})
    pico = max(p["lugares"] for p in puntos)
    return {
        "red": red,
        "duracion_dias": t / 86400,
        "lugares_pico": pico,
        "lugares_red": math.inf if red is None else pico,
        "puntos": puntos,
    }


def _nivel(demanda: Any, red: float) -> float:
    """El nivel λ con Σ min(demanda, λ) = red (reparto max-min de la red). Requiere Σ demanda > red."""
    import numpy as np

    d = np.sort(demanda)
    previos = np.concatenate([[0.0], np.cumsum(d)[:-1]])
    niveles = (red - previos) / (d.size - np.arange(d.size))
    return float(niveles[np.argmax(niveles <= d)])


def en_el_dia(curva_: dict[str, Any], dia: float) -> dict[str, Any]:
    """El estado del modelo en el día `dia` (función escalonada)."""
    segundos = dia * 86400
    return [p for p in curva_["puntos"] if p["t"] <= segundos][-1]


def criterio(
    resumenes: list[dict[str, Any]], simulada: config.Instancia | None = None
) -> list[tuple[str, str, bool | None]]:
    """(escenario, condición, cumple) para la instancia simulada. None si el escenario no se corrió."""
    simulada = simulada or config.instancia()
    linea_base = simulada.cpu_disponible
    rss_max = RSS_MAX_FRACCION * simulada.memoria / config.MB
    texto_cpu = (
        f"CPU media dentro de la línea base de la {simulada.nombre} ({linea_base:.0f} % de un núcleo)"
        if simulada.linea_base_cpu is not None
        else f"CPU media < {linea_base:.0f} % de un núcleo (un proceso usa uno; margen para Postgres)"
    )
    por_id = {r["id"]: r for r in resumenes}
    filas: list[tuple[str, str, bool | None]] = []
    for id in ("4", "9"):
        r = por_id.get(id)
        condiciones = [
            ("Cero violaciones de cortesía y cero errores", lambda r: r["violaciones"] == 0 and r["errores"] == 0),
            (f"Retraso de despacho p99 < {DESPACHO_P99_MAX_MS:.0f} ms", lambda r: r["despacho_p99"] < DESPACHO_P99_MAX_MS),
            (f"Retraso del bucle p99 < {BUCLE_P99_MAX_MS:.0f} ms", lambda r: r["bucle_p99"] < BUCLE_P99_MAX_MS),
            (texto_cpu, lambda r: r["cpu_media"] <= linea_base),
            (f"RSS máxima < {rss_max:.0f} MB", lambda r: r["rss_max"] < rss_max),
            ("El servidor simulado no fue el límite", lambda r: r["valido"]),
        ]  # fmt: skip
        if id == "9":
            condiciones.append(
                (
                    f"Memoria estable: deriva < {DERIVA_RSS_MAX:.0%} de la RSS media por hora",
                    lambda r: abs(r["deriva_rss_mb_h"]) < DERIVA_RSS_MAX * r["rss_media"],
                )
            )
        for texto, cumple in condiciones:
            filas.append((id, texto, None if r is None else bool(cumple(r))))
    return filas


def _graficos(resumenes: list[dict[str, Any]], destino: Path) -> bool:
    sinteticos = [r for r in resumenes if not r["db"] and not r["grupo"] and r["id"] not in {"6", "9"}]
    if not sinteticos:
        return False
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figura, ejes = plt.subplots(1, 3, figsize=(15, 4.5))
    medidas = [("cpu_media", "CPU media (% de un núcleo)"), ("rss_max", "RSS máxima (MB)"), ("bucle_p99", "Retraso p99 (ms)")]
    for eje, (clave, titulo) in zip(ejes, medidas, strict=True):
        for modo, marca in ((Modo.CORRUTINAS, "o-"), (Modo.HILOS, "s--")):
            puntos = sorted((r["lugares"], r[clave]) for r in sinteticos if r["modo"] == modo)
            if puntos:
                eje.plot(*zip(*puntos, strict=True), marca, label=str(modo))
        eje.set_xscale("log")
        eje.set_xlabel("Lugares de ejecución")
        eje.set_title(titulo)
        eje.grid(alpha=0.3)
        eje.legend()
    figura.tight_layout()
    figura.savefig(destino, dpi=120)
    plt.close(figura)
    return True


def _grafico_barrido(grupos: dict[str, dict[str, Any]], destino: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figura, ejes = plt.subplots(1, 3, figsize=(15, 4.5))
    medidas = [("mb_s", "MB/s bajados"), ("rss_max", "RSS máxima (MB)"), ("cpu_media", "CPU media (% de un núcleo)")]
    for eje, (clave, titulo) in zip(ejes, medidas, strict=True):
        for grupo, datos in grupos.items():
            xs = [r["lugares"] for r in datos["filas"]]
            linea = eje.plot(xs, [r[clave] for r in datos["filas"]], "o-", label=_NOMBRE_GRUPO[grupo])[0]
            if datos["rodilla_mb"]:
                eje.axvline(datos["rodilla_mb"]["lugares"], color=linea.get_color(), linestyle=":", alpha=0.7)
        eje.set_xscale("log")
        eje.set_xlabel("Lugares de ejecución (corrutinas)")
        eje.set_title(titulo)
        eje.grid(alpha=0.3)
        eje.legend()
    figura.tight_layout()
    figura.savefig(destino, dpi=120)
    plt.close(figura)


def _grafico_curva(curvas: dict[str, dict[str, Any]], destino: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figura, ejes = plt.subplots(1, 2, figsize=(12, 4.5))
    for nombre, c in curvas.items():
        dias = [p["t"] / 86400 for p in c["puntos"]]
        ejes[0].step(dias, [p["activos"] for p in c["puntos"]], where="post", label=nombre)
        ejes[1].step(dias, [p["lugares"] for p in c["puntos"]], where="post", label=nombre)
    for eje, titulo in zip(ejes, ("Repositorios activos", "Lugares necesarios"), strict=True):
        eje.set_xlabel("Días desde el inicio")
        eje.set_title(titulo)
        eje.set_yscale("log")
        eje.grid(alpha=0.3)
        eje.legend()
    figura.tight_layout()
    figura.savefig(destino, dpi=120)
    plt.close(figura)


_NOMBRE_GRUPO = {config.BARRIDO_RED: "con la red de la instancia simulada", config.BARRIDO_SIN_TOPE: "sin tope de red"}
_DIAS = (0, 1, 7, 14, 30, 60)


def _seccion_multiproceso(todos: list[dict[str, Any]]) -> list[str]:
    """Un proceso contra un planificador y N workers: mismo problema, mismos lugares, misma red."""
    multi = [r for r in todos if r["modo"] == Modo.MULTIPROCESO]
    if not multi:
        return []

    def referencia(r: dict[str, Any]) -> dict[str, Any] | None:
        return next(
            (o for o in todos if o["modo"] == Modo.CORRUTINAS and not o["subida"] and o["lugares"] == r["lugares"]
             and o["repos"] == r["repos"] and bool(o["red"]) == bool(r["red"])),
            None,
        )  # fmt: skip

    lineas = [
        "",
        "## Un proceso contra un planificador y N workers",
        "",
        "El modo multiproceso tiene un proceso planificador (aplica la cortesía y reparte) y N procesos workers con sus "
        "propias corrutinas, conectados por TCP (en la prueba por 127.0.0.1; los mismos workers podrían estar en otras "
        "máquinas). Se compara con un solo proceso en el mismo problema, con los mismos lugares y la misma red. "
        "**La CPU total es la del planificador más la de todos los workers**: lo que cuesta repartir el trabajo.",
        "",
        "| Esc. | Arquitectura | Lugares | Tope de red | MB/s | CPU planificador | CPU workers (suma) | CPU total "
        "| CPU ms/MB | Memoria total (MB) | Bucle p99 planificador / workers (ms) | Despacho p99 (ms) | Errores | Violaciones |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    def fila(r: dict[str, Any], arquitectura: str) -> str:
        cpu_total = r["cpu_media"] + r["cpu_workers"]
        ms_por_mb = 10 * cpu_total / r["mb_s"] if r["mb_s"] else 0.0
        memoria = r["rss_max"] + r["rss_workers_max"]
        bucles = f"{_f(r['bucle_p99'], 1)}" + (f" / {_f(r['bucle_workers_p99'], 1)}" if r["workers"] else "")
        return (
            f"| {r['id']} | {arquitectura} | {r['lugares']} | {_f(r['red'] / config.MB) if r['red'] else 'sin tope'} "
            f"| {_f(r['mb_s'])} | {_f(r['cpu_media'])} | {_f(r['cpu_workers']) if r['workers'] else '—'} "
            f"| {_f(cpu_total)} | {_f(ms_por_mb, 2)} | {_f(memoria, 0)} | {bucles} | {_f(r['despacho_p99'], 1)} "
            f"| {r['errores']} | {r['violaciones']} |"
        )

    for r in multi:
        ref = referencia(r)
        if ref is not None:
            lineas.append(fila(ref, "un proceso"))
        lineas.append(fila(r, f"planificador + {r['workers']} workers"))
    lineas += [
        "",
        "- **Con la red de la instancia:** si un proceso ya llena la red, repartir no baja más rápido; muestra cuánto "
        "cuesta de más (CPU total y memoria) tener el trabajo en varios procesos.",
        "- **Sin tope de red:** muestra si repartir en N procesos pasa el techo de un núcleo. Si los MB/s suben, el "
        "multiproceso escala; la pregunta es si hace falta, frente a lo que da la red de la instancia.",
        "- La CPU ms/MB se calcula con la CPU total: es el costo de procesar cada MB en cada arquitectura.",
    ]
    return lineas


def _seccion_subida(todos: list[dict[str, Any]]) -> list[str]:
    """Spool y subida a S3: si la descarga llegó a esperar a la subida y cuánto cuesta subir."""
    con_subida = [r for r in todos if r["subida"]]
    if not con_subida:
        return []
    # Referencia sin subida: el mismo problema, los mismos lugares y la misma red.
    def referencia(r: dict[str, Any]) -> dict[str, Any] | None:
        return next(
            (o for o in todos if not o["subida"] and o["lugares"] == r["lugares"] and o["repos"] == r["repos"]
             and bool(o["red"]) == bool(r["red"]) and o["modo"] == r["modo"]),
            None,
        )  # fmt: skip

    lineas = [
        "",
        "## Subida a S3",
        "",
        "El motor guarda cada archivo mientras lo baja (los de menos de "
        f"{_f(config.UMBRAL_DISCO / config.MB, 0)} MB en el spool en RAM, los demás en disco) y, al terminar, lo deja "
        "para un proceso subidor aparte (boto3, en su propio núcleo) que lo sube a un S3 falso local. Si lo pendiente "
        f"pasa {_f(config.LIMITE_PENDIENTE_RAM / config.MB, 0)} MB en RAM o "
        f"{_f(config.LIMITE_PENDIENTE_DISCO / config.MB, 0)} MB en disco, el motor deja de repartir tareas nuevas "
        "(\"frenado\"). **Lo que importa: frenado en 0 y lo pendiente estable** quieren decir que la descarga nunca "
        "esperó a la subida.",
        "",
        "| Esc. | Dónde sube | Lugares | Tope de red | MB/s bajados | MB/s subidos | CPU motor | CPU motor sin subida | RSS motor (MB) "
        "| CPU subidor (media / máx) | RSS subidor (MB) | Pendiente máx RAM / disco (MB) | Frenado (s) | Escritura a disco (MB/s) "
        "| Objetos (multipart) | Errores de subida | Pendiente al final (MB) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    donde = {"proceso": "proceso aparte", "hilos": "hilos en el motor", "corrutinas": "corrutinas en el motor"}
    for r in con_subida:
        ref = referencia(r)
        cpu_subidor = (
            f"{_f(r['subidor_cpu'])} / {_f(r['subidor_cpu_max'])}" if r["modo_subida"] == "proceso" else "en la del motor"
        )
        lineas.append(
            f"| {r['id']} | {donde.get(r['modo_subida'], r['modo_subida'])}{', CRT' if r['cliente_subida'] == 'crt' else ''}"
            f"{', mismo núcleo que el motor' if r['subidor_mismo_nucleo'] else ''} ({r['subida_paralelo']}) | {r['lugares']} "
            f"| {_f(r['red'] / config.MB) if r['red'] else 'sin tope'} | {_f(r['mb_s'])} "
            f"| {_f(r['subida_mb_s'])} | {_f(r['cpu_media'])} | {_f(ref['cpu_media']) + ' (' + ref['id'] + ')' if ref else '—'} "
            f"| {_f(r['rss_max'], 0)} | {cpu_subidor} | {_f(r['subidor_rss_max'], 0) if r['modo_subida'] == 'proceso' else '—'} "
            f"| {_f(r['pendiente_ram_max'], 0)} / {_f(r['pendiente_disco_max'], 0)} | {_f(r['frenado_s'], 1)} "
            f"| {_f(r['disco_escritura_mb_s'])} | {r['subida_objetos']} ({r['subida_multipart']}) | {r['subida_errores']} "
            f"| {_f(r['pendiente_final_mb'], 0)} |"
        )
    lineas += [
        "",
        "- **Dónde sube:** proceso aparte (en su propio núcleo), un pool de hilos con s3transfer dentro del proceso del "
        "motor, o un pool de corrutinas con aiobotocore dentro del bucle del motor. Entre paréntesis, las subidas a la vez. "
        "El spool es el mismo en los tres: la única diferencia es dónde corre la subida.",
        "- **CPU motor contra la referencia sin subida:** en proceso aparte, lo que le agrega al motor escribir el spool; "
        "con hilos o corrutinas, además todo el costo de subir, que comparte el núcleo (y el GIL) con la descarga. Si la "
        "CPU llega al 100 %, bajan los MB/s bajados y sube el retraso del bucle.",
        "- **CPU del subidor:** solo se ve aparte en el proceso aparte. Con el SDK CRT de AWS sería menos.",
        "- **Escritura a disco:** de toda la máquina; es lo que tiene que sostener el volumen EBS del spool (gp3 da "
        "125 MB/s de base) y el tope de EBS de la instancia.",
        "- En corridas cortas pasan sobre todo los archivos chicos: los grandes no llegan a terminar de bajar, así que "
        "el spool en disco aparece menos de lo que se usaría en la descarga real.",
    ]
    return lineas


def _picos_contenedores(resultados: Path) -> dict[str, dict[str, float]]:
    """Pico de memoria (MB) por escenario y contenedor, de `docker_stats.csv` y `tiempos.csv`.

    `scripts/candidata.sh` anota `docker stats` cada 5 s (t en segundos UTC, contenedor, CPU %,
    memoria usada) y el inicio y el fin de cada escenario.
    """
    from datetime import datetime

    stats, tiempos = resultados / "docker_stats.csv", resultados / "tiempos.csv"
    if not stats.exists() or not tiempos.exists():
        return {}
    with tiempos.open() as f:
        ventanas = [
            (t["escenario"], datetime.fromisoformat(t["inicio_utc"]).timestamp(), datetime.fromisoformat(t["fin_utc"]).timestamp())
            for t in csv.DictReader(f)
        ]  # fmt: skip
    picos: dict[str, dict[str, float]] = {}
    with stats.open() as f:
        for fila in csv.DictReader(f):
            t, mb = float(fila["t"]), _a_mb(fila["memoria"])
            # `docker compose run` nombra el contenedor `<proyecto>-motor-run-<id>`.
            nombre = next((n for n in ("postgres", "motor") if n in fila["contenedor"]), fila["contenedor"])
            for id, inicio, fin in ventanas:
                if inicio <= t <= fin:
                    picos.setdefault(id, {})[nombre] = max(picos.get(id, {}).get(nombre, 0.0), mb)
    return picos


def _a_mb(texto: str) -> float:
    """`123.4MiB / 1.8GiB` (lo que muestra `docker stats`) → 123.4."""
    usado = texto.split("/")[0].strip()
    for unidad, factor in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024), ("GB", 1000**3 / 2**20), ("MB", 1000**2 / 2**20), ("kB", 1000 / 2**20), ("B", 1 / 2**20)):
        if usado.endswith(unidad):
            return float(usado[: -len(unidad)]) * factor
    return 0.0


def _seccion_validacion(todos: list[dict[str, Any]], resultados: Path) -> list[str]:
    """Validación en una instancia real: el motor en la candidata y el servidor simulado en otra."""
    validos = [r for r in todos if r["dos_maquinas"]]
    if not validos:
        return []
    instancia = resultados / "instancia.json"
    datos = json.loads(instancia.read_text()) if instancia.exists() else {}
    tipo = datos.get("tipo") or "instancia candidata"
    memoria_total = float(datos.get("memoria_mb", 0)) or None
    picos = _picos_contenedores(resultados)
    lineas = [
        "",
        f"## Validación en una {tipo} real",
        "",
        f"El motor corre en una **{tipo} real**, en Docker Compose junto con PostgreSQL, y le pide a un servidor "
        "simulado que corre en **otra instancia** de la misma subred: el tráfico pasa por la placa de red real, el spool "
        "en disco escribe en el EBS real y todo comparte la memoria real de la máquina."
        + (" La subida va a **S3 real**." if any(r["s3_real"] for r in validos) else " La subida va al S3 falso.")
        + " La CPU y la memoria de la máquina incluyen todo lo que corre en ella (motor, subidor, PostgreSQL y Docker).",
        "",
        "| Esc. | Red de Docker | Base | Subida | MB/s bajados | MB/s subidos | CPU máquina (media / máx) | CPU motor "
        "(sistema) | Bucle p99 (ms) | Despacho p99 (ms) | Frenado (s) | Pendiente al final (MB) | Errores (descarga / subida) "
        "| Violaciones |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in validos:
        subida = ("S3 real" if r["s3_real"] else "S3 falso") if r["subida"] else "no"
        if r["subida"] and r["cliente_subida"] == "crt":
            subida += " (CRT)"
        base = ("en lotes" if r["db_lotes"] else "por resultado") if r["db"] else "no"
        lineas.append(
            f"| {r['id']} | {r['red_docker'] or '—'} | {base} | {subida} | {_f(r['mb_s'])} "
            f"| {_f(r['subida_mb_s']) if r['subida'] else '—'} | {_f(r['maquina_cpu'])} / {_f(r['maquina_cpu_max'])} "
            f"| {_f(r['cpu_media'])} ({_f(r['cpu_sistema'])}) | {_f(r['bucle_p99'], 2)} | {_f(r['despacho_p99'], 1)} | {_f(r['frenado_s'], 1)} "
            f"| {_f(r['pendiente_final_mb'], 0) if r['subida'] else '—'} | {r['errores']} / {r['subida_errores']} "
            f"| {r['violaciones']} |"
        )
    lineas += [
        "",
        "| Esc. | Memoria disponible mín (MB) | Pico contenedor motor (MB) | Pico PostgreSQL (MB) | RSS motor máx (MB) "
        "| RSS subidor máx (MB) | Escritura a disco (media / máx, MB/s) | DB tx/s (filas/s) | Commit p95 (ms) | Deriva memoria disponible (MB/h) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in validos:
        p = picos.get(r["id"], {})
        lineas.append(
            f"| {r['id']} | {_f(r['memoria_disponible_min'], 0)}"
            f"{f' de {_f(memoria_total, 0)}' if memoria_total else ''} "
            f"| {_f(p['motor'], 0) if 'motor' in p else '—'} | {_f(p['postgres'], 0) if 'postgres' in p else '—'} "
            f"| {_f(r['rss_max'], 0)} | {_f(r['subidor_rss_max'], 0) if r['subida'] else '—'} "
            f"| {_f(r['disco_escritura_mb_s'])} / {_f(r['disco_escritura_max'])} | {_f(r['db_tx'])} ({_f(r['db_filas'])}) "
            f"| {_f(r['db_commit_p95'], 2)} | {_f(r['deriva_memoria_mb_h']) if r['segundos'] >= 600 else '—'} |"
        )
    lineas += [
        "",
        "- **Red de Docker:** `puente` es la red por defecto de Docker (cada paquete pasa por una interfaz virtual y NAT); "
        "`host`, el motor usa la red de la máquina directamente. La diferencia de CPU es lo que cuesta el puente.",
        "- **Base:** `por resultado`, una transacción por resultado que el lugar espera antes de tomar otra tarea; "
        "`en lotes`, como el descargador, el lugar encola y sigue, y una tarea aparte escribe lo encolado cada "
        f"{_f(config.LOTE_DB_SEGUNDOS, 0)} s. **Subida (CRT):** el cliente de S3 de AWS escrito en C en vez de s3transfer.",
        "- **CPU máquina:** % de toda la instancia (en una de 1 vCPU, 100 % es el núcleo entero). Es lo que importa "
        "para saber si entra todo: motor, subidor, PostgreSQL y Docker.",
        "- **Memoria disponible mínima:** la que le quedó libre a la máquina en el peor segundo. El pico del "
        "contenedor del motor incluye el spool en RAM (`/dev/shm`) y el subidor; el de PostgreSQL, sus buffers.",
        "- **Escritura a disco:** del EBS real. Si se acerca al tope de EBS de la instancia (o a los 125 MB/s de base "
        "de un gp3), el spool en disco se atrasa y lo pendiente crece; en corridas cortas cuenta la ráfaga de EBS.",
        "- **Deriva:** solo en corridas de 10 minutos o más (la de 1 hora). Negativa quiere decir que la memoria "
        "disponible baja; el caché de páginas del disco también la baja sin que sea una pérdida.",
    ]
    return lineas


def _f(valor: float, decimales: int = 1) -> str:
    return f"{valor:,.{decimales}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _marca(cumple: bool | None) -> str:
    return {True: "cumple", False: "**no cumple**", None: "sin datos"}[cumple]


def armar(resultados: Path, salida: Path) -> None:
    todos = [resumir(r) for r in cargar(resultados)]
    if not todos:
        raise SystemExit(f"no hay resultados en {resultados}")
    # La instancia simulada es la de los resultados (los viejos, sin ese dato, eran de la t3.small).
    simuladas = {r["instancia"] or "t3.small" for r in todos}
    simulada = config.instancia(sorted(simuladas)[0])
    # La velocidad por conexión con la que corrió el servidor (ANCHO_MB_S), según los resultados.
    # Del JSON de cualquier escenario (instancia.json no es de un escenario).
    primero = next(r.datos for r in cargar(resultados))
    supuestos = primero.get("supuestos", {})
    ancho = supuestos.get("ancho_por_conexion_mb_s", config.ANCHO_POR_CONEXION / config.MB) * config.MB
    p = proyeccion(todos, simulada, ancho)
    grupos = barridos(todos)
    # Las tablas generales no repiten los escenarios del barrido: tienen su propia sección.
    resumenes = [r for r in todos if not r["grupo"]]
    lineas = ["# Prueba de concurrencia: resultados", ""]

    instancia = resultados / "instancia.json"
    if instancia.exists():
        datos = json.loads(instancia.read_text())
        lineas += ["## Instancia", "", *[f"- **{k}:** {v}" for k, v in datos.items()], ""]
    versiones = primero.get("versiones", {})
    lineas += [
        "## Supuestos",
        "",
        f"- Velocidad por conexión: distinta para cada repositorio, log-normal con mediana {_f(ancho / config.MB, 2)} MB/s "
        f"(desvío del logaritmo {_f(config.ANCHO_SIGMA, 2)}, cortada entre {_f(config.ANCHO_RANGO[0])} y "
        f"{_f(config.ANCHO_RANGO[1])} veces la mediana), calibrada con la medición desde EC2 (15 repositorios: "
        f"cuartiles {_f(config.ANCHO_CUARTILES_MEDIDOS[0], 2)} y {_f(config.ANCHO_CUARTILES_MEDIDOS[1], 2)} MB/s). "
        "Es el supuesto que más mueve la duración: ver el rango y la sensibilidad en la proyección.",
        f"- Archivos por ítem: {_f(config.DESCARGAS_POR_ITEM, 2)} en promedio, medidos con `relevamiento medir-tamanos` "
        f"en 11 repositorios ({', '.join(f'{k}: {n}' for k, n in config.ARCHIVOS_POR_ITEM.items())} de cada "
        f"{sum(config.ARCHIVOS_POR_ITEM.values())} ítems); {_f(100 * config.ARCHIVOS_POR_ITEM.get(0, 0) / sum(config.ARCHIVOS_POR_ITEM.values()), 0)} % no tiene ninguno.",
        f"- Latencia hasta los encabezados: log-normal con p50 {config.LATENCIA_P50} s y p95 {config.LATENCIA_P95} s (pilotos).",
        f"- Archivos: log-normal con p50 {_f(config.ARCHIVO_P50 / 1e6, 2)} MB y p95 {_f(config.ARCHIVO_P95 / 1e6)} MB, "
        f"cortada en {_f(config.ARCHIVO_MAXIMO / 1e6)} MB: media {_f(ARCHIVO.media() / 1e6)} MB (medir-tamanos: 1.676 archivos de "
        "11 repositorios, ponderado por sus ítems).",
        f"- Intervalo de cortesía: {config.INTERVALO} s desde el fin de cada pedido.",
        f"- Versiones: {', '.join(f'{k} {v}' for k, v in versiones.items())}.",
        "",
        "## Escenarios",
        "",
        f"Instancia simulada: **{simulada.nombre}** ({simulada.vcpu} vCPU, {_f(simulada.memoria_gb, 0)} GB, red base "
        f"{_f(simulada.red_base / config.MB)} MB/s). Valores sin el calentamiento; CPU en % de un núcleo."
        + (f" ⚠ Hay resultados de varias instancias simuladas: {', '.join(sorted(simuladas))}." if len(simuladas) > 1 else ""),
        "",
        "| Esc. | Modo | Lugares | Repos | Tope de red (MB/s) | Pedidos/s | MB/s | CPU media | CPU máx | RSS máx (MB) | Bucle p99 (ms) "
        "| Despacho p99 (ms) | Lat. p50 (ms) | Lat. p95 (ms) | Errores | Violaciones | CPU servidor máx |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in resumenes:
        lineas.append(
            f"| {r['id']} | {r['modo']}{' + db' if r['db'] else ''} | {r['lugares']} | {r['repos']} "
            f"| {_f(r['red'] / config.MB) if r['red'] else 'sin tope'} | {_f(r['pedidos_s'])} | {_f(r['mb_s'])} | {_f(r['cpu_media'])} | {_f(r['cpu_max'])} "
            f"| {_f(r['rss_max'])} | {_f(r['bucle_p99'], 2)} | {_f(r['despacho_p99'], 2)} | {_f(r['lat_p50'])} "
            f"| {_f(r['lat_p95'])} | {r['errores']} | {r['violaciones']} "
            f"| {_f(r['cpu_servidor_max'])}{'' if r['valido'] else ' ⚠'} |"
        )
    lineas += [
        "",
        "Más detalle por escenario:",
        "",
        "| Esc. | CPU sistema | CPU ms/pedido | CPU ms/MB | Hilos máx | Descriptores máx | Conexiones máx "
        "| Listos sin atender | GC ms/s | Steal % | Deriva RSS (MB/h) | DB tx/s | Commit p95 (ms) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in resumenes:
        lineas.append(
            f"| {r['id']} | {_f(r['cpu_sistema'])} | {_f(r['cpu_ms_por_pedido'], 2)} | {_f(r['cpu_ms_por_mb'], 2)} "
            f"| {r['hilos_max']:.0f} | {r['descriptores_max']:.0f} | {r['conexiones_max']:.0f} "
            f"| {_f(r['listos_sin_atender'])} | {_f(r['gc_ms_s'], 2)} | {_f(r['steal'], 2)} "
            f"| {_f(r['deriva_rss_mb_h'])} | {_f(r['db_tx'])} | {_f(r['db_commit_p95'], 2)} |"
        )
    invalidos = [r["id"] for r in resumenes if not r["valido"]]
    if invalidos:
        lineas += [
            "",
            f"⚠ En los escenarios {', '.join(invalidos)} el servidor simulado pasó el {CPU_SERVIDOR_MAX:.0f} % de un "
            "núcleo: el límite pudo ser él y no el motor. Sus números son una cota inferior de lo que aguanta el motor.",
        ]
    errores = {r["id"]: r["errores_por_tipo"] for r in resumenes if r["errores"]}
    if errores:
        lineas += ["", f"Errores por tipo: `{json.dumps(errores, ensure_ascii=False)}`"]

    if _graficos(resumenes, resultados / "graficos.png"):
        lineas += ["", "## Gráficos", "", f"![CPU, memoria y retraso según los lugares]({resultados.name}/graficos.png)"]

    if grupos:
        lineas += [
            "",
            "## Barrido: cuántas tareas concurrentes convienen",
            "",
            "El problema real (2.400 repositorios) con distintas cantidades de lugares de ejecución. La **rodilla** es "
            f"la menor cantidad de lugares que llega al {FRACCION_RODILLA:.0%} del máximo: más lugares ya no bajan más "
            "rápido, solo esperan. Con pocos lugares, la ocupación alta y el despacho largo son lo esperado: hay más "
            "trabajo que lugares.",
        ]
        for grupo, datos in grupos.items():
            lineas += [
                "",
                f"### {_NOMBRE_GRUPO[grupo].capitalize()}",
                "",
                "| Lugares | Ocupación % | Pedidos/s | Ítems/s | MB/s | CPU media | RSS máx (MB) | Bucle p99 (ms) "
                "| Despacho p99 (ms) | Errores | Violaciones |",
                "|---|---|---|---|---|---|---|---|---|---|---|",
                *[
                    f"| {r['lugares']} | {_f(r['ocupacion'])} | {_f(r['pedidos_s'])} | {_f(r['items_s'], 2)} "
                    f"| {_f(r['mb_s'])} | {_f(r['cpu_media'])} | {_f(r['rss_max'])} | {_f(r['bucle_p99'], 2)} "
                    f"| {_f(r['despacho_p99'], 1)} | {r['errores']} | {r['violaciones']} |"
                    for r in datos["filas"]
                ],
                "",
            ]
            if datos["rodilla_mb"]:
                lineas.append(
                    f"- **Rodilla:** con {datos['rodilla_mb']['lugares']} lugares se llega al {FRACCION_RODILLA:.0%} de los "
                    f"MB/s máximos ({_f(datos['rodilla_mb']['mb_s'])} MB/s). Los ítems/s no sirven para esto en corridas "
                    "cortas: con más lugares se terminan primero los ítems chicos aunque los MB/s sean los mismos; a la "
                    "larga, ítems/s = MB/s / MB por ítem."
                )
                lineas.append(
                    f"- **Costo de sobrar:** cada lugar de más después de la rodilla suma {_f(datos['rss_kb_por_lugar'])} KB "
                    f"de memoria y {_f(datos['cpu_por_lugar'], 3)} puntos de CPU ({datos['sobran']} lugares de más)."
                )
        _grafico_barrido(grupos, resultados / "barrido.png")
        lineas += ["", f"![Rendimiento y CPU según los lugares]({resultados.name}/barrido.png)"]

    lineas += _seccion_multiproceso(todos)
    lineas += _seccion_subida(todos)
    lineas += _seccion_validacion(todos, resultados)
    lineas += [
        "",
        "## Proyección al problema real",
        "",
        f"{config.REPOS_REALES} repositorios y {_f(config.ITEMS_REALES / 1e6, 0)} M de ítems repartidos con la regla del "
        f"simulador del descargador: el repositorio más grande tiene {p['items_max']:,} ítems, la mediana es "
        f"{_f(p['items_mediana'], 0)} y el 4 % más grande concentra el {_f(100 * p['items_top4'])} %.",
        "",
        "Modelo calibrado (cuentas):",
        "",
        f"- Resolución: latencia media {_f(p['t_res'], 2)} s. Descarga: latencia + archivo medio / ancho = "
        f"{_f(p['t_desc'], 2)} s. Medido en el escenario de referencia: resolución "
        f"{_f(p['t_res_medido'] or 0, 2)} s y descarga {_f(p['t_desc_medido'] or 0, 2)} s.",
        f"- Un ítem son 1 resolución y {_f(p['descargas_por_item'], 2)} descargas: con {config.INTERVALO} s de cortesía después de "
        f"cada pedido, a la velocidad mediana ocupa al repositorio {_f(p['ciclo_item'])} s y baja {_f(p['bytes_item'] / 1e6)} MB.",
        f"- Total a bajar: {_f(p['total_tb'])} TB.",
        f"- **Pico** (todos los repositorios activos): {_f(p['pico_pedidos_s'], 0)} pedidos/s, "
        f"{_f(p['pico_mb_s'], 0)} MB/s de demanda y unos {_f(p['pico_lugares'], 0)} pedidos en curso.",
        f"- **Cota por cortesía:** el repositorio que más tarda solo (grande y lento: {p['mas_lento']['items']:,} ítems a "
        f"{_f(p['mas_lento']['mb_s'], 2)} MB/s) tarda **{_f(p['duracion_cortesia_dias'])} días**, haya una máquina o cien. "
        f"El más grande ({p['items_max']:,} ítems) a la velocidad mediana tardaría "
        f"{_f(p['items_max'] * p['ciclo_item'] / 86400)}.",
    ]
    for nombre, dias in p["duracion_red_dias"].items():
        lineas.append(
            f"- **Cota por red ({nombre}, {_f(p['red_mb_s'][nombre])} MB/s):** {_f(p['total_tb'])} TB / red = "
            f"**{_f(dias)} días**. CPU que pide procesar esa red a {_f(p['cpu_ms_por_mb'], 2)} ms de CPU por MB: "
            f"{_f(p['cpu_nucleos_para_red'][nombre], 2)} núcleos."
        )
    if p["capacidad_mb_s"] is not None:
        cota = " (cota inferior: el servidor simulado saturó antes)" if p["capacidad_es_cota_inferior"] else ""
        lineas.append(
            f"- **Capacidad medida de un proceso con corrutinas:** {_f(p['capacidad_mb_s'])} MB/s en el escenario "
            f"{p['capacidad_escenario']}{cota}."
        )
    lineas += [
        "",
        "Distribuir solo ayuda si un proceso no llega a procesar lo que da la red de la instancia; si llega, el "
        "límite es la red, y se resuelve con una instancia de más red.",
        "",
        "### A lo largo de toda la descarga",
        "",
        "Modelo de fluido con la distribución de ítems y una velocidad para cada repositorio: cada uno avanza al ritmo "
        "de su cortesía o, si la red no alcanza, a la parte de la red que le toca (los que piden menos que su parte se "
        "llevan lo que piden); los chicos y rápidos terminan primero. "
        f"Lugares necesarios: los que pide la cortesía (a la velocidad mediana, cada repositorio está ocupado "
        f"{_f(p['ocupado_item'], 2)} s de cada {_f(p['ciclo_item'], 2)} s) o los que llenan la red, lo que sea menor.",
        "",
        "**La duración es un rango:** depende de qué repositorios grandes salen lentos. Con "
        f"{next(iter(p['rango'].values()))['muestras']} sorteos distintos de velocidades:",
        "",
        "| Red | Percentil 10 (días) | Mediana (días) | Percentil 90 (días) |",
        "|---|---|---|---|",
        *[f"| {nombre} | {_f(r['p10'])} | {_f(r['p50'])} | {_f(r['p90'])} |" for nombre, r in p["rango"].items()],
        "",
        "La tabla de abajo es un solo sorteo (el de la semilla de la prueba).",
        "",
        "| Red | Duración (días) | Lugares que llenan la red | "
        + " | ".join(f"Día {d}: activos / lugares / MB/s" for d in _DIAS)
        + " |",
        "|---|---|---|" + "---|" * len(_DIAS),
    ]
    for nombre, c in p["curvas"].items():
        celdas = []
        for d in _DIAS:
            if d * 86400 > c["puntos"][-1]["t"]:
                celdas.append("terminó")
                continue
            punto = en_el_dia(c, d)
            celdas.append(f"{punto['activos']} / {_f(punto['lugares'], 0)} / {_f(punto['mb_s'])}")
        llenan = "—" if c["red"] is None else _f(c["lugares_red"], 0)
        lineas.append(f"| {nombre} | {_f(c['duracion_dias'])} | {llenan} | " + " | ".join(celdas) + " |")
    _grafico_curva(p["curvas"], resultados / "curva.png")
    lineas += [
        "",
        f"![Repositorios activos y lugares necesarios en el tiempo]({resultados.name}/curva.png)",
        "",
        "### Sensibilidad a la velocidad por conexión",
        "",
        "Como cada repositorio recibe un pedido a la vez, si sus archivos llegan más lento avanza más lento: "
        "ninguna máquina ni más red compensa eso. Con la red de la "
        f"{simulada.nombre}, moviendo la mediana (cada repositorio sigue con su velocidad relativa):",
        "",
        "| Mediana MB/s por conexión | Tiempo por ítem a la mediana (s) | Repositorio que más tarda (días) | Duración total (días) | Corrutinas que llenan la red |",
        "|---|---|---|---|---|",
        *[
            f"| {_f(s['mb_s'])}{' ← esta corrida' if abs(s['mb_s'] * config.MB - ancho) < 1 else ''} | {_f(s['ciclo'])} "
            f"| {_f(s['duracion_cortesia_dias'])} | {_f(s['duracion_dias'])} | {_f(s['lugares'], 0)} |"
            for s in sensibilidad(simulada, tuple(sorted({*config.ANCHOS_SENSIBILIDAD, ancho / config.MB})))
        ],
        "",
        "### Instancias candidatas",
        "",
        "Para cada instancia, con su red base: duración del modelo, corrutinas que llenan la red, MB/s al principio, "
        f"CPU que pide procesarlos ({_f(p['cpu_ms_por_mb'], 2)} ms de CPU por MB, medidos en el escenario "
        f"{p['cpu_mb_escenario'] or '—'} en la máquina de esta corrida: vale para ese mismo tipo de procesador), memoria según el barrido y costo on-demand en us-east-1 "
        "por toda la descarga. Precios y redes de config.INSTANCIAS: verificar antes de decidir.",
        "",
        "| Instancia | vCPU / RAM | Red base (MB/s) | $/h | Duración (días) | Corrutinas | MB/s | CPU (% de un núcleo) | Memoria (MB) | Costo total |",
        "|---|---|---|---|---|---|---|---|---|---|",
        *[
            f"| {c['instancia'].nombre}{' ←' if c['instancia'] == simulada else ''} | {c['instancia'].vcpu} / "
            f"{_f(c['instancia'].memoria_gb, 0)} GB | {_f(c['instancia'].red_base / config.MB)} "
            f"| {_f(c['instancia'].precio_hora, 4)} | {_f(c['duracion_dias'])} | {_f(c['lugares'], 0)} | {_f(c['mb_s'])} "
            f"| {'—' if c['cpu'] is None else _f(c['cpu']) + ('' if c['cpu_alcanza'] else ' ⚠')} "
            f"| {'—' if c['rss_mb'] is None else _f(c['rss_mb'], 0)} | ${_f(c['costo'], 0)} |"
            for c in p["candidatas"]
        ],
        "",
        f"⚠: la CPU que pide pasa lo que la instancia sostiene (línea base de créditos en las t3; 80 % de un núcleo en "
        "las demás, porque un proceso de Python usa uno solo).",
        "",
        "",
        "## Criterio de decisión",
        "",
        "| Escenario | Condición | Resultado |",
        "|---|---|---|",
        *[f"| {id} | {texto} | {_marca(cumple)} |" for id, texto, cumple in criterio(resumenes, simulada)],
        "",
        "## Conclusión",
        "",
        "_Completar a mano con los números de arriba: si alcanza un proceso con corrutinas, en qué instancia, qué "
        "recurso se agota primero y cómo se comparan las corrutinas con los hilos._",
        "",
    ]
    salida.write_text("\n".join(lineas))
