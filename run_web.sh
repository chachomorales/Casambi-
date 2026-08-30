#!/usr/bin/env bash
set -euo pipefail

# ─── Casambi Report Web App — macOS / Linux ───────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python 3 no encontrado. Instálalo desde https://www.python.org/downloads/"
    exit 1
fi

if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "Creando entorno virtual..."
    python3 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"

pip install -q -r "$SCRIPT_DIR/requirements.txt"

echo "Abriendo la aplicación web en http://127.0.0.1:5000 ..."
python "$SCRIPT_DIR/app.py"
