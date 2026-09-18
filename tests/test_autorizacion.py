"""
Autorización por red.

Antes solo `network_view` comprobaba que la red existiera, así que las otras
diez rutas atendían cualquier network_id inventado: se podían leer y escribir
anotaciones, descargar informes y encender escenas de redes ajenas a las
cuentas configuradas. Y como el id se interpola en nombres de fichero, valía
también para escribir fuera de sitio.
"""

from __future__ import annotations

import io

import pytest

from conftest import RED_ID


# Las 11 rutas de red, con el método y el cuerpo que cada una espera.
RUTAS = [
    ("GET",  "",                       None),
    ("POST", "/refresh",               None),
    ("GET",  "/excel",                 None),
    ("POST", "/planos",                {"action": "rename", "plano_id": 1, "name": "X"}),
    ("GET",  "/planos/img/1",          None),
    ("POST", "/scene_levels",          {"scene_id": "20", "unit_id": "1", "level": "70"}),
    ("POST", "/buttons",               {"unit_id": "3", "count": 1}),
    ("POST", "/sensors",               {"unit_id": "2", "modo": "Presencia"}),
    ("POST", "/schedules",             {"action": "create"}),
    ("POST", "/scenes/20/capture",     None),
]

# Ids que no deben pasar. Flask ya descarta las barras, pero no los puntos, y el
# id acaba en planos_<id>.json y plano_<id>_<n>.png.
IDS_RECHAZADOS = [
    "red-que-no-existe",
    "..",
    "...",
    "..%2F..%2Fetc",
    "red con espacios",
    "red;rm",
    "red$(whoami)",
    "a" * 200,
    "red/../otra",
]


def _llamar(cliente, sufijo, metodo, cuerpo, red):
    url = f"/network/{red}{sufijo}"
    if metodo == "GET":
        return cliente.get(url)
    return cliente.post(url, json=cuerpo) if cuerpo else cliente.post(url)


@pytest.mark.parametrize("red", IDS_RECHAZADOS)
@pytest.mark.parametrize("metodo,sufijo,cuerpo", RUTAS)
def test_ninguna_ruta_atiende_una_red_ajena(cliente, metodo, sufijo, cuerpo, red):
    r = _llamar(cliente, sufijo, metodo, cuerpo, red)
    assert r.status_code == 404, (
        f"{metodo} /network/{red}{sufijo} devolvió {r.status_code}"
    )


def test_subir_plano_a_una_red_ajena_se_rechaza(cliente):
    r = cliente.post(
        "/network/red-que-no-existe/planos/upload",
        data={"file": (io.BytesIO(b"x"), "p.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 404


@pytest.mark.parametrize("metodo,sufijo,cuerpo", RUTAS)
def test_la_red_buena_sigue_funcionando(cliente, datos_limpios, metodo, sufijo,
                                        cuerpo, monkeypatch):
    """La contrapartida: el decorador no debe estorbar al uso normal."""
    monkeypatch.setattr("app.time.sleep", lambda _s: None)
    r = _llamar(cliente, sufijo, metodo, cuerpo, RED_ID)
    # 404 solo se acepta donde el recurso concreto no existe (plano 1 sin subir).
    if sufijo == "/planos/img/1" or sufijo == "/planos":
        assert r.status_code in (200, 400, 404)
    else:
        assert r.status_code in (200, 302), f"{metodo} {sufijo} -> {r.status_code}"


def test_el_id_no_llega_al_sistema_de_ficheros(cliente, datos_limpios):
    """Un id con puntos no debe crear ningún fichero suelto en data/."""
    antes = set(p.name for p in datos_limpios.iterdir())
    cliente.post("/network/..%2F..%2Fescape/buttons", json={"unit_id": "3", "count": 1})
    cliente.post("/network/../buttons", json={"unit_id": "3", "count": 1})
    assert set(p.name for p in datos_limpios.iterdir()) == antes


def test_sin_redes_cargadas_no_se_sirve_nada(app_sin_cargar):
    """Con las redes sin cargar no se puede comprobar pertenencia; tampoco hay
    cliente autenticado, así que ninguna vista debe llegar a servir datos."""
    cli = app_sin_cargar.test_client()
    app_sin_cargar.config["WTF_CSRF_ENABLED"] = False
    r = cli.get(f"/network/{RED_ID}/excel")
    assert r.status_code in (302, 404, 500, 502, 503)
    assert r.status_code != 200
