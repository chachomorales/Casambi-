#!/usr/bin/env bash
set -euo pipefail

# ─── Casambi Report Generator — macOS / Linux ─────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Verificar que Python 3 esté instalado
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python 3 no encontrado. Instálalo desde https://www.python.org/downloads/"
    exit 1
fi

# Crear entorno virtual si no existe
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "Creando entorno virtual..."
    python3 -m venv "$VENV_DIR"
fi

# Activar entorno virtual
source "$VENV_DIR/bin/activate"

# Instalar/actualizar dependencias
pip install -q -r "$SCRIPT_DIR/requirements.txt"

# Ejecutar la aplicación
python "$SCRIPT_DIR/main.py"
