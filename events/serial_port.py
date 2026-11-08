"""
V-GUARD — Thread de leitura de uma porta serial (ESP32 na USB, GNSS).

Abre a porta em modo cru, lê com select e passa os bytes ao pump() da
subclasse, que devolve o que escrever na porta. Se a porta some (cabo
solto, ESP32 reiniciando), fecha, espera REOPEN_S e abre de novo:

    porta --bytes--> SerialReader.pump() --bytes a escrever--> porta

    class MeuLeitor(SerialReader):
        def pump(self, data):
            ...
            return b""

    leitor = MeuLeitor("/dev/ttyUSB0", termios.B115200)
    leitor.start()
"""

import fcntl
import logging
import os
import select
import termios
import threading
import tty


# ============================================================
# CONFIGURAÇÃO
# ============================================================

# O pump() é chamado pelo menos a cada POLL_S, mesmo sem bytes: é nele que
# a subclasse percebe o tempo passar (o $H de 60 em 60 s, o ESP32 mudo).
POLL_S = 0.1

# Espera antes de reabrir a porta depois de um erro.
REOPEN_S = 2.0


def open_port(path, baud):
    """
    Abre a porta em modo cru, 8N1, sem controle de fluxo e sem HUPCL: fechar
    a porta não derruba o DTR e, com isso, não reinicia o ESP32 (proposta §6.4).
    """
    # O O_NONBLOCK evita que o open() espere a portadora (DCD) numa porta
    # sem CLOCAL; depois de configurada, a porta volta a bloquear.
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[2] &= ~(termios.CSTOPB | termios.CRTSCTS | termios.HUPCL)
        attrs[4] = attrs[5] = baud
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return fd


class SerialReader:
    """
    Lê uma porta numa thread e reabre depois de erro. A subclasse define o
    pump() e, se precisar, o opened() e o closed().
    """

    def __init__(self, port, baud):
        self.port = port
        self.baud = baud
        self._stop = threading.Event()
        self._thread = None
        # Porta ausente é esperado (ESP32 ou GNSS desligado): avisa uma vez
        # por episódio, e não a cada tentativa.
        self._failing = False

    def pump(self, data):
        """Bytes lidos (pode ser b"") viram o que escrever na porta."""
        raise NotImplementedError

    def opened(self):
        """A porta acabou de abrir."""

    def closed(self):
        """A porta acabou de fechar (erro, cabo solto ou stop())."""

    def run(self):
        # Se o leitor morrer, a viagem inteira fica sem os dados: qualquer
        # erro é registrado e a porta é reaberta.
        while not self._stop.is_set():
            try:
                self._read()
            except OSError as error:
                if not self._failing:
                    logging.warning(
                        "porta %s: %s; nova tentativa a cada %g s",
                        self.port, error, REOPEN_S
                    )
                    self._failing = True
            except Exception:
                logging.exception(
                    "leitor da porta %s falhou; nova tentativa em %g s",
                    self.port, REOPEN_S
                )
            self._stop.wait(REOPEN_S)

    def _read(self):
        fd = open_port(self.port, self.baud)
        if self._failing:
            logging.info("porta %s aberta", self.port)
            self._failing = False
        try:
            self.opened()
            while not self._stop.is_set():
                ready, _, _ = select.select([fd], [], [], POLL_S)
                data = os.read(fd, 4096) if ready else b""
                if ready and not data:
                    raise OSError("porta fechada")
                out = self.pump(data)
                if out:
                    os.write(fd, out)
        finally:
            os.close(fd)
            self.closed()

    def start(self):
        self._thread = threading.Thread(target=self.run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
