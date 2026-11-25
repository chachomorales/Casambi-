"""
Registro propio de temperatura y medidas eléctricas.

Lo importante es lo que NO se guarda: la nube repite la última lectura en cada
consulta, y guardarla cada vez dibujaría datos que nunca llegaron.
"""

from __future__ import annotations

import app as app_module
import config
import registro

from conftest import RED_ID

T0 = 1_764_000_000_000  # ms


def _estado(*lecturas):
    """lecturas: (unidad, nombre, valor, timestamp)."""
    unidades = {}
    for unidad, nombre, valor, momento in lecturas:
        unidades.setdefault(unidad, {"id": unidad, "online": True, "sensors": []})
        unidades[unidad]["sensors"].append(
            {"name": nombre, "value": valor, "timestamp": momento})
    return {"units": list(unidades.values())}


def test_extrae_temperatura_y_medidas_electricas():
    estado = _estado((1, "Temperature", 41.0, T0), (2, "Power", 12, T0),
                     (2, "Energy", 340, T0))
    assert sorted(registro.extraer(estado)) == [
        (1, "Temperature", T0, 41.0), (2, "Energy", T0, 340.0), (2, "Power", T0, 12.0)]


def test_ignora_lo_que_no_se_puede_graficar():
    estado = _estado((1, "Temperature", None, T0), (1, "Voltage", 230, None),
                     (1, "Humedad", 50, T0), (1, "Power", True, T0))
    assert registro.extraer(estado) == []
    assert registro.extraer({"units": None}) == []


def test_una_lectura_repetida_no_se_guarda_dos_veces(tmp_path):
    conn = registro.abrir(tmp_path / "r.db")
    filas = registro.extraer(_estado((1, "Temperature", 41.0, T0)))
    assert registro.guardar(conn, "red", filas) == 1
    assert registro.guardar(conn, "red", filas) == 0
    nuevas = registro.extraer(_estado((1, "Temperature", 43.0, T0 + 900_000)))
    assert registro.guardar(conn, "red", nuevas) == 1
    assert registro.contar(conn)["lecturas"] == 2


def test_purgar_quita_solo_lo_viejo(tmp_path):
    conn = registro.abrir(tmp_path / "r.db")
    ahora = T0 / 1000
    viejo = int((ahora - 401 * 86400) * 1000)
    registro.guardar(conn, "red", [(1, "Temperature", viejo, 30.0),
                                   (1, "Temperature", T0, 40.0)])
    assert registro.purgar(conn, 400, ahora=ahora) == 1
    assert registro.contar(conn)["lecturas"] == 1


def test_en_escritorio_no_arranca_por_defecto():
    assert config.ES_ESCRITORIO and not config.REGISTRO_ACTIVO
    assert app_module.arrancar_registro() is False


def _preparar(app_cargada, monkeypatch, tmp_path, estado):
    monkeypatch.setattr(app_module, "REGISTRO_DB", tmp_path / "registro.db")
    monkeypatch.setitem(app_module._registro_estado, "redes", None)
    monkeypatch.setitem(app_module._registro_estado, "buscado", 0.0)
    consultas = []

    def get_network_state(network_id):
        consultas.append(network_id)
        return estado
    monkeypatch.setattr(app_cargada.cliente_api_falso, "get_network_state",
                        get_network_state)
    return consultas


def test_un_ciclo_guarda_y_recuerda_las_redes_con_lecturas(app_cargada, monkeypatch,
                                                           tmp_path):
    consultas = _preparar(app_cargada, monkeypatch, tmp_path,
                          _estado((1, "Temperature", 41.0, T0)))
    cifras = app_module._ciclo_registro(ahora=10_000.0)
    assert cifras == {"consultadas": 1, "errores": 0, "nuevas": 1, "con_lecturas": 1}
    assert app_module._registro_estado["redes"] == {RED_ID}
    # La siguiente pasada repite la lectura: se consulta, pero no se guarda.
    assert app_module._ciclo_registro(ahora=10_900.0)["nuevas"] == 0
    assert consultas == [RED_ID, RED_ID]


def test_una_red_sin_lecturas_deja_de_consultarse_hasta_rebuscar(app_cargada,
                                                                 monkeypatch, tmp_path):
    consultas = _preparar(app_cargada, monkeypatch, tmp_path, {"units": []})
    app_module._ciclo_registro(ahora=10_000.0)
    assert app_module._ciclo_registro(ahora=10_900.0)["consultadas"] == 0
    rebusca = 10_000.0 + app_module.REGISTRO_REBUSCAR + 1
    assert app_module._ciclo_registro(ahora=rebusca)["consultadas"] == 1
    assert len(consultas) == 2


def test_si_falla_todo_se_rehace_la_sesion(app_cargada, monkeypatch, tmp_path):
    _preparar(app_cargada, monkeypatch, tmp_path, {})

    def falla(network_id):
        raise app_module.CasambiAPIError("GET → 401")
    monkeypatch.setattr(app_cargada.cliente_api_falso, "get_network_state", falla)
    assert app_module._ciclo_registro(ahora=10_000.0)["errores"] == 1
    assert app_module._state["networks"] is None


# ── Gráfica de temperatura ────────────────────────────────────────────────────

H = 3_600_000


def test_la_serie_promedia_por_tramos_y_las_cifras_no(tmp_path):
    conn = registro.abrir(tmp_path / "r.db")
    base = (T0 // H) * H
    registro.guardar(conn, "red", [(1, "Temperature", base, 30.0),
                                   (1, "Temperature", base + 900_000, 50.0),
                                   (1, "Temperature", base + H, 40.0)])
    s = registro.serie(conn, "red", "Temperature", base, H)[1]
    assert s["puntos"] == [[base + H // 2, 40.0], [base + H + H // 2, 40.0]]
    assert (s["min"], s["max"], s["media"]) == (30.0, 50.0, 40.0)
    assert (s["ultima"], s["ultima_momento"]) == (40.0, base + H)


def test_la_serie_no_mezcla_redes_ni_magnitudes(tmp_path):
    conn = registro.abrir(tmp_path / "r.db")
    registro.guardar(conn, "a", [(1, "Temperature", T0, 30.0), (1, "Power", T0, 9.0)])
    registro.guardar(conn, "b", [(1, "Temperature", T0, 70.0)])
    assert registro.serie(conn, "a", "Temperature", 0, H)[1]["max"] == 30.0
    assert registro.primera_lectura(conn, "c", "Temperature") is None


def test_el_tramo_no_baja_de_15_min_ni_pasa_de_200_puntos():
    assert app_module._tramo(0, 86_400_000) == 15 * 60_000
    tramo = app_module._tramo(0, 180 * 86_400_000)
    assert tramo % (15 * 60_000) == 0
    assert 180 * 86_400_000 / tramo <= app_module.PUNTOS_GRAFICA


def _con_temperaturas(monkeypatch, tmp_path, ahora_ms):
    monkeypatch.setattr(app_module, "REGISTRO_DB", tmp_path / "registro.db")
    conn = registro.abrir(tmp_path / "registro.db")
    registro.guardar(conn, RED_ID, [(1, "Temperature", ahora_ms - 2 * H, 41.0),
                                    (1, "Temperature", ahora_ms - H, 45.0),
                                    (4, "Temperature", ahora_ms - H, 33.0)])
    conn.close()


def test_sin_registro_no_hay_pestana(cliente, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "REGISTRO_DB", tmp_path / "no-existe.db")
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    assert 'data-tab="temperatura"' not in html


def test_con_lecturas_sale_la_pestana_y_su_json(cliente, monkeypatch, tmp_path):
    import time
    ahora = int(time.time() * 1000)
    _con_temperaturas(monkeypatch, tmp_path, ahora)
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    assert 'data-tab="temperatura"' in html
    d = cliente.get(f"/network/{RED_ID}/serie/temperatura?rango=24h").get_json()
    assert [u["nombre"] for u in d["unidades"]] == ["Luminaria pasillo", "Pasarela"]
    assert d["unidades"][0]["max"] == 45.0
    assert d["desfase_min"] == -360


def test_un_rango_inventado_es_un_400(cliente):
    assert cliente.get(f"/network/{RED_ID}/serie/temperatura?rango=1y").status_code == 400


def test_sin_lecturas_de_potencia_no_hay_pestana_de_potencia(cliente, monkeypatch,
                                                             tmp_path):
    import time
    _con_temperaturas(monkeypatch, tmp_path, int(time.time() * 1000))
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    assert 'data-tab="temperatura"' in html
    assert 'data-tab="potencia"' not in html


def test_con_lecturas_de_potencia_sale_su_pestana_y_su_serie(cliente, monkeypatch,
                                                             tmp_path):
    import time
    ahora = int(time.time() * 1000)
    monkeypatch.setattr(app_module, "REGISTRO_DB", tmp_path / "registro.db")
    conn = registro.abrir(tmp_path / "registro.db")
    registro.guardar(conn, RED_ID, [(1, "Power", ahora - H, 120.0),
                                    (1, "Power", ahora - H // 2, 180.0),
                                    (1, "Voltage", ahora - H, 230.0)])
    conn.close()
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    assert 'data-tab="potencia"' in html and 'data-tab="temperatura"' not in html
    assert 'data-unidad="W"' in html
    d = cliente.get(f"/network/{RED_ID}/serie/potencia?rango=24h").get_json()
    u = d["unidades"][0]
    assert (u["min"], u["max"], u["ultima"]) == (120.0, 180.0, 180.0)


def test_una_medida_sin_grafica_es_un_404(cliente):
    assert cliente.get(f"/network/{RED_ID}/serie/tension").status_code == 404


# ── Luz: lectura viva, sin hora propia, y el historial de la nube ─────────────

def _con_lux(online=True, lux=320.0):
    return {"units": [{"id": 7, "online": online,
                       "controls": [[{"type": "Presence", "status": "absent"},
                                     {"type": "Lux", "value": lux}]]}]}


def test_los_lux_van_con_la_hora_de_la_consulta_redondeada_al_minuto():
    consulta = T0 + 42_123
    filas = registro.extraer(_con_lux(), consulta=consulta)
    assert filas == [(7, "Lux", consulta // 60_000 * 60_000, 320.0)]


def test_un_sensor_desconectado_no_da_lux():
    """Conserva el último valor, y guardarlo lo haría pasar por actual."""
    assert registro.extraer(_con_lux(online=False), consulta=T0) == []


def test_los_controles_planos_tambien_valen():
    estado = {"units": [{"id": 7, "online": True,
                         "controls": [{"type": "Lux", "value": 12}]}]}
    assert registro.extraer(estado, consulta=T0)[0][3] == 12.0


def test_el_historial_de_la_nube_se_guarda_en_medias_de_5_min():
    puntos = [
        {"unit": 33, "key": "lux", "value": "10.0", "time": "2026-11-24T12:00:01.5+00:00"},
        {"unit": 33, "key": "lux", "value": "30.0", "time": "2026-11-24T12:04:59.25+00:00"},
        {"unit": 33, "key": "lux", "value": "50.0", "time": "2026-11-24T12:05:00+00:00"},
        {"unit": 33, "key": "lux", "value": "malo", "time": "2026-11-24T12:06:00+00:00"},
    ]
    filas = registro.promediar_datapoints(puntos, "Lux")
    base = registro.momento_datapoint("2026-11-24T12:00:00+00:00")
    assert filas == [(33, "Lux", base + 150_000, 20.0), (33, "Lux", base + 450_000, 50.0)]


def _cliente_con_historial(app_cargada, monkeypatch, respuestas):
    pedidas = []

    def tramo(network_id, keys, desde, hasta):
        pedidas.append((desde, hasta, keys))
        return respuestas(desde, hasta)
    monkeypatch.setattr(app_cargada.cliente_api_falso, "get_datapoints_tramo",
                        tramo, raising=False)
    monkeypatch.setattr(app_module.time, "sleep", lambda s: None)
    return pedidas


def test_una_red_sin_lux_en_la_nube_se_resuelve_con_una_consulta(app_cargada,
                                                                 monkeypatch, tmp_path):
    pedidas = _cliente_con_historial(app_cargada, monkeypatch,
                                     lambda d, h: {"puntos": [], "hay_mas": False})
    conn = registro.abrir(tmp_path / "r.db")
    assert app_module._importar_historial(RED_ID, "Lux", conn) == 0
    assert len(pedidas) == 1 and registro.importado(conn, RED_ID, "Lux")


def test_un_historial_grande_se_trae_por_tramos(app_cargada, monkeypatch, tmp_path):
    def respuestas(desde, hasta):
        if hasta - desde > app_module.TRAMO_HISTORIAL:
            return {"puntos": [], "hay_mas": True}       # el tanteo no cabe
        momento = (desde + (hasta - desde) / 2).strftime("%Y-%m-%dT%H:%M:%S.5+00:00")
        return {"puntos": [{"unit": 33, "value": "40", "time": momento}], "hay_mas": False}
    pedidas = _cliente_con_historial(app_cargada, monkeypatch, respuestas)
    conn = registro.abrir(tmp_path / "r.db")
    guardadas = app_module._importar_historial(RED_ID, "Lux", conn)
    assert guardadas == len(pedidas) - 1 == 120     # 60 días en tramos de 12 h


def test_el_ciclo_importa_una_sola_vez(app_cargada, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "REGISTRO_DB", tmp_path / "registro.db")
    monkeypatch.setitem(app_module._registro_estado, "redes", None)
    monkeypatch.setattr(app_cargada.cliente_api_falso, "get_network_state",
                        lambda red: _con_lux())
    pedidas = _cliente_con_historial(app_cargada, monkeypatch,
                                     lambda d, h: {"puntos": [], "hay_mas": False})
    app_module._ciclo_registro(ahora=10_000.0)
    app_module._ciclo_registro(ahora=10_900.0)
    # Una consulta de lux (el historial) y una de presencia (la ocupación del
    # día), y ninguna más en la segunda pasada.
    assert sorted(k for _, _, k in pedidas) == ["lux", "presence"]


def test_la_luz_tiene_su_pestana(cliente, monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "REGISTRO_DB", tmp_path / "registro.db")
    conn = registro.abrir(tmp_path / "registro.db")
    registro.guardar(conn, RED_ID, registro.extraer(_con_lux()))
    conn.close()
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    assert 'data-tab="luz"' in html and 'data-unidad="lx"' in html


# ── Ocupación: tramos de presencia de la nube ─────────────────────────────────

def _tramo(unidad, hora_utc, valor):
    return {"unit": unidad, "key": "presence", "value": str(valor),
            "time": f"2026-11-23T{hora_utc}.5+00:00"}


def test_un_tramo_que_cruza_la_hora_se_reparte():
    horas = registro.sumar_ocupacion([_tramo(33, "13:59:00", 120.0)])
    base = registro.momento_datapoint("2026-11-23T13:00:00+00:00")
    assert horas == {(33, base): [59.5, 59.5],
                     (33, base + registro.HORA): [60.5, 60.5]}


def test_el_signo_dice_si_hubo_alguien():
    horas = registro.sumar_ocupacion([_tramo(33, "14:00:00", 600.0),
                                      _tramo(33, "14:10:00", -1800.0)])
    (ocupado, observado), = horas.values()
    assert (ocupado, observado) == (600.0, 2400.0)


def test_sumar_por_trozos_da_lo_mismo_que_de_una_vez():
    tramos = [_tramo(33, "14:00:00", 600.0), _tramo(33, "14:10:00", -1800.0)]
    acumulado = registro.sumar_ocupacion(tramos[:1])
    registro.sumar_ocupacion(tramos[1:], acumulado)
    assert acumulado == registro.sumar_ocupacion(tramos)


def test_reimportar_sustituye_las_horas_y_no_las_suma(tmp_path):
    conn = registro.abrir(tmp_path / "r.db")
    horas = registro.sumar_ocupacion([_tramo(33, "14:00:00", 600.0)])
    registro.guardar_ocupacion(conn, "red", horas, 0)
    registro.guardar_ocupacion(conn, "red", horas, 0)
    assert registro.ocupacion(conn, "red", 0)[0][2:] == (600.0, 600.0)


def test_la_ocupacion_se_agrupa_por_dia_y_hora_de_guatemala(cliente, monkeypatch,
                                                            tmp_path):
    import time
    monkeypatch.setattr(app_module, "REGISTRO_DB", tmp_path / "registro.db")
    conn = registro.abrir(tmp_path / "registro.db")
    # Un lunes a las 20:00 UTC son las 14:00 de Guatemala, también lunes.
    ahora = int(time.time() * 1000)
    lunes = ahora - (ahora % (7 * 86_400_000)) - 3 * 86_400_000 + 20 * registro.HORA
    while lunes > ahora:
        lunes -= 7 * 86_400_000
    from datetime import datetime, timezone
    assert datetime.fromtimestamp(lunes / 1000, timezone.utc).weekday() == 0
    registro.guardar_ocupacion(conn, RED_ID, {(1, lunes): [900.0, 3600.0],
                                              (4, lunes): [0.0, 120.0]}, 0)
    conn.close()
    html = cliente.get(f"/network/{RED_ID}").get_data(as_text=True)
    assert 'data-tab="ocupacion"' in html
    d = cliente.get(f"/network/{RED_ID}/ocupacion?dias=30").get_json()
    sensores = {s["nombre"]: s for s in d["sensores"]}
    assert sensores["Luminaria pasillo"]["celdas"][0][14] == 25
    assert sensores["Luminaria pasillo"]["pico"] == {"dia": 0, "hora": 14, "pct": 25}
    # Dos minutos observados no dan para un porcentaje.
    assert sensores["Pasarela"]["celdas"][0][14] is None


def test_un_periodo_inventado_es_un_400(cliente):
    assert cliente.get(f"/network/{RED_ID}/ocupacion?dias=13").status_code == 400
    assert cliente.get(f"/network/{RED_ID}/ocupacion?dias=x").status_code == 400


def test_la_presencia_se_reimporta_desde_el_dia_anterior(app_cargada, monkeypatch,
                                                         tmp_path):
    pedidas = _cliente_con_historial(app_cargada, monkeypatch,
                                     lambda d, h: {"puntos": [], "hay_mas": False})
    conn = registro.abrir(tmp_path / "r.db")
    import time
    ultima = (int(time.time() * 1000) // registro.HORA - 5) * registro.HORA
    registro.guardar_ocupacion(conn, RED_ID, {(1, ultima): [0.0, 60.0]}, 0)
    app_module._importar_presencia(RED_ID, conn)
    desde, _, clave = pedidas[0]
    esperado = ultima - 24 * registro.HORA - 12 * registro.HORA
    assert clave == "presence"
    assert int(desde.timestamp() * 1000) == esperado
