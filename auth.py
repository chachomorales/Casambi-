"""
Puerta de entrada: Cloudflare Access.

Cloudflare Access autentica delante (con las cuentas de Google Workspace) y
firma un JWT que llega en la cabecera `Cf-Access-Jwt-Assertion`. Este módulo lo
verifica **otra vez** en la aplicación, y eso no es redundante: sin esta
comprobación, el modelo de seguridad sería «nadie conoce el origen», que no es
un modelo de seguridad. Cualquiera que alcanzase el contenedor sin pasar por el
túnel entraría con todos los permisos, incluida la ruta que enciende escenas en
instalaciones de clientes.

De paso, el JWT trae el correo de quien entra, que es lo único que permite
auditar quién hizo qué.

En modo escritorio no se comprueba nada: ahí la app solo escucha en 127.0.0.1
dentro de la ventana nativa.
"""

from __future__ import annotations

import threading

from flask import g, jsonify, render_template, request

import config

# Cloudflare pone el JWT en esta cabecera; la cookie es el respaldo para una
# navegación directa en la que el proxy no la haya reescrito.
CABECERA = "Cf-Access-Jwt-Assertion"
COOKIE = "CF_Authorization"

# Rutas públicas. Son CSS, logos, el latido del contenedor y el manifiesto de
# la aplicación: ni datos de clientes ni acciones. Todo lo demás exige JWT
# válido.
#
# El manifiesto entra en esta lista porque, si respondiera 403 en el momento en
# que iOS lo pide, «Añadir a pantalla de inicio» degradaría a un marcador
# normal sin decir nada, y ese es justo el tipo de fallo mudo que no queremos.
# Su contenido es el nombre de la app, sus colores y las rutas de los iconos.
RUTAS_LIBRES = ("/static/", "/logos/", "/salud", "/manifest.webmanifest")

# Margen para desfases de reloj entre Cloudflare y el servidor.
_HOLGURA_SEGUNDOS = 30

_lock = threading.Lock()
_cliente_jwks = None


class AccesoDenegado(Exception):
    """El visitante no ha superado Access. Se traduce en un 403."""


class AccesoNoVerificable(Exception):
    """No se pudo comprobar la firma (p. ej. Cloudflare no responde).

    Se distingue de AccesoDenegado a propósito: aquí el problema es nuestro, no
    del visitante, y la respuesta es un 503. Lo que no hace ninguna de las dos
    es dejar pasar: ante la duda, se cierra.
    """


def _emisor() -> str:
    return f"https://{config.ACCESS_TEAM_DOMAIN}"


def _url_jwks() -> str:
    return f"{_emisor()}/cdn-cgi/access/certs"


def obtener_cliente_jwks():
    """Cliente de claves públicas, cacheado y compartido entre hilos.

    PyJWT se encarga de refrescar el JWKS y de resolver el `kid`, lo que importa
    porque Cloudflare rota sus claves de firma cada pocas semanas.
    """
    global _cliente_jwks
    with _lock:
        if _cliente_jwks is None:
            from jwt import PyJWKClient
            _cliente_jwks = PyJWKClient(
                _url_jwks(), cache_keys=True, lifespan=300, max_cached_keys=16
            )
        return _cliente_jwks


def reiniciar_cliente_jwks() -> None:
    """Olvida el cliente cacheado. Para los tests y para cambiar de equipo."""
    global _cliente_jwks
    with _lock:
        _cliente_jwks = None


def verificar_token(token: str) -> str:
    """Devuelve el correo del JWT, o lanza si no se puede confiar en él."""
    import jwt

    if not token:
        raise AccesoDenegado("Falta el identificador de Cloudflare Access.")

    # Un token ilegible es culpa de quien llama, así que se descarta antes de
    # salir a la red: si no, un 403 acabaría reportado como caída de Cloudflare.
    try:
        jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as e:
        raise AccesoDenegado(f"Identificador ilegible: {e}") from e

    try:
        clave = obtener_cliente_jwks().get_signing_key_from_jwt(token)
    except jwt.exceptions.PyJWKClientConnectionError as e:
        raise AccesoNoVerificable(f"No se pudo consultar Cloudflare: {e}") from e
    except (jwt.exceptions.PyJWKClientError, jwt.InvalidTokenError) as e:
        # El JWKS se leyó, pero no tiene esa clave de firma. PyJWT refresca
        # cuando el kid no está en caché, así que a estas alturas significa que
        # el token no lo firmó este equipo de Access.
        raise AccesoDenegado(f"Clave de firma desconocida: {e}") from e
    except Exception as e:  # DNS, TLS, timeouts…
        raise AccesoNoVerificable(f"No se pudo consultar Cloudflare: {e}") from e

    try:
        claims = jwt.decode(
            token,
            clave.key,
            algorithms=["RS256"],
            audience=config.ACCESS_AUD,
            issuer=_emisor(),
            leeway=_HOLGURA_SEGUNDOS,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.InvalidTokenError as e:
        raise AccesoDenegado(f"Identificador no válido: {e}") from e

    correo = (claims.get("email") or "").strip().lower()
    if not correo:
        raise AccesoDenegado("El identificador no trae correo.")

    permitidos = config.emails_autorizados()
    if permitidos and correo not in permitidos:
        raise AccesoDenegado(f"{correo} no está en la lista de acceso.")

    return correo


def _token_de_la_peticion() -> str:
    return (request.headers.get(CABECERA) or request.cookies.get(COOKIE) or "").strip()


def es_ruta_libre(ruta: str) -> bool:
    return ruta.startswith(RUTAS_LIBRES)


def proteger(app) -> None:
    """Instala la comprobación antes de cada petición."""

    @app.before_request
    def _exigir_acceso():
        # El escritorio no tiene Access delante y no lo necesita.
        if config.ES_ESCRITORIO:
            g.user_email = "escritorio"
            return None
        if es_ruta_libre(request.path):
            return None

        try:
            g.user_email = verificar_token(_token_de_la_peticion())
        except AccesoDenegado as e:
            app.logger.warning("Acceso denegado en %s: %s", request.path, e)
            return _respuesta_error(
                403, "Acceso denegado", str(e),
                "Entra por el enlace de la aplicación para que Cloudflare "
                "Access te identifique. Si el problema persiste, puede que tu "
                "cuenta no tenga permiso.",
            )
        except AccesoNoVerificable as e:
            app.logger.error("Acceso no verificable en %s: %s", request.path, e)
            return _respuesta_error(
                503, "No se puede verificar el acceso ahora mismo", str(e),
                "No se ha podido consultar a Cloudflare para comprobar tu "
                "identidad. Es un problema del servidor, no tuyo: vuelve a "
                "intentarlo en un momento.",
            )
        return None

    @app.route("/salud")
    def _salud():
        """Latido para el contenedor. Sin datos, por eso es ruta libre."""
        return jsonify({"ok": True})


def _respuesta_error(codigo: int, titulo: str, detalle: str, ayuda: str = ""):
    """HTML para el navegador, JSON para las llamadas de la interfaz."""
    if request.accept_mimetypes.best == "application/json" or request.is_json:
        return jsonify({"ok": False, "error": detalle}), codigo
    return render_template(
        "error.html", titulo=titulo, mensaje=detalle, ayuda=ayuda,
    ), codigo
