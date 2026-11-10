"""
Testes do programa da visão (vision/main.py como processo filho, Issue #10):
as opções, o mensageiro do canal e o programa de verdade, com um vídeo
sintético, o MediaPipe e o modelo. Precisa do venv da visão (3.11).

    cd vision && python3 test_program.py
"""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import main as vision  # noqa: E402
import protocol  # noqa: E402
from config import MODEL_PATH  # noqa: E402

JPEG = b"\xff\xd8" + b"x" * 50 + b"\xff\xd9"


def make_video(path, frames):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 15.0, (640, 480))
    for i in range(frames):
        frame = np.full((480, 640, 3), 40 + i % 150, np.uint8)
        cv2.putText(frame, f"quadro {i}", (40, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        writer.write(frame)
    writer.release()


def start_vision(*options):
    """O programa da visão como o principal o inicia: o canal em --channel-fd."""
    read_fd, write_fd = os.pipe()
    child = subprocess.Popen([sys.executable, "-u", "-B", str(HERE / "main.py"), "--channel-fd", str(write_fd),
                              *options], pass_fds=(write_fd,), stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True)
    os.close(write_fd)
    return child, read_fd


def read_messages(read_fd, until=None, timeout=60):
    """Lê o canal até o fim (ou até a mensagem `until`): as mensagens e os problemas."""
    reader = protocol.Reader()
    messages, problems = [], []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        data = os.read(read_fd, 65536)
        if not data:
            problems += reader.close()
            break
        got, bad = reader.feed(data)
        messages += got
        problems += bad
        if until and any(m.kind == until for m in got):
            break
    return messages, problems


class OptionsTests(unittest.TestCase):

    def test_csi_capture_picks_the_system_python_when_needed(self):
        cases = (   # (picamera2 aqui?, Python atual, esperado)
            (True, "/home/pi/.venv/bin/python", None),
            (False, "/home/pi/.venv/bin/python", "/usr/bin/python3"),
            (False, vision.SYSTEM_PYTHON, None),
        )
        for has_picamera2, executable, expected in cases:
            with self.subTest(has_picamera2=has_picamera2, executable=executable), \
                 patch.object(vision.importlib.util, "find_spec",
                              return_value=object() if has_picamera2 else None), \
                 patch.object(vision.sys, "executable", executable), \
                 patch.object(vision.os.path, "exists", return_value=True):
                self.assertEqual(vision.parse_args([]).camera_python, expected)
                # Webcam ou vídeo nunca recebem o auxiliar CSI.
                self.assertIsNone(vision.parse_args(["--source", "0"]).camera_python)

    def test_channel_fd_is_optional(self):
        self.assertIsNone(vision.parse_args(["--source", "0"]).channel_fd)
        self.assertEqual(vision.parse_args(["--source", "0", "--channel-fd", "7"]).channel_fd, 7)


class MessengerTests(unittest.TestCase):

    def test_channel_sink_has_the_record_event_signature(self):
        sent = []

        class Writer:
            def event(self, ts, type, score, jpeg):
                sent.append((ts, type, score, jpeg))

        sink = vision.ChannelSink(Writer())
        self.assertEqual(sink.record_event(type="fadiga", score=0.82, jpeg=JPEG, ts=1793980812.4), 1)
        self.assertEqual(sink.record_event(type="distracao", score=0.7, jpeg=JPEG, ts=1793980813.0), 2)
        self.assertEqual([item[1] for item in sent], ["fadiga", "distracao"])
        self.assertEqual(sink.event_label, "EVENTO ENVIADO")

    def test_console_writer_shows_the_json_without_the_jpeg(self):
        with patch("builtins.print") as printed:
            writer = vision.ConsoleWriter()
            writer.ready()
            writer.event(1793980812.4, "fadiga", 0.82, JPEG)
        lines = [call.args[0] for call in printed.call_args_list]
        self.assertEqual(lines[0], '[canal] {"msg":"pronto","versao":1}')
        self.assertTrue(lines[1].startswith('[canal] {"msg":"evento"'))
        self.assertTrue(lines[1].endswith(f"(+ {len(JPEG)} bytes do JPEG)"))


@unittest.skipUnless(MODEL_PATH.exists(), "sem o face_landmarker.task (vision/scripts/download_model.py)")
class ProgramTests(unittest.TestCase):
    """O programa da visão de verdade, como processo filho."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.video = Path(self.directory.name) / "sintetico.avi"

    def test_ready_then_exit_zero_with_max_frames(self):
        make_video(self.video, 20)
        child, read_fd = start_vision("--source", str(self.video), "--headless", "--max-frames", "20")
        try:
            messages, problems = read_messages(read_fd)
        finally:
            os.close(read_fd)
        self.assertEqual(child.wait(timeout=60), protocol.EXIT_OK, child.stderr.read())
        self.assertEqual(problems, [])
        self.assertEqual([m.kind for m in messages], ["pronto"])
        self.assertEqual(messages[0].data["versao"], protocol.VERSION)

    def test_pending_calibration_without_screen_is_a_configuration_error(self):
        make_video(self.video, 5)
        missing = Path(self.directory.name) / "nao-existe" / "calibration-driver.json"
        child, read_fd = start_vision("--source", str(self.video), "--headless",
                                      "--calibration-file", str(missing))
        try:
            messages, _ = read_messages(read_fd)
        finally:
            os.close(read_fd)
        self.assertEqual(child.wait(timeout=60), protocol.EXIT_CONFIG)
        self.assertEqual(messages, [])
        self.assertIn("erro de configuração: Calibracao pendente", child.stderr.read())

    def test_sigterm_closes_the_camera_and_exits_143(self):
        make_video(self.video, 600)
        child, read_fd = start_vision("--source", str(self.video), "--headless")
        try:
            messages, _ = read_messages(read_fd, until="pronto")
            self.assertEqual([m.kind for m in messages], ["pronto"])
            child.send_signal(signal.SIGTERM)
            self.assertEqual(child.wait(timeout=30), protocol.EXIT_TERMINATED)
        finally:
            os.close(read_fd)
            if child.poll() is None:
                child.kill()
        self.assertIn("visão: encerrada pelo SIGTERM", child.stderr.read())

    def test_sigint_is_left_to_the_principal(self):
        make_video(self.video, 600)
        child, read_fd = start_vision("--source", str(self.video), "--headless")
        try:
            read_messages(read_fd, until="pronto")
            child.send_signal(signal.SIGINT)
            time.sleep(1.0)
            self.assertIsNone(child.poll(), "a visão saiu com o Ctrl+C")
            child.send_signal(signal.SIGTERM)
            self.assertEqual(child.wait(timeout=30), protocol.EXIT_TERMINATED)
        finally:
            os.close(read_fd)
            if child.poll() is None:
                child.kill()


if __name__ == "__main__":
    unittest.main()
