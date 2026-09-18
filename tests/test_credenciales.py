"""
El almacén de credenciales, sobre el backend de fichero cifrado.

Estos tests usan el módulo `credentials` de verdad —no el falso de conftest—
porque lo que se comprueba es precisamente el backend nuevo. Nunca tocan el
Llavero: fuerzan `CASAMBI_CREDENTIALS_BACKEND=encfile`.
"""

from __future__ import annotations

import base64
import json
import os

import pytest

import config
import credentials


CLAVE = "clave-de-pruebas-larga-y-aleatoria-0123456789"


@pytest.fixture
def almacen(tmp_path, monkeypatch):
    """Un almacén cifrado vacío, en un directorio desechable."""
    monkeypatch.setenv("CASAMBI_HOME", str(tmp_path))
    monkeypatch.setenv("CASAMBI_CREDENTIALS_BACKEND", "encfile")
    monkeypatch.setenv("CASAMBI_SECRET_KEY", CLAVE)
    (tmp_path / "data").mkdir()
    # Los backends y las claves derivadas se cachean por proceso.
    monkeypatch.setattr(credentials, "_backends", {})
    monkeypatch.setattr(credentials, "_claves_derivadas", {})
    return tmp_path / "data" / "credentials.enc"


# ── Elección de backend ───────────────────────────────────────────────────────

def test_en_macos_elige_el_llavero_por_defecto(monkeypatch):
    monkeypatch.delenv("CASAMBI_CREDENTIALS_BACKEND", raising=False)
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.setattr(config.Path, "exists", lambda self: True)
    assert config.backend_credenciales() == "keychain"


def test_fuera_de_macos_elige_el_fichero(monkeypatch):
    monkeypatch.delenv("CASAMBI_CREDENTIALS_BACKEND", raising=False)
    monkeypatch.setattr(config.sys, "platform", "linux")
    assert config.backend_credenciales() == "encfile"


def test_backend_desconocido_se_rechaza(monkeypatch):
    monkeypatch.setenv("CASAMBI_CREDENTIALS_BACKEND", "sqlite")
    with pytest.raises(config.ConfigError, match="no admite"):
        config.backend_credenciales()


# ── Ciclo completo ────────────────────────────────────────────────────────────

def test_crear_leer_y_listar(almacen):
    assert credentials.list_accounts() == []
    assert credentials.is_configured() is False

    cid = credentials.add_account("Impelsa", "clave-api", "a@b.c", "secreto")

    indice = credentials.list_accounts()
    assert len(indice) == 1
    assert indice[0] == {"id": cid, "label": "Impelsa", "email": "a@b.c"}
    # El índice no debe llevar secretos.
    assert "api_key" not in indice[0] and "password" not in indice[0]

    completa = credentials.get_account(cid)
    assert completa["api_key"] == "clave-api"
    assert completa["password"] == "secreto"
    assert credentials.is_configured() is True


def test_acentos_y_unicode_sobreviven(almacen):
    cid = credentials.add_account("Sede Ñandú", "k", "a@b.c", "contraseña-áéíóú-ñ")
    assert credentials.get_account(cid)["password"] == "contraseña-áéíóú-ñ"


def test_contrasena_vacia_conserva_la_guardada(almacen):
    cid = credentials.add_account("Impelsa", "k1", "a@b.c", "secreto")
    credentials.update_account(cid, "Renombrada", "k2", "nuevo@b.c", password="")
    cuenta = credentials.get_account(cid)
    assert cuenta["password"] == "secreto"
    assert cuenta["api_key"] == "k2"
    assert cuenta["label"] == "Renombrada"


def test_borrar_cuenta_borra_sus_secretos(almacen):
    cid = credentials.add_account("Impelsa", "k", "a@b.c", "secreto")
    credentials.delete_account(cid)
    assert credentials.list_accounts() == []
    assert credentials.get_account(cid) is None
    # Nada del valor debe quedar suelto en el fichero.
    assert b"secreto" not in almacen.read_bytes()


def test_varias_cuentas_conviven(almacen):
    a = credentials.add_account("Una", "k1", "a@b.c", "p1")
    b = credentials.add_account("Otra", "k2", "d@e.f", "p2")
    assert {c["id"] for c in credentials.list_accounts()} == {a, b}
    assert credentials.get_account(a)["api_key"] == "k1"
    assert credentials.get_account(b)["api_key"] == "k2"
    assert len(credentials.load_all()) == 2


def test_faltan_datos_se_rechaza(almacen):
    with pytest.raises(credentials.CredentialsError, match="Faltan datos"):
        credentials.add_account("Impelsa", "", "a@b.c", "secreto")


# ── El cifrado de verdad ──────────────────────────────────────────────────────

def test_el_fichero_esta_cifrado_y_es_privado(almacen):
    credentials.add_account("Impelsa", "clave-api-secreta", "a@b.c", "contraseña")

    crudo = almacen.read_bytes()
    for secreto in (b"clave-api-secreta", b"contrase", b"a@b.c", b"Impelsa"):
        assert secreto not in crudo, f"{secreto!r} aparece en claro en el fichero"

    # Solo el dueño puede leerlo: el volumen del servidor acaba en las copias.
    assert oct(almacen.stat().st_mode)[-3:] == "600"

    sobre = json.loads(crudo)
    assert sobre["v"] == 1
    assert len(base64.b64decode(sobre["salt"])) == 16


def test_sobrevive_a_un_reinicio(almacen, monkeypatch):
    cid = credentials.add_account("Impelsa", "clave-api", "a@b.c", "secreto")

    # Simular un proceso nuevo: se pierden las cachés, el fichero permanece.
    monkeypatch.setattr(credentials, "_backends", {})
    monkeypatch.setattr(credentials, "_claves_derivadas", {})
    assert credentials.get_account(cid)["password"] == "secreto"


def test_clave_equivocada_avisa_en_vez_de_callar(almacen, monkeypatch):
    """El fallo importante: no debe presentarse como «sin configurar».

    Si devolviera {} en silencio, la app pediría configurar cuentas de nuevo y
    el siguiente guardado pisaría las credenciales buenas.
    """
    credentials.add_account("Impelsa", "clave-api", "a@b.c", "secreto")

    monkeypatch.setenv("CASAMBI_SECRET_KEY", "otra-clave-completamente-distinta")
    monkeypatch.setattr(credentials, "_backends", {})
    monkeypatch.setattr(credentials, "_claves_derivadas", {})

    with pytest.raises(credentials.CredentialsError, match="descifrar"):
        credentials.list_accounts()


def test_sin_clave_no_se_puede_guardar(almacen, monkeypatch):
    """Sin clave, un almacén vacío se lee sin más (no hay nada que descifrar),
    pero guardar tiene que fallar explicando cómo generar la clave."""
    monkeypatch.delenv("CASAMBI_SECRET_KEY", raising=False)
    monkeypatch.setattr(credentials, "_backends", {})
    monkeypatch.setattr(credentials, "_claves_derivadas", {})

    assert credentials.list_accounts() == []
    with pytest.raises(credentials.CredentialsError, match="CASAMBI_SECRET_KEY"):
        credentials.add_account("Impelsa", "k", "a@b.c", "secreto")


def test_sin_clave_no_se_puede_leer_lo_ya_guardado(almacen, monkeypatch):
    """Con cuentas guardadas y sin clave, hay que avisar, no decir que no hay."""
    credentials.add_account("Impelsa", "clave-api", "a@b.c", "secreto")

    monkeypatch.delenv("CASAMBI_SECRET_KEY", raising=False)
    monkeypatch.setattr(credentials, "_backends", {})
    monkeypatch.setattr(credentials, "_claves_derivadas", {})

    with pytest.raises(credentials.CredentialsError, match="CASAMBI_SECRET_KEY"):
        credentials.list_accounts()


# ── Migración heredada ────────────────────────────────────────────────────────

def test_migra_un_env_heredado(almacen, tmp_path):
    (tmp_path / ".env").write_text(
        "CASAMBI_API_KEY=k-heredada\n"
        "CASAMBI_EMAIL=viejo@b.c\n"
        "CASAMBI_PASSWORD=p-heredada\n",
        encoding="utf-8",
    )
    cuentas = credentials.list_accounts(tmp_path)
    assert len(cuentas) == 1
    assert cuentas[0]["email"] == "viejo@b.c"
    assert credentials.get_account(cuentas[0]["id"])["api_key"] == "k-heredada"


def test_la_migracion_no_tumba_la_peticion_si_no_puede_escribir(almacen, tmp_path,
                                                                monkeypatch):
    """Era un 500 en la portada: list_accounts() se llama en casi todas las rutas."""
    (tmp_path / ".env").write_text(
        "CASAMBI_API_KEY=k\nCASAMBI_EMAIL=a@b.c\nCASAMBI_PASSWORD=p\n",
        encoding="utf-8",
    )

    def escritura_rota(self, datos):
        raise credentials.CredentialsError("disco de solo lectura")

    monkeypatch.setattr(credentials._BackendFichero, "_guardar", escritura_rota)
    assert credentials.list_accounts(tmp_path) == []


# ── Validación de arranque ────────────────────────────────────────────────────

def test_arranque_web_exige_clave_estable(monkeypatch):
    monkeypatch.setenv("CASAMBI_CREDENTIALS_BACKEND", "encfile")
    monkeypatch.delenv("CASAMBI_SECRET_KEY", raising=False)
    with pytest.raises(config.ConfigError, match="CASAMBI_SECRET_KEY"):
        config.validar_arranque()
