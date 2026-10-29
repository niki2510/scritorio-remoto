@echo off
REM Arranca el servidor de Escritorio Remoto en Windows.
cd /d "%~dp0"

if not exist venv (
    echo Creando entorno virtual...
    python -m venv venv
    venv\Scripts\python -m pip install --upgrade pip
    venv\Scripts\python -m pip install -r requirements.txt
)

venv\Scripts\python app.py %*
