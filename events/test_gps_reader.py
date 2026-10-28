"""
Verificação do leitor do GPS com um receptor falso num pty (só Linux ou
macOS). Executar:

    python3 test_gps_reader.py
"""

import os
import pty
import tempfile
import time
from functools import reduce

from event_manager import EventManager
from gps_reader import GpsReader, parse_rmc


def nmea(body):
    """Acrescenta o $ e o checksum, como o receptor faz."""
    checksum = reduce(lambda acc, c: acc ^ ord(c), body, 0)
    return "$%s*%02X\r\n" % (body, checksum)


# Exemplo clássico da norma: 48°07.038' N, 11°31.000' E, 22,4 nós.
FIX = "GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230394,003.1,W"
# Perto da UTFPR, hemisférios sul e oeste, receptor multiconstelação.
UTFPR = "GNRMC,141500.00,A,2526.580,S,04915.660,W,10.0,90.0,281026,,,A"
NO_FIX = "GNRMC,141501.00,V,,,,,,,281026,,,N"


def test_parse_rmc():
    assert nmea(FIX) == "$" + FIX + "*6A\r\n"   # checksum da norma
    assert parse_rmc(nmea(FIX)) == {"lat": 48.1173, "lon": 11.516667, "speed": 41.5}
    assert parse_rmc(nmea(UTFPR)) == {"lat": -25.443, "lon": -49.261, "speed": 18.5}

    # Sem fix: amostra vazia, para o evento não levar posição velha.
    assert parse_rmc(nmea(NO_FIX)) == {}

    # Outras frases, checksum errado e linha cortada são ignoradas.
    assert parse_rmc(nmea("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,,,,")) is None
    assert parse_rmc("$" + FIX + "*00") is None
    assert parse_rmc("$GPRMC,123519,A,48") is None
    assert parse_rmc("") is None


def test_with_fake_receiver():
    data_dir = tempfile.mkdtemp()
    manager = EventManager(data_dir)
    master, slave = pty.openpty()

    reader = GpsReader(manager, os.ttyname(slave))
    reader.start()
    # Ao abrir a porta, o tty.setraw descarta o que já estava no buffer:
    # escrever antes disso perderia as frases. O receptor real repete a cada 1 s.
    time.sleep(0.5)
    try:
        # Lixo do boot e uma linha partida ao meio antes das frases válidas.
        os.write(master, b"\x00\xff lixo\r\n")
        line = nmea(UTFPR).encode()
        os.write(master, line[:20])
        time.sleep(0.2)
        os.write(master, line[20:])
        time.sleep(0.5)
        with_fix = manager.record_event("fadiga", 0.9)

        os.write(master, nmea(NO_FIX).encode())
        time.sleep(0.5)
        without_fix = manager.record_event("distracao", 0.9)
    finally:
        reader.stop()
        os.close(master)
        os.close(slave)

    manager.close()
    events = {e["id"]: e["data"] for e in EventManager(data_dir).pending_events()}

    gps = events[with_fix]["gps"]
    assert (gps["lat"], gps["lon"], gps["speed"]) == (-25.443, -49.261, 18.5)

    # Perdeu o fix: o evento fica só com o horário.
    assert set(events[without_fix]["gps"]) == {"ts"}

    print("OK")


if __name__ == "__main__":
    test_parse_rmc()
    test_with_fake_receiver()
