"""
V-GUARD — Processo único da Raspberry Pi: visão + gerenciador de eventos.

    câmera --> VisionPipeline --record_event()--> EventManager --> SD
    ESP32  --> Esp32Reader    --update()-------->      |
    GNSS   --> GpsReader      --update()-------->      |
                                     Sync <--pending_*/ack_*--+
    Pi     --> PiMonitor: temperatura e throttled no log, a cada 10 s

A visão roda na thread principal (cv2.imshow exige); ESP32, GPS, Sync e o
PiMonitor em threads. Ao encerrar, uma linha de resumo vai ao log.

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
from main import VisionPipeline, parse_args  # vision/main.py

log = logging.getLogger("vguard")


def run(argv=None):
    parser = argparse.ArgumentParser(description="V-GUARD — aplicação integrada da Raspberry Pi")
    # --no-uart é o nome antigo, de quando o ESP32 ficava na UART.
    parser.add_argument("--no-esp32", "--no-uart", dest="no_esp32", action="store_true",
                        help="Executa sem iniciar o leitor do ESP32")
    parser.add_argument("--no-gps", action="store_true", help="Executa sem iniciar o leitor do GPS")
    parser.add_argument("--no-sync", action="store_true", help="Desativa sincronização e acesso ao backend")
    parser.add_argument("--max-frames", type=int, help="Encerra após processar este número de frames")
    args = parse_args(argv, parser=parser)
    if args.max_frames is not None and args.max_frames <= 0:
        parser.error("--max-frames deve ser maior que zero")
    if args.calibrate and args.calibration_file is None:
        args.calibration_file = Path(os.environ.get("VGUARD_DATA", "dados")) / "calibration-driver.json"
    manager = EventManager(os.environ.get("VGUARD_DATA", "dados"))
    reader = None
    gps = None
    sync = None
    pipeline = None
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
        pipeline = VisionPipeline(
            event_manager=manager,
            headless=args.headless,
            source=int(args.source) if args.source.isdigit() else args.source,
            frame_width=args.width,
            frame_height=args.height,
            max_frames=args.max_frames,
            camera_fps=args.fps,
            camera_num=args.camera_num,
            infrared=args.infrared,
            camera_python=args.camera_python,
            calibration_path=args.calibration_file,
            calibrate=args.calibrate,
        )
        return pipeline.run()
    except KeyboardInterrupt:
        log.info("encerramento solicitado")
    finally:
        for worker in (reader, gps, sync, monitor):
            if worker is not None:
                worker.stop()
        manager.close()
        if pipeline is not None:
            # FPS médio e eventos por tipo; temperatura máxima e avisos do throttled.
            log.info("resumo: %s; %s", pipeline.summary(), monitor.summary())


def _request_stop(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _request_stop)
    run()
