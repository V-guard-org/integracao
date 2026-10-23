"""
V-GUARD — Processo único da Raspberry Pi: visão + gerenciador de eventos.

    câmera --> VisionPipeline --record_event()--> EventManager --> SD
    ESP32  --> UartReader     --update()-------->      |
                                     Sync <--pending_*/ack_*--+

A visão roda na thread principal (cv2.imshow exige); UART e Sync em threads.

    python3 vguard.py --headless              # na Pi
    python3 vguard.py --source video.mp4      # no notebook
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "events"), os.path.join(HERE, "vision")]

from event_manager import EventManager
from sync import Sync
from uart_reader import UartReader
from main import VisionPipeline, parse_args  # vision/main.py


def run():
    args = parse_args()
    manager = EventManager(os.environ.get("VGUARD_DATA", "dados"))
    reader = UartReader(manager)
    sync = Sync(manager)
    reader.start()
    sync.start()

    try:
        VisionPipeline(
            event_manager=manager,
            headless=args.headless,
            source=int(args.source) if args.source.isdigit() else args.source,
            frame_width=args.width,
            frame_height=args.height,
        ).run()
    finally:
        reader.stop()
        sync.stop()
        manager.close()


if __name__ == "__main__":
    run()
