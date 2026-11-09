"""
As opções da visão na linha de comando, sem OpenCV, MediaPipe nem NumPy
(proposta §6.9, Issue #10).

O programa principal (vguard.py, no Python 3.13 do sistema) as importa para
conferir as opções antes de criar o banco e as repassa à visão
(vision/main.py, no venv 3.11), que as lê de novo com o mesmo parser.
"""

import argparse
import math
from pathlib import Path

# Resolução padrão de captura e inferência (equilíbrio para a Raspberry Pi 4).
FRAME_WIDTH = 640
FRAME_HEIGHT = 480


class ConfigError(ValueError):
    """
    Erro de configuração da visão: repetir não resolve (perfil de outra
    câmera, calibração pendente sem tela, modelo ausente...). O programa da
    visão sai com o código 78, e o principal não a reinicia.
    """


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """As opções da câmera e da calibração, as mesmas no principal e na visão."""
    parser.add_argument(
        "--headless", action="store_true",
        help="Sem a janela do OpenCV (o modo da Pi no carro)")
    parser.add_argument(
        "--source", type=str, default="picamera2",
        help="picamera2 para a CSI OV5647 (padrao), indice da webcam (ex: 0), "
             "dispositivo /dev/v4l/by-id/... ou arquivo de video")
    parser.add_argument(
        "--width", type=int, default=FRAME_WIDTH,
        help=f"Largura alvo de captura (padrao: {FRAME_WIDTH})")
    parser.add_argument(
        "--height", type=int, default=FRAME_HEIGHT,
        help=f"Altura alvo de captura (padrao: {FRAME_HEIGHT})")
    parser.add_argument(
        "--fps", type=float, default=30.0,
        help="FPS pedido a camera (padrao: 30; nao altera videos)")
    parser.add_argument(
        "--camera-num", type=int, default=0,
        help="Indice da camera CSI listado por rpicam-hello --list-cameras")
    parser.add_argument(
        "--infrared", action="store_true",
        help="Fixa os ganhos de cor para a iluminacao IR na CSI; nao aciona LEDs nem IR-CUT")
    parser.add_argument(
        "--camera-python",
        help="Python com Picamera2/libcamera para a captura num processo auxiliar "
             "(padrao: /usr/bin/python3 se o Python da visao nao tiver picamera2)")
    parser.add_argument(
        "--calibration-file", type=Path,
        help="Perfil JSON do motorista e da montagem; se faltar, a tela guia a calibracao")
    parser.add_argument(
        "--calibrate", action="store_true",
        help="Refaz a calibracao do motorista pela tela (ESPACO comeca)")
    parser.add_argument(
        "--max-frames", type=int,
        help="Encerra depois de processar este numero de quadros")
    return parser


def check(parser: argparse.ArgumentParser, args: argparse.Namespace) -> argparse.Namespace:
    """Confere as combinações; um erro sai pelo parser.error (SystemExit 2)."""
    if args.width <= 0 or args.height <= 0:
        parser.error("--width e --height devem ser maiores que zero")
    if not math.isfinite(args.fps) or args.fps <= 0:
        parser.error("--fps deve ser finito e maior que zero")
    if args.camera_num < 0:
        parser.error("--camera-num deve ser maior ou igual a zero")
    if args.source != "picamera2" and (args.infrared or args.camera_num != 0 or args.camera_python):
        parser.error("--infrared, --camera-num e --camera-python exigem --source picamera2")
    if args.headless and args.calibrate:
        parser.error("--calibrate exige interface grafica; retire --headless")
    if args.max_frames is not None and args.max_frames <= 0:
        parser.error("--max-frames deve ser maior que zero")
    return args


def to_argv(args: argparse.Namespace) -> list:
    """As opções conferidas, de volta em linha de comando, para o programa da visão."""
    argv = ["--source", args.source, "--width", str(args.width), "--height", str(args.height),
            "--fps", repr(args.fps), "--camera-num", str(args.camera_num)]
    for flag in ("headless", "infrared", "calibrate"):
        if getattr(args, flag):
            argv.append("--" + flag)
    if args.camera_python:
        argv += ["--camera-python", args.camera_python]
    if args.calibration_file is not None:
        argv += ["--calibration-file", str(args.calibration_file)]
    if args.max_frames is not None:
        argv += ["--max-frames", str(args.max_frames)]
    return argv
