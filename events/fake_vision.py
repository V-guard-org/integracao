"""
Uma visão falsa para os testes do VisionProcess (events/test_vision_process.py),
como o ESP32 falso do ticket 1: fala o protocolo do canal (vision/protocol.py)
e faz o que o plano manda, sem câmera nem MediaPipe.

    python3 fake_vision.py --channel-fd N --plans "exit:1|pronto,fps:13.9,hang" --count ARQUIVO

Cada tentativa (o principal reinicia a visão) usa o plano seguinte da lista,
separada por "|"; a última se repete. Os passos, separados por vírgula:

    pronto[:versao]  fps:VALOR  evento:TIPO  evento-png  evento-cortado  lixo
    sleep:SEGUNDOS   hang       exit:CODIGO  kill-self
"""

import argparse
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "vision"))

import protocol  # noqa: E402

JPEG = b"\xff\xd8" + b"x" * 100 + b"\xff\xd9"


def attempt_number(path):
    """Quantas vezes esta visão falsa já começou (um contador num arquivo)."""
    if not path:
        return 0
    number = 0
    if os.path.exists(path):
        with open(path) as stream:
            number = int(stream.read() or 0)
    with open(path, "w") as stream:
        stream.write(str(number + 1))
    return number


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel-fd", type=int, required=True)
    parser.add_argument("--plans", default="pronto,exit:0")
    parser.add_argument("--count", help="arquivo que conta as tentativas")
    parser.add_argument("--ignore-term", action="store_true", help="não sai no SIGTERM")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if args.ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(protocol.EXIT_TERMINATED))

    plans = args.plans.split("|")
    plan = plans[min(attempt_number(args.count), len(plans) - 1)]
    writer = protocol.Writer(args.channel_fd)
    for step in plan.split(","):
        name, _, value = step.partition(":")
        if name == "pronto":
            version = int(value) if value else protocol.VERSION
            writer.send(protocol.encode({"msg": "pronto", "versao": version}))
        elif name == "fps":
            writer.fps(float(value))
        elif name == "evento":
            writer.event(time.time(), value, 0.8, JPEG)
        elif name == "evento-cortado":      # o JPEG pela metade, e a visão sai
            writer.send(protocol.event(time.time(), "fadiga", 0.8, JPEG)[:-20])
            return 1
        elif name == "evento-png":
            writer.event(time.time(), "fadiga", 0.8, b"\x89PNG nao e JPEG")
        elif name == "lixo":
            writer.send(b"isto nao e json\n")
        elif name == "sleep":
            time.sleep(float(value))
        elif name == "hang":
            while True:
                time.sleep(0.1)
        elif name == "exit":
            return int(value)
        elif name == "kill-self":
            os.kill(os.getpid(), signal.SIGKILL)
    return protocol.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
