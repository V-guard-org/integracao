"""
V-GUARD — Leitor do GPS (RF02).

Lê as frases NMEA do receptor GNSS pela UART e entrega a posição ao
EventManager, no formato que o backend e o dashboard esperam:

    GNSS --NMEA--> GpsReader --update("gps", {"lat", "lon", "speed"})--> EventManager

Só a frase RMC é usada: ela traz posição, velocidade e se há fix. Sem
fix, sai {} (como o OBD com o carro desligado), para o evento não levar
uma posição velha.

    reader = GpsReader(manager)
    reader.start()
"""

import os
import termios
from functools import reduce
from operator import xor

from uart_reader import UartReader


# ============================================================
# CONFIGURAÇÃO
# ============================================================

# UART da Pi ligada ao GNSS (pinos 8 e 10). A confirmar com o esquemático.
PORT = os.environ.get("VGUARD_GPS", "/dev/serial0")

# 9600 baud é o padrão de fábrica dos receptores NMEA comuns.
BAUD = getattr(termios, "B" + os.environ.get("VGUARD_GPS_BAUD", "9600"))

# Uma linha NMEA tem no máximo 82 caracteres; lixo maior é descartado.
MAX_LINE = 256

KNOT_KMH = 1.852


def _degrees(value, hemisphere):
    """'4807.038' + 'N' (graus e minutos) vira 48.1173 (graus decimais)."""
    dot = value.index(".")
    degrees = int(value[:dot - 2]) + float(value[dot - 2:]) / 60
    return -degrees if hemisphere in ("S", "W") else degrees


def parse_rmc(line):
    """
    Uma linha NMEA vira a amostra de GPS:
    - dict com lat, lon e speed (km/h) se for RMC com fix;
    - {} se for RMC sem fix;
    - None para outras frases, checksum errado ou linha truncada.
    """
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return None

    body, checksum = line[1:].rsplit("*", 1)
    if checksum[:2].upper() != "%02X" % reduce(xor, map(ord, body), 0):
        return None

    fields = body.split(",")
    # $GPRMC, $GNRMC etc.: o talker muda conforme a constelação
    if len(fields) < 8 or not fields[0].endswith("RMC"):
        return None
    if fields[2] != "A":
        return {}

    try:
        return {
            "lat": round(_degrees(fields[3], fields[4]), 6),
            "lon": round(_degrees(fields[5], fields[6]), 6),
            "speed": round(float(fields[7] or 0) * KNOT_KMH, 1),
        }
    except ValueError:
        return {}


class GpsReader(UartReader):
    """Reaproveita a thread, o select e a reabertura do UartReader; só o pump muda."""

    def __init__(self, manager, port=PORT, baud=BAUD):
        super().__init__(manager, port, baud)
        self._pending = b""

    def pump(self, data):
        """Bytes lidos viram update() a cada linha RMC. Nada a escrever no GPS."""
        *lines, self._pending = (self._pending + data).split(b"\n")
        if len(self._pending) > MAX_LINE:
            self._pending = b""
        for line in lines:
            sample = parse_rmc(line.decode("ascii", "replace"))
            if sample is not None:
                self.manager.update("gps", sample)
        return b""
