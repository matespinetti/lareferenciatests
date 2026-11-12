"""Comandos: `servidor`, `motor` e `informe`."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from concurrencia import config
from concurrencia.config import Modo

servidor_app = typer.Typer(add_completion=False)
motor_app = typer.Typer(add_completion=False)
informe_app = typer.Typer(add_completion=False)
s3falso_app = typer.Typer(add_completion=False)

_CERTS = Path("certs")
_INSTANCIA = typer.Option(
    envvar="INSTANCIA", help=f"Instancia de EC2 que se simula: {', '.join(config.INSTANCIAS)}"
)
_PROCESOS = typer.Option(envvar="SERVIDOR_PROCESOS", help="Procesos del servidor simulado, en puertos consecutivos")
_HOST = typer.Option(
    envvar="HOST", help="Interfaz donde escucha: 0.0.0.0 solo para la validación en dos máquinas (GUIA_DOS_MAQUINAS.md)"
)


@servidor_app.command()
def servidor(
    escenario: Annotated[str, typer.Option(help="Id del escenario (define el tamaño de archivo y la red)")] = "2",
    instancia: Annotated[str, _INSTANCIA] = config.INSTANCIA_POR_DEFECTO,
    procesos: Annotated[int, _PROCESOS] = 1,
    puerto: int = config.PUERTO,
    host: Annotated[str, _HOST] = "127.0.0.1",
    certs: Path = _CERTS,
    semilla: int = config.SEMILLA,
    red_mb_s: Annotated[
        float | None, typer.Option(envvar="RED_MB_S", help="Cambia el tope de red del escenario; 0, sin tope")
    ] = None,
) -> None:
    """Levanta el servidor simulado (en 127.0.0.1 si no se cambia `--host`) hasta que se lo detenga."""
    from concurrencia.servidor import Parametros, servir, servir_en_proceso

    e = config.escenario(escenario, instancia_=instancia)
    red = e.red if red_mb_s is None else (red_mb_s * config.MB or None)
    tope = f"{red / config.MB:.1f} MB/s" if red else "sin tope"
    typer.echo(
        f"servidor simulado en https://*.{config.DOMINIO}:{puerto} ({host})"
        f"{f'-{puerto + procesos - 1}' if procesos > 1 else ''} "
        f"(escenario {e.id}, red de {e.instancia}: {tope}, {procesos} proceso(s))"
    )
    # El tope de red es del total: cada proceso se lleva su parte.
    parametros = Parametros(semilla=semilla, tam_archivo=e.tam_archivo, red=red / procesos if red else None)
    if procesos == 1:
        try:
            asyncio.run(servir(parametros, certs, puerto, host=host))
        except KeyboardInterrupt:
            pass
        return
    _varios_procesos(servir_en_proceso, parametros, certs, puerto, procesos, host)


def _varios_procesos(objetivo, parametros, certs: Path, puerto: int, procesos: int, host: str) -> None:  # type: ignore[no-untyped-def]
    import multiprocessing
    import signal

    contexto = multiprocessing.get_context("spawn")
    hijos = [
        contexto.Process(target=objetivo, args=(replace(parametros, indice=i), certs, puerto + i, host))
        for i in range(procesos)
    ]
    for hijo in hijos:
        hijo.start()

    def parar(*_: object) -> None:
        for hijo in hijos:
            hijo.terminate()

    signal.signal(signal.SIGTERM, parar)
    signal.signal(signal.SIGINT, parar)
    for hijo in hijos:
        hijo.join()


@motor_app.command()
def motor(
    escenario: Annotated[str, typer.Option(help="Id del escenario: 1-9, 1h-3h, b<N> o bs<N>")],
    salida: Annotated[Path, typer.Option(help="Ruta sin extensión: escribe .csv y .json")] = Path("results/escenario"),
    duracion: Annotated[float | None, typer.Option(help="Acorta la medición (ensayos)")] = None,
    calentamiento: Annotated[float | None, typer.Option(help="Acorta el calentamiento (ensayos)")] = None,
    modo: Annotated[Modo | None, typer.Option(help="Fuerza el modo del escenario")] = None,
    instancia: Annotated[str, _INSTANCIA] = config.INSTANCIA_POR_DEFECTO,
    procesos: Annotated[int, _PROCESOS] = 1,
    modo_subida: Annotated[
        str | None, typer.Option(envvar="MODO_SUBIDA", help="Cambia dónde corre la subida: proceso, hilos o corrutinas")
    ] = None,
    dsn: Annotated[str | None, typer.Option(envvar="CONCURRENCIA_DSN", help="PostgreSQL para --db")] = None,
    puerto: int = config.PUERTO,
    puerto_s3: Annotated[int, typer.Option(envvar="PUERTO_S3", help="S3 falso (escenarios con subida)")] = config.PUERTO_S3,
    spool: Annotated[Path, typer.Option(envvar="SPOOL", help="Spool en disco (escenarios con subida)")] = Path("spool"),
    certs: Path = _CERTS,
    semilla: int = config.SEMILLA,
) -> None:
    """Corre un escenario contra el servidor simulado ya levantado o, en los r* (modo muestra), contra
    los repositorios reales de la muestra de LA Referencia (MUESTRA, con CONTACTO para el User-Agent)."""
    e = config.escenario(escenario, duracion, calentamiento, instancia)
    if e.muestra:
        if not config.CONTACTO.strip():
            raise typer.BadParameter("el modo muestra pide a repositorios reales: definí CONTACTO (correo del User-Agent)")
        if not (Path(config.MUESTRA) / "items.csv").exists():
            raise typer.BadParameter(f"no está la muestra en {config.MUESTRA} (MUESTRA)")
        if modo not in (None, Modo.CORRUTINAS):
            raise typer.BadParameter("el modo muestra corre solo con corrutinas", param_hint="--modo")
    else:
        _resolver_ip_servidor()
    if modo is not None:
        e = replace(e, modo=modo)
    if modo_subida is not None and e.subida:
        e = replace(e, modo_subida=modo_subida)
    if e.subida and e.modo is Modo.HILOS:
        raise typer.BadParameter("la subida solo está en el modo corrutinas", param_hint="--modo")
    if e.modo is Modo.MULTIPROCESO:
        from concurrencia import multiproceso

        resumen = asyncio.run(multiproceso.correr(e, puerto, certs, salida, semilla, procesos))
    elif e.modo is Modo.HILOS:
        from concurrencia import hilos

        resumen = hilos.correr(e, puerto, certs, salida, semilla, procesos)
    else:
        from concurrencia import motor as motor_corrutinas

        resumen = asyncio.run(
            motor_corrutinas.correr(e, puerto, certs, salida, semilla, dsn, procesos=procesos, puerto_s3=puerto_s3, dir_spool=spool)
        )
    servidor = resumen["servidor"]
    if e.muestra:
        m = resumen["muestra"]
        typer.echo(
            f"escenario {e.id} (repositorios reales, parte {e.parte + 1}/{e.partes}): {servidor['pedidos']} pedidos a "
            f"{m['repositorios']} repositorios, {servidor['violaciones']} violaciones de cortesía, "
            f"{len(m['abandonados'])} abandonados, errores {json.dumps(resumen['errores_por_tipo'])}"
        )
        return
    typer.echo(
        f"escenario {e.id} ({e.modo}, red de {e.instancia}): {servidor['pedidos']} pedidos, "
        f"{servidor['violaciones']} violaciones, errores {json.dumps(resumen['errores_por_tipo'])}"
    )


def _resolver_ip_servidor() -> None:
    """Si `IP_SERVIDOR` es un nombre (`host.docker.internal` en el ensayo con Docker), lo resuelve una
    vez, antes de arrancar el bucle; los procesos hijos heredan la IP por el entorno."""
    import ipaddress
    import os
    import socket

    try:
        ipaddress.ip_address(config.IP_SERVIDOR)
    except ValueError:
        config.IP_SERVIDOR = socket.gethostbyname(config.IP_SERVIDOR)
        os.environ["IP_SERVIDOR"] = config.IP_SERVIDOR


@s3falso_app.command()
def s3falso(
    puerto: int = config.PUERTO_S3, host: Annotated[str, _HOST] = "127.0.0.1", certs: Path = _CERTS
) -> None:
    """Levanta el S3 falso (en 127.0.0.1 si no se cambia `--host`) hasta que se lo detenga (escenarios con subida)."""
    from concurrencia.s3falso import servir

    typer.echo(f"S3 falso en https://s3.{config.DOMINIO}:{puerto} ({host})")
    try:
        asyncio.run(servir(certs, puerto, host))
    except KeyboardInterrupt:
        pass


@informe_app.command()
def informe(
    resultados: Path = Path("results"),
    salida: Path = Path("report.md"),
) -> None:
    """Arma report.md con las tablas, los gráficos, la proyección y el criterio de decisión."""
    from concurrencia.informe import armar

    armar(resultados, salida)
    typer.echo(f"informe en {salida}")
