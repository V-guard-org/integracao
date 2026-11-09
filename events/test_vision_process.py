"""
Testes do VisionProcess (events/vision_process.py, Issue #10) com uma visão
falsa (fake_vision.py), sem câmera nem MediaPipe. Só a biblioteca padrão:
rodam no Python 3.11 e no 3.13.

    cd events && python3 test_vision_process.py
"""

import logging
import os
import signal
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import vision_process  # noqa: E402
from vision_process import Backoff, VisionProcess, vision_python  # noqa: E402

FAKE = os.path.join(HERE, "fake_vision.py")
FAST = dict(first=0.05, limit=0.2, stable_s=60.0)


class FakeManager:
    """O record_event do EventManager: devolve 1, 2, 3... ou None (a espera entre eventos)."""

    def __init__(self, cooldown=False):
        self.events = []
        self.cooldown = cooldown
        self.lock = threading.Lock()

    def record_event(self, type, score, jpeg=None, ts=None):
        with self.lock:
            if self.cooldown and self.events:
                return None
            self.events.append((type, score, jpeg, ts))
            return len(self.events)


def wait_until(condition, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return False


class VisionProcessTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.count = os.path.join(self.directory.name, "tentativas")
        self.vision = None

    def tearDown(self):
        if self.vision is not None:
            self.vision.stop()

    def start(self, plans, manager=None, backoff=None, extra=(), **options):
        argv = ["--plans", plans, "--count", self.count, *extra]
        self.manager = manager or FakeManager()
        self.vision = VisionProcess(self.manager, argv, python=sys.executable, script=FAKE,
                                    backoff=backoff or Backoff(**FAST), **options)
        self.vision.start()
        return self.vision

    def attempts(self):
        if not os.path.exists(self.count):
            return 0
        with open(self.count) as stream:
            return int(stream.read() or 0)

    def test_messages_reach_the_manager_and_exit_zero_ends_the_program(self):
        with self.assertLogs("vision.process", level="INFO") as logs:
            vision = self.start("pronto,fps:13.9,evento:fadiga,fps:14.1,exit:0")
            self.assertTrue(vision.wait(10))
            vision.stop()
        self.assertEqual(len(self.manager.events), 1)
        kind, score, jpeg, ts = self.manager.events[0]
        self.assertEqual((kind, score), ("fadiga", 0.8))
        self.assertTrue(jpeg.startswith(b"\xff\xd8") and len(jpeg) == 104)
        self.assertAlmostEqual(ts, time.time(), delta=30)
        self.assertEqual(vision.events, {"fadiga": 1})
        self.assertEqual(vision.summary(), "visão 14.0 FPS em média; eventos: fadiga 1; visão reiniciada 0 vezes")
        self.assertIn("INFO:vision.process:[EVENTO REGISTRADO] ID=1 Tipo='fadiga' Score=0.8 Tamanho=104B",
                      logs.output)
        self.assertTrue(any("visão iniciada (pid " in line for line in logs.output))
        self.assertEqual(self.attempts(), 1)

    def test_event_inside_the_cooldown_is_not_counted(self):
        vision = self.start("pronto,evento:fadiga,evento:fadiga,exit:0", manager=FakeManager(cooldown=True))
        self.assertTrue(vision.wait(10))
        self.assertEqual(vision.events, {"fadiga": 1})

    def test_vision_fps_follows_the_messages_and_drops_to_zero(self):
        now = [1000.0]
        vision = self.start("pronto,fps:13.9,hang", clock=lambda: now[0])
        self.assertTrue(wait_until(lambda: vision.vision_fps == 13.9))
        now[0] += 30.5                                  # sem fps há mais de 30 s
        self.assertEqual(vision.vision_fps, 0.0)
        now[0] -= 30.5
        self.assertEqual(vision.vision_fps, 13.9)
        vision.stop()                                   # com a visão fora, 0
        self.assertEqual(vision.vision_fps, 0.0)

    def test_crashes_restart_with_one_log_line_per_episode(self):
        with self.assertLogs("vision.process", level="INFO") as logs:
            vision = self.start("exit:1|exit:1|exit:1|pronto,hang")
            self.assertTrue(wait_until(lambda: vision._running))
        self.assertEqual(self.attempts(), 4)
        self.assertEqual(vision.restarts, 3)
        falls = [line for line in logs.output if "visão caiu" in line]
        self.assertEqual(falls, ["WARNING:vision.process:visão caiu (código 1); nova tentativa em 0.05 s"])
        self.assertTrue(any("visão de volta depois de 3 tentativas" in line for line in logs.output))
        self.assertFalse(vision.wait(0))

    def test_killed_with_sigkill_comes_back(self):
        with self.assertLogs("vision.process", level="INFO") as logs:
            vision = self.start("pronto,fps:12.0,hang")
            self.assertTrue(wait_until(lambda: vision._running))
            first = vision.pid
            os.kill(first, signal.SIGKILL)
            self.assertTrue(wait_until(lambda: vision._running and vision.pid not in (None, first)))
        self.assertIn(f"WARNING:vision.process:visão caiu (código -{int(signal.SIGKILL)}); nova tentativa em 0.05 s",
                      logs.output)
        self.assertEqual(vision.restarts, 1)

    def test_configuration_error_does_not_restart(self):
        with self.assertLogs("vision.process", level="ERROR") as logs:
            vision = self.start("exit:78")
            self.assertTrue(wait_until(lambda: vision.config_error is not None))
            time.sleep(0.3)
        self.assertEqual(self.attempts(), 1)
        self.assertFalse(vision.wait(0))
        self.assertEqual(vision.vision_fps, 0.0)
        self.assertIn("ERROR:vision.process:visão: erro de configuração (código 78): sem reinício; "
                      "a telemetria continua", logs.output)

    def test_wrong_protocol_version_is_a_configuration_error(self):
        with self.assertLogs("vision.process", level="ERROR"):
            vision = self.start("pronto:2,hang")
            self.assertTrue(wait_until(lambda: vision.config_error is not None and vision.pid is None))
            time.sleep(0.3)
        self.assertEqual(self.attempts(), 1)
        self.assertIn("versão 2 do canal", vision.config_error)

    def test_vision_that_cannot_start_is_a_configuration_error(self):
        vision = VisionProcess(FakeManager(), [], python="/caminho/que/nao/existe/python", script=FAKE)
        self.vision = vision
        with self.assertLogs("vision.process", level="ERROR"):
            vision.start()
            self.assertTrue(wait_until(lambda: vision.config_error is not None))
        self.assertFalse(vision.wait(0))

    def test_bad_messages_are_logged_and_the_good_ones_still_count(self):
        with self.assertLogs("vision.process", level="WARNING") as logs:
            vision = self.start("pronto,lixo,evento-png,evento:distracao,exit:0")
            self.assertTrue(vision.wait(10))
        self.assertEqual(vision.events, {"distracao": 1})
        self.assertIn("WARNING:vision.process:visão: mensagem com JSON inválido descartada", logs.output)
        self.assertIn("WARNING:vision.process:visão: evento descartado: o JPEG não começa com FF D8", logs.output)

    def test_stop_sends_sigterm_and_kills_after_the_timeout(self):
        vision = self.start("pronto,hang", extra=["--ignore-term"], stop_timeout_s=0.5)
        self.assertTrue(wait_until(lambda: vision._running))
        child = vision._child
        started = time.monotonic()
        with self.assertLogs("vision.process", level="WARNING") as logs:
            vision.stop()
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertIn("WARNING:vision.process:visão não saiu 0.5 s depois do SIGTERM: kill", logs.output)

    def test_stop_ends_a_vision_that_obeys_sigterm(self):
        vision = self.start("pronto,hang")
        self.assertTrue(wait_until(lambda: vision._running))
        child = vision._child
        vision.stop()
        self.assertEqual(child.returncode, 143)
        self.assertFalse(vision.wait(0))
        self.assertEqual(self.attempts(), 1)

    def test_sigterm_to_the_whole_group_is_not_a_fall(self):
        # timeout e systemd mandam o SIGTERM ao grupo: a visão sai um pouco antes do stop()
        with self.assertLogs("vision.process", level="INFO") as logs:
            vision = self.start("pronto,hang")
            self.assertTrue(wait_until(lambda: vision._running))
            vision._child.send_signal(signal.SIGTERM)
            time.sleep(0.3)
            vision.stop()
        self.assertFalse(any("visão caiu" in line for line in logs.output), logs.output)
        self.assertEqual(self.attempts(), 1)

    def test_sigterm_from_outside_still_counts_as_a_fall(self):
        with self.assertLogs("vision.process", level="INFO") as logs:
            vision = self.start("exit:143|pronto,hang")
            self.assertTrue(wait_until(lambda: self.attempts() == 2 and vision._running))
        self.assertIn("WARNING:vision.process:visão caiu (código 143); nova tentativa em 0.05 s", logs.output)

    def test_stop_during_the_wait_between_attempts_is_quick(self):
        vision = self.start("exit:1", backoff=Backoff(first=10.0, limit=60.0))
        self.assertTrue(wait_until(lambda: vision._down_since is not None))
        started = time.monotonic()
        vision.stop()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(self.attempts(), 1)

    def test_message_cut_when_the_vision_dies_is_dropped(self):
        with self.assertLogs("vision.process", level="WARNING") as logs:
            vision = self.start("pronto,evento:fadiga,evento-cortado|pronto,hang")
            self.assertTrue(wait_until(lambda: self.attempts() == 2 and vision._running))
        self.assertEqual(vision.events, {"fadiga": 1})
        self.assertIn("WARNING:vision.process:visão: mensagem cortada no fim do canal (a visão saiu): descartada",
                      logs.output)


class BackoffTests(unittest.TestCase):

    def test_doubles_from_two_to_sixty(self):
        backoff = Backoff()
        self.assertEqual([backoff.after_failure(1) for _ in range(8)], [2, 4, 8, 16, 32, 60, 60, 60])

    def test_back_to_two_after_sixty_seconds_running(self):
        backoff = Backoff()
        for _ in range(4):
            backoff.after_failure(1)
        self.assertEqual(backoff.after_failure(59.9), 32)
        self.assertEqual(backoff.after_failure(60.0), 2)
        self.assertEqual(backoff.after_failure(5), 4)


class VisionPythonTests(unittest.TestCase):

    def test_choice(self):
        self.assertEqual(vision_python({"VGUARD_VISION_PYTHON": "/opt/py/bin/python"}), "/opt/py/bin/python")
        default = vision_process.DEFAULT_VISION_PYTHON
        expected = default if os.path.exists(default) else sys.executable
        self.assertEqual(vision_python({}), expected)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
