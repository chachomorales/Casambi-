"""Lanzador de escritorio para CASAMBI.

Arranca el servidor Flask en un hilo y abre una ventana nativa de macOS
(WKWebView vía pywebview) apuntando a él. Empaquetado con PyInstaller
produce un CASAMBI.app autónomo.

Los datos escribibles (data/, reportes/) viven en
~/Library/Application Support/CASAMBI; en el primer arranque se copia desde el
bundle la carpeta semilla data/. Las credenciales de Casambi no se empaquetan:
se guardan en el Llavero de macOS (ver credentials.py).
"""

import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
BUNDLE_DIR = Path(__file__).parent


def _prepare_home() -> Path:
    """Crea el directorio de datos del usuario y siembra data/."""
    if not FROZEN:
        return BUNDLE_DIR  # en desarrollo todo sigue junto al código

    home = Path.home() / "Library" / "Application Support" / "CASAMBI"
    home.mkdir(parents=True, exist_ok=True)

    # Las credenciales NO se siembran: viven en el Llavero de macOS y se
    # introducen desde la pantalla de Ajustes de la app.

    seed_data = BUNDLE_DIR / "data"
    if seed_data.is_dir() and not (home / "data").exists():
        shutil.copytree(seed_data, home / "data")

    (home / "reportes").mkdir(exist_ok=True)
    return home


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.15)


def main() -> None:
    home = _prepare_home()
    os.environ["CASAMBI_HOME"] = str(home)
    # La misma base de código sirve la web; este es el interruptor que le dice a
    # config.py que aquí no hay Cloudflare Access ni cookies sobre HTTPS.
    os.environ.setdefault("CASAMBI_MODE", "desktop")

    from app import app  # importar después de fijar CASAMBI_HOME

    port = _free_port()
    threading.Thread(
        target=lambda: app.run(port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    _wait_for_server(port)

    import webview

    # WKWebView cancela en silencio las descargas si no se habilitan; sin esto
    # el botón "Descargar Excel" genera el reporte pero no lo entrega.
    webview.settings["ALLOW_DOWNLOADS"] = True

    webview.create_window(
        "CASAMBI",
        f"http://127.0.0.1:{port}",
        width=1320,
        height=880,
        min_size=(900, 600),
    )
    webview.start()


if __name__ == "__main__":
    main()
