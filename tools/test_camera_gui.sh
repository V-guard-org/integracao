#!/bin/sh
# Execute na tela da Pi (Pi Connect); testes locais sem backend/GPS/UART.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
export VGUARD_DATA="${VGUARD_DATA:-$project_dir/../dados-teste-gui}"
# Padrão: OV5647 com perfil IR. Webcam C525: CAMERA_ARGS="--source 0" bash tools/test_camera_gui.sh
exec .venv/bin/python -B vguard.py ${CAMERA_ARGS:---infrared} --no-uart --no-gps --no-sync --calibration-file "$VGUARD_DATA/calibration-driver.json" "$@"
