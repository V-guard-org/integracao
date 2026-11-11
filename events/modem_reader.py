"""
V-GUARD — o leitor do modem A7670SA pela porta AT (ticket 6, Issue #21): o
GNSS e a hora de reserva, no lugar do GPS por NMEA.

    modem --AT--> ModemReader --update("gps", {"lat", "lon", "speed"})--> EventManager

A cada abertura da porta:

    1. AT a cada 1 s até o OK (nos primeiros segundos a porta não aceita escrita);
    2. ATE0;
    3. AT+CGNSSPWR?; desligado, AT+CGNSSPWR=1 (o GNSS volta desligado a cada
       partida do modem); depois AT+CGNSSPWR? a cada 2 s até ligar, por até 60 s;
    4. AT+CGNSSINFO a 1 Hz: com posição, update("gps", {...}); sem posição,
       update("gps", {}), o sensor sem dado do contrato; ERROR, o GNSS
       desligou, e a partida volta ao passo 3;
    5. AT+CCLK? a cada 60 s: a hora da operadora.

As horas do GNSS e da operadora ficam em gnss_time e operator_time, (epoch
UTC, time.monotonic() da leitura), para o ticket 5 (o time_source) e o ticket
7 (o chrony). A diferença para o relógio da Pi vai ao log.

    reader = ModemReader(manager)
    reader.start()
"""

import calendar
import glob
import logging
import os
import re
import termios
import time

from serial_port import SerialReader

log = logging.getLogger("modem")


# ============================================================
# CONFIGURAÇÃO
# ============================================================

# A porta AT: a regra udev do ticket 7 ou, sem ela, a interface USB 04 do
# modem (os números ttyUSB andam conforme o que está na USB).
PORT = os.environ.get("VGUARD_MODEM_AT", "")
UDEV_PORT = "/dev/vguard-modem-at"
BY_ID = "/dev/serial/by-id/*SIMCom*-if04-port0"

BAUD = termios.B115200

KNOT_KMH = 1.852

HELLO_INTERVAL = 1.0        # o AT até o modem responder
POWER_POLL = 2.0            # o AT+CGNSSPWR? enquanto o GNSS liga
POWER_TIMEOUT = 60.0        # quanto esperar o GNSS ligar
FIX_INTERVAL = 1.0          # o AT+CGNSSINFO
CLOCK_INTERVAL = 60.0       # o AT+CCLK?
REPLY_TIMEOUT = 2.0         # a resposta de um comando
OFFSET_LOG_INTERVAL = 600.0 # a diferença entre o relógio da Pi e o GNSS no log
MAX_LINE = 512


def find_port():
    """A porta AT do modem: VGUARD_MODEM_AT, a regra udev, ou a interface 04 pelo by-id."""
    if PORT:
        return PORT
    if os.path.exists(UDEV_PORT):
        return UDEV_PORT
    found = sorted(glob.glob(BY_ID))
    return found[0] if found else UDEV_PORT


# ============================================================
# AS RESPOSTAS
# ============================================================

def parse_cgnssinfo(line):
    """
    Uma linha +CGNSSINFO vira (amostra, info):
    - com posição: ({"lat", "lon", "speed"}, {"utc", "satellites", "hdop"});
    - sem posição: ({}, None);
    - None, se não der para ler.

    Os campos, conferidos no A7670SA em 10/11:
    modo,GPS,GLONASS,BEIDOU,GALILEO,lat,N/S,lon,E/W,ddmmaa,hhmmss.ss,altitude,velocidade (nós),rumo,PDOP,HDOP,VDOP
    """
    if not line.startswith("+CGNSSINFO:"):
        return None
    fields = [field.strip() for field in line.split(":", 1)[1].split(",")]
    if len(fields) < 9:
        return None
    if not any(fields[:9]):
        return {}, None
    if len(fields) < 13:
        return None
    try:
        lat = float(fields[5]) * (-1 if fields[6] == "S" else 1)
        lon = float(fields[7]) * (-1 if fields[8] == "W" else 1)
        speed = float(fields[12] or 0) * KNOT_KMH
        utc = None
        if re.fullmatch(r"\d{6}", fields[9]) and re.fullmatch(r"\d{6}(\.\d+)?", fields[10]):
            day, month, year = int(fields[9][:2]), int(fields[9][2:4]), 2000 + int(fields[9][4:])
            hour, minute, second = int(fields[10][:2]), int(fields[10][2:4]), float(fields[10][4:])
            utc = calendar.timegm((year, month, day, hour, minute, 0)) + second
    except ValueError:
        return None
    satellites = int(fields[1]) if fields[1].isdigit() else None
    hdop = float(fields[15]) if len(fields) > 15 and fields[15] else None
    sample = {"lat": round(lat, 6), "lon": round(lon, 6), "speed": round(speed, 1)}
    return sample, {"utc": utc, "satellites": satellites, "hdop": hdop}


def parse_cclk(line):
    """'+CCLK: "26/11/10,23:48:05-12"' (hora local, fuso em quartos de hora) vira o epoch UTC. None se não der."""
    match = re.fullmatch(r'\+CCLK:\s*"(\d\d)/(\d\d)/(\d\d),(\d\d):(\d\d):(\d\d)([+-]\d+)"', line.strip())
    if not match:
        return None
    year, month, day, hour, minute, second, zone = (int(group) for group in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return calendar.timegm((2000 + year, month, day, hour, minute, second)) - zone * 15 * 60


def parse_power(line):
    """'+CGNSSPWR: 1,0,1' vira True (ligado) ou False; o aviso '+CGNSSPWR: READY!' vira True. None se não for."""
    if not line.startswith("+CGNSSPWR:"):
        return None
    value = line.split(":", 1)[1].strip()
    if value.startswith("READY"):
        return True
    return value.split(",")[0] == "1"


# ============================================================
# O LEITOR
# ============================================================

class ModemReader(SerialReader):
    """
    O GNSS e a hora pela porta AT. gnss_time e operator_time: (epoch UTC,
    time.monotonic() da leitura), ou None antes da primeira.
    """

    def __init__(self, manager, port=None, clock=time.time, monotonic=time.monotonic,
                 hello_interval=HELLO_INTERVAL, power_poll=POWER_POLL, power_timeout=POWER_TIMEOUT,
                 fix_interval=FIX_INTERVAL, clock_interval=CLOCK_INTERVAL, reply_timeout=REPLY_TIMEOUT,
                 offset_log_interval=OFFSET_LOG_INTERVAL):
        self._auto_port = port is None
        super().__init__(port or find_port(), BAUD)
        self.manager = manager
        self.clock = clock
        self.monotonic = monotonic
        self.hello_interval = hello_interval
        self.power_poll = power_poll
        self.power_timeout = power_timeout
        self.fix_interval = fix_interval
        self.clock_interval = clock_interval
        self.reply_timeout = reply_timeout
        self.offset_log_interval = offset_log_interval

        self.gnss_time = None
        self.operator_time = None
        self.fixes = 0                  # linhas com posição desde o início
        self.first_fix_after = None     # segundos do GNSS ligado à primeira posição
        self._reset()

    def _reset(self):
        self._state = "hello"
        self._buffer = b""
        self._waiting = None            # (comando, monotonic do envio)
        self._lines = []                # as linhas da resposta em curso
        self._last_sent = {}            # comando -> monotonic do último envio
        self._power_since = None        # quando o GNSS foi mandado ligar
        self._has_fix = None            # None antes da primeira resposta
        self._no_fix_since = None
        self._bad_line = False
        self._last_offset_log = None
        self._operator_logged = False

    # ------------------------------------------------------------------ a porta

    def _read(self):
        if self._auto_port:
            self.port = find_port()     # o modem pode ter aparecido em outro ttyUSB
        super()._read()

    def opened(self):
        self._reset()

    def closed(self):
        if self._has_fix is not None or self._state == "fix":
            self.manager.update("gps", {})      # o modem sumiu: sem posição
        self._reset()

    # ------------------------------------------------------------------ o laço

    def pump(self, data):
        now = self.monotonic()
        self._buffer += data
        *lines, self._buffer = self._buffer.split(b"\n")
        if len(self._buffer) > MAX_LINE:
            self._buffer = b""
        for raw in lines:
            line = raw.decode("ascii", "replace").strip()
            if line:
                self._line(line, now)

        if self._waiting is not None:
            command, sent = self._waiting
            if now - sent < self.reply_timeout:
                return b""
            self._waiting = None            # sem resposta: o comando volta no próximo ciclo
            self._lines = []
            if self._state != "hello":
                log.warning("modem não respondeu ao %s; recomeçando a partida", command)
                self._state = "hello"
        return self._next(now)

    def _send(self, command, now):
        self._waiting = (command, now)
        self._lines = []
        self._last_sent[command] = now
        return (command + "\r").encode()

    def _due(self, command, interval, now):
        last = self._last_sent.get(command)
        return last is None or now - last >= interval

    def _next(self, now):
        """O próximo comando, conforme o estado. Só um por vez."""
        state = self._state
        if state == "hello":
            return self._send("AT", now) if self._due("AT", self.hello_interval, now) else b""
        if state == "echo":
            return self._send("ATE0", now)
        if state == "power":
            return self._send("AT+CGNSSPWR?", now)
        if state == "power_on":
            return self._send("AT+CGNSSPWR=1", now)
        if state == "powering":
            if now - self._power_since >= self.power_timeout:
                log.warning("GNSS não ligou em %.0f s; tentando de novo", self.power_timeout)
                self._state = "power"
                return b""
            if self._due("AT+CGNSSPWR?", self.power_poll, now):
                return self._send("AT+CGNSSPWR?", now)
            return b""
        if state == "fix":
            if self._due("AT+CCLK?", self.clock_interval, now):
                return self._send("AT+CCLK?", now)
            if self._due("AT+CGNSSINFO", self.fix_interval, now):
                return self._send("AT+CGNSSINFO", now)
        return b""

    # ------------------------------------------------------------------ as respostas

    def _line(self, line, now):
        if line.startswith("+CGNSSPWR: READY"):
            if self._state == "powering":
                self._gnss_on(now)
            return
        if self._waiting is None:
            return                          # outro aviso do modem (RDY, SMS Ready...)
        if line in ("OK", "ERROR") or line.startswith("+CME ERROR"):
            command, _ = self._waiting
            lines, self._lines, self._waiting = self._lines, [], None
            self._reply(command, lines, line == "OK", now)
        elif line != self._waiting[0]:      # o eco, antes do ATE0
            self._lines.append(line)

    def _reply(self, command, lines, ok, now):
        if command == "AT":
            if ok:
                log.info("porta AT pronta (%s)", self.port)
                self._state = "echo"
        elif command == "ATE0":
            self._state = "power"
        elif command == "AT+CGNSSPWR?":
            on = next((value for value in map(parse_power, lines) if value is not None), None)
            if on:
                if self._state == "powering":
                    self._gnss_on(now)
                elif self._state == "power":
                    self._state = "fix"         # já estava ligado
                    self._power_since = now
            elif self._state == "power":
                self._state = "power_on"
        elif command == "AT+CGNSSPWR=1":
            self._state = "powering"
            self._power_since = now
        elif command == "AT+CGNSSINFO":
            if not ok:
                if self._has_fix is not None:
                    log.warning("GNSS desligou (ERROR); ligando de novo")
                self._has_fix = None
                self.manager.update("gps", {})
                self._state = "power"
                return
            for line in lines:
                parsed = parse_cgnssinfo(line)
                if parsed is None:
                    if not self._bad_line:
                        self._bad_line = True
                        log.warning("linha do GNSS que não deu para ler: %s", line[:120])
                    continue
                self._bad_line = False
                self._position(*parsed, now)
                break
        elif command == "AT+CCLK?":
            for line in lines:
                utc = parse_cclk(line)
                if utc is not None:
                    self.operator_time = (utc, now)
                    if not self._operator_logged:
                        self._operator_logged = True
                        log.info("relógio da Pi − operadora: %+.1f s", self.clock() - utc)
                    break

    def _gnss_on(self, now):
        # O _power_since continua no AT+CGNSSPWR=1: a primeira posição conta dele.
        log.info("GNSS ligado em %.0f s", now - self._power_since)
        self._state = "fix"

    def _position(self, sample, info, now):
        self.manager.update("gps", sample)
        if not sample:
            if self._has_fix:
                log.warning("GNSS: sem posição")
                self._no_fix_since = now
            elif self._has_fix is None:
                self._no_fix_since = now
            self._has_fix = False
            return
        self.fixes += 1
        if self._has_fix is False and self.first_fix_after is not None:
            log.info("GNSS: posição de volta depois de %.0f s", now - self._no_fix_since)
        if self.first_fix_after is None:
            self.first_fix_after = now - (self._power_since or now)
            log.info("GNSS: primeira posição em %.0f s (%s satélites GPS, HDOP %s)",
                     self.first_fix_after, info["satellites"], info["hdop"])
        self._has_fix = True
        if info["utc"] is not None:
            self.gnss_time = (info["utc"], now)
            if self._last_offset_log is None or now - self._last_offset_log >= self.offset_log_interval:
                self._last_offset_log = now
                log.info("relógio da Pi − GNSS: %+.1f s", self.clock() - info["utc"])
