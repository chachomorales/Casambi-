"""
Ajustes que difieren entre la app de escritorio y el servidor web.

La misma base de código sirve las dos versiones: `desktop.py` fija
CASAMBI_MODE=desktop antes de importar la app, y `wsgi.py` deja el valor por
defecto (web). De esa única variable salen las tres cosas que cambian —dónde
viven las credenciales, si hay que autenticar al visitante y si las cookies
pueden exigir HTTPS—, concentradas aquí para no repartir condicionales de modo
por todo `app.py`.

Nada de este módulo importa `app`: se lee en tiempo de import, antes que él.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class ConfigError(RuntimeError):
    """Falta algo imprescindible para arrancar. Se lanza al iniciar, no al servir."""


# ── Modo ──────────────────────────────────────────────────────────────────────

MODO = (os.environ.get("CASAMBI_MODE") or "web").strip().lower()
ES_ESCRITORIO = MODO == "desktop"


# ── Credenciales ──────────────────────────────────────────────────────────────
# El Llavero solo existe en macOS. En Linux se usa un fichero cifrado con la
# clave de CASAMBI_SECRET_KEY; ver credentials.py.

_SECURITY = "/usr/bin/security"

_AYUDA_CLAVE = (
    "Falta CASAMBI_SECRET_KEY. Genera una con:\n"
    "    python3 -c \"import base64,os; "
    "print(base64.urlsafe_b64encode(os.urandom(32)).decode())\"\n"
    "y guárdala en el gestor de contraseñas de la empresa: sin ella no se "
    "pueden descifrar las credenciales de una copia de seguridad."
)


def backend_credenciales() -> str:
    """Devuelve 'keychain' o 'encfile'.

    Por defecto se decide sola, para que el escritorio no necesite configurar
    nada; CASAMBI_CREDENTIALS_BACKEND fuerza uno de los dos, lo que permite
    probar el backend del servidor desde el propio Mac.
    """
    elegido = (os.environ.get("CASAMBI_CREDENTIALS_BACKEND") or "auto").strip().lower()
    if elegido in ("keychain", "encfile"):
        return elegido
    if elegido != "auto":
        raise ConfigError(
            f"CASAMBI_CREDENTIALS_BACKEND no admite «{elegido}»: "
            "usa 'keychain', 'encfile' o 'auto'."
        )
    if sys.platform == "darwin" and Path(_SECURITY).exists():
        return "keychain"
    return "encfile"


def clave_secreta_persistente() -> bytes:
    """Clave para cifrar en disco. A diferencia de la de Flask, nunca es efímera.

    Existe aparte porque `clave_secreta()` puede devolver bytes aleatorios en el
    escritorio, y con una clave distinta en cada arranque el fichero de
    credenciales quedaría ilegible al siguiente inicio.
    """
    bruta = os.environ.get("CASAMBI_SECRET_KEY") or ""
    if not bruta:
        raise ConfigError(_AYUDA_CLAVE)
    return bruta.encode("utf-8")


def clave_secreta() -> bytes:
    """Clave de Flask: cookies de sesión y tokens CSRF.

    En el servidor tiene que ser estable: con una clave aleatoria por arranque,
    cada reinicio invalidaría todos los tokens CSRF en las pestañas abiertas y,
    peor, dejaría ilegible el fichero de credenciales escrito antes. En el
    escritorio da igual, porque el proceso sirve una sola ventana y las
    credenciales las guarda el Llavero.
    """
    bruta = os.environ.get("CASAMBI_SECRET_KEY") or ""
    if bruta:
        return bruta.encode("utf-8")
    if ES_ESCRITORIO:
        return os.urandom(24)
    raise ConfigError(_AYUDA_CLAVE)


# ── Cloudflare Access ─────────────────────────────────────────────────────────
# En modo escritorio no se comprueba nada: la app solo escucha en 127.0.0.1
# dentro de la ventana nativa.

ACCESS_TEAM_DOMAIN = (os.environ.get("CASAMBI_ACCESS_TEAM_DOMAIN") or "").strip()
ACCESS_AUD = (os.environ.get("CASAMBI_ACCESS_AUD") or "").strip()


def emails_autorizados() -> set[str]:
    """Lista blanca de correos, separados por comas. Vacía = cualquiera que pase Access."""
    bruto = os.environ.get("CASAMBI_ACCESS_EMAILS") or ""
    return {e.strip().lower() for e in bruto.split(",") if e.strip()}


# ── Límites ───────────────────────────────────────────────────────────────────
# Hoy no hay ninguno: una subida grande se lee entera en memoria y tumba el
# proceso, y con él la sesión de todo el equipo.

MAX_CONTENT_LENGTH = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 64_000_000
MAX_PLANO_PAGINAS_PDF = 50
MAX_NIVELES_COBERTURA = 20


def aplicar(app) -> None:
    """Vuelca la configuración sobre la app de Flask ya creada."""
    app.secret_key = clave_secreta()
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        # Exigir HTTPS en la cookie rompería el escritorio, que sirve por http
        # en 127.0.0.1.
        SESSION_COOKIE_SECURE=not ES_ESCRITORIO,
        # Los formularios de anotación se dejan abiertos mucho rato mientras se
        # revisa una instalación; que caduque el token sería un fastidio y no
        # aporta nada frente a CSRF.
        WTF_CSRF_TIME_LIMIT=None,
    )


def validar_arranque() -> None:
    """Falla pronto y con un mensaje legible si falta configuración.

    Sin esto, el modo de fallo en Linux es silencioso: el backend de
    credenciales devuelve vacío, `is_configured()` da False y la app redirige
    en bucle a /ajustes sin decir por qué.
    """
    clave_secreta()
    backend = backend_credenciales()
    if backend == "encfile":
        # El fichero cifrado exige una clave estable, también en el escritorio
        # si se fuerza este backend para probarlo.
        clave_secreta_persistente()
    if ES_ESCRITORIO:
        return
    faltan = []
    if not ACCESS_TEAM_DOMAIN:
        faltan.append("CASAMBI_ACCESS_TEAM_DOMAIN (p. ej. impelsa.cloudflareaccess.com)")
    if not ACCESS_AUD:
        faltan.append("CASAMBI_ACCESS_AUD (el AUD tag de la aplicación en Access)")
    if faltan:
        raise ConfigError(
            "Faltan variables de Cloudflare Access:\n  · " + "\n  · ".join(faltan)
        )
    if backend != "encfile":
        raise ConfigError(
            f"En modo web el backend de credenciales debe ser 'encfile', no '{backend}'."
        )
