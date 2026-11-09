"""
O canal da visão para o programa principal (proposta §6.9, Issue #10), só com
a biblioteca padrão: os dois lados importam este arquivo.

Um pipe dedicado, de mão única. Cada mensagem é uma linha de JSON terminada
em \\n, com no máximo 1 KB; só no `evento`, os bytes do JPEG vêm logo depois
da linha. Sem checksum: o pipe é local, e a falha real é a mensagem cortada,
que o tamanho do JPEG pega.

    {"msg":"pronto","versao":1}
    {"msg":"fps","fps":13.9}
    {"msg":"evento","ts":1793980812.437,"type":"fadiga","score":0.82,"jpeg":40074} + 40074 bytes
"""

from dataclasses import dataclass
import json
import math
import os

VERSION = 1
MAX_LINE = 1024                 # bytes, com o \n
MAX_JPEG = 5 * 1024 * 1024      # o limite do contrato com o servidor
JPEG_START = b"\xff\xd8"

# Códigos de saída do programa da visão
EXIT_OK = 0                     # fim normal (Q na tela ou --max-frames): o programa todo encerra
EXIT_CONFIG = 78                # EX_CONFIG do sysexits.h: repetir não resolve, sem reinício
EXIT_TERMINATED = 143           # 128 + SIGTERM: conta como queda se não foi o principal que mandou


def _number(value):
    """Número de verdade (bool não conta) e finito."""
    return type(value) in (int, float) and math.isfinite(value)


def encode(message, payload=b""):
    """Uma mensagem pronta para o pipe: a linha de JSON e, no evento, o JPEG."""
    line = json.dumps(message, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii") + b"\n"
    if len(line) > MAX_LINE:
        raise ValueError(f"mensagem de {len(line)} bytes, acima de {MAX_LINE}")
    return line + payload


def ready():
    return encode({"msg": "pronto", "versao": VERSION})


def fps(value):
    return encode({"msg": "fps", "fps": round(float(value), 1)})


def event(ts, type, score, jpeg):
    return encode({"msg": "evento", "ts": round(float(ts), 3), "type": type,
                   "score": round(float(score), 3), "jpeg": len(jpeg)}, bytes(jpeg))


class Writer:
    """A ponta da visão: escreve cada mensagem inteira no descritor do canal."""

    def __init__(self, fd):
        self.fd = fd

    def send(self, data):
        view = memoryview(data)
        while view:
            view = view[os.write(self.fd, view):]

    def ready(self):
        self.send(ready())

    def fps(self, value):
        self.send(fps(value))

    def event(self, ts, type, score, jpeg):
        self.send(event(ts, type, score, jpeg))


@dataclass(frozen=True)
class Message:
    kind: str           # "pronto", "fps" ou "evento"
    data: dict          # a linha de JSON, já conferida
    jpeg: bytes = b""   # só no evento


class Reader:
    """
    A ponta do principal: recebe os bytes do pipe aos pedaços (`feed`) e
    devolve as mensagens conferidas e os problemas, em texto para o log.
    No fim do pipe, `close` descarta o que ficou pela metade.
    """

    def __init__(self):
        self._buffer = bytearray()
        self._event = None          # cabeçalho de um evento, esperando os bytes do JPEG
        self._skip = 0              # bytes de um JPEG descartado, a jogar fora
        self._long_line = False     # no meio de uma linha acima de 1 KB, até o próximo \n

    def feed(self, data):
        self._buffer += data
        messages, problems = [], []
        while True:
            if self._skip:
                count = min(self._skip, len(self._buffer))
                del self._buffer[:count]
                self._skip -= count
                if self._skip:
                    break
                continue
            if self._event is not None:
                size = self._event["jpeg"]
                if len(self._buffer) < size:
                    break
                jpeg = bytes(self._buffer[:size])
                del self._buffer[:size]
                header, self._event = self._event, None
                if jpeg.startswith(JPEG_START):
                    messages.append(Message("evento", header, jpeg))
                else:
                    problems.append("evento descartado: o JPEG não começa com FF D8")
                continue
            end = self._buffer.find(b"\n")
            if self._long_line:
                if end < 0:
                    self._buffer.clear()
                    break
                del self._buffer[:end + 1]
                self._long_line = False
                continue
            if end < 0:
                if len(self._buffer) >= MAX_LINE:
                    problems.append("mensagem acima de 1 KB descartada")
                    self._buffer.clear()
                    self._long_line = True
                break
            line = bytes(self._buffer[:end])
            del self._buffer[:end + 1]
            if len(line) + 1 > MAX_LINE:
                problems.append("mensagem acima de 1 KB descartada")
                continue
            message, problem = self._parse(line)
            if problem:
                problems.append(problem)
            if message is not None:
                messages.append(message)
        return messages, problems

    def close(self):
        """O pipe fechou (a visão saiu): o que ficou pela metade é descartado."""
        cut = bool(self._buffer) or self._event is not None or self._skip
        self._buffer.clear()
        self._event, self._skip, self._long_line = None, 0, False
        return ["mensagem cortada no fim do canal (a visão saiu): descartada"] if cut else []

    def _parse(self, line):
        """Uma linha de JSON: a mensagem (ou None) e um problema (ou None)."""
        try:
            data = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            return None, "mensagem com JSON inválido descartada"
        if not isinstance(data, dict) or not isinstance(data.get("msg"), str):
            return None, "mensagem sem o campo msg descartada"
        kind = data["msg"]
        if kind == "pronto":
            if type(data.get("versao")) is not int:
                return None, "pronto sem a versao descartado"
            return Message(kind, data), None
        if kind == "fps":
            if not _number(data.get("fps")) or data["fps"] < 0:
                return None, "fps sem um valor válido descartado"
            return Message(kind, data), None
        if kind == "evento":
            size = data.get("jpeg")
            if type(size) is not int or size <= 0:
                return None, "evento sem o tamanho do JPEG descartado"
            if size > MAX_JPEG:
                self._skip = size
                return None, f"evento descartado: JPEG de {size} bytes, acima de 5 MB"
            if not (_number(data.get("ts")) and data["ts"] > 0
                    and isinstance(data.get("type"), str) and data["type"]
                    and _number(data.get("score"))):
                self._skip = size
                return None, "evento com campo faltando ou inválido descartado"
            self._event = data
            return None, None
        return None, None       # msg desconhecido: ignorado, como o tipo desconhecido da §5.1
