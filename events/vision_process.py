"""
A visão num processo filho, vista do programa principal (proposta §6.1 e
§6.9, Issue #10). Só com a biblioteca padrão, como o resto do events/.

    vguard.py --> VisionProcess --subprocess--> vision/main.py --channel-fd N
                        ^                              |
                        +------- pipe: pronto, fps, evento + JPEG

O VisionProcess inicia a visão no Python dela (VGUARD_VISION_PYTHON), lê o
canal, grava os eventos no EventManager, acompanha o vision_fps e decide o
que fazer quando a visão sai:

    0   fim normal (Q na tela ou --max-frames): o programa todo encerra
    78  erro de configuração: sem reinício; a telemetria continua
    outro (inclusive morta por sinal): reinicia, com a espera de 2 s
        dobrando até 60 s, que volta a 2 s depois de 60 s rodando

O stdout e o stderr da visão vão para o mesmo lugar que os do principal (o
log); o canal só leva as mensagens.
"""

import collections
import logging
import os
import signal
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if os.path.join(REPO, "vision") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "vision"))

import protocol  # noqa: E402  (vision/protocol.py, só biblioteca padrão)

log = logging.getLogger("vision.process")

VISION_SCRIPT = os.path.join(REPO, "vision", "main.py")
# O venv 3.11 com o MediaPipe 0.10.18 validado na Pi (README).
DEFAULT_VISION_PYTHON = "/home/vguard/vguard/compat/mediapipe-0.10.18-py311/.venv/bin/python"
FPS_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 5.0
# A visão saiu pelo SIGTERM (143): espera-se este tanto pelo encerramento do
# principal antes de contar como queda (o SIGTERM pode ter ido ao grupo todo).
TERM_GRACE_S = 1.0


def vision_python(environ=os.environ):
    """O Python da visão: VGUARD_VISION_PYTHON, o venv padrão da Pi ou, no notebook, o mesmo do principal."""
    chosen = environ.get("VGUARD_VISION_PYTHON")
    if chosen:
        return chosen
    if os.path.exists(DEFAULT_VISION_PYTHON):
        return DEFAULT_VISION_PYTHON
    return sys.executable


def plural(count, one, many):
    return f"{count} {one if count == 1 else many}"


class Backoff:
    """
    A espera entre as tentativas: começa em `first`, dobra a cada queda até
    `limit` e volta a `first` quando a visão rodou `stable_s` sem cair.
    """

    def __init__(self, first=2.0, limit=60.0, stable_s=60.0):
        self.first, self.limit, self.stable_s = first, limit, stable_s
        self._next = first

    def after_failure(self, ran_s):
        """A espera depois de uma queda, sabendo quanto tempo a visão rodou."""
        if ran_s >= self.stable_s:
            self._next = self.first
        delay = self._next
        self._next = min(self._next * 2, self.limit)
        return delay


class VisionProcess:
    """
    Quem cuida da visão no programa principal: start(), wait() (True quando
    a visão terminou normalmente e o programa deve encerrar), stop() e, para
    a saúde e o resumo, vision_fps, events, restarts e summary().
    """

    def __init__(self, manager, argv, python=None, script=VISION_SCRIPT, backoff=None,
                 stop_timeout_s=STOP_TIMEOUT_S, fps_timeout_s=FPS_TIMEOUT_S, clock=time.monotonic):
        self.manager = manager
        self.argv = list(argv)
        self.python = python or vision_python()
        self.script = script
        self.backoff = backoff or Backoff()
        self.stop_timeout_s = stop_timeout_s
        self.fps_timeout_s = fps_timeout_s
        self._clock = clock

        # Para a saúde (ticket 5) e o resumo
        self.events = collections.Counter()     # eventos gravados, por tipo
        self.starts = 0
        self.config_error = None                # texto do erro de configuração, se houve
        self._fps = 0.0
        self._fps_at = None
        self._fps_values = []
        self._running = False                   # mandou o pronto e ainda não saiu

        self._down_since = None                 # início do episódio de queda em curso
        self._attempts = 0                      # tentativas no episódio em curso
        self._version_error = False
        self._child = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ended = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ interface

    @property
    def restarts(self):
        return max(0, self.starts - 1)

    @property
    def vision_fps(self):
        """O último fps da visão; 0 sem fps há mais de 30 s ou com a visão fora."""
        if not self._running or self._fps_at is None or self._clock() - self._fps_at > self.fps_timeout_s:
            return 0.0
        return self._fps

    @property
    def pid(self):
        child = self._child
        return child.pid if child is not None and child.poll() is None else None

    def summary(self):
        average = sum(self._fps_values) / len(self._fps_values) if self._fps_values else 0.0
        events = ", ".join(f"{kind} {count}" for kind, count in self.events.items()) or "nenhum"
        return (f"visão {average:.1f} FPS em média; eventos: {events}; "
                f"visão reiniciada {plural(self.restarts, 'vez', 'vezes')}")

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="vision", daemon=True)
        self._thread.start()

    def wait(self, timeout=None):
        """True quando a visão terminou normalmente (código 0): o programa todo encerra."""
        return self._ended.wait(timeout)

    def stop(self):
        """SIGTERM à visão, espera de até stop_timeout_s e, se for preciso, kill."""
        self._stop.set()
        with self._lock:
            child = self._child
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=self.stop_timeout_s)
            except subprocess.TimeoutExpired:
                log.warning("visão não saiu %g s depois do SIGTERM: kill", self.stop_timeout_s)
                child.kill()
                child.wait()
        if self._thread is not None:
            self._thread.join(timeout=self.stop_timeout_s + 5)
        self._running = False

    # ------------------------------------------------------------------ o laço

    def _spawn(self):
        """Inicia a visão com a ponta de escrita do canal. Devolve (filho, ponta de leitura)."""
        read_fd, write_fd = os.pipe()
        try:
            with self._lock:
                if self._stop.is_set():
                    os.close(read_fd)
                    return None, None
                command = [self.python, "-u", self.script, "--channel-fd", str(write_fd), *self.argv]
                child = subprocess.Popen(command, pass_fds=(write_fd,))
                self._child = child
        except BaseException:
            os.close(read_fd)
            raise
        finally:
            os.close(write_fd)      # só a visão escreve: o fim dela chega como fim do pipe
        return child, read_fd

    def _loop(self):
        while not self._stop.is_set():
            try:
                child, read_fd = self._spawn()
            except OSError as exc:
                self.config_error = f"a visão não iniciou ({self.python}): {exc}"
                log.error("visão: %s; sem reinício, a telemetria continua", self.config_error)
                return
            if child is None:
                return
            self.starts += 1
            started = self._clock()
            if self._down_since is None:
                log.info("visão iniciada (pid %d)", child.pid)
            else:
                log.debug("visão: tentativa %d (pid %d)", self._attempts + 1, child.pid)
            self._read(read_fd, child)
            code = child.wait()
            self._running = False
            if self._stop.is_set():
                return
            if code == protocol.EXIT_OK:
                log.info("visão terminou normalmente (código 0): o programa encerra")
                self._ended.set()
                return
            if code == protocol.EXIT_TERMINATED and self._stop.wait(TERM_GRACE_S):
                # O SIGTERM foi ao grupo todo (timeout, systemd): a visão saiu um
                # instante antes de o principal começar a encerrar. Não é queda.
                return
            if code == protocol.EXIT_CONFIG or self._version_error:
                self.config_error = self.config_error or f"código {code}"
                log.error("visão: erro de configuração (código %s): sem reinício; a telemetria continua",
                          code)
                return
            delay = self.backoff.after_failure(self._clock() - started)
            if self._down_since is None:
                self._down_since = self._clock()
                self._attempts = 0
                log.warning("visão caiu (código %s); nova tentativa em %g s", code, delay)
            self._attempts += 1
            if self._stop.wait(delay):
                return

    def _read(self, fd, child):
        """Lê o canal até o fim (a visão saiu) e trata cada mensagem."""
        reader = protocol.Reader()
        try:
            while True:
                try:
                    data = os.read(fd, 65536)
                except InterruptedError:
                    continue
                if not data:
                    break
                messages, problems = reader.feed(data)
                for problem in problems:
                    log.warning("visão: %s", problem)
                for message in messages:
                    self._handle(message, child)
        finally:
            os.close(fd)
        for problem in reader.close():
            log.warning("visão: %s", problem)

    def _handle(self, message, child):
        if message.kind == "pronto":
            version = message.data["versao"]
            if version != protocol.VERSION:
                self._version_error = True
                self.config_error = f"versão {version} do canal, esperada {protocol.VERSION}"
                log.error("visão: %s; encerrando a visão", self.config_error)
                child.terminate()
                return
            self._running = True
            if self._down_since is not None:
                log.info("visão de volta depois de %s (%.0f s fora)",
                         plural(self._attempts, "tentativa", "tentativas"), self._clock() - self._down_since)
                self._down_since = None
        elif message.kind == "fps":
            self._fps = float(message.data["fps"])
            self._fps_at = self._clock()
            self._fps_values.append(self._fps)
        elif message.kind == "evento":
            self._record(message)

    def _record(self, message):
        data = message.data
        try:
            event_id = self.manager.record_event(type=data["type"], score=float(data["score"]),
                                                 jpeg=message.jpeg, ts=float(data["ts"]))
        except Exception as exc:
            log.error("visão: falha ao gravar o evento %s: %s", data["type"], exc)
            return
        if event_id is None:
            log.info("visão: evento %s dentro da espera entre eventos do mesmo tipo: não gravado",
                     data["type"])
            return
        self.events[data["type"]] += 1
        # A mesma linha de antes do processo filho: a bancada guiada a lê.
        log.info("[EVENTO REGISTRADO] ID=%s Tipo='%s' Score=%s Tamanho=%dB",
                 event_id, data["type"], data["score"], len(message.jpeg))
