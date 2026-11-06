import asyncio
import random
from datetime import datetime, timedelta, timezone

import pytest

from relevamiento.config import ConfigCortesia
from relevamiento.cortesia import SinPedido
from relevamiento.modelos import Contexto
from relevamiento.verificar import verificar, verificar_pedidos

T0 = datetime(2026, 10, 28, 22, 0, tzinfo=timezone.utc)


def pedido(pid, host, inicio, fin, **kw):
    return {
        "id": pid, "corrida": "c1", "repo": kw.pop("repo", "r1"), "host": host, "url": kw.pop("url", f"https://{host}/{pid}"),
        "metodo": "pagina", "cadena": kw.pop("cadena", pid), "tipo": "pagina", "bytes_cuerpo": 10,
        "inicio": (T0 + timedelta(seconds=inicio)).isoformat(), "fin": (T0 + timedelta(seconds=fin)).isoformat(), **kw,
    }


def test_una_corrida_correcta_no_tiene_violaciones():
    pedidos = [pedido("1", "a", 0, 1), pedido("2", "a", 6, 7), pedido("3", "b", 0, 4), pedido("4", "b", 12, 13)]
    res = verificar_pedidos(pedidos)
    assert res.ok, res.violaciones
    assert res.estadisticas["hueco_minimo_s"] == 5


@pytest.mark.parametrize(
    "pedidos, fragmento",
    [
        ([pedido("1", "a", 0, 3), pedido("2", "a", 2, 4)], "simultáneos"),
        ([pedido("1", "a", 0, 1), pedido("2", "a", 4, 5)], "se requerían 5.000"),
        ([pedido("1", "a", 0, 4), pedido("2", "a", 10, 11)], "se requerían 8.000"),
        ([pedido(str(n), h, 0, 1) for n, h in enumerate("abcd")], "4 pedidos y 4 hosts"),
        ([pedido("1", "a", 0, 1, abandono="código 429"), pedido("2", "a", 60, 61)], "después del abandono"),
        ([pedido(str(n), f"h{n}", 0, 1, repo="r") for n in range(41)], "41 pedidos"),
        ([pedido(str(n), f"h{n}", n * 10, n * 10 + 1, cadena="x") for n in range(7)], "7 saltos"),
        ([pedido("1", "a", 0, 1, tipo="archivo", bytes_cuerpo=4096)], "4096 bytes"),
    ],
)
def test_detecta_cada_violacion(pedidos, fragmento):
    res = verificar_pedidos(pedidos)
    assert not res.ok
    assert any(fragmento in v for v in res.violaciones), res.violaciones


def test_respeta_el_crawl_delay_desde_que_se_conoce():
    pedidos = [pedido("1", "a", 0, 1), pedido("2", "a", 7, 8), pedido("3", "a", 14, 15)]
    assert verificar_pedidos(pedidos, {"a": [(9e99, 10)]}).ok
    desde = (T0 + timedelta(seconds=1)).timestamp()
    res = verificar_pedidos(pedidos, {"a": [(desde, 10)]})
    assert len(res.violaciones) == 2


def test_una_url_repetida_es_solo_un_aviso():
    res = verificar_pedidos([pedido("1", "a", 0, 1, url="https://a/x"), pedido("2", "a", 10, 11, url="https://a/x")])
    assert res.ok and res.avisos


@pytest.mark.parametrize("semilla", range(5))
async def test_la_salida_de_la_cortesia_siempre_verifica(internet, crear_cortesia, salida, semilla):
    """Carga aleatoria: varios repos comparten hosts, latencias variables, redirecciones, errores y crawl-delay."""
    azar = random.Random(semilla)
    hosts = [f"h{n}.org" for n in range(5)]
    internet.agregar("https://h0.org/robots.txt", cuerpo="User-agent: *\nCrawl-delay: 9\n", tipo="text/plain")
    for h in hosts:
        for n in range(8):
            opcion = azar.random()
            if opcion < 0.15:
                internet.redireccion(f"https://{h}/{n}", f"https://{azar.choice(hosts)}/{(n + 1) % 8}")
            elif opcion < 0.2:
                internet.agregar(f"https://{h}/{n}", codigo=403)
            else:
                internet.agregar(f"https://{h}/{n}", cuerpo="ok", latencia=azar.uniform(0.01, 6))
    cfg = ConfigCortesia(pedidos_por_repo=15)

    async def repo(nombre: str):
        ctx = Contexto(repo=nombre, paso="metodo", metodo="x")
        for _ in range(20):
            try:
                await c.pedir(f"https://{azar.choice(hosts)}/{azar.randrange(8)}", ctx)
            except SinPedido:
                pass

    async with crear_cortesia(cfg) as c:
        await asyncio.gather(*(repo(f"r{n}") for n in range(6)))
    res = verificar(salida, cfg)
    assert res.ok, res.violaciones
    assert res.estadisticas["max_en_vuelo"] <= 3


async def test_el_verificador_detecta_una_cortesia_mal_configurada(internet, crear_cortesia, salida):
    for n in range(3):
        internet.agregar(f"https://a.org/{n}", cuerpo="ok")
    async with crear_cortesia(ConfigCortesia(intervalo_minimo=1)) as c:
        for n in range(3):
            await c.pedir(f"https://a.org/{n}", Contexto(repo="r1", paso="metodo", metodo="x"))
    res = verificar(salida)
    assert not res.ok and len(res.violaciones) == 3
