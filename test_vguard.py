"""
Contratos de inicialização e encerramento do programa principal, com a visão
num processo filho (Issue #10); executar com python -B test_vguard.py.

O principal não importa o OpenCV nem o MediaPipe: estes testes rodam no
Python 3.13 do sistema e no venv 3.11.
"""

from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import vguard


def vision_argv(vision_class):
    """As opções que o principal passou à visão (o 2º argumento do VisionProcess)."""
    return vision_class.call_args.args[1]


def option(argv, name):
    return argv[argv.index(name) + 1]


class ApplicationLifecycle(unittest.TestCase):
    def setUp(self):
        # O monitor de verdade chamaria o vcgencmd do notebook.
        patcher = patch.object(vguard, "PiMonitor")
        self.monitor = patcher.start()
        self.addCleanup(patcher.stop)
        self.monitor.return_value.summary.return_value = "Pi até 61.2 °C, throttled sem avisos"

    def run_app(self, argv, vision=None, **patches):
        """Roda o principal com a visão falsa: por padrão, ela termina normalmente na hora."""
        vision = vision or Mock()
        if not vision.wait.side_effect:
            vision.wait.return_value = True
        vision.summary.return_value = "visão 13.8 FPS em média; eventos: nenhum; visão reiniciada 0 vezes"
        with ExitStack() as stack:
            manager = stack.enter_context(patch.object(vguard, "EventManager"))
            vision_class = stack.enter_context(patch.object(vguard, "VisionProcess", return_value=vision))
            for name, value in patches.items():
                stack.enter_context(patch.object(vguard, name, value))
            vguard.run(argv)
        return manager, vision_class, vision

    def test_principal_does_not_import_opencv_mediapipe_or_numpy(self):
        code = ("import sys; sys.path.insert(0, %r); import vguard; "
                "print(sorted(m for m in ('cv2', 'mediapipe', 'numpy') if m in sys.modules))"
                % os.path.dirname(os.path.abspath(__file__)))
        output = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(output.stdout.strip(), "[]")

    def test_calibration_options_reach_the_vision(self):
        _, vision_class, _ = self.run_app(["--no-esp32", "--no-gps", "--no-sync", "--calibrate",
                                           "--calibration-file", "/tmp/calibration-driver.json"])
        argv = vision_argv(vision_class)
        self.assertIn("--calibrate", argv)
        self.assertEqual(option(argv, "--calibration-file"), "/tmp/calibration-driver.json")
        self.assertNotIn("--headless", argv)

    def test_calibrate_without_a_file_uses_the_data_folder(self):
        with patch.dict(os.environ, {"VGUARD_DATA": "/tmp/dados-teste"}):
            _, vision_class, _ = self.run_app(["--no-esp32", "--no-gps", "--no-sync", "--calibrate"])
        self.assertEqual(option(vision_argv(vision_class), "--calibration-file"),
                         str(Path("/tmp/dados-teste") / "calibration-driver.json"))

    def test_csi_options_reach_the_vision(self):
        _, vision_class, _ = self.run_app(["--headless", "--no-esp32", "--no-gps", "--no-sync",
                                           "--source", "picamera2", "--infrared", "--camera-num", "1",
                                           "--fps", "20", "--camera-python", "/usr/bin/python3",
                                           "--max-frames", "10"])
        argv = vision_argv(vision_class)
        self.assertEqual(option(argv, "--source"), "picamera2")
        self.assertEqual(float(option(argv, "--fps")), 20)
        self.assertEqual(option(argv, "--camera-num"), "1")
        self.assertEqual(option(argv, "--camera-python"), "/usr/bin/python3")
        self.assertEqual(option(argv, "--max-frames"), "10")
        self.assertIn("--infrared", argv)
        self.assertIn("--headless", argv)

    def test_webcam_index_reaches_the_vision(self):
        _, vision_class, _ = self.run_app(["--headless", "--no-esp32", "--no-gps", "--no-sync", "--source", "0"])
        argv = vision_argv(vision_class)
        self.assertEqual(option(argv, "--source"), "0")
        self.assertNotIn("--infrared", argv)

    def test_invalid_camera_options_fail_before_database_creation(self):
        for options in (["--fps", "nan"], ["--fps", "0"], ["--width", "0"],
                        ["--camera-num", "-1"], ["--source", "0", "--infrared"],
                        ["--headless", "--calibrate"], ["--max-frames", "0"]):
            with self.subTest(options=options), patch.object(vguard, "EventManager") as manager, \
                 patch.object(vguard, "VisionProcess") as vision:
                with self.assertRaises(SystemExit):
                    vguard.run(options)
                manager.assert_not_called()
                vision.assert_not_called()

    def test_camera_only_never_constructs_esp32_gps_or_sending(self):
        esp32, gps, publisher, sender = Mock(), Mock(), Mock(), Mock()
        manager, vision_class, vision = self.run_app(
            ["--headless", "--no-esp32", "--no-gps", "--no-sync"], Esp32Reader=esp32, ModemReader=gps,
            MqttPublisher=publisher, EventSender=sender)
        esp32.assert_not_called()
        gps.assert_not_called()
        publisher.assert_not_called()       # o --no-sync desliga o MQTT
        sender.assert_not_called()          # e o envio dos eventos
        self.assertIs(vision_class.call_args.args[0], manager.return_value)
        vision.start.assert_called_once()
        vision.stop.assert_called_once()
        manager.return_value.close.assert_called_once()
        self.monitor.return_value.start.assert_called_once()
        self.monitor.return_value.stop.assert_called_once()

    def test_the_publisher_and_the_event_sender_start_together(self):
        publisher_class, sender_class = Mock(), Mock()
        publisher_class.return_value.summary.return_value = "mqtt: 5 linhas publicadas, 0 não publicadas (desconectado), 1 saúde"
        sender_class.return_value.summary.return_value = "envio: 1 evento enviado, 0 pendentes"
        with self.assertLogs("vguard", level="INFO") as logs:
            manager, _, _ = self.run_app(["--headless", "--no-esp32", "--no-gps"], MqttPublisher=publisher_class,
                                         EventSender=sender_class)
        publisher_class.assert_called_once_with(manager.return_value)
        sender_class.assert_called_once_with(manager.return_value, publisher_class.return_value)
        for worker in (publisher_class, sender_class):
            worker.return_value.start.assert_called_once()
            worker.return_value.stop.assert_called_once()
        self.assertIn("INFO:vguard:resumo: visão 13.8 FPS em média; eventos: nenhum; visão reiniciada 0 vezes; "
                      "mqtt: 5 linhas publicadas, 0 não publicadas (desconectado), 1 saúde; "
                      "envio: 1 evento enviado, 0 pendentes; Pi até 61.2 °C, throttled sem avisos", logs.output)

    def test_old_no_uart_option_still_skips_esp32(self):
        esp32 = Mock()
        self.run_app(["--headless", "--no-uart", "--no-gps", "--no-sync"], Esp32Reader=esp32)
        esp32.assert_not_called()

    def test_runs_until_the_vision_ends_normally(self):
        vision = Mock()
        vision.wait.side_effect = [False, False, True]
        self.run_app(["--headless", "--no-esp32", "--no-gps", "--no-sync"], vision=vision)
        self.assertEqual(vision.wait.call_count, 3)
        vision.stop.assert_called_once()

    def test_shutdown_stops_the_vision_first_and_closes_the_database_last(self):
        order = []
        manager, reader, gps, publisher, sender, vision = Mock(), Mock(), Mock(), Mock(), Mock(), Mock()
        vision.wait.side_effect = KeyboardInterrupt     # Ctrl+C ou SIGTERM
        vision.summary.return_value = "visão 13.8 FPS em média; eventos: nenhum; visão reiniciada 0 vezes"
        vision.stop.side_effect = lambda: order.append("vision")
        reader.stop.side_effect = lambda: order.append("esp32")
        reader.link.lost, reader.link.corrupt = 0, 2
        gps.stop.side_effect = lambda: order.append("gps")
        publisher.stop.side_effect = lambda: order.append("mqtt")
        publisher.summary.return_value = "mqtt: 9 linhas publicadas, 0 não publicadas (desconectado), 1 saúde"
        sender.stop.side_effect = lambda: order.append("sender")
        sender.summary.side_effect = lambda: order.append("sender summary") or "envio: 2 eventos enviados, 0 pendentes"
        manager.close.side_effect = lambda: order.append("database")
        self.monitor.return_value.stop.side_effect = lambda: order.append("pi")
        with patch.object(vguard, "EventManager", return_value=manager), \
             patch.object(vguard, "Esp32Reader", return_value=reader), \
             patch.object(vguard, "ModemReader", return_value=gps), \
             patch.object(vguard, "MqttPublisher", return_value=publisher), \
             patch.object(vguard, "EventSender", return_value=sender), \
             patch.object(vguard, "VisionProcess", return_value=vision), \
             self.assertLogs("vguard", level="INFO") as logs:
            vguard.run(["--headless"])
        # O resumo do envio conta os pendentes no banco: antes de fechá-lo.
        self.assertEqual(order, ["vision", "esp32", "gps", "sender", "mqtt", "pi", "sender summary", "database"])
        self.assertIn("INFO:vguard:encerramento solicitado", logs.output)
        self.assertIn("INFO:vguard:resumo: visão 13.8 FPS em média; eventos: nenhum; visão reiniciada 0 vezes; "
                      "ESP32: lost 0, corrupt 2; mqtt: 9 linhas publicadas, 0 não publicadas (desconectado), 1 saúde; "
                      "envio: 2 eventos enviados, 0 pendentes; "
                      "Pi até 61.2 °C, throttled sem avisos", logs.output)

    def test_resume_without_the_esp32_has_no_esp32_part(self):
        with self.assertLogs("vguard", level="INFO") as logs:
            self.run_app(["--headless", "--no-esp32", "--no-gps", "--no-sync"])
        self.assertIn("INFO:vguard:resumo: visão 13.8 FPS em média; eventos: nenhum; visão reiniciada 0 vezes; "
                      "Pi até 61.2 °C, throttled sem avisos", logs.output)

    def test_failure_while_starting_stops_what_started(self):
        order = []
        manager, reader = Mock(), Mock()
        manager.close.side_effect = lambda: order.append("database")
        reader.stop.side_effect = lambda: order.append("esp32")
        self.monitor.return_value.stop.side_effect = lambda: order.append("pi")
        with patch.object(vguard, "EventManager", return_value=manager), \
             patch.object(vguard, "Esp32Reader", return_value=reader), \
             patch.object(vguard, "ModemReader", side_effect=RuntimeError("GPS indisponível")):
            with self.assertRaisesRegex(RuntimeError, "GPS indisponível"):
                vguard.run(["--headless"])
        self.assertEqual(order, ["esp32", "pi", "database"])


if __name__ == "__main__":
    unittest.main()
