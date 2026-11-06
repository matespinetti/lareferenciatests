"""medir-velocidad: la medición pasa por la cortesía, lee con tope y no guarda archivos."""

import json
from dataclasses import replace

import httpx

from relevamiento.config import CORTESIA_MEDICION
from relevamiento.medir import Candidato, candidatos, medir, resumir_medicion, velocidades
from relevamiento.modelos import Contexto
from relevamiento.registro import PEDIDOS, leer_jsonl
from relevamiento.verificar import verificar

from conftest import CuerpoInfinito

CTX = Contexto(repo="r1", paso="medicion", metodo="medicion")
CHICA = replace(CORTESIA_MEDICION, bytes_medicion=512 * 1024, segundos_medicion=5.0)
DOS_MB = replace(CHICA, bytes_medicion=2 * 1024 * 1024)


def _pdf(cuerpo: CuerpoInfinito, largo: int | None = None):
    encabezados = {"content-type": "application/pdf"}
    if largo is not None:
        encabezados["content-length"] = str(largo)
    return lambda _r: httpx.Response(200, headers=encabezados, stream=cuerpo)


async def test_lee_hasta_el_tope_de_bytes_sin_range_y_guarda_solo_el_principio(internet, crear_cortesia, salida):
    cuerpo = CuerpoInfinito()
    internet.agregar("https://a.org/f.pdf", _pdf(cuerpo))
    async with crear_cortesia(CHICA) as c:
        r = await c.pedir("https://a.org/f.pdf", CTX, tipo="medicion")
    assert len(r.cuerpo) == 2048 and r.truncado and cuerpo.cerrado
    pedido = internet.llamadas_a("a.org")[-1]
    assert "range" not in pedido.encabezados
    assert pedido.encabezados["accept-encoding"] == "identity"
    registro = [p for p in leer_jsonl(salida / PEDIDOS) if p["tipo"] == "medicion"][0]
    assert registro["bytes_cuerpo"] == 512 * 1024
    assert (salida / registro["captura"]).with_suffix(".cuerpo").stat().st_size == 2048


async def test_corta_por_tiempo_un_archivo_lento(internet, crear_cortesia, salida):
    cuerpo = CuerpoInfinito(trozo=b"x" * 1024, pausa=1.0)
    internet.agregar("https://a.org/lento.pdf", _pdf(cuerpo))
    async with crear_cortesia(CHICA) as c:
        r = await c.pedir("https://a.org/lento.pdf", CTX, tipo="medicion")
    assert r.truncado and cuerpo.entregados <= 7 * 1024


async def test_un_archivo_que_entra_entero_no_queda_truncado(internet, crear_cortesia, salida):
    cuerpo = CuerpoInfinito(total=4 * 65536)
    internet.agregar("https://a.org/chico.pdf", _pdf(cuerpo, 4 * 65536))
    async with crear_cortesia(CHICA) as c:
        r = await c.pedir("https://a.org/chico.pdf", CTX, tipo="medicion")
    assert not r.truncado


def _piloto(tmp_path, filas):
    piloto = tmp_path / "piloto"
    piloto.mkdir()
    (piloto / PEDIDOS).write_text("\n".join(json.dumps(f) for f in filas))
    return piloto


def test_candidatos_grandes_permitidos_y_de_hosts_no_abandonados(tmp_path):
    def archivo(repo, host, n, mb, robots=True, codigo=206):
        return {"repo": repo, "host": host, "url": f"https://{host}/{n}.pdf", "tipo": "archivo", "codigo": codigo,
                "content_range": f"bytes 0-2047/{int(mb * 1e6)}", "robots_permitido": robots}  # fmt: skip

    piloto = _piloto(tmp_path, [
        archivo("r1", "a.org", 1, 5), archivo("r1", "a.org", 2, 3), archivo("r1", "a.org", 3, 2),
        archivo("r1", "a.org", 4, 9), archivo("r1", "a.org", 5, 0.5),
        archivo("r2", "b.org", 1, 4, robots=False), archivo("r2", "b.org", 2, 4, codigo=404),
        archivo("r3", "c.org", 1, 7), {"repo": "r3", "host": "c.org", "url": "https://c.org/", "abandono": "429"},
    ])  # fmt: skip
    elegidos = candidatos([piloto])
    assert [(c.repo, c.tamano) for c in elegidos] == [("r1", 9_000_000), ("r1", 5_000_000), ("r1", 3_000_000)]


async def test_medir_respeta_la_cortesia_de_medicion_y_resume(internet, salida):
    for n in range(3):
        internet.agregar(f"https://a.org/{n}.pdf", _pdf(CuerpoInfinito(pausa=0.05)))
        internet.agregar(f"https://b.org/{n}.pdf", _pdf(CuerpoInfinito(pausa=0.05)))
    elegidos = [Candidato(repo, host, f"https://{host}/{n}.pdf", 9_000_000)
                for repo, host in (("ra", "a.org"), ("rb", "b.org")) for n in range(3)]  # fmt: skip
    import relevamiento.medir as modulo

    original = modulo.Cortesia
    modulo.Cortesia = lambda cfg, registro, contacto: original(  # type: ignore[assignment]
        cfg, registro, contacto, transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar
    )
    try:
        await medir(elegidos, salida, "prueba@example.org", DOS_MB)
    finally:
        modulo.Cortesia = original
    assert internet.max_por_host == 1
    llamadas = [ll for ll in internet.llamadas if ll.url.endswith(".pdf")]
    assert len(llamadas) == 6
    assert verificar(salida, DOS_MB).ok
    filas = velocidades(salida)
    assert len(filas) == 6 and all(f["mb_s"] > 0 for f in filas)
    resumen = resumir_medicion(salida)
    assert resumen["repositorios"] == 2 and "p50" in resumen["mb_s_por_archivo"]
    assert (salida / "velocidad.csv").exists()


def test_el_verificador_acepta_el_tope_de_medicion_y_no_mas(tmp_path):
    from relevamiento.verificar import verificar_pedidos

    def pedido(n, leidos):
        return {"id": f"p{n}", "repo": "r1", "host": "a.org", "tipo": "medicion", "corrida": "c1",
                "metodo": "medicion", "url": "https://a.org/f.pdf",
                "inicio": f"2026-11-06T12:0{n}:00+00:00", "fin": f"2026-11-06T12:0{n}:01+00:00", "bytes_cuerpo": leidos}  # fmt: skip

    ok = verificar_pedidos([pedido(0, CORTESIA_MEDICION.bytes_medicion + 16_000)], {}, CORTESIA_MEDICION)
    assert ok.ok
    mal = verificar_pedidos([pedido(0, CORTESIA_MEDICION.bytes_medicion * 2)], {}, CORTESIA_MEDICION)
    assert not mal.ok
