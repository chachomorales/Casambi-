"""
Presencia y luz que miden los sensores (p. ej. los STARCO PIR + fotosensor).

Llegan en `controls` del estado, sin hora propia. Lo que se comprueba: que se
leen, que un sensor offline no enseña su último valor como si fuera actual, y
que las redes sin ellos no ganan columnas vacías, ni en la pestaña ni en el
Excel.
"""

from __future__ import annotations

import io

import pytest
from openpyxl import load_workbook

import app as app_module
import report
from conftest import RED_ID, STATE


def _vivo(*controles, online=True) -> dict:
    return {"online": online, "controls": [list(controles)]}


PRESENTE = {"name": "presence", "type": "Presence", "status": "present"}
AUSENTE = {"name": "presence", "type": "Presence", "status": "absent"}
LUX = {"name": "lux", "type": "Lux", "value": 37.4}


def test_lee_presencia_y_luz():
    assert report.lectura_sensor(_vivo(PRESENTE, LUX)) == {"presencia": True, "lux": 37}
    assert report.lectura_sensor(_vivo(AUSENTE)) == {"presencia": False, "lux": None}


def test_sensor_offline_no_da_lectura():
    """Conserva el último valor; enseñarlo lo haría pasar por actual."""
    assert report.lectura_sensor(_vivo(AUSENTE, LUX, online=False)) is None


def test_sin_controles_de_sensor_no_hay_lectura():
    assert report.lectura_sensor(_vivo({"type": "Dimmer", "value": 1})) is None
    assert report.lectura_sensor({"online": True, "controls": [[]]}) is None
    assert report.lectura_sensor({}) is None


@pytest.fixture
def con_lectura(app_cargada):
    """El sensor 2 detecta presencia y mide 37 lx."""
    estado = {**STATE, "units": [dict(u) for u in STATE["units"]]}
    estado["units"][1]["controls"] = [[PRESENTE, LUX]]
    app_module._state["cache"][RED_ID]["state"] = estado


def test_pestana_sensores_muestra_la_lectura(cliente, con_lectura):
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    assert "<th>Presencia</th>" in html and "<th>Luz</th>" in html
    assert ">Presente</td>" in html
    assert ">37 lx</td>" in html
    assert "Lectura al descargar la red" in html


def test_red_sin_lecturas_no_gana_columnas(cliente):
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    assert "<th>Presencia</th>" not in html
    assert "<th>Luz</th>" not in html


def test_excel_lleva_la_lectura(cliente, con_lectura):
    wb = load_workbook(io.BytesIO(cliente.get(f"/network/{RED_ID}/excel").data))
    ws = wb["Sensores"]
    cabecera = [c.value for c in ws[1]]
    assert cabecera[-2:] == ["Presencia", "Luz (lx)"]
    fila = [c.value for c in ws[2]]
    assert fila[-2:] == ["Presente", 37]
    notas = [c.value for c in ws["A"] if isinstance(c.value, str)]
    assert any(n.startswith("Presencia y luz: lectura al descargar la red") for n in notas)
    assert ws.column_dimensions["A"].width < 20          # la nota no ensancha el ID


def test_excel_sin_lecturas_no_gana_columnas(cliente):
    wb = load_workbook(io.BytesIO(cliente.get(f"/network/{RED_ID}/excel").data))
    cabecera = [c.value for c in wb["Sensores"][1]]
    assert "Presencia" not in cabecera and "Luz (lx)" not in cabecera


# ── Sensores que podrían medir pero no mandan nada ───────────────────────────

STARCO = {"type": "Sensor", "isLightSensor": True, "isPresenceSensor": True,
          "controls": [{"type": "presence"}, {"type": "lux"}, {"type": "placeholder"}]}


def test_capacidad_sale_del_perfil():
    assert report.capacidad_sensor(STARCO) == {"presencia": True, "lux": True}
    assert report.capacidad_sensor({"controls": [{"type": "presence"}]}) == \
        {"presencia": True, "lux": False}
    assert report.capacidad_sensor({"controls": {}}) == {"presencia": False, "lux": False}
    assert report.capacidad_sensor(None) == {"presencia": False, "lux": False}


def test_celdas_distinguen_sin_lectura_de_no_lo_mide():
    capaz = {"presencia": True, "lux": True}
    assert report.celdas_lectura({"presencia": False, "lux": 12}, capaz) == \
        {"presencia": "Ausente", "lux": 12}
    assert report.celdas_lectura(None, capaz) == \
        {"presencia": "Sin lectura", "lux": "Sin lectura"}
    assert report.celdas_lectura(None, {"presencia": True, "lux": False}) == \
        {"presencia": "Sin lectura", "lux": None}


@pytest.fixture
def capaz_sin_gateway(app_cargada):
    """El sensor 2 es un STARCO con fotosensor, pero la red no llega a la nube."""
    cache = app_module._state["cache"][RED_ID]
    cache["fixtures"] = {**cache["fixtures"], 101: {**cache["fixtures"][101], **STARCO}}
    cache["state"] = {**STATE, "gateway": {},
                      "units": [{"id": u["id"], "online": False} for u in STATE["units"]]}


def test_pestana_dice_sin_lectura(cliente, capaz_sin_gateway):
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    assert "<th>Presencia</th>" in html and "<th>Luz</th>" in html
    assert html.count(">Sin lectura</td>") == 2
    assert "falte un gateway en línea" in html


def test_excel_dice_sin_lectura(cliente, capaz_sin_gateway):
    wb = load_workbook(io.BytesIO(cliente.get(f"/network/{RED_ID}/excel").data))
    ws = wb["Sensores"]
    assert [c.value for c in ws[1]][-2:] == ["Presencia", "Luz (lx)"]
    assert [c.value for c in ws[2]][-2:] == ["Sin lectura", "Sin lectura"]
    notas = [c.value for c in ws["A"] if isinstance(c.value, str)]
    assert any("falte un gateway en línea" in n for n in notas)


# ── Luminarias con sensor integrado (McWong PSC-BL de los MM) ────────────────

@pytest.fixture
def luminaria_con_sensor(app_cargada):
    """La luminaria 1 lleva presencia y luz: ausente, 4 lx."""
    cache = app_module._state["cache"][RED_ID]
    cache["fixtures"] = {**cache["fixtures"], 100: {
        **cache["fixtures"][100], "isLightSensor": True, "isPresenceSensor": True,
        "controls": [{"type": "presence"}, {"type": "lux"}, {"type": "dimmer"}]}}
    estado = {**STATE, "units": [dict(u) for u in STATE["units"]]}
    estado["units"][0]["controls"] = [[AUSENTE, {"type": "Lux", "value": 4.0},
                                       {"type": "Dimmer", "value": 1.0}]]
    cache["state"] = estado


def test_pestana_luminarias_muestra_el_sensor_integrado(cliente, luminaria_con_sensor):
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    panel = html[html.index('id="panel-luminarias"'):html.index('id="panel-sensores"')]
    assert "<th>Presencia</th>" in panel and "<th>Luz</th>" in panel
    assert ">Ausente</td>" in panel and ">4 lx</td>" in panel


def test_excel_luminarias_lleva_el_sensor_integrado(cliente, luminaria_con_sensor):
    wb = load_workbook(io.BytesIO(cliente.get(f"/network/{RED_ID}/excel").data))
    ws = wb["Luminarias"]
    assert [c.value for c in ws[1]][-2:] == ["Presencia", "Luz (lx)"]
    assert [c.value for c in ws[2]][-2:] == ["Ausente", 4]
    assert ws.column_dimensions["A"].width < 20


# ── Un perfil con presencia no convierte un driver en sensor ─────────────────

def test_capacidad_solo_cuenta_en_sensores():
    assert report.capacidad_de_unidad({"type": "Sensor"}, STARCO) == \
        {"presencia": True, "lux": True}
    # CBU-A2D «DALI/BC/Sensors»: entrada de sensor que puede estar vacía
    assert report.capacidad_de_unidad({"type": "Luminaire"}, STARCO) == \
        {"presencia": False, "lux": False}


@pytest.fixture
def driver_capaz_offline(app_cargada):
    """La luminaria 1 tiene perfil con presencia y luz, pero no manda nada."""
    cache = app_module._state["cache"][RED_ID]
    cache["fixtures"] = {**cache["fixtures"], 100: {**cache["fixtures"][100], **STARCO}}
    cache["state"] = {**STATE, "units": [{"id": u["id"], "online": False}
                                         for u in STATE["units"]]}


def test_driver_sin_lectura_no_gana_columnas(cliente, driver_capaz_offline):
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    panel = html[html.index('id="panel-luminarias"'):html.index('id="panel-sensores"')]
    assert "<th>Presencia</th>" not in panel and "Sin lectura" not in panel
    wb = load_workbook(io.BytesIO(cliente.get(f"/network/{RED_ID}/excel").data))
    assert "Presencia" not in [c.value for c in wb["Luminarias"][1]]
