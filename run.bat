@echo off
setlocal

:: ─── Casambi Report Generator — Windows ───────────────────────────────────
set "VENV_DIR=%~dp0.venv"

:: Verificar que Python esté instalado
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python no encontrado. Instálalo desde https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Crear entorno virtual si no existe
if not exist "%VENV_DIR%\Scripts\activate.bat" (
    echo Creando entorno virtual...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [ERROR] No se pudo crear el entorno virtual.
        pause
        exit /b 1
    )
)

:: Activar entorno virtual
call "%VENV_DIR%\Scripts\activate.bat"

:: Instalar/actualizar dependencias
pip install -q -r "%~dp0requirements.txt"

:: Ejecutar la aplicación
python "%~dp0main.py"

endlocal
pause
