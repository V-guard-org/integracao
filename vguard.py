"""
V-GUARD — o programa principal da Raspberry Pi (proposta §6.1 e §6.9).

    câmera --> vision/main.py: o programa da visão, num processo filho (venv 3.11)
                   |  canal (pipe): pronto, fps, evento + JPEG
                   v
               VisionProcess --record_event()--> EventManager --> SD
    ESP32  --> Esp32Reader   --update()-------------->      |
    GNSS   --> GpsReader     --update()-------------->      |
                            MqttPublisher <--on_telemetry---+--> MQTT: telemetria 1/s e saúde a cada 60 s
                              EventSender <--pending_events--+--> MQTT: o evento com a janela (QoS 1)
                                                                  HTTPS: o PUT da foto, que confirma o evento
    Pi     --> PiMonitor: temperatura e throttled no log, a cada 10 s

O principal roda no Python 3.13 do sistema (o venv-principal, com o paho-mqtt
do requirements-principal.txt) e não importa o OpenCV, o MediaPipe nem o NumPy. A visão roda no Python dela
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
from event_sender import EventSender
from gps_reader import GpsReader
from http_demo_publisher import HttpDemoPublisher
from mqtt_publisher import MqttPublisher
from pi_monitor import PiMonitor
from simulated_telemetry import SimulatedTelemetry
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
    parser.add_argument("--simulate-telemetry", action="store_true",
                        help="Gera IMU, OBD-II e GPS fictícios no lugar dos sensores")
    parser.add_argument("--demo-http", action="store_true",
                        help="Usa a API HTTP do backend na demonstração semissimulada")
    parser.add_argument("--no-sync", action="store_true",
                        help="Desativa o envio ao servidor: o MQTT e a foto (tudo fica no cartão)")
    vision_options.add_arguments(parser)
    args = parser.parse_args(argv)
    vision_options.check(parser, args)
    if args.demo_http and (not args.simulate_telemetry or args.no_sync):
        parser.error("--demo-http exige --simulate-telemetry e envio habilitado")
    if args.calibrate and args.calibration_file is None:
        args.calibration_file = Path(data_directory(args)) / "calibration-driver.json"
    return args


def data_directory(args):
    default = "dados-simulados" if args.simulate_telemetry else "dados"
    return os.environ.get("VGUARD_DATA", default)


def run(argv=None):
    args = parse_args(argv)
    manager = EventManager(data_directory(args))
    reader = None
    gps = None
    simulation = None
    publisher = None
    sender = None
    vision = None
    monitor = PiMonitor()

    try:
        monitor.start()
        if args.simulate_telemetry:
            log.info("modo semissimulado: IMU, OBD-II e GPS fictícios")
            simulation = SimulatedTelemetry(manager)
            simulation.start()
        else:
            if not args.no_esp32:
                reader = Esp32Reader(manager)
                reader.start()
            if not args.no_gps:
                gps = GpsReader(manager)
                gps.start()
        if not args.no_sync:
            publisher = HttpDemoPublisher(manager) if args.demo_http else MqttPublisher(manager)
            publisher.start()
            sender = EventSender(manager, publisher)
            sender.start()
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
        # O envio antes do publicador, que ele usa.
        for worker in (reader, gps, simulation, sender, publisher, monitor):
            if worker is not None:
                worker.stop()
        sent = sender.summary() if sender is not None else None     # antes de fechar o banco
        manager.close()
        if vision is not None:
            parts = [vision.summary()]
            if reader is not None:
                parts.append(f"ESP32: lost {reader.link.lost}, corrupt {reader.link.corrupt}")
            if publisher is not None:
                parts.append(publisher.summary())
            if sent:
                parts.append(sent)
            parts.append(monitor.summary())
            log.info("resumo: %s", "; ".join(parts))


def _request_stop(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _request_stop)
    run()
