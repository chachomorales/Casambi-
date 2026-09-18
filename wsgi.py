"""
Punto de entrada del servidor web (el equivalente de `desktop.py` sin ventana).

    gunicorn -w 1 -k gthread --threads 8 --timeout 300 wsgi:application

Un solo worker a propósito: la caché de redes y las sesiones de Casambi viven
en memoria del proceso y se comparten entre todo el equipo, que es justo lo que
queremos con credenciales de empresa. Con varios workers habría una
autenticación y una caché por proceso, y la barra de progreso respondería desde
un worker distinto al que está descargando.
"""

from __future__ import annotations

import os
from pathlib import Path

# `app.py` resuelve CASAMBI_HOME en tiempo de import, así que estas dos
# variables tienen que estar puestas antes de importarlo.
os.environ.setdefault("CASAMBI_HOME", "/data")
os.environ.setdefault("CASAMBI_MODE", "web")

_HOME = Path(os.environ["CASAMBI_HOME"])
for sub in ("data", "reportes"):
    (_HOME / sub).mkdir(parents=True, exist_ok=True)

import config  # noqa: E402  (después de fijar el entorno, a propósito)

# Mejor reventar aquí, con un mensaje que explique qué falta, que arrancar y
# redirigir en bucle a /ajustes sin decir por qué.
config.validar_arranque()

from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

from app import app as application  # noqa: E402

# Detrás del túnel de Cloudflare, sin esto Flask ve http y la IP del proxy:
# `url_for(_external=True)` generaría enlaces http y los logs registrarían
# siempre la misma IP.
application.wsgi_app = ProxyFix(
    application.wsgi_app, x_for=1, x_proto=1, x_host=1
)
