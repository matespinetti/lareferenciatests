"""Línea de comandos: relevar, resumir, verificar-cortesia, medir-velocidad y medir-tamanos."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated, Optional

import typer

from relevamiento.config import CORTESIA_MEDICION, CORTESIA_TAMANOS, ConfigCortesia
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
) -> None:
    """Comprueba sobre pedidos.jsonl que nunca hubo pedidos simultáneos a un host y que se respetó el intervalo."""
    if medicion and tamanos:
        raise typer.BadParameter("Elegí --medicion o --tamanos, no los dos.")
    res = verificar(salida, CORTESIA_MEDICION if medicion else CORTESIA_TAMANOS if tamanos else ConfigCortesia())
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
