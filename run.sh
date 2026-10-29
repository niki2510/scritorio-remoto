#!/usr/bin/env bash
# Arranca el servidor de Escritorio Remoto en Linux/macOS.
set -e
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "Creando entorno virtual..."
  python3 -m venv venv
  ./venv/bin/pip install --upgrade pip
  ./venv/bin/pip install -r requirements.txt
fi

exec ./venv/bin/python app.py "$@"
