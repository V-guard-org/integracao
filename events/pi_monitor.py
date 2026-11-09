"""
V-GUARD — Temperatura e throttled da Raspberry Pi (proposta §6.5).

A cada 10 s, numa thread, lê a temperatura da CPU e o get_throttled
(subtensão, frequência limitada, redução de clock e limite de temperatura),
escreve uma linha no log e guarda os valores para a saúde (ticket 5):

    vcgencmd --> read_sensors() --> PiMonitor.poll() --> log
                                         |
                                         +--> temperature, throttled (saúde)

    monitor = PiMonitor()
    monitor.start()
    monitor.temperature   # 58.4, ou None sem fonte
    monitor.throttled     # {"undervoltage": False, ...}, ou None sem fonte
"""

import logging
import subprocess
import threading


# ============================================================
# CONFIGURAÇÃO
# ============================================================

# Intervalo entre duas leituras (§6.5).
PERIOD_S = 10.0

# Sem o vcgencmd, a temperatura vem daqui, em milésimos de °C.
THERMAL = "/sys/class/thermal/thermal_zone0/temp"

# Bits do get_throttled, com os nomes do contrato com o servidor. Os de 0 a 3
# valem "agora"; os de 16 a 19, "desde o boot".
THROTTLED_BITS = {
    "undervoltage": 0,
    "freq_capped": 1,
    "throttling": 2,
    "soft_temp_limit": 3,
    "undervoltage_since_boot": 16,
    "freq_capped_since_boot": 17,
    "throttling_since_boot": 18,
    "soft_temp_limit_since_boot": 19,
}

# Os bits de "agora", cuja mudança vai ao log, e o que cada um quer dizer.
NOW_BITS = {
    "undervoltage": "subtensão",
    "freq_capped": "frequência limitada",
    "throttling": "redução de clock",
    "soft_temp_limit": "limite de temperatura",
}

# Alerta da temperatura (§6.5). A Pi 4 reduz o clock a partir de 80 °C.
HOT_C = 75.0

log = logging.getLogger("pi")


# ============================================================
# LEITURA
# ============================================================

def _vcgencmd(run, command):
    """A resposta do vcgencmd depois do '=', ou None se ele faltar ou falhar."""
    try:
        result = run(["vcgencmd", command], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or "=" not in result.stdout:
        return None
    return result.stdout.strip().split("=", 1)[1]


def read_sensors(run=subprocess.run, thermal=THERMAL):
    """
    (temperatura em °C, valor do get_throttled), cada um None sem fonte.
    O vcgencmd responde 'temp=58.4'C' e 'throttled=0x50000'.
    """
    temperature = _parse(_vcgencmd(run, "measure_temp"), lambda text: float(text.rstrip("'C")))
    if temperature is None:
        temperature = _parse(_read_file(thermal), lambda text: int(text) / 1000)
    throttled = _parse(_vcgencmd(run, "get_throttled"), lambda text: int(text, 16))
    return temperature, throttled


def _read_file(path):
    try:
        with open(path) as file:
            return file.read().strip()
    except OSError:
        return None


def _parse(text, convert):
    """O convert(text), ou None se o texto faltar ou não for número."""
    if text is None:
        return None
    try:
        return convert(text)
    except ValueError:
        return None


def throttled_flags(value):
    """O valor do get_throttled vira os 8 booleanos do contrato."""
    return {name: bool(value >> bit & 1) for name, bit in THROTTLED_BITS.items()}


# ============================================================
# MONITOR
# ============================================================

class PiMonitor:
    """
    Lê a Pi a cada PERIOD_S numa thread. O poll() é uma leitura, e os testes
    o chamam direto, com a leitura injetada.
    """

    def __init__(self, read=read_sensors, period=PERIOD_S):
        self._read = read
        self.period = period
        self._stop = threading.Event()
        self._thread = None
        self.temperature = None
        self.throttled = None
        # Para o resumo ao encerrar: a temperatura mais alta e o nome do bit
        # de cada aviso do throttled (um bit de "agora" que ligou).
        self.max_temperature = None
        self.throttled_warnings = []
        self._throttled_read = False
        # Sem fonte, o aviso sai uma vez só, e não a cada 10 s.
        self._warned_temperature = False
        self._warned_throttled = False
        # Episódio acima de HOT_C: um aviso ao começar, e ele só termina
        # quando a temperatura volta abaixo de HOT_C.
        self._hot = False
        # Os bits de "agora" da leitura anterior; antes da 1ª, todos desligados.
        self._now_bits = dict.fromkeys(NOW_BITS, False)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="pi-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self):
        # Uma leitura quebrada não pode calar o monitor pelo resto da viagem.
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:
                log.exception("leitura da Pi falhou; nova tentativa em %g s", self.period)
            self._stop.wait(self.period)

    def poll(self):
        """Uma leitura: guarda os valores e escreve a linha no log."""
        temperature, raw = self._read()
        flags = None if raw is None else throttled_flags(raw)
        self.temperature = temperature
        self.throttled = flags

        if temperature is None and not self._warned_temperature:
            log.warning("Pi sem fonte da temperatura (vcgencmd nem %s)", THERMAL)
            self._warned_temperature = True
        if raw is None and not self._warned_throttled:
            log.warning("Pi sem fonte do throttled (vcgencmd get_throttled)")
            self._warned_throttled = True

        if temperature is not None:
            self._check_hot(temperature)
            if self.max_temperature is None or temperature > self.max_temperature:
                self.max_temperature = temperature
        if raw is not None:
            self._throttled_read = True
            self._check_now_bits(flags, raw)

        log.info("Pi: %s, throttled %s",
                 "temperatura sem valor" if temperature is None else "%.1f °C" % temperature,
                 "sem valor" if raw is None else "0x%x" % raw)

    def summary(self):
        """A parte da Pi no resumo ao encerrar."""
        if self.max_temperature is None:
            hottest = "Pi sem temperatura"
        else:
            hottest = "Pi até %.1f °C" % self.max_temperature
        if not self._throttled_read:
            throttled = "throttled sem valor"
        elif not self.throttled_warnings:
            throttled = "throttled sem avisos"
        else:
            count = len(self.throttled_warnings)
            throttled = "throttled com %d %s (%s)" % (
                count, "aviso" if count == 1 else "avisos",
                ", ".join(dict.fromkeys(self.throttled_warnings)))
        return "%s, %s" % (hottest, throttled)

    def _check_hot(self, temperature):
        if not self._hot and temperature > HOT_C:
            log.warning("Pi acima de %g °C: %.1f °C (a Pi 4 reduz o clock a partir de 80 °C)",
                        HOT_C, temperature)
            self._hot = True
        elif self._hot and temperature < HOT_C:
            log.info("Pi abaixo de %g °C de novo: %.1f °C", HOT_C, temperature)
            self._hot = False

    def _check_now_bits(self, flags, raw):
        for name, meaning in NOW_BITS.items():
            on = flags[name]
            if on == self._now_bits[name]:
                continue
            self._now_bits[name] = on
            if on:
                self.throttled_warnings.append(name)
            log.log(logging.WARNING if on else logging.INFO, "Pi: %s (%s) %s, throttled 0x%x",
                    name, meaning, "ligou" if on else "desligou", raw)
