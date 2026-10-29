"""
V-GUARD — Processo único da Raspberry Pi: visão + gerenciador de eventos.

    câmera --> VisionPipeline --record_event()--> EventManager --> SD
    ESP32  --> UartReader     --update()-------->      |
    GNSS   --> GpsReader      --update()-------->      |
                                     Sync <--pending_*/ack_*--+

A visão roda na thread principal (cv2.imshow exige); UART, GPS e Sync em threads.

    python3 vguard.py --headless              # na Pi
    python3 vguard.py --source video.mp4      # no notebook
"""

import argparse
import logging
import os
import signal
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "events"), os.path.join(HERE, "vision")]

from event_manager import EventManager
from gps_reader import GpsReader
from sync import Sync
from uart_reader import UartReader
from main import VisionPipeline, parse_args  # vision/main.py


def run(argv=None):
    parser = argparse.ArgumentParser(description="V-GUARD — aplicação integrada da Raspberry Pi")
    parser.add_argument("--no-uart", action="store_true", help="Executa sem iniciar o leitor do ESP32")
    parser.add_argument("--no-gps", action="store_true", help="Executa sem iniciar o leitor do GPS")
    parser.add_argument("--no-sync", action="store_true", help="Desativa sincronização e acesso ao backend")
    parser.add_argument("--max-frames", type=int, help="Encerra após processar este número de frames")
    args = parse_args(argv, parser=parser)
    if args.max_frames is not None and args.max_frames <= 0:
        parser.error("--max-frames deve ser maior que zero")
    manager = EventManager(os.environ.get("VGUARD_DATA", "dados"))
    reader = None
    gps = None
    sync = None

    try:
        if not args.no_uart:
            reader = UartReader(manager)
            reader.start()
        if not args.no_gps:
            gps = GpsReader(manager)
            gps.start()
        if not args.no_sync:
            sync = Sync(manager)
            sync.start()
        return VisionPipeline(
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
        ).run()
    except KeyboardInterrupt:
        logging.info("encerramento solicitado")
    finally:
        if reader is not None:
            reader.stop()
        if gps is not None:
            gps.stop()
        if sync is not None:
            sync.stop()
        manager.close()


def _request_stop(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _request_stop)
    run()
