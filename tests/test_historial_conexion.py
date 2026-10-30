"""
Historial de conexión: de los registros `online` del data logging a cortes.

Los casos salen de las redes reales: el log arranca con un 0 que no es un corte,
las unidades caen con segundos de diferencia, y la nube recorta los milisegundos.
"""

from __future__ import annotations

from datetime import datetime

import pytest

import casambi_api
import report

from conftest import RED_ID

AHORA = datetime(2026, 10, 30, 18, 0)
RED = {"units": [
    {"id": 1, "name": "Luz 1", "type": "Luminaire"},
    {"id": 2, "name": "Luz 2", "type": "Luminaire"},
    {"id": 3, "name": "Luz 3", "type": "Luminaire"},
    {"id": 4, "name": "Luz 4", "type": "Luminaire"},
    {"id": 5, "name": "Luz 5", "type": "Luminaire"},
    {"id": 9, "name": "Pulsador", "type": "BatterySwitch"},
]}
TODAS_EN_LINEA = {"units": [{"id": i, "online": True} for i in (1, 2, 3, 4, 5)]}


def _punto(unidad, hora_utc, valor, milis=".5"):
    """Hora en UTC, como la manda la nube; Guatemala va 6 h por detrás."""
    return {"unit": unidad, "key": "online", "value": str(float(valor)),
            "time": f"2026-10-30T{hora_utc}{milis}+00:00"}


def _arranque(*unidades):
    """El 0-1 con el que empieza el log de cada unidad."""
    puntos = []
    for u in unidades:
        puntos += [_punto(u, "12:15:34", 0), _punto(u, "12:15:56", 1)]
    return puntos


def _historial(puntos, estado=TODAS_EN_LINEA):
    return report.historial_conexion(RED, estado, {"puntos": puntos, "truncado": False},
                                     ahora=AHORA)


def test_el_arranque_del_log_no_es_un_corte():
    h = _historial(_arranque(1, 2, 3, 4, 5))
    assert h["estado"] == "ok"
    assert h["cortes"] == []
    assert h["desde"] == datetime(2026, 10, 30, 6, 15, 34, 500000)


def test_caer_casi_a_la_vez_es_un_solo_corte_de_red():
    puntos = _arranque(1, 2, 3, 4, 5)
    for i, u in enumerate((1, 2, 3, 4, 5)):
        puntos += [_punto(u, f"14:57:1{i}", 0), _punto(u, "14:59:21", 1)]
    h = _historial(puntos)
    assert h["cortes_red"] == 1 and h["cortes_parciales"] == 0
    corte = h["cortes"][0]
    assert corte["afectadas"] == 5 and corte["total"] == 5
    assert corte["lectura"] == "Gateway o energía"
    assert corte["duracion"] == "2 min 11 s"


def test_una_unidad_sola_no_se_achaca_al_gateway():
    puntos = _arranque(1, 2, 3, 4, 5) + [_punto(3, "15:00:00", 0), _punto(3, "15:00:49", 1)]
    h = _historial(puntos)
    assert h["cortes_parciales"] == 1 and h["cortes_red"] == 0
    assert h["cortes"][0]["unidades"] == ["Luz 3"]
    assert h["cortes"][0]["lectura"] == "Esa unidad"


def test_los_pulsadores_a_pila_no_cuentan():
    puntos = _arranque(1) + [_punto(9, "15:00:00", 0), _punto(9, "15:10:00", 1),
                             _punto(9, "15:20:00", 0), _punto(9, "15:30:00", 1)]
    assert _historial(puntos)["cortes"] == []


def test_corte_que_sigue_abierto():
    puntos = _arranque(1) + [_punto(1, "20:00:00", 0)]
    estado = {"units": [{"id": 1, "online": False}]}
    corte = _historial(puntos, estado)["cortes"][0]
    assert corte["en_curso"] is True
    assert corte["duracion"] == "4 h 0 min"


def test_volvio_sin_que_el_log_lo_anotara():
    # Ya responde: decir «sigue sin conexión» sería falso, y sumar sus horas
    # inflaría el tiempo sin conexión.
    puntos = _arranque(1) + [_punto(1, "20:00:00", 0)]
    h = _historial(puntos)
    assert h["cortes"][0]["en_curso"] is False
    assert h["cortes"][0]["duracion"] == "Sin registro"
    assert h["sin_conexion"] == "0 s"


def test_milisegundos_recortados_por_la_nube():
    # `.47` rompe fromisoformat en Python 3.9, el del `.app`.
    assert report._hora_datapoint("2026-10-30T19:09:26.47+00:00") == \
        datetime(2026, 10, 30, 13, 9, 26, 470000)
    assert report._hora_datapoint("2026-10-30T19:09:26+00:00") == \
        datetime(2026, 10, 30, 13, 9, 26)
    assert report._hora_datapoint("ayer") is None


def test_la_franja_marca_lo_que_el_log_no_cubre():
    h = _historial(_arranque(1) + [_punto(1, "18:00:00", 0), _punto(1, "18:30:00", 1)])
    dia = h["dias"][0]
    assert dia["etiqueta"] == "Vie 30 oct"
    clases = [t["clase"] for t in dia["tramos"]]
    assert clases.count("nodata") == 2
    corte = next(t for t in dia["tramos"] if t["clase"] == "unidad")
    assert corte["izq"] == pytest.approx(50.0, abs=0.01)
    assert corte["ancho"] == pytest.approx(2.083, abs=0.001)


def test_sin_registros_y_sin_lectura_se_distinguen():
    assert _historial([])["estado"] == "vacio"
    assert report.historial_conexion(RED, TODAS_EN_LINEA, None)["estado"] == "error"


def test_la_pestana_explica_que_no_hay_historial(cliente):
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    assert "Historial de conexión" in html
    assert "Usage of devices" in html


def test_un_fallo_del_log_no_tumba_la_red(cliente, monkeypatch):
    import app as modulo
    def falla(*a, **k):
        raise casambi_api.CasambiAPIError("503")
    monkeypatch.setattr(modulo.CasambiClient, "get_datapoints", falla, raising=False)
    monkeypatch.setattr("conftest._ClienteFalso.get_datapoints", falla)
    r = cliente.get(f"/network/{RED_ID}?refresh=1", follow_redirects=True)
    assert r.status_code == 200


def test_si_no_cabe_de_una_vez_se_pide_por_semanas(monkeypatch):
    llamadas = []
    def responde(self, path, network_id=None, params=None):
        llamadas.append(params)
        if len(llamadas) == 1:
            return {"hasMoreResults": True, "dataPoints": [{"a": 0}]}
        return {"hasMoreResults": False, "dataPoints": [{"a": len(llamadas)}]}
    monkeypatch.setattr(casambi_api.CasambiClient, "_get", responde)
    r = casambi_api.CasambiClient("k").get_datapoints("red", keys="online")
    assert len(llamadas) == 1 + 9        # 60 días en tramos de 7
    assert len(r["puntos"]) == 9
    assert r["truncado"] is False
    assert all(p["keys"] == "online" for p in llamadas)
