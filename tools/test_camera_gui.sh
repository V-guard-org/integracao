#!/bin/sh
# Execute na tela da Pi (Pi Connect); testes locais sem backend/GPS/UART.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
export VGUARD_DATA="${VGUARD_DATA:-$project_dir/../dados-teste-gui}"
# Webcam Logitech C525 (USB): a fonte padrão já é a webcam 0; sem perfil IR.
exec .venv/bin/python -B vguard.py --no-uart --no-gps --no-sync --calibration-file "$VGUARD_DATA/calibration-driver.json" "$@"
