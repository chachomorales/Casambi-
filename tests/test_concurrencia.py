"""
Concurrencia y persistencia.

Con la app de escritorio había un usuario y un hilo; en el servidor hay varios
hilos sobre el mismo estado y los mismos ficheros. Lo que se comprueba aquí es
que no se pierden anotaciones, que un fichero corrupto no desaparece en
silencio, y que cada persona sigue su propia barra de progreso.
"""

from __future__ import annotations

import io
import json
import threading

import pytest

import app as app_module

from conftest import RED_ID
from test_humo import _png_de_prueba


# ── Escritura atómica ─────────────────────────────────────────────────────────

def test_no_deja_temporales(cliente, datos_limpios):
    cliente.post(f"/network/{RED_ID}/buttons", json={"unit_id": "3", "count": 2})
    sobrantes = [p.name for p in datos_limpios.iterdir() if ".tmp" in p.name]
    assert sobrantes == []


def test_un_fallo_al_escribir_no_destruye_lo_guardado(cliente, datos_limpios,
                                                      monkeypatch):
    """Con write_text directo, un corte a media escritura dejaba el fichero
    truncado y el lector lo leía como «no hay anotaciones»."""
    ruta = datos_limpios / f"buttons_{RED_ID}.json"
    cliente.post(f"/network/{RED_ID}/buttons", json={"unit_id": "3", "count": 2})
    antes = ruta.read_text(encoding="utf-8")

    def replace_roto(origen, destino):
        raise OSError("disco lleno")

    monkeypatch.setattr(app_module.os, "replace", replace_roto)
    with pytest.raises(OSError):
        app_module._write_json(ruta, {"3": {"count": 99}})

    # El contenido bueno sigue intacto y no queda basura alrededor.
    assert ruta.read_text(encoding="utf-8") == antes
    assert [p.name for p in datos_limpios.iterdir() if ".tmp" in p.name] == []


# ── Corrupción: apartar, no borrar ────────────────────────────────────────────

def test_un_json_corrupto_se_aparta_y_no_se_pierde(cliente, datos_limpios):
    ruta = datos_limpios / f"buttons_{RED_ID}.json"
    ruta.write_text('{"3": {"count": 2', encoding="utf-8")  # truncado

    assert app_module._load_buttons(RED_ID) == {}          # la app no se cae
    assert not ruta.exists()                                # se apartó
    apartados = list(datos_limpios.glob(f"buttons_{RED_ID}.json.corrupto-*"))
    assert len(apartados) == 1
    assert apartados[0].read_text(encoding="utf-8") == '{"3": {"count": 2'


def test_la_red_sigue_usable_tras_un_json_corrupto(cliente, datos_limpios):
    (datos_limpios / f"sensors_{RED_ID}.json").write_text("no es json", encoding="utf-8")
    assert cliente.get(f"/network/{RED_ID}").status_code == 200


# ── Varios hilos sobre la misma red ───────────────────────────────────────────

def test_anotaciones_simultaneas_no_se_pierden(datos_limpios):
    """Sin lock, este read-modify-write perdía actualizaciones."""
    unidades = [str(i) for i in range(1, 31)]
    errores = []

    def guardar(uid):
        try:
            app_module._save_scene_level(RED_ID, "20", uid, "50")
        except Exception as e:  # pragma: no cover
            errores.append(e)

    hilos = [threading.Thread(target=guardar, args=(u,)) for u in unidades]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert errores == []
    guardado = json.loads((datos_limpios / f"scene_levels_{RED_ID}.json").read_text())
    assert sorted(guardado["20"], key=int) == sorted(unidades, key=int)


def test_escrituras_simultaneas_dejan_json_valido(datos_limpios):
    """El fichero nunca debe quedar a medias, ni en el peor momento."""
    def guardar(n):
        app_module._save_buttons(RED_ID, {str(n): {"count": n}})

    hilos = [threading.Thread(target=guardar, args=(n,)) for n in range(40)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    # Gana el último, pero el JSON tiene que ser legible siempre.
    contenido = (datos_limpios / f"buttons_{RED_ID}.json").read_text()
    assert isinstance(json.loads(contenido), dict)


def test_dos_planos_nunca_comparten_fichero(cliente, datos_limpios):
    for _ in range(2):
        cliente.post(
            f"/network/{RED_ID}/planos/upload",
            data={"file": (io.BytesIO(_png_de_prueba()), "planta.png")},
            content_type="multipart/form-data",
        )
    planos = json.loads((datos_limpios / f"planos_{RED_ID}.json").read_text())
    assert len(planos) == 2
    imagenes = [p["image"] for p in planos]
    assert len(set(imagenes)) == 2, "dos planos apuntan al mismo PNG"
    # El sufijo aleatorio es lo que lo garantiza aunque el id se repitiera.
    for nombre in imagenes:
        assert len(nombre.split("_")[-1].removesuffix(".png")) == 8


# ── Locks acotados ────────────────────────────────────────────────────────────

def test_una_red_inventada_no_deja_lock(cliente):
    """require_network va antes que el lock: si no, cada id inventado dejaría
    una entrada y la memoria crecería sin tope."""
    antes = len(app_module._net_locks)
    for i in range(20):
        cliente.post(f"/network/inventada{i}/buttons",
                     json={"unit_id": "3", "count": 1})
    assert len(app_module._net_locks) == antes


# ── Progreso por tarea ────────────────────────────────────────────────────────

@pytest.fixture
def tareas_limpias():
    app_module._tasks.clear()
    app_module._task_por_clave.clear()
    yield
    app_module._tasks.clear()
    app_module._task_por_clave.clear()


def test_la_misma_red_comparte_una_sola_descarga(tareas_limpias):
    """Antes la segunda petición se descartaba en silencio."""
    listo = threading.Event()
    veces = []

    def trabajo(progress):
        veces.append(1)
        progress("descargando", 1, 3)
        listo.wait(timeout=5)

    a = app_module._start_loading("net:red-1", trabajo)
    b = app_module._start_loading("net:red-1", trabajo)
    assert a == b, "dos personas en la misma red deberían compartir la descarga"

    listo.set()
    assert sum(veces) == 1


def test_redes_distintas_no_se_bloquean(tareas_limpias):
    listo = threading.Event()

    def trabajo(progress):
        progress("descargando", 0, 1)
        listo.wait(timeout=5)

    a = app_module._start_loading("net:red-1", trabajo)
    b = app_module._start_loading("net:red-2", trabajo)
    assert a != b
    assert app_module._estado_tarea(a)["active"] is True
    assert app_module._estado_tarea(b)["active"] is True
    listo.set()


def test_cada_tarea_lleva_su_propia_etiqueta(tareas_limpias):
    """El fallo visible de antes: ver el nombre de la red de otra persona."""
    listo = threading.Event()

    def trabajo(etiqueta):
        def _t(progress):
            progress(etiqueta, 1, 2)
            listo.wait(timeout=5)
        return _t

    a = app_module._start_loading("net:a", trabajo("Cargando Edificio A"))
    b = app_module._start_loading("net:b", trabajo("Cargando Edificio B"))
    # Puede tardar un instante en que el hilo llame a progress.
    for _ in range(200):
        if app_module._estado_tarea(a)["label"] and app_module._estado_tarea(b)["label"]:
            break
    assert app_module._estado_tarea(a)["label"] == "Cargando Edificio A"
    assert app_module._estado_tarea(b)["label"] == "Cargando Edificio B"
    listo.set()


def test_un_error_queda_en_su_tarea(tareas_limpias):
    from casambi_api import CasambiAPIError

    def revienta(progress):
        raise CasambiAPIError("la nube no responde")

    tid = app_module._start_loading("auth", revienta)
    for _ in range(500):
        if not app_module._estado_tarea(tid)["active"]:
            break
    estado = app_module._estado_tarea(tid)
    assert estado["active"] is False
    assert estado["error"] == "la nube no responde"


def test_una_tarea_desconocida_se_da_por_terminada(cliente):
    """Tras un reinicio, la pestaña abierta debe redirigir, no girar sin fin."""
    r = cliente.get("/carga/estado?task=no-existe")
    assert r.status_code == 200
    assert r.get_json() == {"active": False, "label": "", "done": 0,
                            "total": 0, "error": None}


def test_la_pantalla_de_carga_consulta_su_tarea(app_sin_cargar):
    html = app_sin_cargar.test_client().get("/").get_data(as_text=True)
    assert "task=tarea-de-prueba" in html
