"""
Andamiaje de los tests: una red sintética y un CASAMBI_HOME desechable.

Dos cosas hay que resolver antes de importar `app`:

  · `app.py` resuelve CASAMBI_HOME en tiempo de import, así que la variable
    tiene que estar puesta ya cuando pytest recoge este fichero. Si no, los
    tests escribirían en el directorio del código.
  · `credentials` habla con el Llavero de macOS por subprocess. Los tests no
    deben tocar el Llavero real —ni depender de que la máquina tenga cuentas
    configuradas—, así que se sustituye entero.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_RAIZ))

_HOME = Path(tempfile.mkdtemp(prefix="casambi-tests-"))
(_HOME / "data").mkdir(parents=True, exist_ok=True)
(_HOME / "reportes").mkdir(parents=True, exist_ok=True)
os.environ["CASAMBI_HOME"] = str(_HOME)
os.environ["CASAMBI_MODE"] = "desktop"

import app as app_module  # noqa: E402  (después de fijar CASAMBI_HOME, a propósito)


RED_ID = "red-de-prueba"

CUENTA = {
    "id": "cuenta1",
    "label": "Cuenta de prueba",
    "email": "pruebas@ejemplo.com",
    "api_key": "clave-de-prueba",
    "password": "secreto",
}

# Una unidad de cada categoría que distingue `_classify_unit`, para que las
# nueve pestañas tengan algo que pintar. El BatterySwitch va sin
# firmwareVersion y sin estado online a propósito: es el caso "En reposo" que
# el diagnóstico de conectividad no debe contar como avería.
UNIDADES = [
    {"id": 1, "name": "Luminaria pasillo", "type": "Luminaire", "fixtureId": 100,
     "groupId": 10, "address": "aa01", "firmwareVersion": "30.10",
     "controls": [{"type": "Dimmer"}]},
    {"id": 2, "name": "Sensor recepción", "type": "Sensor", "fixtureId": 101,
     "groupId": 10, "address": "aa02", "firmwareVersion": "30.10",
     "controls": [{"type": "OnOff"}]},
    {"id": 3, "name": "Pulsador entrada", "type": "BatterySwitch", "fixtureId": 102,
     "groupId": 10, "address": "aa03", "controls": [{"type": "PushButton"}]},
    {"id": 4, "name": "Pasarela", "type": "Gateway", "fixtureId": 103,
     "groupId": 0, "address": "aa04", "firmwareVersion": "30.10", "controls": []},
]

NETWORK = {
    "id": RED_ID,
    "name": "Red de prueba",
    "site_name": "Sede de prueba",
    "units": UNIDADES,
    "groups": [{"id": 10, "name": "Planta baja"}],
    "scenes": [{"id": 20, "name": "Escena general", "type": "Regular",
                "position": 0, "units": {"1": {"id": 1}, "2": {"id": 2}}}],
    "photos": [],
    "gateway": {"name": "Pasarela"},
}

# `online` a True en alguna unidad hace que el diagnóstico sea "fiable" y
# distinga una avería real de un fallo de pasarela.
STATE = {
    "gateway": {"name": "Pasarela"},
    "units": [
        {"id": 1, "online": True, "status": "ok", "condition": 0},
        {"id": 2, "online": True, "status": "ok", "condition": 0},
        {"id": 3, "online": False},
        {"id": 4, "online": True, "status": "ok", "condition": 0},
    ],
}

FIXTURES = {
    100: {"vendor": "Fabricante", "model": "Downlight", "controls": {}},
    101: {"vendor": "Fabricante", "model": "Sensor PIR", "controls": {}},
    102: {"vendor": "Fabricante", "model": "Pulsador 4T", "controls": {}},
    103: {"vendor": "Fabricante", "model": "Gateway", "controls": {}},
}

META_RED = {
    "id": RED_ID,
    "name": "Red de prueba",
    "site_name": "Sede de prueba",
    "account_id": CUENTA["id"],
    "account_label": CUENTA["label"],
}


# El estado que devuelve el cliente tras activar una escena: `activeSceneId`
# confirma la escena, que es lo que hace a `scene_capture` dejar de reintentar.
STATE_TRAS_ESCENA = {
    "gateway": {"name": "Pasarela"},
    "units": [
        {"id": 1, "name": "Luminaria pasillo", "online": True, "status": "ok",
         "dimLevel": 0.8, "activeSceneId": 20},
        {"id": 2, "name": "Sensor recepción", "online": True, "status": "ok",
         "dimLevel": 0.5, "activeSceneId": 20},
    ],
}

# PNG de 1x1 transparente, para no depender de ficheros de ejemplo en disco.
PNG_MINIMO = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


class _ClienteFalso:
    """Sustituye a CasambiClient: ningún test debe salir a Casambi Cloud.

    `activate_scene` es la razón principal de que exista: esa ruta enciende
    escenas en instalaciones reales de clientes.
    """

    def __init__(self):
        self.escenas_activadas = []

    def get_network(self, network_id):
        return dict(NETWORK)

    def get_network_state(self, network_id):
        return STATE_TRAS_ESCENA if self.escenas_activadas else dict(STATE)

    def get_fixtures_for_units(self, units, progress=None):
        return dict(FIXTURES)

    def get_image(self, network_id, image_id):
        return PNG_MINIMO

    def activate_scene(self, network_id, scene_id, level=1.0, timeout=10):
        self.escenas_activadas.append((str(network_id), str(scene_id)))

    def list_networks(self, user_session, networks_session):
        return [dict(META_RED)]

    def create_user_session(self, email, password):
        return {"sessionId": "sesion-de-prueba"}

    def create_networks_session(self, email, password):
        return {META_RED["id"]: {"sessionId": "sesion-de-red"}}


class _CredencialesFalsas:
    """Sustituye al módulo `credentials` para no tocar el Llavero real."""

    CredentialsError = app_module.credentials.CredentialsError

    def __init__(self):
        self.cuentas = [dict(CUENTA)]

    def is_configured(self, home=None):
        return bool(self.cuentas)

    def list_accounts(self, home=None):
        return [{k: c[k] for k in ("id", "label", "email")} for c in self.cuentas]

    def get_account(self, account_id):
        return next((dict(c) for c in self.cuentas if c["id"] == account_id), None)

    def load_all(self, home=None):
        return [dict(c) for c in self.cuentas]

    def add_account(self, label, api_key, email, password):
        nueva = {"id": "cuenta2", "label": label, "api_key": api_key,
                 "email": email, "password": password}
        self.cuentas.append(nueva)
        return nueva["id"]

    def update_account(self, account_id, label, api_key, email, password=""):
        for c in self.cuentas:
            if c["id"] == account_id:
                c.update(label=label, api_key=api_key, email=email)
                if password:
                    c["password"] = password

    def delete_account(self, account_id):
        self.cuentas = [c for c in self.cuentas if c["id"] != account_id]

    def legacy_env_path(self, home=None):
        return None


@pytest.fixture
def credenciales(monkeypatch):
    falsas = _CredencialesFalsas()
    monkeypatch.setattr(app_module, "credentials", falsas)
    return falsas


@pytest.fixture
def datos_limpios():
    """Vacía data/ entre tests: las anotaciones se guardan por red en ficheros."""
    data = _HOME / "data"
    for hijo in data.iterdir():
        shutil.rmtree(hijo) if hijo.is_dir() else hijo.unlink()
    yield data


@pytest.fixture
def app_cargada(credenciales, datos_limpios):
    """La app con la red sintética ya autenticada y cacheada.

    Evita salir a Casambi Cloud: se precarga `_state` exactamente con la forma
    que deja `_authenticate` + `_fetch_network_data`.
    """
    cliente_api = _ClienteFalso()
    app_module._state.update({
        "clients": {CUENTA["id"]: cliente_api},
        "networks": [dict(META_RED)],
        "cache": {RED_ID: {
            "network": NETWORK,
            "state": STATE,
            "fixtures": FIXTURES,
            "fetched_at": datetime(2026, 9, 18, 12, 0, 0),
        }},
        "error": None,
    })
    app_module._load.update({"active": False, "label": "", "done": 0,
                             "total": 0, "error": None})
    app_module.app.config["TESTING"] = True
    app_module.app.cliente_api_falso = cliente_api
    yield app_module.app
    app_module._state.update({"clients": {}, "networks": None,
                              "cache": {}, "error": None})


@pytest.fixture
def cliente(app_cargada):
    return app_cargada.test_client()


@pytest.fixture
def app_sin_cargar(credenciales, datos_limpios):
    """La app recién arrancada, sin autenticar: debe enseñar pantalla de carga."""
    app_module._state.update({"clients": {}, "networks": None,
                              "cache": {}, "error": None})
    app_module._load.update({"active": True, "label": "Conectando…",
                             "done": 0, "total": 0, "error": None})
    app_module.app.config["TESTING"] = True
    yield app_module.app
    app_module._load["active"] = False
