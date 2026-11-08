"""
V-GUARD — Leitor do ESP32 pela USB (protocolo de texto, proposta §5).

O ESP32 manda linhas $<tipo>,<campos...>*<hh>; a Pi confere cada uma,
manda a hora ($H) e a taxa do OBD ($C) e entrega as amostras ao
EventManager:

    ESP32 --USB--> Esp32Reader --bytes--> Esp32Link --update("imu"|"obd")--> EventManager
          <------- $H e $C <-------------'

O Esp32Link é a conversa com o ESP32 e não conhece a porta: recebe os
bytes e os relógios e devolve o que mandar. Assim se testa sem o ESP32.
A referência de como a Pi conversa com o ESP32 é a ferramentas/bancada.py
do firmware_esp32.

    reader = Esp32Reader(manager)
    reader.start()
    reader.health()   # {"connected": True, "lost": 0, "corrupt": 0}
"""

import dataclasses
import logging
import os
import re
import subprocess
import termios
import time

from serial_port import SerialReader


# ============================================================
# CONFIGURAÇÃO
# ============================================================

# Nome fixo dado pela regra udev (proposta §6.4). No notebook: /dev/ttyUSB0.
PORT = os.environ.get("VGUARD_ESP32", "/dev/vguard-esp32")
BAUD = termios.B115200

# Taxa do OBD que a Pi pede ao ESP32, em pedidos por segundo no total
# (proposta §5.4): de 1 a 20, com até 1 casa decimal.
OBD_HZ = os.environ.get("VGUARD_OBD_HZ", "12.5")

# Linha mais longa aceita, do $ ao último dígito do hh (proposta §5.1).
MAX_LINE = 120

# Bytes sem \n que se acumulam antes de virarem uma linha corrompida.
MAX_PENDING = 4 * MAX_LINE

# O que chega nos primeiros instantes depois de abrir a porta é descartado.
# Na bancada de 08/11, o CP2102 entregou, em ~60% das aberturas, uma rajada
# de até ~15 KB em 50 ms: blocos de 32 bytes repetidos, com pedaços de
# linhas velhas. Depois dela, o fluxo é normal. Ler e jogar fora os
# primeiros 0,2 s acabou com a rajada (24 aberturas, nenhuma recusada).
OPEN_DISCARD_S = 0.2

# As amostras só são aceitas depois desta quantidade de linhas válidas
# seguidas, contada de novo ao abrir a porta e no reinício do ESP32.
LINES_TO_ACCEPT = 10

# O $H é reenviado de tempos em tempos, para corrigir o relógio do ESP32.
HOUR_INTERVAL_S = 60.0

# Depois de um $H ou de um $C, um $S só dispara outro passado este tempo:
# o $S que já vinha a caminho não deve repetir o envio.
RESEND_PAUSE_S = 2.0

# Sem linha válida por este tempo, o ESP32 é dado como desaparecido.
CONNECTED_S = 3.0

# A checagem da hora é um subprocesso: no máximo uma por este intervalo.
CLOCK_CHECK_S = 1.0

# Valor do OBD mais velho que isto fica de fora da amostra. Com os 8 PIDs
# a 12,5 pedidos/s, cada um chega a cada ~0,64 s. Valor INICIAL, a
# calibrar no carro.
OBD_MAX_AGE_MS = 3000

# Campos do $O e o tipo que vai ao servidor (contrato).
OBD_FIELDS = {
    "rpm": float, "speed": int, "coolant": int, "load": float,
    "map": int, "iat": int, "throttle": float, "voltage": float,
}

# Campos do $S: $S,<seq>,<t>,<uptime_s>,<estado>,<obd>,<imu>,<sd>,<sd_livre>,<obd_hz>,<descartadas>
STATUS_FIELDS = 11
STATE, OBD, IMU, SD, OBD_HZ_FIELD = 4, 5, 6, 7, 9
# Campos do $S cuja mudança vai ao log, pela posição.
STATUS_CHANGES = {"estado": STATE, "obd": OBD, "imu": IMU, "sd": SD, "obd_hz": OBD_HZ_FIELD}

SEQ_MASK = 0xFFFFFFFF

log = logging.getLogger("esp32")


# ============================================================
# MOLDURA
# ============================================================

def checksum(body):
    """O XOR de todos os bytes: o checksum do NMEA."""
    result = 0
    for byte in body:
        result ^= byte
    return result


def frame(text):
    """Põe a moldura numa linha para o ESP32: "H,1793980812437" vira b"$H,1793980812437*52\\n"."""
    body = text.encode("ascii")
    return b"$" + body + b"*%02X\n" % checksum(body)


@dataclasses.dataclass
class Line:
    type: str
    seq: int
    fields: list   # todos os campos, a começar pelo tipo


def parse_line(raw):
    """
    Confere a moldura $<tipo>,<campos...>*<hh> (proposta §5.1). Devolve a
    Line ou, se a linha for recusada, o motivo (str).
    """
    line = raw[:-1] if raw.endswith(b"\r") else raw
    if len(line) > MAX_LINE:
        return "mais de %d caracteres" % MAX_LINE
    if not line.startswith(b"$"):
        return "sem $"
    star = line.rfind(b"*")
    hh = line[star + 1:]
    if star < 0 or len(hh) != 2 or not all(c in b"0123456789ABCDEF" for c in hh):
        return "sem *hh"
    body = line[1:star]
    if checksum(body) != int(hh, 16):
        return "checksum errado"
    fields = body.decode("ascii", "replace").split(",")
    if len(fields) < 2 or not fields[1].isdigit():
        return "sem a seq"
    return Line(fields[0], int(fields[1]), fields)


def parse_obd_hz(text):
    """
    "12.5" vira 12.5; fora de 1 a 20 ou com mais de 1 casa decimal, None
    (as mesmas regras do $C no ESP32, proposta §5.4).
    """
    if not re.fullmatch(r"\d{1,2}(\.\d)?", text.strip()):
        return None
    value = float(text)
    return value if 1 <= value <= 20 else None


def clock_synchronized():
    """
    A hora da Pi está certa? Até o ticket da inicialização trocar pela consulta
    ao chrony (proposta §6.2), vale o NTPSynchronized do systemd, que o chrony
    (com o rtcsync do Debian) e o systemd-timesyncd mantêm.
    """
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=2
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() == "yes"


# ============================================================
# CONVERSA COM O ESP32
# ============================================================

class Esp32Link:
    """
    A conversa da Pi com o ESP32 (proposta §5.5), sem a porta: confere as
    linhas, conta as perdas, decide o $H e o $C e entrega as amostras.

    `now` é o relógio monotônico (intervalos) e `wall` o time.time() (o ts
    das amostras e a hora do $H), os dois em segundos.
    """

    def __init__(self, manager, clock_ok=clock_synchronized, obd_hz=OBD_HZ):
        self.manager = manager
        self._clock_ok = clock_ok
        self.obd_hz = parse_obd_hz(obd_hz)
        if self.obd_hz is None:
            log.error("VGUARD_OBD_HZ=%r inválido (de 1 a 20, até 1 casa): nenhum $C", obd_hz)

        # Contagens desde que o processo começou (saúde, contrato esp32.*).
        self.lost = 0
        self.corrupt = 0
        # Para a ferramenta de bancada e o log.
        self.ignored = 0            # texto do boot, fora do protocolo
        self.discarded_bytes = 0    # jogados fora logo depois de abrir a porta
        self.restarts = 0
        self.hours_sent = 0
        self.rates_sent = 0
        self.counts = {}            # tipo -> linhas válidas
        self.first_seen = {}        # tipo -> now da primeira linha
        self.last_seen = {}         # tipo -> now da última linha

        self._last_valid_at = None
        self._present = False       # o ESP32 está mandando linhas (para avisar uma vez quando some)
        self._clock_checked_at = None
        self._clock_answer = False
        self._warned_clock = False
        self._last_hour = None
        self._last_rate = None
        self._hour_wanted = False
        self._rate_wanted = None    # obd_hz que o $S mostrava, se um $C deve sair
        self._last_corrupt_log = None
        self._status = {}           # nome -> último valor do $S
        self.port_opened(None)

    # --------------------------------------------------------
    # PORTA
    # --------------------------------------------------------

    def port_opened(self, now):
        """
        A porta abriu em `now` (None: sem o descarte da abertura). O que chegar
        nos primeiros OPEN_DISCARD_S é jogado fora, e o que chegar antes da
        primeira linha válida é boot.
        """
        self._discard_until = None if now is None else now + OPEN_DISCARD_S
        self._buffer = b""
        self._rejected = []         # motivos das recusadas antes da próxima válida
        self._last_seq = None
        # A primeira linha válida dispara um $H, para o ESP32 que já estava
        # em ATIVO (a Pi reiniciou e ele não) acertar o relógio.
        self._first_line = True
        self._reset_session()

    def port_closed(self, wall):
        """A porta fechou: o ESP32 sumiu."""
        self._lose(wall, "porta fechada")

    def health(self, now):
        """O esp32 da saúde (contrato)."""
        return {"connected": self.connected(now), "lost": self.lost, "corrupt": self.corrupt}

    def connected(self, now):
        return self._last_valid_at is not None and now - self._last_valid_at <= CONNECTED_S

    # --------------------------------------------------------
    # ENTRADA
    # --------------------------------------------------------

    def feed(self, data, now, wall):
        """Bytes lidos (pode ser b"") viram amostras; devolve o que mandar ao ESP32."""
        if self._discard_until is not None and now < self._discard_until:
            self.discarded_bytes += len(data)
            data = b""
        self._buffer += data
        *lines, self._buffer = self._buffer.split(b"\n")
        for raw in lines:
            self._line(raw, now, wall)
        if len(self._buffer) > MAX_PENDING:
            self._rejected.append("mais de %d caracteres" % MAX_LINE)
            self._buffer = b""

        if self._present and not self.connected(now):
            self._lose(wall, "nenhuma linha válida em %g s" % CONNECTED_S)
        return self._outgoing(now, wall)

    def _reset_session(self):
        """Porta aberta ou ESP32 reiniciado: a validação recomeça."""
        self._run = 0               # linhas válidas seguidas
        self.accepted = False
        self._obd = {}              # campo -> (valor, now da chegada)
        self._a_since_status = False

    def _line(self, raw, now, wall):
        result = parse_line(raw)
        if isinstance(result, str):
            # Só a próxima linha válida diz se era texto do boot ou corrompida.
            self._rejected.append(result)
            return
        line = result

        expected = None if self._last_seq is None else (self._last_seq + 1) & SEQ_MASK
        restart = expected is not None and line.seq < expected
        if expected is None or restart:
            self.ignored += len(self._rejected)
        elif self._rejected:
            self._corrupted(now)
        self._rejected = []

        if restart:
            self.restarts += 1
            log.info("ESP32 reiniciou: a seq voltou de %d para %d", self._last_seq, line.seq)
            self._mark_no_data(wall)
            self._reset_session()
            self._hour_wanted = True
        elif expected is not None and line.seq > expected:
            gap = line.seq - expected
            self.lost += gap
            log.warning("lacuna na seq: %d linha(s) perdida(s), de %d para %d",
                        gap, self._last_seq, line.seq)
            if not self.accepted:
                self._run = 0
        self._last_seq = line.seq

        if not self._present and self._last_valid_at is not None:
            log.info("ESP32 voltou a mandar linhas")
        self._present = True
        self._last_valid_at = now
        self.counts[line.type] = self.counts.get(line.type, 0) + 1
        self.first_seen.setdefault(line.type, now)
        self.last_seen[line.type] = now

        if self._first_line:
            self._first_line = False
            self._hour_wanted = True

        self._run += 1
        if not self.accepted and self._run >= LINES_TO_ACCEPT:
            self.accepted = True
            log.info("ESP32 validado: %d linhas válidas seguidas", LINES_TO_ACCEPT)

        handler = getattr(self, "_on_" + line.type, None)
        if handler is not None:   # tipo desconhecido: ignorado (proposta §5.1)
            try:
                handler(line, now, wall)
            except (ValueError, IndexError):
                self._corrupted(now, "campos inválidos no $" + line.type)

    def _corrupted(self, now, reason=None):
        """As recusadas entre duas válidas eram corrompidas. Avisa no máximo uma vez a cada 10 s."""
        reasons = self._rejected if reason is None else [reason]
        self.corrupt += len(reasons)
        if not self.accepted:
            self._run = 0
        if self._last_corrupt_log is None or now - self._last_corrupt_log >= 10:
            log.warning("linha do ESP32 descartada (%s); %d desde o início",
                        reasons[-1], self.corrupt)
            self._last_corrupt_log = now

    # --------------------------------------------------------
    # TIPOS
    # --------------------------------------------------------

    def _on_B(self, line, now, wall):
        firmware = line.fields[3] if len(line.fields) > 3 else "?"
        reason = line.fields[4] if len(line.fields) > 4 else "?"
        log.info("ESP32 ligou: firmware %s, motivo %s", firmware, reason)
        self._hour_wanted = True

    def _on_L(self, line, now, wall):
        level = line.fields[3] if len(line.fields) > 3 else "?"
        text = ",".join(line.fields[4:])
        if level == "ERRO":
            log.error("ESP32: %s", text)
        else:
            log.warning("ESP32: %s", text)

    def _on_P(self, line, now, wall):
        log.info("ESP32: PIDs suportados %s", " ".join(line.fields[3:]))

    def _on_A(self, line, now, wall):
        if len(line.fields) != 6:
            raise ValueError
        sample = dict(zip(("ax", "ay", "az"), map(float, line.fields[3:6])))
        self._a_since_status = True
        if self.accepted:
            self.manager.update("imu", sample, ts=wall)

    def _on_O(self, line, now, wall):
        if len(line.fields) != 5:
            raise ValueError
        field, text = line.fields[3], line.fields[4]
        if field not in OBD_FIELDS or not self.accepted:
            return
        self._obd[field] = (OBD_FIELDS[field](text), now)

        sample, ages = {}, {}
        for key, (value, arrived) in list(self._obd.items()):
            age = round((now - arrived) * 1000)
            if age > OBD_MAX_AGE_MS:
                del self._obd[key]
                continue
            sample[key] = value
            ages[key] = age
        sample["age_ms"] = ages
        self.manager.update("obd", sample, ts=wall)

    def _on_S(self, line, now, wall):
        if len(line.fields) != STATUS_FIELDS:
            raise ValueError
        fields = line.fields
        for name, position in STATUS_CHANGES.items():
            value, before = fields[position], self._status.get(name)
            if value != before:
                log.info("ESP32 %s: %s", name, value if before is None else before + " → " + value)
                self._status[name] = value

        if fields[STATE] == "ESPERA" and self._pause_over(self._last_hour, now):
            self._hour_wanted = True
        if (fields[STATE] == "ATIVO" and self.obd_hz is not None
                and float(fields[OBD_HZ_FIELD]) != self.obd_hz
                and self._pause_over(self._last_rate, now)):
            self._rate_wanted = fields[OBD_HZ_FIELD]

        # Sem dado: só o horário, como o GPS sem fix (contrato).
        if self.accepted:
            if fields[OBD] != "LENDO":
                self._obd = {}
                self.manager.update("obd", {}, ts=wall)
            if not self._a_since_status:
                self.manager.update("imu", {}, ts=wall)
        self._a_since_status = False

    # --------------------------------------------------------
    # SAÍDA
    # --------------------------------------------------------

    @staticmethod
    def _pause_over(last, now):
        return last is None or now - last >= RESEND_PAUSE_S

    def _outgoing(self, now, wall):
        out = b""
        if self._last_hour is not None and now - self._last_hour >= HOUR_INTERVAL_S:
            self._hour_wanted = True
        # Só para um ESP32 presente: o $H mandado para a porta muda se perderia.
        if self._hour_wanted and self.connected(now) and self._clock(now):
            out += frame("H,%d" % int(wall * 1000))
            self._hour_wanted = False
            self._last_hour = now
            self.hours_sent += 1
        if self._rate_wanted is not None:
            log.info("mandando $C,obd_hz,%g (o $S mostrava %s)", self.obd_hz, self._rate_wanted)
            out += frame("C,obd_hz,%g" % self.obd_hz)
            self._rate_wanted = None
            self._last_rate = now
            self.rates_sent += 1
        return out

    def _clock(self, now):
        """A hora da Pi está certa? Pergunta no máximo uma vez por CLOCK_CHECK_S."""
        if self._clock_checked_at is None or now - self._clock_checked_at >= CLOCK_CHECK_S:
            self._clock_checked_at = now
            answer = bool(self._clock_ok())
            if not answer and not self._warned_clock:
                log.warning("hora da Pi ainda não sincronizada: o $H espera")
                self._warned_clock = True
            elif answer and self._warned_clock:
                log.info("hora da Pi sincronizada")
                self._warned_clock = False
            self._clock_answer = answer
        return self._clock_answer

    # --------------------------------------------------------

    def _lose(self, wall, reason):
        """O ESP32 sumiu: avisa uma vez por episódio e marca o IMU e o OBD sem dado."""
        if not self._present:
            return
        log.warning("ESP32 desaparecido (%s)", reason)
        self._present = False
        self._mark_no_data(wall)

    def _mark_no_data(self, wall):
        if self.accepted:
            self._obd = {}
            self.manager.update("imu", {}, ts=wall)
            self.manager.update("obd", {}, ts=wall)


# ============================================================
# THREAD
# ============================================================

class Esp32Reader(SerialReader):
    """O Esp32Link numa thread, lendo a USB do ESP32."""

    def __init__(self, manager, port=PORT, clock_ok=clock_synchronized, obd_hz=OBD_HZ):
        super().__init__(port, BAUD)
        self.link = Esp32Link(manager, clock_ok, obd_hz)

    def opened(self):
        self.link.port_opened(time.monotonic())

    def closed(self):
        # No stop() a porta fecha de propósito: não é o ESP32 que sumiu.
        if not self._stop.is_set():
            self.link.port_closed(time.time())

    def pump(self, data):
        return self.link.feed(data, time.monotonic(), time.time())

    def health(self):
        return self.link.health(time.monotonic())
