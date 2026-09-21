"""
Protección CSRF.

Sin ella, una página cualquiera podía hacer que el navegador de un técnico ya
identificado creara o borrara cuentas de Casambi, subiera planos o encendiera
escenas en la instalación de un cliente. Aquí la protección va **encendida**,
al contrario que en los tests de humo.
"""

from __future__ import annotations

import io
import re

import pytest

from conftest import RED_ID


@pytest.fixture
def cli(app_cargada):
    app_cargada.config["WTF_CSRF_ENABLED"] = True
    yield app_cargada.test_client()
    app_cargada.config["WTF_CSRF_ENABLED"] = False


def _token_de(html: str) -> str:
    """Saca un token del HTML servido, como haría un navegador."""
    m = re.search(r'name="csrf[_-]token"[^>]*(?:value|content)="([^"]+)"', html)
    assert m, "la página no trae token CSRF"
    return m.group(1)


# ── Sin token, nada pasa ──────────────────────────────────────────────────────

@pytest.mark.parametrize("ruta,datos", [
    ("/ajustes/cuentas", {"label": "X", "api_key": "k",
                          "email": "a@b.c", "password": "p"}),
    ("/ajustes/cuentas/cuenta1", {"accion": "borrar"}),
    # Rehacer la lista reautentica todas las cuentas: sin token, una página
    # ajena podría obligar a ello una y otra vez.
    ("/redes/refresh", {}),
])
def test_formularios_sin_token_se_rechazan(cli, ruta, datos):
    assert cli.post(ruta, data=datos).status_code == 400


@pytest.mark.parametrize("ruta,cuerpo", [
    ("scene_levels", {"scene_id": "20", "unit_id": "1", "level": "70"}),
    ("buttons", {"unit_id": "3", "count": 2}),
    ("sensors", {"unit_id": "2", "modo": "Presencia"}),
    ("schedules", {"action": "create"}),
    ("planos", {"action": "rename", "plano_id": 1, "name": "X"}),
])
def test_llamadas_json_sin_token_se_rechazan(cli, ruta, cuerpo):
    r = cli.post(f"/network/{RED_ID}/{ruta}", json=cuerpo)
    assert r.status_code == 400


def test_encender_escena_sin_token_se_rechaza(cli):
    """La más sensible: actúa sobre la instalación real del cliente."""
    assert cli.post(f"/network/{RED_ID}/scenes/20/capture").status_code == 400


def test_subir_plano_sin_token_se_rechaza(cli):
    r = cli.post(
        f"/network/{RED_ID}/planos/upload",
        data={"file": (io.BytesIO(b"x"), "p.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400


# ── Con token, todo sigue funcionando ─────────────────────────────────────────

def test_las_paginas_sirven_el_token(cli):
    for ruta in ("/ajustes", f"/network/{RED_ID}"):
        html = cli.get(ruta).get_data(as_text=True)
        assert 'name="csrf-token"' in html, f"{ruta} sin meta para el JavaScript"
        _token_de(html)


def test_crear_cuenta_con_token_funciona(cli, credenciales):
    token = _token_de(cli.get("/ajustes").get_data(as_text=True))
    r = cli.post("/ajustes/cuentas", data={
        "csrf_token": token, "label": "Nueva", "api_key": "k",
        "email": "a@b.c", "password": "p",
    })
    assert r.status_code == 302
    assert any(c["label"] == "Nueva" for c in credenciales.cuentas)


def test_llamada_json_con_cabecera_funciona(cli, datos_limpios):
    """Es lo que hacen los fetch de network.html: token en X-CSRFToken."""
    token = _token_de(cli.get(f"/network/{RED_ID}").get_data(as_text=True))
    r = cli.post(
        f"/network/{RED_ID}/buttons",
        json={"unit_id": "3", "count": 2},
        headers={"X-CSRFToken": token},
    )
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_subir_plano_con_token_funciona(cli, datos_limpios):
    from test_humo import _png_de_prueba

    token = _token_de(cli.get(f"/network/{RED_ID}").get_data(as_text=True))
    r = cli.post(
        f"/network/{RED_ID}/planos/upload",
        data={"csrf_token": token, "file": (io.BytesIO(_png_de_prueba()), "p.png")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 302
    assert (datos_limpios / f"planos_{RED_ID}.json").is_file()


def test_token_falsificado_se_rechaza(cli):
    r = cli.post("/ajustes/cuentas", data={
        "csrf_token": "me-lo-acabo-de-inventar", "label": "X",
        "api_key": "k", "email": "a@b.c", "password": "p",
    })
    assert r.status_code == 400
