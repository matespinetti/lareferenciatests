from concurrencia.planificador import Planificador, SeguimientoItems, Tarea, Tipo


def test_un_pedido_a_la_vez_por_repo():
    p = Planificador([5], intervalo=1.0, inicio=0.0)
    tarea, listo_en = p.siguiente(0.0)
    assert (tarea.tipo, tarea.item, listo_en) == (Tipo.RESOLUCION, 0, 0.0)
    assert p.siguiente(100.0) is None
    assert p.en_curso == 1


def test_intervalo_contado_desde_el_fin():
    p = Planificador([5], intervalo=1.0, inicio=0.0)
    tarea, _ = p.siguiente(0.0)
    p.terminada(tarea, fin=3.0)
    assert p.siguiente(3.999) is None
    assert p.proximo_listo() == 4.0
    siguiente, listo_en = p.siguiente(4.0)
    assert siguiente.item == 1
    assert listo_en == 4.0


def test_repos_independientes():
    p = Planificador([2, 2, 2], intervalo=1.0, inicio=0.0)
    repos = {p.siguiente(0.0)[0].repo for _ in range(3)}
    assert repos == {0, 1, 2}
    assert p.siguiente(0.0) is None


def test_descargas_antes_que_nuevas_resoluciones():
    p = Planificador([3], intervalo=1.0, inicio=0.0)
    tarea, _ = p.siguiente(0.0)
    p.agregar(Tarea(Tipo.DESCARGA, 0, tarea.item, 0))
    p.agregar(Tarea(Tipo.DESCARGA, 0, tarea.item, 1))
    p.terminada(tarea, 0.5)
    tipos = []
    for t in (1.5, 3.0, 4.5):
        siguiente, _ = p.siguiente(t)
        tipos.append(siguiente.tipo)
        p.terminada(siguiente, t)
    assert tipos == [Tipo.DESCARGA, Tipo.DESCARGA, Tipo.RESOLUCION]


def test_pausa_por_retry_after():
    p = Planificador([3], intervalo=1.0, inicio=0.0)
    tarea, _ = p.siguiente(0.0)
    p.pausar(0, hasta=30.0)
    p.agregar(tarea)
    p.terminada(tarea, 0.5)
    assert p.siguiente(29.9) is None
    reencolada, _ = p.siguiente(30.0)
    assert reencolada.item == tarea.item


def test_termina_cuando_no_queda_trabajo():
    p = Planificador([1, 0], intervalo=1.0, inicio=0.0)
    tarea, _ = p.siguiente(0.0)
    assert not p.terminado()
    p.terminada(tarea, 0.1)
    assert p.terminado()


def test_listos_sin_atender():
    p = Planificador([1, 1, 1], intervalo=1.0, inicio=0.0)
    assert p.listos_sin_atender(0.0) == 3
    p.siguiente(0.0)
    assert p.listos_sin_atender(0.0) == 2


def test_primero_los_repos_con_trabajo_empezado():
    # Dos repos listos a la vez: gana el que tiene descargas pendientes aunque haya esperado menos.
    p = Planificador([5, 5], intervalo=1.0, inicio=0.0)
    tarea, _ = p.siguiente(0.0)
    p.agregar(Tarea(Tipo.DESCARGA, tarea.repo, tarea.item, 0))
    p.terminada(tarea, 0.0)
    otro = 1 - tarea.repo
    siguiente, _ = p.siguiente(5.0)
    assert (siguiente.repo, siguiente.tipo) == (tarea.repo, Tipo.DESCARGA)
    siguiente, _ = p.siguiente(5.0)
    assert siguiente.repo == otro


def test_item_completo_con_todas_sus_descargas():
    items = SeguimientoItems()
    assert items.resuelto(1, 1, 0)
    assert not items.resuelto(1, 2, 1)
    assert items.descargado(1, 2)
    assert not items.resuelto(1, 3, 3)
    assert not items.descargado(1, 3)
    assert not items.descargado(1, 3)
    assert items.descargado(1, 3)


def test_item_con_una_descarga_fallida_no_se_completa():
    items = SeguimientoItems()
    items.resuelto(2, 1, 2)
    assert not items.descargado(2, 1)
    # La segunda descarga falló: nunca se llama a descargado y el ítem no cuenta.
    assert not items.descargado(9, 9)
