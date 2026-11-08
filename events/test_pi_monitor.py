"""
Testes da temperatura e do throttled da Pi (pi_monitor.py, proposta §6.5). Executar:

    python3 test_pi_monitor.py

Nenhum teste chama o vcgencmd: a leitura é injetada.
"""

import os
import subprocess
import tempfile
import threading
import time
import unittest

from pi_monitor import PiMonitor, read_sensors

# Bits do get_throttled e os nomes do contrato com o servidor (Issue #6).
BITS = {
    0: "undervoltage", 1: "freq_capped", 2: "throttling", 3: "soft_temp_limit",
    16: "undervoltage_since_boot", 17: "freq_capped_since_boot",
    18: "throttling_since_boot", 19: "soft_temp_limit_since_boot",
}


def vcgencmd(answers):
    """Um subprocess.run falso que responde como o vcgencmd da Pi."""
    def run(args, **kwargs):
        if args[0] != "vcgencmd":
            raise FileNotFoundError(args[0])
        return subprocess.CompletedProcess(args, 0, stdout=answers[args[1]] + "\n", stderr="")
    return run


def missing(args, **kwargs):
    raise FileNotFoundError(args[0])


class ReadSensorsTest(unittest.TestCase):
    def test_reads_measure_temp_and_get_throttled(self):
        run = vcgencmd({"measure_temp": "temp=58.4'C", "get_throttled": "throttled=0x50000"})
        self.assertEqual(read_sensors(run=run, thermal="/nao/existe"), (58.4, 0x50000))

    def test_without_vcgencmd_temperature_comes_from_thermal_zone(self):
        with tempfile.TemporaryDirectory() as directory:
            thermal = os.path.join(directory, "temp")
            with open(thermal, "w") as file:
                file.write("61250\n")
            self.assertEqual(read_sensors(run=missing, thermal=thermal), (61.25, None))

    def test_without_any_source_both_are_none(self):
        self.assertEqual(read_sensors(run=missing, thermal="/nao/existe"), (None, None))

    def test_garbled_vcgencmd_answer_counts_as_missing(self):
        run = vcgencmd({"measure_temp": "temp=?'C", "get_throttled": "throttled=zz"})
        self.assertEqual(read_sensors(run=run, thermal="/nao/existe"), (None, None))


class Readings:
    """Leitura injetada: cada poll() pega a próxima (temperatura, throttled)."""

    def __init__(self, *readings):
        self.readings = list(readings)

    def __call__(self):
        return self.readings.pop(0)


def polled(*readings):
    """Um PiMonitor que já fez um poll() por leitura; devolve ele e as linhas do log."""
    monitor = PiMonitor(read=Readings(*readings))
    with unittest.TestCase().assertLogs("pi", level="INFO") as logs:
        for _ in readings:
            monitor.poll()
    return monitor, logs.output


class PiMonitorTest(unittest.TestCase):
    def test_poll_keeps_values_and_logs_one_line(self):
        monitor, logs = polled((58.4, 0x0))
        self.assertEqual(monitor.temperature, 58.4)
        self.assertEqual(logs, ["INFO:pi:Pi: 58.4 °C, throttled 0x0"])

    def test_each_bit_of_get_throttled_has_its_contract_name(self):
        for bit, name in BITS.items():
            with self.subTest(bit=bit):
                monitor, _ = polled((50.0, 1 << bit))
                self.assertEqual(monitor.throttled, {other: other == name for other in BITS.values()})

    def test_bits_outside_the_eight_are_ignored(self):
        monitor, _ = polled((50.0, 0x50000 | 1 << 8 | 1 << 30))
        self.assertEqual([name for name, on in monitor.throttled.items() if on],
                         ["undervoltage_since_boot", "throttling_since_boot"])

    def test_missing_throttled_has_no_value_and_warns_once(self):
        monitor, logs = polled((61.2, None), (61.3, None), (61.4, None))
        self.assertIsNone(monitor.throttled)
        self.assertEqual(monitor.temperature, 61.4)
        self.assertEqual(len([line for line in logs if line.startswith("WARNING:")]), 1)
        self.assertIn("INFO:pi:Pi: 61.4 °C, throttled sem valor", logs)

    def test_missing_temperature_has_no_value_and_warns_once(self):
        monitor, logs = polled((None, 0x0), (None, 0x0))
        self.assertIsNone(monitor.temperature)
        self.assertEqual(len([line for line in logs if line.startswith("WARNING:")]), 1)
        self.assertIn("INFO:pi:Pi: temperatura sem valor, throttled 0x0", logs)

    def test_hot_warning_once_per_episode(self):
        # 75,0 não passa de 75; o 1º episódio vai de 75,5 a 76,0 e termina em 74,9.
        temperatures = (74.0, 75.0, 75.5, 77.0, 76.0, 74.9, 76.5, 78.0)
        _, logs = polled(*[(t, 0x0) for t in temperatures])
        warnings = [line for line in logs if line.startswith("WARNING:")]
        self.assertEqual(len(warnings), 2, warnings)
        self.assertIn("75.5 °C", warnings[0])
        self.assertIn("76.5 °C", warnings[1])

    def test_one_line_per_change_of_a_now_bit(self):
        # Liga a subtensão, liga a redução de clock, desliga a subtensão,
        # mexe só nos bits "desde o boot" e desliga a redução de clock.
        values = (0x0, 0x1, 0x1, 0x5, 0x4, 0x50004, 0x50000)
        _, logs = polled(*[(50.0, value) for value in values])
        changes = [line for line in logs if "ligou" in line or "desligou" in line]
        self.assertEqual(changes, [
            "WARNING:pi:Pi: undervoltage (subtensão) ligou, throttled 0x1",
            "WARNING:pi:Pi: throttling (redução de clock) ligou, throttled 0x5",
            "INFO:pi:Pi: undervoltage (subtensão) desligou, throttled 0x4",
            "INFO:pi:Pi: throttling (redução de clock) desligou, throttled 0x50000",
        ])

    def test_now_bit_already_on_at_the_first_reading_is_a_change(self):
        _, logs = polled((50.0, 0x2))
        self.assertIn("WARNING:pi:Pi: freq_capped (frequência limitada) ligou, throttled 0x2", logs)

    def test_summary_has_the_hottest_reading_and_the_throttled_warnings(self):
        monitor, _ = polled((60.0, 0x0), (63.5, 0x1), (62.0, 0x50000), (61.0, 0x50005))
        self.assertEqual(monitor.summary(),
                         "Pi até 63.5 °C, throttled com 3 avisos (undervoltage, throttling)")

    def test_summary_of_a_quiet_run(self):
        monitor, _ = polled((58.4, 0x50000), (57.9, 0x50000))
        self.assertEqual(monitor.summary(), "Pi até 58.4 °C, throttled sem avisos")

    def test_summary_without_sources(self):
        monitor, _ = polled((None, None))
        self.assertEqual(monitor.summary(), "Pi sem temperatura, throttled sem valor")

    def test_summary_with_a_single_warning(self):
        monitor, _ = polled((50.0, 0x1))
        self.assertEqual(monitor.summary(), "Pi até 50.0 °C, throttled com 1 aviso (undervoltage)")


class PiMonitorThreadTest(unittest.TestCase):
    def test_thread_reads_at_start_and_every_period_and_survives_a_failed_read(self):
        calls = []
        enough = threading.Event()

        def read():
            calls.append(time.monotonic())
            if len(calls) == 1:
                raise RuntimeError("leitura quebrada")
            if len(calls) >= 3:
                enough.set()
            return 50.0, 0x0

        monitor = PiMonitor(read=read, period=0.05)
        with self.assertLogs("pi", level="INFO") as logs:
            monitor.start()
            self.assertTrue(enough.wait(2), "a thread parou de ler")
            monitor.stop()
        self.assertFalse(monitor._thread.is_alive())
        self.assertTrue(any(line.startswith("ERROR:pi:") for line in logs.output), logs.output)
        self.assertEqual(monitor.temperature, 50.0)
        self.assertGreaterEqual(calls[2] - calls[1], 0.04)

    def test_stop_does_not_wait_for_the_period(self):
        monitor = PiMonitor(read=lambda: (50.0, 0x0), period=60)
        with self.assertLogs("pi", level="INFO"):
            monitor.start()
            started = time.monotonic()
            monitor.stop()
        self.assertLess(time.monotonic() - started, 1)


if __name__ == "__main__":
    unittest.main()
