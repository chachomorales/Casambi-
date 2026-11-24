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
    d = cliente.get(f"/network/{RED_ID}/temperaturas?rango=24h").get_json()
    assert [u["nombre"] for u in d["unidades"]] == ["Luminaria pasillo", "Pasarela"]
    assert d["unidades"][0]["max"] == 45.0
    assert d["desfase_min"] == -360


def test_un_rango_inventado_es_un_400(cliente):
    assert cliente.get(f"/network/{RED_ID}/temperaturas?rango=1y").status_code == 400
