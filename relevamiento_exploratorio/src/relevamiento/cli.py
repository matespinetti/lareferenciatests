"""Línea de comandos: relevar, resumir, verificar-cortesia, medir-velocidad, medir-tamanos y los muestra-*."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer

from relevamiento.config import CORTESIA_MEDICION, CORTESIA_MUESTRA, CORTESIA_TAMANOS, ConfigCortesia
from relevamiento.entradas import EntradaInvalida, leer_items, leer_repositorios
from relevamiento.registro import EVENTOS, leer_jsonl
from relevamiento.relevar import plan, relevar
from relevamiento.resumir import resumir as generar_resumen
from relevamiento.verificar import verificar

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)

Salida = Annotated[Path, typer.Option(help="Directorio de pedidos.jsonl, eventos.jsonl, capturas/ y resultados.")]


def en_horario_laboral(momento: datetime) -> bool:
    return momento.weekday() < 5 and 8 <= momento.hour < 19


@app.command("relevar")
def cmd_relevar(
    repositorios: Annotated[Path, typer.Argument(help="CSV con id, nombre, url_base y, opcionalmente, oai_endpoint.")],
    items: Annotated[Optional[Path], typer.Option(help="CSV opcional con repo_id y url.")] = None,
    salida: Salida = Path("salida"),
    contacto: Annotated[Optional[str], typer.Option(envvar="RELEVAMIENTO_CONTACTO", help="Correo para el User-Agent.")] = None,
    solo: Annotated[Optional[str], typer.Option(help="Ids de repositorio separados por comas.")] = None,
    simulacro: Annotated[bool, typer.Option("--plan", help="Muestra qué se pediría, sin hacer pedidos.")] = False,
    en_horario_ok: Annotated[bool, typer.Option("--en-horario-ok", help="Permite correr en horario laboral.")] = False,
) -> None:
    """Recorre los repositorios respetando las reglas de cortesía."""
    try:
        repos = leer_repositorios(repositorios)
        por_repo = leer_items(items)
    except (EntradaInvalida, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    if solo:
        elegidos = {s.strip() for s in solo.split(",")}
        desconocidos = elegidos - {r.id for r in repos}
        if desconocidos:
            raise typer.BadParameter(f"Ids desconocidos: {', '.join(sorted(desconocidos))}", param_hint="--solo")
        repos = [r for r in repos if r.id in elegidos]
    if simulacro:
        typer.echo("\n".join(plan(repos, por_repo)))
        return
    if not contacto or not contacto.strip():
        raise typer.BadParameter("Hace falta un contacto para el User-Agent (o RELEVAMIENTO_CONTACTO).", param_hint="--contacto")
    if en_horario_laboral(datetime.now()) and not en_horario_ok:
        typer.echo("Es horario laboral: la spec pide correr fuera de horario. Usá --en-horario-ok para continuar igual.", err=True)
        raise typer.Exit(2)

    cfg = ConfigCortesia()
    typer.echo(f"User-Agent: {cfg.user_agent(contacto)}")
    typer.echo(f"Relevando {len(repos)} repositorios en {salida}/ (Ctrl+C corta; al relanzar se saltean los terminados)")
    try:
        resultado = asyncio.run(relevar(repos, por_repo, salida, contacto.strip(), cfg))
    except KeyboardInterrupt:
        terminados = {e["repo"] for e in leer_jsonl(salida / EVENTOS) if e["tipo"] == "repo_terminado"}
        typer.echo(f"\nInterrumpido. {len(terminados)} repositorios terminados en total; relanzá para seguir.", err=True)
        raise typer.Exit(130)
    typer.echo(f"Corrida {resultado['corrida']}: {resultado['relevados']} relevados, {resultado['ya_terminados']} ya terminados antes.")
    typer.echo("Siguiente paso: relevamiento verificar-cortesia, y después relevamiento resumir.")


@app.command("resumir")
def cmd_resumir(
    salida: Salida = Path("salida"),
    repositorios: Annotated[Optional[Path], typer.Option(help="CSV de repositorios, para nombres y URLs.")] = None,
) -> None:
    """Genera repositorios.csv, resultados.csv y resumen.md sin hacer pedidos."""
    repos = leer_repositorios(repositorios) if repositorios else None
    for ruta in generar_resumen(salida, repos):
        typer.echo(f"Escrito {ruta}")


@app.command("verificar-cortesia")
def cmd_verificar(
    salida: Salida = Path("salida"),
    medicion: Annotated[bool, typer.Option("--medicion", help="Verifica con la cortesía de medir-velocidad.")] = False,
    tamanos: Annotated[bool, typer.Option("--tamanos", help="Verifica con la cortesía de medir-tamanos.")] = False,
    muestra: Annotated[
        bool, typer.Option("--muestra", help="Verifica con la cortesía de muestra-ubicar y muestra-cosechar.")
    ] = False,
) -> None:
    """Comprueba sobre pedidos.jsonl que nunca hubo pedidos simultáneos a un host y que se respetó el intervalo."""
    if medicion + tamanos + muestra > 1:
        raise typer.BadParameter("Elegí una sola de --medicion, --tamanos y --muestra.")
    cfg = CORTESIA_MEDICION if medicion else CORTESIA_TAMANOS if tamanos else CORTESIA_MUESTRA if muestra else ConfigCortesia()
    res = verificar(salida, cfg)
    for aviso in res.avisos:
        typer.echo(f"aviso: {aviso}")
    for violacion in res.violaciones:
        typer.echo(f"VIOLACIÓN: {violacion}", err=True)
    for clave, valor in res.estadisticas.items():
        typer.echo(f"{clave}: {valor}")
    if not res.ok:
        raise typer.Exit(1)
    typer.echo("OK")


@app.command("medir-velocidad")
def cmd_medir(
    pilotos: Annotated[
        Optional[list[Path]], typer.Option("--piloto", help="Directorio de un piloto ya corrido (se puede repetir).")
    ] = None,
    salida: Salida = Path("medicion"),
    contacto: Annotated[Optional[str], typer.Option(envvar="RELEVAMIENTO_CONTACTO", help="Correo para el User-Agent.")] = None,
    simulacro: Annotated[bool, typer.Option("--plan", help="Muestra qué archivos se pedirían, sin hacer pedidos.")] = False,
    en_horario_ok: Annotated[bool, typer.Option("--en-horario-ok", help="Permite correr en horario laboral.")] = False,
    desde_tamanos: Annotated[
        Optional[Path],
        typer.Option("--desde-tamanos", help="Toma los archivos de las páginas que guardó medir-tamanos (repositorios grandes)."),
    ] = None,
) -> None:
    """Mide a qué velocidad entregan un archivo los repositorios, leyendo hasta 8 MB o 30 s de cada uno,
    con una cortesía más estricta (30 s entre pedidos a un host, 2 hosts a la vez, 10 pedidos por
    repositorio). Por defecto vuelve a pedir archivos verificados en los pilotos (3 por repositorio);
    con --desde-tamanos, 5 archivos de distintos tamaños de cada repositorio medido con medir-tamanos."""
    from relevamiento.medir import candidatos, candidatos_de_tamanos, medir, resumir_medicion

    if desde_tamanos is not None:
        elegidos = candidatos_de_tamanos(desde_tamanos)
    else:
        elegidos = candidatos(pilotos or [Path("piloto"), Path("piloto2"), Path("piloto3")])
    if not elegidos:
        raise typer.BadParameter("No hay archivos de 1 MB o más para medir.", param_hint="--piloto / --desde-tamanos")
    repos = sorted({c.repo for c in elegidos})
    typer.echo(f"{len(elegidos)} archivos de {len(repos)} repositorios (hasta {CORTESIA_MEDICION.bytes_medicion // 2**20} MB "
               f"o {CORTESIA_MEDICION.segundos_medicion:.0f} s de cada uno).")
    if simulacro:
        for c in elegidos:
            typer.echo(f"  {c.repo:20} {c.tamano / 1e6:7.1f} MB  {c.url}")
        return
    if not contacto or not contacto.strip():
        raise typer.BadParameter("Hace falta un contacto para el User-Agent (o RELEVAMIENTO_CONTACTO).", param_hint="--contacto")
    if en_horario_laboral(datetime.now()) and not en_horario_ok:
        typer.echo("Es horario laboral: la spec pide correr fuera de horario. Usá --en-horario-ok para continuar igual.", err=True)
        raise typer.Exit(2)
    typer.echo(f"User-Agent: {CORTESIA_MEDICION.user_agent(contacto)}")
    try:
        corrida = asyncio.run(medir(elegidos, salida, contacto.strip(), CORTESIA_MEDICION))
    except KeyboardInterrupt:
        typer.echo("\nInterrumpido.", err=True)
        raise typer.Exit(130)
    resumen = resumir_medicion(salida)
    typer.echo(f"Corrida {corrida}: {resumen['archivos_medidos']} archivos medidos en {resumen['repositorios']} repositorios.")
    typer.echo(f"MB/s por archivo: {resumen['mb_s_por_archivo']}  ·  por repositorio: {resumen['mb_s_por_repositorio']}")
    typer.echo(f"Detalle en {salida}/velocidad.csv. Siguiente paso: relevamiento verificar-cortesia --salida {salida} --medicion")


@app.command("medir-tamanos")
def cmd_tamanos(
    pilotos: Annotated[
        Optional[list[Path]], typer.Option("--piloto", help="Directorio de un piloto ya corrido (se puede repetir).")
    ] = None,
    salida: Salida = Path("tamanos"),
    contacto: Annotated[Optional[str], typer.Option(envvar="RELEVAMIENTO_CONTACTO", help="Correo para el User-Agent.")] = None,
    simulacro: Annotated[bool, typer.Option("--plan", help="Muestra qué páginas se pedirían, sin hacer pedidos.")] = False,
    en_horario_ok: Annotated[bool, typer.Option("--en-horario-ok", help="Permite correr en horario laboral.")] = False,
) -> None:
    """Mide el tamaño de los archivos desde los metadatos OAI (METS o xoai), sin descargar archivos:
    páginas enteras de ListRecords de los repositorios de los pilotos, con una cortesía más lenta que
    la del relevamiento (10 s entre pedidos a un host, 2 hosts a la vez, 8 pedidos por repositorio)."""
    from relevamiento.tamanos import candidatos, medir_tamanos, resumir_tamanos, urls

    repos = candidatos(pilotos or [Path("piloto"), Path("piloto2"), Path("piloto3")])
    if not repos:
        raise typer.BadParameter("Ningún repositorio de los pilotos ofrece METS o xoai.", param_hint="--piloto")
    paginas = sum(len(urls(r)) for r in repos)
    typer.echo(f"{len(repos)} repositorios, {paginas} páginas de ListRecords (más robots.txt de cada host).")
    if simulacro:
        for r in repos:
            typer.echo(f"  {r.repo:20} {r.plataforma:8} {r.formato:5} {r.endpoint}")
        return
    if not contacto or not contacto.strip():
        raise typer.BadParameter("Hace falta un contacto para el User-Agent (o RELEVAMIENTO_CONTACTO).", param_hint="--contacto")
    if en_horario_laboral(datetime.now()) and not en_horario_ok:
        typer.echo("Es horario laboral: la spec pide correr fuera de horario. Usá --en-horario-ok para continuar igual.", err=True)
        raise typer.Exit(2)
    typer.echo(f"User-Agent: {CORTESIA_TAMANOS.user_agent(contacto)}")
    try:
        corrida = asyncio.run(medir_tamanos(repos, salida, contacto.strip(), CORTESIA_TAMANOS))
    except KeyboardInterrupt:
        typer.echo("\nInterrumpido.", err=True)
        raise typer.Exit(130)
    resumen = resumir_tamanos(salida)
    total = resumen["ponderado_por_items"]
    typer.echo(f"Corrida {corrida}: {total.get('items', 0)} ítems y {total.get('archivos', 0)} archivos de "
               f"{total.get('repositorios', 0)} repositorios.")
    typer.echo(f"Ponderado por ítems: {total.get('archivos_por_item')} archivos por ítem, mediana "
               f"{(total.get('tamano_p50') or 0) / 1e6:.2f} MB, media {(total.get('tamano_media') or 0) / 1e6:.2f} MB.")
    typer.echo(f"Detalle en {salida}/tamanos.json. Siguiente paso: relevamiento verificar-cortesia --salida {salida} --tamanos")


# --- Muestra de LA Referencia (pedida por el usuario, nov 2026) -------------------------------

Muestra = Annotated[Path, typer.Option("--salida", help="Directorio de la muestra (candidatos, ubicacion/, cosecha/ y resultados).")]
Contacto = Annotated[Optional[str], typer.Option(envvar="RELEVAMIENTO_CONTACTO", help="Correo para el User-Agent.")]
Simulacro = Annotated[bool, typer.Option("--plan", help="Muestra qué se pediría, sin hacer pedidos.")]
EnHorario = Annotated[bool, typer.Option("--en-horario-ok", help="Permite correr en horario laboral.")]


def _antes_de_pedir(contacto: Optional[str], en_horario_ok: bool) -> str:
    if not contacto or not contacto.strip():
        raise typer.BadParameter("Hace falta un contacto para el User-Agent (o RELEVAMIENTO_CONTACTO).", param_hint="--contacto")
    if en_horario_laboral(datetime.now()) and not en_horario_ok:
        typer.echo("Es horario laboral: la spec pide correr fuera de horario. Usá --en-horario-ok para continuar igual.", err=True)
        raise typer.Exit(2)
    typer.echo(f"User-Agent: {CORTESIA_MUESTRA.user_agent(contacto)}")
    return contacto.strip()


@app.command("muestra-elegir")
def cmd_muestra_elegir(
    listado: Annotated[Path, typer.Argument(help="CSV de LA Referencia con repositorio y documentos.")] = Path(
        "entradas/la_referencia_repos.csv"
    ),
    salida: Muestra = Path("muestra"),
    ampliar: Annotated[
        Optional[str], typer.Option(help="Agrega candidatos a una muestra existente, por ejemplo C=100,D=50.")
    ] = None,
) -> None:
    """Elige los candidatos de la muestra por estratos de tamaño, sin hacer pedidos. Con --ampliar, suma
    candidatos nuevos a la muestra que ya existe, sin cambiar los anteriores."""
    from relevamiento.muestra import ampliar as ampliar_muestra
    from relevamiento.muestra import elegir, escribir_candidatos, leer_candidatos, leer_poblacion, resumen_poblacion

    poblacion = leer_poblacion(listado)
    if ampliar:
        extra = {k.strip(): int(v) for k, v in (p.split("=") for p in ampliar.split(","))}
        antes = leer_candidatos(salida)
        candidatas = ampliar_muestra(antes, poblacion, extra)
        escribir_candidatos(salida, candidatas, poblacion)
        typer.echo(f"{len(candidatas) - len(antes)} candidatos nuevos ({len(candidatas)} en total). "
                   "Siguiente paso: relevamiento muestra-ubicar (solo consulta los nuevos)")
        return
    candidatas = elegir(poblacion)
    escribir_candidatos(salida, candidatas, poblacion)
    resumen = resumen_poblacion(poblacion)
    for estrato, datos in resumen.items():
        de_estrato = [c for c in candidatas if c.estrato == estrato]
        reservas = sum(c.reserva for c in de_estrato)
        typer.echo(f"  {estrato}: {datos['repositorios']:5} repositorios, {datos['documentos']:>10,} documentos → "
                   f"{len(de_estrato) - reservas} titulares y {reservas} reservas")
    typer.echo(f"{len(candidatas)} candidatos en {salida}/candidatos.csv. Siguiente paso: relevamiento muestra-ubicar")


@app.command("muestra-ubicar")
def cmd_muestra_ubicar(
    salida: Muestra = Path("muestra"),
    contacto: Contacto = None,
    reprocesar: Annotated[
        bool, typer.Option("--reprocesar", help="Reinterpreta las respuestas guardadas, sin pedidos.")
    ] = False,
    simulacro: Simulacro = False,
    en_horario_ok: EnHorario = False,
) -> None:
    """Consulta el buscador de LA Referencia (10 registros por repositorio) para saber el país, el
    namespace OAI y la URL base de cada candidato, y handle.net si solo hay un handle. Los pedidos van
    de a uno. Con --reprocesar, reinterpreta las respuestas ya guardadas sin pedir nada."""
    from relevamiento.muestra import leer_candidatos, leer_ubicaciones, reprocesar_ubicaciones, ubicar_candidatas, url_buscador

    candidatas = leer_candidatos(salida)
    if reprocesar:
        n = reprocesar_ubicaciones(candidatas, salida)
        ubicaciones = leer_ubicaciones(salida)
        handles = sum(1 for u in ubicaciones.values() if u.get("problema") == "solo_handle")
        typer.echo(f"{n} ubicaciones reinterpretadas sin pedidos; {handles} quedan con un handle por resolver "
                   "(relanzá muestra-ubicar).")
        return
    hechas = leer_ubicaciones(salida)
    pendientes = [c for c in candidatas if c.id not in hechas or hechas[c.id].get("problema") == "solo_handle"]
    typer.echo(f"{len(pendientes)} consultas al buscador ({len(hechas)} ya hechas), una cada "
               f"{CORTESIA_MUESTRA.intervalo_minimo:.0f} s o más.")
    if simulacro:
        for c in pendientes[:10]:
            typer.echo(f"  {c.id} {c.estrato} {url_buscador(c.repositorio)}")
        return
    contacto = _antes_de_pedir(contacto, en_horario_ok)
    try:
        resultado = asyncio.run(ubicar_candidatas(pendientes, salida, contacto, CORTESIA_MUESTRA))
    except KeyboardInterrupt:
        typer.echo("\nInterrumpido. Relanzá para seguir.", err=True)
        raise typer.Exit(130)
    ubicaciones = leer_ubicaciones(salida)
    sin = sum(1 for u in ubicaciones.values() if u.get("problema"))
    typer.echo(f"{len(ubicaciones)} ubicados, {sin} con problema.")
    if resultado["cortado"]:
        typer.echo(f"Cortado: {resultado['cortado']}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Siguiente paso: relevamiento verificar-cortesia --salida {salida}/ubicacion --muestra, "
               "y después relevamiento muestra-cosechar")


@app.command("muestra-cosechar")
def cmd_muestra_cosechar(
    salida: Muestra = Path("muestra"),
    contacto: Contacto = None,
    solo: Annotated[Optional[str], typer.Option(help="Ids de candidato separados por comas (para una prueba).")] = None,
    rehacer: Annotated[
        Optional[str], typer.Option(help="Ids ya cosechados que se vuelven a cosechar (por ejemplo, con otro contexto OAI).")
    ] = None,
    rehacer_fallidos: Annotated[
        bool, typer.Option("--rehacer-fallidos", help="Vuelve a cosechar todos los que fallaron, salvo los de anti-bot.")
    ] = False,
    ignorar_robots: Annotated[
        bool, typer.Option("--ignorar-robots", help="Registra lo que robots.txt prohíbe pero no corta (se sigue respetando su Crawl-delay).")
    ] = False,
    simulacro: Simulacro = False,
    en_horario_ok: EnHorario = False,
) -> None:
    """Reconoce cada repositorio ubicado (endpoint OAI y formatos) y cosecha páginas de ListRecords con
    los archivos de sus ítems, sin descargar archivos. En los estratos con cupo, reemplaza a los que no
    sirven. Se puede cortar con Ctrl+C y relanzar."""
    from relevamiento.muestra import cosechar, endpoints_conocidos, leer_candidatos, leer_roar, leer_ubicaciones

    candidatas = leer_candidatos(salida)
    ubicaciones = leer_ubicaciones(salida)
    conocidos = endpoints_conocidos([Path("entradas/repos.csv"), *sorted(Path(".").glob("piloto*/repositorios.csv"))])
    a_rehacer = {s.strip() for s in rehacer.split(",")} if rehacer else set()
    # Con --rehacer (ids) se cosechan solo esos; con --rehacer-fallidos, además todo lo pendiente.
    elegidos = {s.strip() for s in solo.split(",")} if solo else (a_rehacer or None)
    if rehacer_fallidos:
        from relevamiento.muestra import fallidos

        a_rehacer |= fallidos(salida)
        elegidos = {s.strip() for s in solo.split(",")} if solo else None
    listos = [c for c in candidatas if ubicaciones.get(c.id, {}).get("url_base") and (elegidos is None or c.id in elegidos)]
    typer.echo(f"{len(listos)} candidatos ubicados ({len(conocidos)} endpoints ya conocidos de los pilotos).")
    if simulacro:
        for c in listos:
            u = ubicaciones[c.id]
            typer.echo(f"  {c.id} {c.estrato}{'*' if c.reserva else ' '} {u.get('pais') or '?':3} "
                       f"{conocidos.get(u.get('host') or '') or u['url_base']}")
        return
    contacto = _antes_de_pedir(contacto, en_horario_ok)
    try:
        resultado = asyncio.run(cosechar(candidatas, ubicaciones, conocidos, salida, contacto, CORTESIA_MUESTRA,
                                         solo=elegidos, rehacer=a_rehacer, roar=leer_roar(salida),
                                         ignorar_robots=ignorar_robots))
    except KeyboardInterrupt:
        typer.echo("\nInterrumpido. Relanzá para seguir.", err=True)
        raise typer.Exit(130)
    typer.echo(f"{resultado['cosechados']} repositorios cosechados en esta corrida; útiles por estrato: {resultado['utiles']}")
    typer.echo(f"Siguiente paso: relevamiento verificar-cortesia --salida {salida}/cosecha --muestra, "
               "y después relevamiento muestra-resumir")


@app.command("muestra-roar")
def cmd_muestra_roar(
    rawlist: Annotated[Path, typer.Argument(help="Listado completo de ROAR (roar.eprints.org/rawlist.xml), ya bajado.")],
    salida: Muestra = Path("muestra"),
) -> None:
    """Cruza el listado de ROAR con los candidatos ubicados y guarda los endpoints OAI que publica para
    cada uno (muestra/endpoints_roar.json), sin hacer pedidos. muestra-cosechar los prueba cuando no
    encuentra el OAI o el encontrado no lista nada."""
    from relevamiento.muestra import ROAR, endpoints_de_roar, leer_candidatos, leer_ubicaciones

    encontrados = endpoints_de_roar(rawlist, leer_candidatos(salida), leer_ubicaciones(salida))
    (salida / ROAR).write_text(json.dumps(encontrados, indent=1, ensure_ascii=False), encoding="utf-8")
    typer.echo(f"{len(encontrados)} candidatos con endpoints en ROAR, en {salida}/{ROAR}.")


@app.command("muestra-resumir")
def cmd_muestra_resumir(salida: Muestra = Path("muestra")) -> None:
    """Escribe repositorios.csv, items.csv, archivos.csv, resumen.json y resumen.md, sin hacer pedidos."""
    from relevamiento.muestra import resumir_muestra

    resumen = resumir_muestra(salida)
    p = resumen["para_la_prueba"]
    typer.echo(f"{p['repositorios_utiles']} repositorios útiles, {p['items']:,} ítems y {p['archivos']:,} archivos.")
    typer.echo(f"Detalle en {salida}/resumen.md")
