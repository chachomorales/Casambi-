@echo off
REM ─── Casambi Report Web App — Windows ─────────────────────────────────────
setlocal

set SCRIPT_DIR=%~dp0
set VENV_DIR=%SCRIPT_DIR%.venv

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python no encontrado. Instalalo desde https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Creando entorno virtual...
    python -m venv "%VENV_DIR%"
)

call "%VENV_DIR%\Scripts\activate.bat"

pip install -q -r "%SCRIPT_DIR%requirements.txt"

echo Abriendo la aplicacion web en http://127.0.0.1:5000 ...
python "%SCRIPT_DIR%app.py"

pause
