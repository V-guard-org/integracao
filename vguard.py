"""
V-GUARD — o programa principal da Raspberry Pi (proposta §6.1 e §6.9).

    câmera --> vision/main.py: o programa da visão, num processo filho (venv 3.11)
                   |  canal (pipe): pronto, fps, evento + JPEG
                   v
               VisionProcess --record_event()--> EventManager --> SD
    ESP32  --> Esp32Reader   --update()-------------->      |
    GNSS   --> GpsReader     --update()-------------->      |
                                     Sync <--pending_*/ack_*--+
    Pi     --> PiMonitor: temperatura e throttled no log, a cada 10 s

O principal roda no Python 3.13 do sistema (o venv-principal) e não importa o
OpenCV, o MediaPipe nem o NumPy. A visão roda no Python dela
(VGUARD_VISION_PYTHON) e é reiniciada se cair; uma pane dela não para a
telemetria. O programa encerra quando a visão termina normalmente (o Q na tela
ou --max-frames) ou com o SIGTERM/Ctrl+C. Ao encerrar, uma linha de resumo
vai ao log.

    python3 vguard.py --headless              # na Pi
    python3 vguard.py --source video.mp4      # no notebook
"""

import argparse
import logging
import os
import signal
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "events"), os.path.join(HERE, "vision")]

from esp32_reader import Esp32Reader
from event_manager import EventManager
from gps_reader import GpsReader
from pi_monitor import PiMonitor
from sync import Sync
from vision_process import VisionProcess
import options as vision_options  # vision/options.py: as opções da câmera, sem OpenCV

log = logging.getLogger("vguard")


def parse_args(argv=None):
    """As opções do principal e as da visão, conferidas antes de criar o banco."""
    parser = argparse.ArgumentParser(description="V-GUARD — aplicação integrada da Raspberry Pi")
    # --no-uart é o nome antigo, de quando o ESP32 ficava na UART.
    parser.add_argument("--no-esp32", "--no-uart", dest="no_esp32", action="store_true",
                        help="Executa sem iniciar o leitor do ESP32")
    parser.add_argument("--no-gps", action="store_true", help="Executa sem iniciar o leitor do GPS")
    parser.add_argument("--no-sync", action="store_true", help="Desativa sincronização e acesso ao backend")
    vision_options.add_arguments(parser)
    args = parser.parse_args(argv)
    vision_options.check(parser, args)
    if args.calibrate and args.calibration_file is None:
        args.calibration_file = Path(os.environ.get("VGUARD_DATA", "dados")) / "calibration-driver.json"
    return args


def run(argv=None):
    args = parse_args(argv)
    manager = EventManager(os.environ.get("VGUARD_DATA", "dados"))
    reader = None
    gps = None
    sync = None
    vision = None
    monitor = PiMonitor()

    try:
        monitor.start()
        if not args.no_esp32:
            reader = Esp32Reader(manager)
            reader.start()
        if not args.no_gps:
            gps = GpsReader(manager)
            gps.start()
        if not args.no_sync:
            sync = Sync(manager)
            sync.start()
        vision = VisionProcess(manager, vision_options.to_argv(args))
        vision.start()
        # Até a visão terminar normalmente. Num erro de configuração ela não volta,
        # e a telemetria segue até o SIGTERM.
        while not vision.wait(0.5):
            pass
    except KeyboardInterrupt:
        log.info("encerramento solicitado")
    finally:
        # Primeiro a visão (SIGTERM, até 5 s, kill); depois o resto; por último o banco.
        if vision is not None:
            vision.stop()
        for worker in (reader, gps, sync, monitor):
            if worker is not None:
                worker.stop()
        manager.close()
        if vision is not None:
            parts = [vision.summary()]
            if reader is not None:
                parts.append(f"ESP32: lost {reader.link.lost}, corrupt {reader.link.corrupt}")
            parts.append(monitor.summary())
            log.info("resumo: %s", "; ".join(parts))


def _request_stop(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _request_stop)
    run()
