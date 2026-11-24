"""
Portada: el resumen de todas las redes y la lista de lo que pide atención.

Los umbrales salen del barrido de las 46 redes reales (2026-11-24): la mayoría
no se ven desde la nube, y avisar de cada una taparía los problemas atendibles.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import app as app_module
import report

from conftest import RED_ID

AHORA = datetime(2026, 11, 24, 12, 0)
RED = {"units": [{"id": i, "name": f"Luz {i}", "type": "Luminaire"} for i in (1, 2, 3)],
       "gateway": {"name": "CGW-1"}}
EN_LINEA = {"units": [{"id": i, "online": True} for i in (1, 2, 3)]}


def _punto(unidad, momento: datetime, valor):
    """`momento` en hora de Guatemala; la nube lo manda en UTC (+6 h)."""
    utc = momento + timedelta(hours=6)
    return {"unit": unidad, "key": "online", "value": str(float(valor)),
            "time": utc.strftime("%Y-%m-%dT%H:%M:%S.5+00:00")}


def _corte_de_red(inicio: datetime, segundos: int) -> list:
    puntos = []
    for u in (1, 2, 3):
        puntos += [_punto(u, inicio, 0),
                   _punto(u, inicio + timedelta(seconds=segundos), 1)]
    return puntos


def _registro_con_cortes(*inicios) -> dict:
    arranque = AHORA - timedelta(days=20)
    puntos = []
    for u in (1, 2, 3):
        puntos += [_punto(u, arranque, 0), _punto(u, arranque + timedelta(seconds=20), 1)]
    for inicio in inicios:
        puntos += _corte_de_red(inicio, 60)
    puntos.sort(key=lambda p: p["time"])
    return {"puntos": puntos, "truncado": False}


def _fila(**cambios) -> dict:
    fila = {"id": "r", "nombre": "Red", "sitio": "",
            **report.resumen_red(RED, EN_LINEA, None, ahora=AHORA)}
    fila.update(cambios)
    return fila


# ── resumen_red ───────────────────────────────────────────────────────────────

def test_solo_cuentan_los_cortes_de_la_ultima_semana():
    registro = _registro_con_cortes(AHORA - timedelta(days=10),
                                    AHORA - timedelta(days=2),
                                    AHORA - timedelta(hours=3))
    r = report.resumen_red(RED, EN_LINEA, registro, ahora=AHORA)
    assert r["historial"] == "ok"
    assert r["cortes_red"] == 2
    assert r["red_caida"] == "2 min"


def test_sin_registro_no_hay_cortes_sino_historial_ausente():
    r = report.resumen_red(RED, EN_LINEA, None, ahora=AHORA)
    assert r["historial"] == "error"
    assert r["cortes_red"] == 0 and r["caidas_parciales"] == 0


def test_un_cgw_es_gateway_fijo_y_un_movil_no():
    assert report.resumen_red(RED, EN_LINEA, None, ahora=AHORA)["gateway_fijo"]
    movil = dict(RED, gateway={"name": "Galaxy Tab A with S Pen"})
    assert not report.resumen_red(movil, EN_LINEA, None, ahora=AHORA)["gateway_fijo"]


# ── avisos_portada ────────────────────────────────────────────────────────────

def test_equipos_sin_senal_en_una_red_que_responde():
    avisos = report.avisos_portada([_fila(responden=36, no_responden=15)])
    assert avisos[0]["texto"] == "15 de 51 equipos sin señal"
    assert avisos[0]["grave"]


def test_pocos_equipos_sin_senal_es_aviso_no_fallo():
    avisos = report.avisos_portada([_fila(responden=75, no_responden=4)])
    assert not avisos[0]["grave"]


def test_un_corte_suelto_no_avisa_y_varios_si():
    assert report.avisos_portada([_fila(cortes_red=1, historial="ok")]) == []
    aviso = report.avisos_portada([_fila(cortes_red=17, red_caida="3 h 54 min",
                                         historial="ok")])[0]
    assert aviso["texto"] == "17 cortes de toda la red en 7 días, 3 h 54 min sin conexión"
    assert aviso["detalle"] == "Gateway o energía"
    assert aviso["grave"]


def test_muchas_caidas_parciales_avisan():
    aviso = report.avisos_portada([_fila(caidas_parciales=652, historial="ok")])[0]
    assert "652 caídas parciales" in aviso["texto"]
    assert aviso["detalle"] == "Equipos que entran y salen de la malla"


def test_solo_avisa_del_gateway_caido_si_es_fijo():
    """Un móvil apagado como gateway es lo normal; un CGW-1 sin responder, no."""
    movil = _fila(nivel="gateway-offline", gateway="iPad", gateway_fijo=False)
    cgw = _fila(nivel="gateway-offline", gateway="CGW-1", gateway_fijo=True)
    sin = _fila(nivel="sin-gateway", gateway="", gateway_fijo=False)
    avisos = report.avisos_portada([movil, cgw, sin])
    assert [a["texto"] for a in avisos] == ["el gateway CGW-1 no responde"]


def test_lo_grave_va_primero_y_una_red_sale_una_sola_vez():
    leve = _fila(id="a", nombre="Leve", responden=75, no_responden=4)
    grave = _fila(id="b", nombre="Grave", responden=36, no_responden=15,
                  con_aviso=6)
    avisos = report.avisos_portada([leve, grave])
    assert [a["nombre"] for a in avisos] == ["Grave", "Leve"]
    assert avisos[0]["texto"] == "15 de 51 equipos sin señal · 6 con aviso de estado"


def test_una_red_que_no_se_pudo_leer_no_es_un_aviso():
    assert report.avisos_portada([_fila(nivel="error", con_aviso=3)]) == []


# ── La carga y la página ──────────────────────────────────────────────────────

def test_la_portada_muestra_cifras_y_la_tabla(cliente):
    html = cliente.get("/").get_data(as_text=True)
    assert "Visibles desde la nube" in html
    assert 'id="tbl-redes"' in html
    assert f'/network/{RED_ID}' in html
    assert "Red de prueba" in html


def test_cargar_el_resumen_lee_cada_red(app_cargada):
    app_module._state["resumen"] = None
    app_module._cargar_resumen()
    fila = app_module._state["resumen"]["redes"][0]
    assert fila["id"] == RED_ID and fila["nivel"] == "online"
    assert fila["historial"] == "vacio"


def test_una_red_que_falla_no_tumba_el_resumen(app_cargada, monkeypatch):
    def falla(network_id):
        raise app_module.CasambiAPIError("GET → 503")
    monkeypatch.setattr(app_cargada.cliente_api_falso, "get_network", falla)
    app_module._cargar_resumen()
    fila = app_module._state["resumen"]["redes"][0]
    assert fila["nivel"] == "error" and "503" in fila["error"]


def test_un_resumen_caducado_vuelve_a_la_pantalla_de_carga(app_cargada):
    app_module._state["resumen"]["momento"] -= app_module.VIDA_RESUMEN + 1
    # Una tarea ya en marcha: así no se lanza un hilo de verdad.
    app_module._tasks["tarea-resumen"] = {
        "active": True, "label": "…", "done": 0, "total": 0,
        "error": None, "clave": "resumen", "fin": None,
    }
    app_module._task_por_clave["resumen"] = "tarea-resumen"
    html = app_cargada.test_client().get("/").get_data(as_text=True)
    assert "task=tarea-resumen" in html


def test_actualizar_olvida_el_resumen(cliente):
    r = cliente.post("/resumen/refresh")
    assert r.status_code == 302
    assert app_module._state["resumen"] is None


def test_abrir_una_red_pone_al_dia_su_fila(app_cargada):
    app_module._state["resumen"]["redes"][0]["nivel"] = "gateway-offline"
    app_module._fetch_network_data(RED_ID)
    assert app_module._state["resumen"]["redes"][0]["nivel"] == "online"
