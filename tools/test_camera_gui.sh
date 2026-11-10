#!/bin/sh
# Execute na tela da Pi (Pi Connect); testes locais sem backend/GPS/UART.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
export VGUARD_DATA="${VGUARD_DATA:-$project_dir/../dados-teste-gui}"
# O principal no Python dele (Issue #10): VGUARD_PYTHON, o venv-principal ou o .venv.
# A visão, com a janela, roda num processo filho, no Python de VGUARD_VISION_PYTHON.
python="${VGUARD_PYTHON:-$project_dir/../venv-principal/bin/python}"
[ -x "$python" ] || python="$project_dir/.venv/bin/python"
# Padrão: OV5647 com perfil IR. Webcam: CAMERA_ARGS="--source 0" bash tools/test_camera_gui.sh
exec "$python" -B vguard.py ${CAMERA_ARGS:---infrared} --no-esp32 --no-gps --no-sync --calibration-file "$VGUARD_DATA/calibration-driver.json" "$@"
