"""
Endurecimiento: límites de subida, cabeceras y secretos que no deben viajar.

Cada cosa de aquí tapaba un agujero concreto: no había tope de subida y el
fichero se leía entero en memoria, Pillow solo avisaba ante una imagen
descomprimible en gigabytes, el formato se creía por la extensión, y la API key
de Casambi viajaba en claro en el HTML de Ajustes.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

import config

from conftest import RED_ID
from test_humo import _png_de_prueba


def _subir(cliente, datos, nombre="plano.png", tipo=None):
    return cliente.post(
        f"/network/{RED_ID}/planos/upload",
        data={"file": (io.BytesIO(datos), nombre)},
        content_type="multipart/form-data",
    )


# ── Límites de tamaño ─────────────────────────────────────────────────────────

def test_el_limite_configurado_es_el_esperado(app_cargada):
    assert app_cargada.config["MAX_CONTENT_LENGTH"] == 25 * 1024 * 1024


def test_subida_demasiado_grande_se_explica(cliente, app_cargada, monkeypatch):
    """Antes no había límite: un POST grande tumbaba el proceso y con él la
    sesión de todo el equipo. Ahora debe avisar, no morir."""
    monkeypatch.setitem(app_cargada.config, "MAX_CONTENT_LENGTH", 2048)
    r = _subir(cliente, b"x" * 8192)
    assert r.status_code in (302, 413)
    if r.status_code == 302:
        # Se redirige con un flash explicando el límite.
        html = cliente.get("/").get_data(as_text=True)
        assert "supera el límite" in html


def test_pillow_tiene_tope_de_pixeles():
    assert Image.MAX_IMAGE_PIXELS == config.MAX_IMAGE_PIXELS
    assert Image.MAX_IMAGE_PIXELS is not None, "sin tope, una imagen puede reventar la RAM"


def test_una_imagen_por_encima_del_tope_se_rechaza(cliente, datos_limpios,
                                                   monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 10)
    r = _subir(cliente, _png_de_prueba(40, 30))
    assert r.status_code == 302
    assert not (datos_limpios / f"planos_{RED_ID}.json").exists()


# ── El formato lo dice el contenido, no la extensión ──────────────────────────

def test_un_gif_disfrazado_de_png_se_rechaza(cliente, datos_limpios):
    buf = io.BytesIO()
    Image.new("RGB", (20, 20), (1, 2, 3)).save(buf, format="GIF")
    r = _subir(cliente, buf.getvalue(), nombre="disfrazado.png")
    assert r.status_code == 302
    assert not (datos_limpios / f"planos_{RED_ID}.json").exists()


def test_un_ejecutable_renombrado_se_rechaza(cliente, datos_limpios):
    r = _subir(cliente, b"\x7fELF\x02\x01\x01" + b"\x00" * 200, nombre="virus.png")
    assert r.status_code == 302
    assert not (datos_limpios / f"planos_{RED_ID}.json").exists()


def test_extension_no_admitida_se_rechaza(cliente, datos_limpios):
    r = _subir(cliente, _png_de_prueba(), nombre="plano.svg")
    assert r.status_code == 302
    assert not (datos_limpios / f"planos_{RED_ID}.json").exists()


def test_un_png_de_verdad_sigue_entrando(cliente, datos_limpios):
    r = _subir(cliente, _png_de_prueba())
    assert r.status_code == 302
    assert (datos_limpios / f"planos_{RED_ID}.json").is_file()


# ── Cabeceras ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cabecera,esperado", [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "same-origin"),
])
def test_cabeceras_presentes(cliente, cabecera, esperado):
    assert cliente.get("/").headers[cabecera] == esperado


def test_csp_restrictiva(cliente):
    csp = cliente.get("/").headers["Content-Security-Policy"]
    for directiva in ("default-src 'self'", "frame-ancestors 'none'",
                      "form-action 'self'", "base-uri 'self'"):
        assert directiva in csp


def test_hsts_solo_en_modo_web(cliente, app_cargada, monkeypatch):
    # En escritorio se sirve por http en 127.0.0.1; HSTS haría que el navegador
    # rechazase la propia app.
    assert "Strict-Transport-Security" not in cliente.get("/").headers

    monkeypatch.setattr(config, "ES_ESCRITORIO", False)
    cli = app_cargada.test_client()
    assert "max-age=" in cli.get("/salud").headers["Strict-Transport-Security"]


# ── Secretos que no deben viajar ──────────────────────────────────────────────

def test_la_api_key_no_aparece_en_ajustes(cliente, credenciales):
    credenciales.cuentas[0]["api_key"] = "clave-api-secretisima-1234"
    html = cliente.get("/ajustes").get_data(as_text=True)
    assert "clave-api-secretisima-1234" not in html
    # Sí una máscara, para poder reconocer cuál está puesta.
    assert "clav…1234" in html


def test_la_contrasena_nunca_aparece(cliente, credenciales):
    credenciales.cuentas[0]["password"] = "contraseña-secretisima"
    html = cliente.get("/ajustes").get_data(as_text=True)
    assert "contraseña-secretisima" not in html


def test_guardar_sin_tocar_la_clave_la_conserva(cliente, credenciales):
    """Al no enviarse a la interfaz, vacío tiene que significar «no la cambies»."""
    original = credenciales.cuentas[0]["api_key"]
    r = cliente.post("/ajustes/cuentas/cuenta1", data={
        "label": "Renombrada", "api_key": "", "email": "a@b.c", "password": "",
    })
    assert r.status_code == 302
    assert credenciales.cuentas[0]["api_key"] == original
    assert credenciales.cuentas[0]["label"] == "Renombrada"


def test_enmascarado_de_valores_cortos(cliente):
    import app as app_module
    assert app_module._enmascarar("") == ""
    assert app_module._enmascarar("corta") == "•" * 5
    assert app_module._enmascarar("abcdefghijklmnop") == "abcd…mnop"
