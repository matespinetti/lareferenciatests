import asyncio
from datetime import timedelta

import httpx
import pytest

from relevamiento.config import ConfigCortesia
from relevamiento.cortesia import (
    EstadoHost, HostAbandonado, HostNoPublico, PresupuestoAgotado, TurnoHost, espera_necesaria,
)
from relevamiento.modelos import Contexto
from relevamiento.parseo import es_host_publico
from relevamiento.registro import PEDIDOS, leer_jsonl

from conftest import CuerpoInfinito

CTX = Contexto(repo="r1", paso="metodo", metodo="pagina")
CFG = ConfigCortesia()


def huecos(llamadas):
    return [b.inicio - a.fin for a, b in zip(llamadas, llamadas[1:])]


# --- Funciones puras -------------------------------------------------------------------


@pytest.mark.parametrize(
    "ultimo_fin, duracion, crawl, ahora, esperado",
    [
        (None, 0, 0, 100, 0),
        (100, 1, 0, 100, 5),
        (100, 4, 0, 100, 8),
        (100, 1, 12, 100, 12),
        (100, 1, 0, 103, 2),
        (100, 1, 0, 200, 0),
    ],
)
def test_espera_necesaria(ultimo_fin, duracion, crawl, ahora, esperado):
    turno = TurnoHost(ultimo_fin=ultimo_fin, ultima_duracion=duracion, crawl_delay=crawl)
    assert espera_necesaria(turno, ahora, CFG) == pytest.approx(esperado)


@pytest.mark.parametrize(
    "secuencia, abandona_en",
    [
        ([(429, None, None)], 0),
        ([(503, None, None)], 0),
        ([(200, None, "anubis")], 0),
        ([(403, None, None), (403, None, None)], 1),
        ([(403, None, None), (200, None, None), (403, None, None)], None),
        ([(None, "red", None), (None, "timeout_lectura", None), (502, None, None)], 2),
        ([(None, "red", None), (None, "red", None), (200, None, None), (None, "red", None)], None),
        ([(None, "tls_invalido", None), (None, "red", None), (None, "red", None)], None),
        ([(404, None, None), (500, None, None)], None),
    ],
)
def test_reglas_de_abandono(secuencia, abandona_en):
    estado = EstadoHost()
    motivos = [estado.registrar(c, e, a, CFG) for c, e, a in secuencia]
    indices = [i for i, m in enumerate(motivos) if m]
    assert indices == ([] if abandona_en is None else [abandona_en])


# --- Turnos e intervalos ---------------------------------------------------------------


async def test_pedidos_al_mismo_host_nunca_se_solapan_y_respetan_el_intervalo(internet, crear_cortesia):
    for n in range(4):
        internet.agregar(f"https://a.org/{n}", cuerpo="ok")
    async with crear_cortesia() as c:
        await asyncio.gather(*(c.pedir(f"https://a.org/{n}", CTX) for n in range(4)))
    llamadas = internet.llamadas_a("a.org")
    assert len(llamadas) == 5  # robots.txt + 4
    assert internet.max_por_host == 1
    assert all(h >= 5 - 1e-6 for h in huecos(llamadas))


async def test_el_intervalo_es_el_doble_de_la_ultima_duracion(internet, crear_cortesia):
    internet.agregar("https://a.org/lenta", cuerpo="ok", latencia=4)
    internet.agregar("https://a.org/otra", cuerpo="ok")
    async with crear_cortesia() as c:
        await c.pedir("https://a.org/lenta", CTX)
        await c.pedir("https://a.org/otra", CTX)
    assert huecos(internet.llamadas_a("a.org"))[-1] == pytest.approx(8)


async def test_se_respeta_el_crawl_delay_si_supera_el_intervalo(internet, crear_cortesia):
    internet.agregar("https://a.org/robots.txt", cuerpo="User-agent: *\nCrawl-delay: 12\n", tipo="text/plain")
    internet.agregar("https://a.org/x", cuerpo="ok")
    internet.agregar("https://a.org/y", cuerpo="ok")
    async with crear_cortesia() as c:
        await c.pedir("https://a.org/x", CTX)
        await c.pedir("https://a.org/y", CTX)
    assert all(h >= 12 - 1e-6 for h in huecos(internet.llamadas_a("a.org")))


async def test_robots_se_pide_una_vez_por_host_y_se_registra_si_estaria_permitido(internet, crear_cortesia, salida):
    internet.agregar("https://a.org/robots.txt", cuerpo="User-agent: *\nDisallow: /privado\n", tipo="text/plain")
    internet.agregar("https://a.org/privado/1", cuerpo="ok")
    async with crear_cortesia() as c:
        await asyncio.gather(c.pedir("https://a.org/privado/1", CTX), c.pedir("https://a.org/libre", CTX))
    pedidos = list(leer_jsonl(salida / PEDIDOS))
    assert [p["metodo"] for p in pedidos].count("robots") == 1
    permitidos = {p["url"]: p["robots_permitido"] for p in pedidos if p["metodo"] != "robots"}
    assert permitidos == {"https://a.org/privado/1": False, "https://a.org/libre": True}


async def test_nunca_hay_mas_de_tres_pedidos_en_vuelo(internet, crear_cortesia):
    hosts = [f"h{n}.org" for n in range(6)]
    for h in hosts:
        for n in range(3):
            internet.agregar(f"https://{h}/{n}", cuerpo="ok", latencia=2)
    async with crear_cortesia() as c:
        await asyncio.gather(
            *(c.pedir(f"https://{h}/{n}", Contexto(repo=h, paso="metodo", metodo="x")) for h in hosts for n in range(3))
        )
    assert internet.max_en_vuelo == 3
    assert internet.max_por_host == 1


# --- Presupuesto -----------------------------------------------------------------------


async def test_el_pedido_que_excede_el_presupuesto_no_sale(internet, crear_cortesia):
    async with crear_cortesia(ConfigCortesia(pedidos_por_repo=3)) as c:
        await c.pedir("https://a.org/1", CTX)  # robots + 1
        await c.pedir("https://a.org/2", CTX)
        with pytest.raises(PresupuestoAgotado):
            await c.pedir("https://a.org/3", CTX)
        assert c.restantes("r1") == 0
    assert len(internet.llamadas) == 3


# --- Redirecciones ---------------------------------------------------------------------


async def test_cada_salto_de_una_redireccion_respeta_el_turno_de_su_host(internet, crear_cortesia, salida):
    internet.redireccion("https://a.org/x", "https://b.org/y")
    internet.redireccion("https://b.org/y", "/z")
    internet.redireccion("https://b.org/z", "https://a.org/final")
    internet.agregar("https://a.org/final", cuerpo="fin")
    async with crear_cortesia() as c:
        r = await c.pedir("https://a.org/x", CTX)
    assert r.codigo == 200 and r.url == "https://a.org/final" and r.cuerpo == b"fin"
    assert [s.url for s in r.saltos] == ["https://a.org/x", "https://b.org/y", "https://b.org/z", "https://a.org/final"]
    assert len(r.pedido_ids) == 4
    for host in ("a.org", "b.org"):
        assert all(h >= 5 - 1e-6 for h in huecos(internet.llamadas_a(host)))
    pedidos = [p for p in leer_jsonl(salida / PEDIDOS) if p["metodo"] != "robots"]
    assert {p["cadena"] for p in pedidos} == {r.pedido_ids[0]}
    assert [p["salto"] for p in pedidos] == [0, 1, 2, 3]


async def test_mas_de_cinco_redirecciones_corta_la_cadena(internet, crear_cortesia):
    for n in range(10):
        internet.redireccion(f"https://a.org/{n}", f"https://a.org/{n + 1}")
    async with crear_cortesia() as c:
        r = await c.pedir("https://a.org/0", CTX)
    assert r.error == "demasiadas_redirecciones"
    assert len(r.saltos) == 6


async def test_un_bucle_de_redirecciones_se_corta(internet, crear_cortesia):
    internet.redireccion("https://a.org/1", "https://a.org/2")
    internet.redireccion("https://a.org/2", "https://a.org/1")
    async with crear_cortesia() as c:
        r = await c.pedir("https://a.org/1", CTX)
    assert r.error == "redireccion_invalida" and len(r.saltos) == 2


# --- Hosts no públicos -----------------------------------------------------------------


@pytest.mark.parametrize(
    "host, publico",
    [
        ("repositorio.unlp.edu.ar", True),
        ("8.8.8.8", True),
        ("localhost", False),
        ("app.localhost", False),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("192.168.0.10", False),
        ("169.254.169.254", False),
        ("::1", False),
        ("fd00::1", False),
        ("dspace", False),
        ("dspace.internal", False),
    ],
)
def test_es_host_publico(host, publico):
    assert es_host_publico(host) is publico


async def test_un_host_no_publico_no_sale_ni_pide_robots(internet, crear_cortesia, salida):
    async with crear_cortesia() as c:
        with pytest.raises(HostNoPublico):
            await c.pedir("http://localhost:4000/bitstreams/x/download", CTX)
    assert internet.llamadas == []
    assert list(leer_jsonl(salida / PEDIDOS)) == []


async def test_una_redireccion_a_una_red_privada_se_corta_antes_del_salto(internet, crear_cortesia):
    internet.redireccion("https://a.org/x", "http://10.0.0.5/archivo.pdf")
    async with crear_cortesia() as c:
        with pytest.raises(HostNoPublico) as exc:
            await c.pedir("https://a.org/x", CTX)
    assert {ll.host for ll in internet.llamadas} == {"a.org"}
    assert len(exc.value.pedido_ids) == 1


# --- Lectura truncada ------------------------------------------------------------------


async def test_un_archivo_se_pide_con_range_y_se_corta_a_los_2048_bytes(internet, crear_cortesia):
    cuerpo = CuerpoInfinito()
    internet.agregar("https://a.org/f.pdf", lambda _r: httpx.Response(200, headers={"content-type": "application/pdf"}, stream=cuerpo))
    async with crear_cortesia() as c:
        r = await c.pedir("https://a.org/f.pdf", CTX, tipo="archivo")
    assert len(r.cuerpo) == 2048 and r.truncado
    assert cuerpo.cerrado and cuerpo.entregados <= 65536
    pedido = internet.llamadas_a("a.org")[-1]
    assert pedido.encabezados["range"] == "bytes=0-2047"
    assert pedido.encabezados["accept-encoding"] == "identity"


async def test_una_pagina_se_corta_a_los_2_mb(internet, crear_cortesia):
    cuerpo = CuerpoInfinito()
    internet.agregar("https://a.org/p", lambda _r: httpx.Response(200, headers={"content-type": "text/html"}, stream=cuerpo))
    async with crear_cortesia() as c:
        r = await c.pedir("https://a.org/p", CTX)
    assert len(r.cuerpo) == 2 * 1024 * 1024 and r.truncado and cuerpo.cerrado
    assert cuerpo.entregados <= 2 * 1024 * 1024 + 65536


async def test_si_una_pagina_resulta_ser_un_archivo_se_lee_como_archivo(internet, crear_cortesia):
    cuerpo = CuerpoInfinito()
    internet.agregar("https://a.org/item", lambda _r: httpx.Response(200, headers={"content-type": "application/pdf"}, stream=cuerpo))
    async with crear_cortesia() as c:
        r = await c.pedir("https://a.org/item", CTX)
    assert len(r.cuerpo) == 2048 and cuerpo.cerrado


async def test_una_respuesta_chica_no_se_marca_truncada(internet, crear_cortesia):
    internet.agregar("https://a.org/f", cuerpo=b"%PDF" + b"x" * 2044, tipo="application/pdf",
                     encabezados={"content-range": "bytes 0-2047/90000"})
    async with crear_cortesia() as c:
        r = await c.pedir("https://a.org/f", CTX, tipo="archivo")
    assert not r.truncado and r.total == 90000


# --- Errores, TLS y tiempos ------------------------------------------------------------


async def test_tls_invalido_se_registra_y_se_reintenta_una_sola_vez_sin_verificar(internet, crear_cortesia, salida):
    internet.tls_invalido.add("a.org")
    internet.agregar("https://a.org/x", cuerpo="ok")
    async with crear_cortesia() as c:
        r = await c.pedir("https://a.org/x", CTX)
        r2 = await c.pedir("https://a.org/x2", CTX)
    assert r.codigo == 200 and not r.tls_verificado and r2.codigo == 404
    # robots: verificado (falla) + reintento; después todo va directo sin verificar
    assert [ll.verificado for ll in internet.llamadas] == [True, False, False, False]
    pedidos = list(leer_jsonl(salida / PEDIDOS))
    assert [p["error"] for p in pedidos] == ["tls_invalido", None, None, None]
    assert [p["reintento_tls"] for p in pedidos] == [False, True, False, False]
    assert huecos(internet.llamadas)[0] >= 5 - 1e-6


@pytest.mark.parametrize(
    "respuestas, motivo",
    [
        ([dict(codigo=429)], "código 429"),
        ([dict(codigo=503)], "código 503"),
        ([dict(codigo=403), dict(codigo=403)], "2 respuestas 403 seguidas"),
        ([dict(codigo=200, cuerpo='<script src="/.within.website/x/anubis.js"></script>anubis')], "anti-bot: anubis"),
        ([dict(codigo=403, cuerpo="<title>Just a moment...</title>", encabezados={"cf-mitigated": "challenge"})],
         "anti-bot: cloudflare"),
    ],
)
async def test_abandono_del_host(internet, crear_cortesia, salida, respuestas, motivo):
    for n, kw in enumerate(respuestas):
        internet.agregar(f"https://a.org/{n}", **kw)
    async with crear_cortesia() as c:
        for n in range(len(respuestas)):
            await c.pedir(f"https://a.org/{n}", CTX)
        with pytest.raises(HostAbandonado):
            await c.pedir("https://a.org/despues", CTX)
        assert c.abandono("a.org") == motivo
    assert len(internet.llamadas) == len(respuestas) + 1
    eventos = list(leer_jsonl(salida / "eventos.jsonl"))
    assert eventos[-1]["tipo"] == "abandono" and eventos[-1]["motivo"] == motivo


async def test_tres_errores_de_red_seguidos_abandonan_el_host(internet, crear_cortesia):
    def falla(_r):
        raise httpx.ConnectError("conexión rechazada")

    for n in range(3):
        internet.agregar(f"https://a.org/{n}", falla)
    async with crear_cortesia() as c:
        errores = [(await c.pedir(f"https://a.org/{n}", CTX)).error for n in range(3)]
        assert errores == ["red", "red", "red"]
        assert c.abandono("a.org") == "3 errores de red seguidos"


async def test_un_servidor_que_gotea_bytes_se_corta_a_los_120_s(internet, crear_cortesia):
    internet.agregar("https://a.org/goteo", lambda _r: httpx.Response(
        200, headers={"content-type": "text/html"}, stream=CuerpoInfinito(b"x", pausa=29)))
    async with crear_cortesia() as c:
        r = await c.pedir("https://a.org/goteo", CTX)
    assert r.error == "timeout_total"
    ultima = internet.llamadas_a("a.org")[-1]
    assert ultima.fin - ultima.inicio <= 120


async def test_cancelar_a_mitad_de_un_pedido_lo_registra_y_cierra_el_cuerpo(internet, crear_cortesia, salida):
    cuerpo = CuerpoInfinito(b"x", pausa=1)
    internet.agregar("https://a.org/lenta", lambda _r: httpx.Response(200, headers={"content-type": "text/html"}, stream=cuerpo))
    async with crear_cortesia() as c:
        tarea = asyncio.create_task(c.pedir("https://a.org/lenta", CTX))
        await asyncio.sleep(10)
        tarea.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarea
    assert cuerpo.cerrado
    assert list(leer_jsonl(salida / PEDIDOS))[-1]["error"] == "cancelado"


# --- Reanudación -----------------------------------------------------------------------


async def test_sembrar_restaura_intervalo_presupuesto_robots_y_abandonos(internet, crear_cortesia):
    async with crear_cortesia(ConfigCortesia(pedidos_por_repo=5)) as c:
        ahora = c.reloj.ahora_utc()
        previos = [
            {"repo": "r1", "host": "a.org", "inicio": (ahora - timedelta(seconds=2)).isoformat(),
             "fin": (ahora - timedelta(seconds=1)).isoformat()},
            {"repo": "r1", "host": "b.org", "inicio": ahora.isoformat(), "fin": ahora.isoformat(),
             "abandono": "código 429"},
        ]
        c.sembrar(previos, {"a.org": (404, b"")})
        assert c.restantes("r1") == 3
        with pytest.raises(HostAbandonado):
            await c.pedir("https://b.org/", CTX)
        inicio = c.reloj.ahora()
        await c.pedir("https://a.org/x", CTX)
    llamadas = internet.llamadas_a("a.org")
    assert len(llamadas) == 1  # robots.txt ya estaba resuelto
    assert llamadas[0].inicio - inicio == pytest.approx(4, abs=0.01)
