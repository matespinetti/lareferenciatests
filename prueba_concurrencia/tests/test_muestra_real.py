"""Modo muestra (escenarios r*): catálogo, resolución de OJS, planificador, cortesía y una corrida de humo
contra repositorios locales (nunca reales)."""

import asyncio
import csv
import json
from pathlib import Path

import pytest
from aiohttp import web

from concurrencia import config, motor, muestra_real, pedido
from concurrencia.config import Escenario
from concurrencia.planificador import Planificador, Tarea, Tipo
from concurrencia.resolucion_real import a_descarga, descargas_de_pagina
from tests.conftest import puertos_libres


def _escribir(ruta: Path, columnas: list[str], filas: list[list[object]]) -> None:
    with open(ruta, "w", newline="", encoding="utf-8") as f:
        escritor = csv.writer(f)
        escritor.writerow(columnas)
        escritor.writerows(filas)


def _muestra(directorio: Path, puerto: int) -> Path:
    """Tres repositorios útiles (DSpace, OJS y uno que contesta 403) y uno que no anduvo."""
    directorio.mkdir(parents=True, exist_ok=True)
    base = {h: f"https://{h}.sim.localhost:{puerto}" for h in "abcd"}
    _escribir(directorio / "repositorios.csv", ["id", "host", "url_base", "estado"], [
        ["m0001", "a.sim.localhost", base["a"], "ok"],
        ["m0002", "b.sim.localhost", base["b"], "ok"],
        ["m0003", "c.sim.localhost", base["c"], "ok"],
        ["m0004", "d.sim.localhost", base["d"], "antibot"],
    ])  # fmt: skip
    items = [["m0001", f"oai:a:{i}", "", f"http://hdl.handle.net/1/{i}", 1] for i in range(6)]
    items += [["m0002", f"oai:b:{i}", "", f"{base['b']}/index.php/rev/article/view/{i}", 0] for i in range(3)]
    items += [["m0002", "oai:b:sin", "", "http://hdl.handle.net/9/9", 0]]  # sin archivos ni página OJS: afuera
    items += [["m0003", f"oai:c:{i}", "", "", 1] for i in range(3)]
    items += [["m0004", "oai:d:0", "", "", 1]]
    _escribir(directorio / "items.csv", ["repo", "item", "datestamp", "pagina", "archivos"], items)
    archivos = [["m0001", f"oai:a:{i}", f"{base['a']}/bitstream/1/{i}/archivo.pdf", 50_000, "application/pdf", "ORIGINAL", ""]
                for i in range(6)]  # fmt: skip
    archivos += [["m0003", f"oai:c:{i}", f"{base['c']}/bitstream/3/{i}/x.pdf", 1000, "", "", ""] for i in range(3)]
    archivos += [["m0004", "oai:d:0", f"{base['d']}/x.pdf", 1, "", "", ""]]
    _escribir(directorio / "archivos.csv", ["repo", "item", "url", "tamano", "mime", "bundle", "nombre"], archivos)
    return directorio


# --- Catálogo ----------------------------------------------------------------------------


def test_catalogo_clases_de_item_partes_y_tope(tmp_path):
    c = muestra_real.cargar(_muestra(tmp_path / "m", 8443))
    assert c.repos == ["m0001", "m0002", "m0003"]  # m0004 no anduvo
    assert c.resueltos == [6, 0, 3] and c.items_por_repo() == [6, 3, 3]
    assert len(c.precargadas[0]) == 6 and c.precargadas[1] == []
    assert c.url(Tarea(Tipo.DESCARGA, 0, 2, 0)).endswith("/bitstream/1/2/archivo.pdf")
    assert c.url(Tarea(Tipo.RESOLUCION, 1, 0)).endswith("/index.php/rev/article/view/0")
    assert c.archivos_por_item[(0, 0)] == 1 and c.resumen()["items_por_resolver"] == 3
    # Partes: el ítem k va a la parte k % partes.
    dos = [muestra_real.cargar(tmp_path / "m", parte, 2) for parte in (0, 1)]
    assert [d.items[0] for d in dos] == [["oai:a:0", "oai:a:2", "oai:a:4"], ["oai:a:1", "oai:a:3", "oai:a:5"]]
    # Tope de bytes conocidos por repositorio y paso.
    assert muestra_real.cargar(tmp_path / "m", tope=120_000).resueltos[0] == 2
    # Lo que sale de resolver una página toma índices de archivo nuevos.
    tareas = c.registrar(1, 0, ["https://b/x.pdf", "https://b/y.pdf"])
    assert [(t.item, t.archivo) for t in tareas] == [(0, 0), (0, 1)] and c.url(tareas[1]) == "https://b/y.pdf"


# --- Resolución de OJS ---------------------------------------------------------------------


def test_descargas_de_una_pagina_ojs_3():
    html = b"""<html><head><meta name="citation_pdf_url" content="https://r.org/index.php/rev/article/view/45/30"></head>
    <body><a class="obj_galley_link pdf" href="https://r.org/index.php/rev/article/view/45/30">PDF</a>
    <a href="/index.php/rev/article/view/45/31">EPUB</a><a href="/index.php/rev/article/view/46">otro</a></body></html>"""
    urls = descargas_de_pagina(html, "https://r.org/index.php/rev/article/view/45")
    assert urls == ["https://r.org/index.php/rev/article/download/45/30", "https://r.org/index.php/rev/article/download/45/31"]
    assert a_descarga("https://r.org/rev/article/view/1/2?x=1") == "https://r.org/rev/article/download/1/2?x=1"


def test_solo_archivos_del_mismo_host_que_la_pagina():
    # Las referencias de un artículo enlazan PDFs de otros repositorios: no son del ítem.
    html = b"""<meta name="citation_pdf_url" content="https://www.r.org/index.php/rev/article/view/45/30">
    <a href="https://repositorio.cepal.org/bitstream/handle/11362/5943/S055322_pt.pdf">ref</a>
    <a href="https://otra.org/index.php/x/article/download/1/2">ref</a>"""
    assert descargas_de_pagina(html, "https://r.org/index.php/rev/article/view/45") == [
        "https://www.r.org/index.php/rev/article/download/45/30"]


def test_descargas_de_dspace_y_tope():
    html = b"".join(b'<a href="/bitstream/123/%d/f.pdf">f</a>' % i for i in range(5))
    assert len(descargas_de_pagina(html, "https://d.org/handle/123", maximo=3)) == 3
    assert descargas_de_pagina(b"<html><a href='/about'>x</a></html>", "https://d.org/") == []


# --- Planificador, Retry-After y cortesía -------------------------------------------------


def test_planificador_con_precargadas_y_abandono():
    precargadas = [[Tarea(Tipo.DESCARGA, 0, 0, 0), Tarea(Tipo.DESCARGA, 0, 1, 0)], []]
    plan = Planificador([3, 1], intervalo=2.0, inicio=0.0, precargadas=precargadas)
    tomadas = []
    ahora = 0.0
    while len(tomadas) < 4:
        if (t := plan.siguiente(ahora)) is None:
            ahora += 0.5
            continue
        tarea, _ = t
        tomadas.append((tarea.tipo.value, tarea.repo, tarea.item))
        plan.terminada(tarea, ahora)
    # Primero las descargas ya resueltas; después la resolución del ítem 2 (no se piden la 0 ni la 1).
    assert [x for x in tomadas if x[1] == 0] == [("descarga", 0, 0), ("descarga", 0, 1), ("resolucion", 0, 2)]
    plan = Planificador([5], intervalo=1.0, inicio=0.0)
    tarea, _ = plan.siguiente(0.0)
    plan.terminada(tarea, 0.1)
    plan.abandonar(0)
    assert plan.siguiente(10.0) is None and plan.terminado() and plan.con_trabajo() == 0


def test_planificador_con_vueltas():
    """Con `vuelta_minima`, un repositorio que termina vuelve a empezar: las mismas descargas
    precargadas y las mismas resoluciones, no antes de `vuelta_minima` desde la vuelta anterior."""
    reiniciados = []
    precargadas = [[Tarea(Tipo.DESCARGA, 0, 0, 0)], [Tarea(Tipo.DESCARGA, 1, 0, 0)]]
    plan = Planificador([2, 1], intervalo=1.0, inicio=0.0, precargadas=precargadas, vuelta_minima=10.0,
                        al_reiniciar=reiniciados.append)  # fmt: skip
    tomadas = []
    ahora = 0.0
    while ahora < 25.0:
        if (t := plan.siguiente(ahora)) is None:
            ahora += 0.5
            continue
        tarea, _ = t
        tomadas.append((ahora, tarea.tipo.value, tarea.repo, tarea.item, plan.vuelta(tarea.repo)))
        plan.terminada(tarea, ahora + 0.1)
    del_0 = [(t, tipo, item, v) for t, tipo, repo, item, v in tomadas if repo == 0]
    # Vuelta 0: la descarga precargada y la resolución del ítem 1; a los 10 s, lo mismo otra vez.
    assert [(tipo, item, v) for _, tipo, item, v in del_0[:4]] == [
        ("descarga", 0, 0), ("resolucion", 1, 0), ("descarga", 0, 1), ("resolucion", 1, 1)]
    assert del_0[2][0] >= 10.0 and del_0[4][0] >= 20.0
    assert reiniciados.count(0) >= 2 and plan.vueltas()[0] >= 2
    # Un repositorio abandonado no vuelve a empezar.
    plan.abandonar(1)
    assert all(repo != 1 for _, _, repo, _, _ in [x for x in tomadas if x[0] > ahora])
    plan = Planificador([1], intervalo=1.0, inicio=0.0, vuelta_minima=5.0)
    tarea, _ = plan.siguiente(0.0)
    plan.abandonar(0)
    plan.terminada(tarea, 0.1)
    assert plan.siguiente(100.0) is None and plan.terminado() and plan.vueltas() == [0]


def test_retry_after_en_segundos_o_fecha():
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    assert pedido.retry_after("120") == 120.0 and pedido.retry_after(None) == config.RETRY_AFTER
    en_un_minuto = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
    assert 55 <= pedido.retry_after(en_un_minuto) <= 61
    assert pedido.retry_after("cualquier cosa") == config.RETRY_AFTER


def test_verificar_cortesia_del_lado_del_cliente():
    bien = [(0, "a", "descarga", 0.0, 1.0, 200, 1, "", 0), (0, "a", "descarga", 3.0, 4.0, 200, 1, "", 0)]
    assert muestra_real.verificar_cortesia(bien, 2.0)["violaciones"] == 0
    corto = bien + [(0, "a", "descarga", 4.5, 5.0, 200, 1, "", 0)]
    pisado = bien + [(0, "a", "descarga", 3.5, 3.8, 200, 1, "", 0)]
    assert muestra_real.verificar_cortesia(corto, 2.0)["intervalos_cortos"] == 1
    assert muestra_real.verificar_cortesia(pisado, 2.0)["simultaneos"] == 1
    # El reintento sin verificar TLS es parte del mismo turno.
    tls = [(0, "a", "descarga", 0.0, 0.1, None, 0, "ClientConnectorCertificateError", 0),
           (0, "a", "descarga", 0.1, 1.0, 200, 1, "", 1)]  # fmt: skip
    assert muestra_real.verificar_cortesia(tls, 2.0)["violaciones"] == 0


# --- Corrida de humo contra repositorios locales --------------------------------------------


@pytest.fixture
async def repositorios(certs):
    """Un servidor local con tres "repositorios" por host: a (archivos), b (OJS) y c (403)."""
    from concurrencia.servidor import contexto_ssl

    pedidos: dict[str, list[tuple[float, float]]] = {}
    pdf = b"%PDF-1.4 " + b"x" * 49_991

    async def manejar(request: web.Request) -> web.StreamResponse:
        loop = asyncio.get_running_loop()
        host = request.host.split(":")[0]
        inicio = loop.time()
        try:
            await asyncio.sleep(0.02)
            if host.startswith("c."):
                return web.Response(status=403, text="forbidden")
            ruta = request.path
            if "/article/view/" in ruta:
                n = ruta.rsplit("/", 1)[-1]
                meta = f'<meta name="citation_pdf_url" content="{request.scheme}://{request.host}/index.php/rev/article/view/{n}/9">'
                return web.Response(text=f"<html><head>{meta}</head><body>artículo {n}</body></html>", content_type="text/html")
            if "/article/download/" in ruta or "/bitstream/" in ruta:
                return web.Response(body=pdf, content_type="application/pdf")
            return web.Response(status=404)
        finally:
            pedidos.setdefault(host, []).append((inicio, loop.time()))

    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", manejar)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    puerto = puertos_libres(1)
    await web.TCPSite(runner, "127.0.0.1", puerto, ssl_context=contexto_ssl(certs)).start()
    yield puerto, pedidos
    await runner.cleanup()


async def test_humo_contra_repositorios_locales(repositorios, certs, tmp_path, monkeypatch):
    from concurrencia.s3falso import S3Falso
    from concurrencia.servidor import contexto_ssl

    puerto, en_el_servidor = repositorios
    monkeypatch.setattr(config, "MUESTRA", str(_muestra(tmp_path / "muestra", puerto)))
    monkeypatch.setattr(config, "CONTACTO", "prueba@example.org")
    s3 = S3Falso()
    runner = web.AppRunner(s3.app(), access_log=None)
    await runner.setup()
    puerto_s3 = puertos_libres(1)
    await web.TCPSite(runner, "127.0.0.1", puerto_s3, ssl_context=contexto_ssl(certs)).start()
    escenario = Escenario("rprueba", 4, 0, 0, "humo", muestra=True, intervalo=0.3, subida=True,
                          duracion=8.0, calentamiento=1.0)  # fmt: skip
    salida = tmp_path / "rprueba"
    try:
        resumen = await motor._correr_muestra(
            escenario, certs, salida, 1, None, 1, puerto_s3, tmp_path / "spool",
            resolver=motor.ResolverLocal(), contexto=motor.contexto_ssl(certs),
        )  # fmt: skip
    finally:
        await runner.cleanup()

    assert resumen["terminado_antes_de_tiempo"]  # bajó todo antes de los 9 s
    assert resumen["servidor"]["violaciones"] == 0 and resumen["servidor"]["real"]
    assert resumen["muestra"]["abandonados"] == {"m0003": "http_403"}
    assert resumen["errores_por_tipo"] == {"http_403": 2}  # dos 403 seguidos para abandonar
    # 6 archivos de a, 3 páginas y 3 PDF de b, y los dos pedidos a c (abandonado).
    assert {h: len(v) for h, v in en_el_servidor.items()} == {"a.sim.localhost": 6, "b.sim.localhost": 6, "c.sim.localhost": 2}
    # La cortesía también vista desde el servidor: nunca a la vez y 0,3 s entre pedidos.
    for lista in en_el_servidor.values():
        lista.sort()
        assert all(i1 - f0 >= 0.3 - 0.05 for (_i0, f0), (i1, _f1) in zip(lista, lista[1:], strict=False))
    # Los 9 archivos llegan al spool; lo que el subidor alcanzó a subir es lo que recibió el S3 falso.
    subida = resumen["subida"]
    assert sum(subida["entregado_bytes"].values()) == 9 * 50_000
    subido = subida["subidor"]["bytes_ram"] + subida["subidor"]["bytes_disco"]
    assert subido + sum(subida["pendiente_final_bytes"].values()) == 9 * 50_000 and s3.bytes == subido
    with open(salida.with_name("rprueba-pedidos.csv"), encoding="utf-8") as f:
        filas = list(csv.DictReader(f))
    assert len(filas) == 14 and {f["tipo"] for f in filas} == {"descarga", "resolucion"}
    indice = json.loads(salida.with_name("rprueba-indice.json").read_text())
    assert indice["1"]["id"] == "m0002" and len(indice["0"]["items"]) == 6
    with open(salida.with_suffix(".csv"), encoding="utf-8") as f:
        assert max(float(f["repos_con_trabajo"]) for f in csv.DictReader(f)) >= 1
    # El informe de una carpeta con solo repositorios reales: su sección, sin las del simulador.
    from concurrencia import informe

    informe.armar(tmp_path, tmp_path / "report.md")
    texto = (tmp_path / "report.md").read_text()
    assert "## Repositorios reales" in texto and "Proyección" not in texto and "| 4 | 3 |" in texto


async def test_humo_con_vueltas(repositorios, certs, tmp_path, monkeypatch):
    """La corrida larga (r75h): los repositorios vuelven a empezar y la cortesía se sigue cumpliendo."""
    puerto, en_el_servidor = repositorios
    monkeypatch.setattr(config, "MUESTRA", str(_muestra(tmp_path / "muestra", puerto)))
    monkeypatch.setattr(config, "CONTACTO", "prueba@example.org")
    escenario = Escenario("rvueltas", 4, 0, 0, "humo con vueltas", muestra=True, intervalo=0.2,
                          duracion=6.0, calentamiento=0.5, vuelta_minima=2.0)  # fmt: skip
    salida = tmp_path / "rvueltas"
    resumen = await motor._correr_muestra(
        escenario, certs, salida, 1, None, 1, config.PUERTO_S3, tmp_path / "spool",
        resolver=motor.ResolverLocal(), contexto=motor.contexto_ssl(certs),
    )  # fmt: skip
    assert not resumen["terminado_antes_de_tiempo"]  # con vueltas no se queda sin trabajo
    assert resumen["servidor"]["violaciones"] == 0
    assert sum(resumen["muestra"]["vueltas"].values()) == 3 and max(int(k) for k in resumen["muestra"]["vueltas"]) >= 2
    with open(salida.with_name("rvueltas-pedidos.csv"), encoding="utf-8") as f:
        filas = list(csv.DictReader(f))
    de_a = [f for f in filas if f["host"] == "a.sim.localhost"]
    assert {f["vuelta"] for f in de_a} >= {"0", "1", "2"}
    # Cada vuelta de a pide sus 6 archivos, y la siguiente arranca 2 s o más después que la anterior.
    inicios = {v: min(float(f["inicio"]) for f in de_a if f["vuelta"] == v) for v in ("0", "1", "2")}
    assert inicios["1"] - inicios["0"] >= 2.0 - 0.05 and inicios["2"] - inicios["1"] >= 2.0 - 0.05
    assert sum(1 for f in de_a if f["vuelta"] == "0") == 6
    # Los ítems de las vueltas siguientes también cuentan como completos.
    with open(salida.with_suffix(".csv"), encoding="utf-8") as f:
        assert sum(float(f["items_ok"]) for f in csv.DictReader(f)) > 2 * (6 + 3)
    from concurrencia import informe

    informe.armar(tmp_path, tmp_path / "report.md")
    texto = (tmp_path / "report.md").read_text()
    assert "## Corrida larga" in texto and "| 1ª |" in texto and "| 2ª |" in texto and "| 0–5 |" in texto
    for lista in en_el_servidor.values():
        lista.sort()
        assert all(i1 - f0 >= 0.2 - 0.05 for (_i0, f0), (i1, _f1) in zip(lista, lista[1:], strict=False))


async def test_un_archivo_comprimido_no_es_incompleto(certs, tmp_path, monkeypatch):
    # Un servidor que manda el PDF con gzip: Content-Length es el comprimido y aiohttp entrega el original.
    import gzip

    from concurrencia.servidor import contexto_ssl

    original = b"%PDF-1.4 " + b"y" * 20_000
    comprimido = gzip.compress(original)

    async def manejar(request: web.Request) -> web.Response:
        return web.Response(body=comprimido, headers={"Content-Encoding": "gzip", "Content-Type": "application/pdf"})

    app = web.Application()
    app.router.add_get("/{tail:.*}", manejar)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    puerto = puertos_libres(1)
    await web.TCPSite(runner, "127.0.0.1", puerto, ssl_context=contexto_ssl(certs)).start()
    try:
        async with motor.sesion_real("prueba@example.org", motor.ResolverLocal(), motor.contexto_ssl(certs)) as sesion:
            r = await pedido.hacer(sesion, Tarea(Tipo.DESCARGA, 0, 0, 0), f"https://a.sim.localhost:{puerto}/x.pdf",
                                   lambda n, h: None)  # fmt: skip
    finally:
        await runner.cleanup()
    assert r.error is None and r.recibidos == len(original) and r.sha
