"""medir-tamanos: páginas enteras de ListRecords por la cortesía, sin archivos, y un resumen ponderado."""

from pathlib import Path

import httpx
import pytest

from relevamiento.config import CORTESIA_TAMANOS, VENTANAS_TAMANOS
from relevamiento.extractores import extraer_pagina_tamanos
from relevamiento.modelos import Respuesta
from relevamiento.registro import PEDIDOS, leer_jsonl
from relevamiento.tamanos import Repositorio, candidatos, medir_tamanos, resumir_tamanos, urls
from relevamiento.verificar import verificar

CAPTURAS = Path(__file__).parent.parent / "piloto3" / "capturas"


def _registro_mets(ident: str, archivos: list[tuple[str, int]], borrado: bool = False) -> str:
    if borrado:
        return f'<record><header status="deleted"><identifier>{ident}</identifier></header></record>'
    grupos = "".join(
        f'<fileGrp USE="{uso}"><file ID="f{i}" MIMETYPE="application/pdf" SIZE="{tam}"><FLocat xlink:href="https://x/{i}"/></file></fileGrp>'
        for i, (uso, tam) in enumerate(archivos)
    )
    return (
        f"<record><header><identifier>{ident}</identifier></header><metadata>"
        f'<mets xmlns="http://www.loc.gov/METS/" xmlns:xlink="http://www.w3.org/1999/xlink"><fileSec>{grupos}</fileSec></mets>'
        "</metadata></record>"
    )


def _pagina(registros: list[str], total: int | None = None) -> bytes:
    token = f'<resumptionToken completeListSize="{total}">t</resumptionToken>' if total is not None else ""
    return (
        '<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords>'
        + "".join(registros) + token + "</ListRecords></OAI-PMH>"
    ).encode()


def _respuesta(cuerpo: bytes, truncado: bool = False) -> Respuesta:
    return Respuesta(url_inicial="https://a.org/oai", url="https://a.org/oai", codigo=200, cuerpo=cuerpo, truncado=truncado)


def test_extrae_todos_los_registros_sin_bundles_derivados():
    cuerpo = _pagina(
        [
            _registro_mets("i1", [("ORIGINAL", 1000), ("THUMBNAIL", 50), ("TEXT", 70)]),
            _registro_mets("i2", [("ORIGINAL", 2000), ("ORIGINAL", 3000)]),
            _registro_mets("i3", [("LICENSE", 10)]),
            _registro_mets("i4", [], borrado=True),
        ],
        total=1234,
    )
    pagina = extraer_pagina_tamanos(_respuesta(cuerpo), "mets")
    assert pagina.total == 1234 and pagina.problema is None
    assert [(g.identificador, [a[0] for a in g.archivos], g.borrado) for g in pagina.registros] == [
        ("i1", [1000], False), ("i2", [2000, 3000], False), ("i3", [], False), ("i4", [], True),
    ]  # fmt: skip


def test_una_pagina_cortada_descarta_el_ultimo_registro():
    cuerpo = _pagina([_registro_mets("i1", [("ORIGINAL", 1000)]), _registro_mets("i2", [("ORIGINAL", 2000)])])
    pagina = extraer_pagina_tamanos(_respuesta(cuerpo[:-60], truncado=True), "mets")
    assert [g.identificador for g in pagina.registros] == ["i1"]


def test_sin_resumption_token_la_lista_entra_entera_y_no_records_es_un_problema():
    cuerpo = _pagina([_registro_mets("i1", [("ORIGINAL", 1000)])])
    assert extraer_pagina_tamanos(_respuesta(cuerpo), "mets").total == 1
    vacia = b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="noRecordsMatch"/></OAI-PMH>'
    assert extraer_pagina_tamanos(_respuesta(vacia), "mets").problema == "noRecordsMatch"


@pytest.mark.parametrize(
    "captura, tamano",
    [("rdu.unc.edu.ar/20261029T164359880-00087", 529207), ("ru.iiec.unam.mx/20261029T164359880-00537", 310625)],
)
def test_capturas_reales_de_dspace_y_eprints(captura, tamano):
    cuerpo = (CAPTURAS / f"{captura}.cuerpo").read_bytes()
    pagina = extraer_pagina_tamanos(_respuesta(cuerpo), "mets")
    assert len(pagina.registros) == 1 and tamano in [a[0] for a in pagina.registros[0].archivos]


def test_candidatos_con_mets_o_xoai_y_sin_hosts_abandonados(tmp_path):
    piloto = tmp_path / "piloto"
    piloto.mkdir()
    (piloto / "repositorios.csv").write_text(
        "repo_id,plataforma,oai_endpoint,oai_formatos\n"
        "r1,dspace,https://a.org/oai,oai_dc mets xoai\n"
        "r2,dspace,https://b.org/oai,oai_dc xoai\n"
        "r3,ojs,https://c.org/oai,oai_dc marcxml\n"
        "r4,dspace,https://d.org/oai,mets\n",
        encoding="utf-8",
    )
    (piloto / PEDIDOS).write_text('{"host": "d.org", "abandono": "anti-bot"}\n', encoding="utf-8")
    assert [(r.repo, r.formato) for r in candidatos([piloto])] == [("r1", "mets"), ("r2", "xoai")]


async def test_medir_respeta_la_cortesia_y_resume_ponderado(internet, salida):
    # a.org tiene 1.000 ítems con un archivo de 1 MB; b.org, 100 ítems con dos archivos de 10 MB.
    def oai(registros, total):
        def manejar(request: httpx.Request) -> httpx.Response:
            if "from" in request.url.params or "until" in request.url.params:
                cuerpo = b'<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="noRecordsMatch"/></OAI-PMH>'
            else:
                cuerpo = _pagina(registros, total)
            return httpx.Response(200, headers={"content-type": "text/xml"}, content=cuerpo)

        return manejar

    internet.agregar_host("a.org", oai([_registro_mets(f"a{i}", [("ORIGINAL", 1_000_000)]) for i in range(4)], 1000))
    internet.agregar_host("b.org", oai([_registro_mets(f"b{i}", [("ORIGINAL", 10_000_000)] * 2) for i in range(4)], 100))
    repos = [Repositorio("ra", "dspace", "https://a.org/oai", "mets"), Repositorio("rb", "dspace", "https://b.org/oai", "mets")]
    import relevamiento.tamanos as modulo

    original = modulo.Cortesia
    modulo.Cortesia = lambda cfg, registro, contacto: original(  # type: ignore[assignment]
        cfg, registro, contacto, transporte=internet.transporte, transporte_sin_verificar=internet.transporte_sin_verificar
    )
    try:
        await medir_tamanos(repos, salida, "prueba@example.org", CORTESIA_TAMANOS)
    finally:
        modulo.Cortesia = original
    assert internet.max_por_host == 1
    oai_pedidos = [p for p in leer_jsonl(salida / PEDIDOS) if p["metodo"] == "oai_mets"]
    assert len(oai_pedidos) == 2 * (1 + len(VENTANAS_TAMANOS))
    assert verificar(salida, CORTESIA_TAMANOS).ok
    resumen = resumir_tamanos(salida)
    total = resumen["ponderado_por_items"]
    # Por ítem: (1000 × 1 + 100 × 2) / 1100 archivos; por archivo: 1000 de 1 MB y 200 de 10 MB.
    assert total["archivos_por_item"] == pytest.approx(1200 / 1100, rel=1e-3)
    assert total["tamano_media"] == pytest.approx((1000 * 1e6 + 200 * 1e7) / 1200, rel=1e-3)
    assert resumen["sin_ponderar"]["archivos_por_item"] == pytest.approx(1.5)
    assert resumen["por_repositorio"]["rb"]["items_repositorio"] == 100
    assert (salida / "tamanos.csv").exists()


def test_urls_una_completa_y_una_por_ventana():
    lista = urls(Repositorio("r", "dspace", "https://a.org/oai", "mets"))
    assert lista[0] == ("completa", "https://a.org/oai?verb=ListRecords&metadataPrefix=mets")
    assert len(lista) == 1 + len(VENTANAS_TAMANOS) and all("from=" in u or "until=" in u for _, u in lista[1:])


def test_un_repositorio_cuyo_robots_no_permite_queda_afuera(tmp_path):
    from relevamiento.registro import Registro

    registro = Registro(tmp_path, "c1")
    pagina = [["i1", False, [[1000, "application/pdf"]]]]
    registro.evento("pagina_tamanos", repo="ra", ventana="completa", total=10, registros=pagina)
    registro.evento("pagina_tamanos", repo="rb", ventana="completa", total=10, registros=pagina)
    registro.evento("no_medido", repo="rb", url="https://b.org/oai", motivo="robots")
    registro.cerrar()
    resumen = resumir_tamanos(tmp_path)
    assert list(resumen["por_repositorio"]) == ["ra"] and resumen["excluidos_por_robots"] == ["rb"]
