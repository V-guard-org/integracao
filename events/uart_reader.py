"""
V-GUARD — Leitor da UART do ESP32.

Lê os bytes do ESP32, passa ao Decoder e entrega as amostras de IMU e
OBD ao EventManager:

    ESP32 --UART--> UartReader --feed()--> Decoder --amostras--> manager.update()
          <-------- PING e TAXAS <-- data_to_send()

O esp32_decoder.py é copiado do firmware_telemetria (host/), sem
alterações; para atualizar, copiar de novo (ver VERSION no arquivo).

Roda em uma thread do mesmo processo do EventManager:

    reader = UartReader(manager)
    reader.start()
    reader.decoder.set_rates(imu=50)     # RF29, de qualquer thread
"""

import logging
import os
import select
import termios
import threading
import tty

from esp32_decoder import Decoder


# ============================================================
# CONFIGURAÇÃO
# ============================================================

# UART da Pi ligada ao ESP32 (a confirmar com o Thiago). No notebook,
# com o ESP32 falso do firmware_telemetria: /tmp/vguard-esp32.
PORT = os.environ.get("VGUARD_UART", "/dev/ttyAMA5")

# O Decoder precisa de um feed() a cada 100 ms no máximo, mesmo sem
# bytes: é nele que saem o PING e a percepção de ESP32 mudo.
POLL_S = 0.1

# Espera antes de reabrir a porta depois de um erro.
REOPEN_S = 2.0

# Valor do OBD mais velho que isto é descartado. Os seis PIDs do RF05
# são lidos 1 vez/s, então idades de até ~1 s são normais. Carro
# desligado já chega como {} pelo Decoder; isto pega um PID isolado que
# parou de responder. Valor INICIAL, a calibrar no carro.
OBD_MAX_AGE_MS = 3000


def open_port(path):
    """
    Abre a UART a 460800 baud, 8N1, sem controle de fluxo (PROTOCOLO.md).
    """
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    tty.setraw(fd)
    attrs = termios.tcgetattr(fd)
    attrs[2] |= termios.CLOCAL | termios.CREAD
    attrs[2] &= ~(termios.CSTOPB | termios.CRTSCTS)
    attrs[4] = attrs[5] = termios.B460800
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def drop_stale(sample, max_age_ms=OBD_MAX_AGE_MS):
    """
    Tira da amostra OBD os valores com age_ms acima do limite. Se nenhum
    sobrar, devolve {}, que o update() trata como OBD parado.
    """
    ages = sample.get("age_ms", {})
    fresh = {key: age for key, age in ages.items() if age <= max_age_ms}

    if len(fresh) == len(ages):
        return sample
    if not fresh:
        return {}
    return {**{key: sample[key] for key in fresh}, "age_ms": fresh}


class UartReader:

    def __init__(self, manager, port=PORT):
        self.manager = manager
        self.port = port
        self.decoder = Decoder()
        self._stop = threading.Event()
        self._thread = None

    def pump(self, data):
        """
        Um passo: bytes lidos (pode ser b"") viram update(). Retorna os
        bytes a escrever na porta, que devem sair na hora (DECODIFICADOR.md §2.2).
        """
        for source, ts, sample in self.decoder.feed(data):
            if source == "obd":
                sample = drop_stale(sample)
            self.manager.update(source, sample, ts=ts)

        return self.decoder.data_to_send()

    def run(self):
        # Se o leitor morrer, a viagem inteira fica sem IMU e OBD: qualquer
        # erro é registrado e a porta é reaberta.
        while not self._stop.is_set():
            try:
                self._read()
            except OSError as error:
                # porta ausente ou cabo solto: esperado, sem traceback
                logging.warning(
                    "UART %s: %s; nova tentativa em %g s",
                    self.port, error, REOPEN_S
                )
            except Exception:
                logging.exception(
                    "leitor da UART %s falhou; nova tentativa em %g s",
                    self.port, REOPEN_S
                )
            self._stop.wait(REOPEN_S)

    def _read(self):
        fd = open_port(self.port)
        try:
            while not self._stop.is_set():
                ready, _, _ = select.select([fd], [], [], POLL_S)
                data = os.read(fd, 4096) if ready else b""
                if ready and not data:
                    raise OSError("porta fechada")
                os.write(fd, self.pump(data))
        finally:
            os.close(fd)

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
