"""
La puerta de Cloudflare Access.

Se firma con un par de claves RSA propio y se le sirve a PyJWT un JWKS falso,
así que no hace falta red ni una cuenta de Cloudflare. Lo que se comprueba es
que la app **no deja pasar** nada que no sea un JWT íntegro, vigente, emitido
para esta aplicación y con un correo autorizado — y que cuando no puede
comprobarlo, cierra en vez de abrir.
"""

from __future__ import annotations

import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import auth
import config

from conftest import RED_ID


EQUIPO = "impelsa.cloudflareaccess.com"
EMISOR = f"https://{EQUIPO}"
AUD = "aud-de-la-aplicacion"
KID = "clave-de-prueba"
CORREO = "tecnico@impelsa.es"


@pytest.fixture(scope="module")
def claves():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


@pytest.fixture(scope="module")
def jwks(claves):
    from jwt.algorithms import RSAAlgorithm

    _, pub = claves
    clave = json.loads(RSAAlgorithm.to_jwk(pub))
    clave.update(kid=KID, alg="RS256", use="sig")
    return {"keys": [clave]}


@pytest.fixture
def firmar(claves):
    """Fabrica tokens: por defecto válidos, y con lo que se le pida encima."""
    import jwt

    priv, _ = claves

    def _firmar(clave_privada=None, kid=KID, algoritmo="RS256", **cambios):
        ahora = int(time.time())
        claims = {
            "aud": AUD, "iss": EMISOR, "email": CORREO,
            "iat": ahora, "exp": ahora + 3600, "sub": "usuario-123",
        }
        claims.update(cambios)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, clave_privada or priv,
                          algorithm=algoritmo, headers={"kid": kid})

    return _firmar


@pytest.fixture
def web(app_cargada, jwks, monkeypatch):
    """La app en modo servidor, con Access activo y el JWKS falso."""
    from jwt import PyJWKClient

    monkeypatch.setattr(config, "ES_ESCRITORIO", False)
    monkeypatch.setattr(config, "ACCESS_TEAM_DOMAIN", EQUIPO)
    monkeypatch.setattr(config, "ACCESS_AUD", AUD)
    monkeypatch.delenv("CASAMBI_ACCESS_EMAILS", raising=False)
    # Se deja el cliente real de PyJWT (resuelve el kid de verdad) y solo se le
    # sustituye la descarga del JWKS.
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: jwks)
    auth.reiniciar_cliente_jwks()
    yield app_cargada
    auth.reiniciar_cliente_jwks()


@pytest.fixture
def cli(web):
    return web.test_client()


def _con(token):
    return {auth.CABECERA: token}


# ── Lo que debe pasar ─────────────────────────────────────────────────────────

def test_token_valido_entra(cli, firmar):
    r = cli.get("/", headers=_con(firmar()))
    assert r.status_code == 200
    assert "Red de prueba" in r.get_data(as_text=True)


def test_el_correo_queda_disponible_para_auditar(web, firmar):
    """`g.user_email` es lo que permitirá registrar quién enciende cada escena."""
    from flask import g

    with web.test_request_context("/", headers=_con(firmar())):
        assert web.preprocess_request() is None  # None = deja pasar
        assert g.user_email == CORREO


def test_el_token_tambien_vale_en_la_cookie(cli, firmar):
    cli.set_cookie(auth.COOKIE, firmar())
    assert cli.get("/").status_code == 200


def test_rutas_libres_no_piden_token(cli):
    assert cli.get("/salud").status_code == 200
    assert cli.get("/salud").get_json() == {"ok": True}
    assert cli.get("/logos/impelsa_logo.png").status_code == 200
    assert cli.get("/static/style.css").status_code == 200


def test_lista_blanca_permite_al_autorizado(cli, firmar, monkeypatch):
    monkeypatch.setenv("CASAMBI_ACCESS_EMAILS", f"otro@impelsa.es, {CORREO}")
    assert cli.get("/", headers=_con(firmar())).status_code == 200


# ── Lo que NO debe pasar ──────────────────────────────────────────────────────

def test_sin_token_no_entra(cli):
    r = cli.get("/")
    assert r.status_code == 403
    assert "Acceso denegado" in r.get_data(as_text=True)


def test_token_caducado_no_entra(cli, firmar):
    ahora = int(time.time())
    r = cli.get("/", headers=_con(firmar(iat=ahora - 7200, exp=ahora - 3600)))
    assert r.status_code == 403


def test_otra_aplicacion_no_entra(cli, firmar):
    """Un JWT de otra aplicación de Access del mismo equipo no vale aquí."""
    r = cli.get("/", headers=_con(firmar(aud="aud-de-otra-aplicacion")))
    assert r.status_code == 403


def test_otro_emisor_no_entra(cli, firmar):
    r = cli.get("/", headers=_con(firmar(iss="https://otro.cloudflareaccess.com")))
    assert r.status_code == 403


def test_firmado_con_otra_clave_no_entra(cli, firmar):
    intrusa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    r = cli.get("/", headers=_con(firmar(clave_privada=intrusa)))
    assert r.status_code == 403


def test_kid_desconocido_no_entra(cli, firmar):
    r = cli.get("/", headers=_con(firmar(kid="kid-que-no-existe")))
    assert r.status_code == 403


def test_sin_correo_no_entra(cli, firmar):
    r = cli.get("/", headers=_con(firmar(email=None)))
    assert r.status_code == 403


def test_fuera_de_la_lista_blanca_no_entra(cli, firmar, monkeypatch):
    monkeypatch.setenv("CASAMBI_ACCESS_EMAILS", "solo-este@impelsa.es")
    r = cli.get("/", headers=_con(firmar()))
    assert r.status_code == 403
    assert "lista de acceso" in r.get_data(as_text=True)


def test_token_basura_no_entra(cli):
    assert cli.get("/", headers=_con("esto-no-es-un-jwt")).status_code == 403


def test_confusion_de_algoritmo_no_entra(cli, claves):
    """Ataque clásico: firmar con HS256 usando la clave pública como secreto.

    Se fabrica a mano porque PyJWT se niega a crear ese token —buena defensa por
    su parte, pero lo que hay que comprobar es que *al verificar* se rechaza,
    ya que solo se acepta RS256.
    """
    import base64
    import hashlib
    import hmac

    from cryptography.hazmat.primitives import serialization

    _, pub = claves
    pem = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    def b64(crudo: bytes) -> bytes:
        return base64.urlsafe_b64encode(crudo).rstrip(b"=")

    ahora = int(time.time())
    cabecera = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    cuerpo = b64(json.dumps({
        "aud": AUD, "iss": EMISOR, "email": CORREO,
        "iat": ahora, "exp": ahora + 3600,
    }).encode())
    firma = b64(hmac.new(pem, cabecera + b"." + cuerpo, hashlib.sha256).digest())
    token = (cabecera + b"." + cuerpo + b"." + firma).decode("ascii")

    assert cli.get("/", headers=_con(token)).status_code == 403


def test_sin_firma_no_entra(cli, firmar):
    """Token con alg=none: sin firma que comprobar, hay que rechazarlo."""
    import jwt

    ahora = int(time.time())
    token = jwt.encode(
        {"aud": AUD, "iss": EMISOR, "email": CORREO,
         "iat": ahora, "exp": ahora + 3600},
        key="", algorithm="none", headers={"kid": KID},
    )
    assert cli.get("/", headers=_con(token)).status_code == 403


# ── Cuando no se puede comprobar: cerrar, no abrir ────────────────────────────

def test_si_cloudflare_no_responde_se_cierra(cli, firmar, monkeypatch):
    from jwt import PyJWKClient

    def caido(self):
        raise OSError("la red no responde")

    monkeypatch.setattr(PyJWKClient, "fetch_data", caido)
    auth.reiniciar_cliente_jwks()

    r = cli.get("/", headers=_con(firmar()))
    # 503, no 200: ante la duda no se deja pasar.
    assert r.status_code == 503
    assert "verificar el acceso" in r.get_data(as_text=True)


def test_las_llamadas_json_reciben_json(cli):
    r = cli.post(f"/network/{RED_ID}/buttons", json={"unit_id": "3", "count": 1})
    assert r.status_code == 403
    assert r.get_json()["ok"] is False


# ── Escritorio: sin puerta ────────────────────────────────────────────────────

def test_en_escritorio_no_se_pide_token(cliente):
    """`cliente` viene de conftest, que fija CASAMBI_MODE=desktop."""
    assert config.ES_ESCRITORIO is True
    assert cliente.get("/").status_code == 200
