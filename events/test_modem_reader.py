"""
Testes do leitor do modem (ticket 6, Issue #21): o GNSS e a hora pela porta
AT, com um modem falso num pty, como o ESP32 falso do ticket 1. Só a
biblioteca padrão e Linux. A partir de events/:

    python3 test_modem_reader.py
"""

import logging
import os
import pty
import tempfile
import threading
import time
import tty
import unittest
from unittest.mock import patch

import serial_port
from modem_reader import ModemReader, parse_cclk, parse_cgnssinfo, parse_power

# A linha real do A7670SA de 10/11, com a antena na janela.
FIX = "+CGNSSINFO: 3,10,,04,00,25.4361572,S,49.2688103,W,101126,214056.00,952.1,3.956,,4.40,2.46,3.65,05"
NO_FIX = "+CGNSSINFO: ,,,,,,,,"
CCLK = '+CCLK: "26/11/10,23:48:05-12"'


class ParseTest(unittest.TestCase):

    def test_the_line_of_10_11(self):
        sample, info = parse_cgnssinfo(FIX)
        self.assertEqual(sample, {"lat": -25.436157, "lon": -49.26881, "speed": 7.3})
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(info["utc"])), "2026-11-10 21:40:56")
        self.assertEqual((info["satellites"], info["hdop"]), (10, 2.46))

    def test_north_east_and_empty_speed(self):
        sample, _ = parse_cgnssinfo("+CGNSSINFO: 2,5,,,,48.1173,N,11.516667,E,010126,000000.0,10,,,1,1,1")
        self.assertEqual(sample, {"lat": 48.1173, "lon": 11.516667, "speed": 0.0})

    def test_without_position_is_empty(self):
        self.assertEqual(parse_cgnssinfo(NO_FIX), ({}, None))

    def test_unreadable_lines(self):
        for line in ("+CGNSSINFO: 3,10,,04,00,abc,S,49.2,W,101126,214056.00,952.1,3.9",
                     "+CGNSSINFO: 3,10", "OK", "+CGNSSINFO: 3,10,,04,00,25.4,S,49.2,W"):
            with self.subTest(line=line):
                self.assertIsNone(parse_cgnssinfo(line))

    def test_operator_clock_with_the_time_zone(self):
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(parse_cclk(CCLK))), "2026-11-11 02:48:05")
        self.assertEqual(parse_cclk('+CCLK: "26/11/10,23:48:05+00"') - parse_cclk(CCLK), -3 * 3600)
        self.assertIsNone(parse_cclk('+CCLK: "lixo"'))

    def test_power(self):
        self.assertEqual([parse_power(line) for line in ("+CGNSSPWR: 0,0,1", "+CGNSSPWR: 1,0,1",
                                                          "+CGNSSPWR: READY!", "OK")], [False, True, True, None])


class FakeModem:
    """
    O A7670SA falso: responde aos comandos AT pelo pty, com o eco até o ATE0.
    deaf: quantos comandos ignorar no começo (a porta ainda não pronta).
    power_after: quantos AT+CGNSSPWR? depois do =1 até o GNSS ligar.
    infos: as respostas do AT+CGNSSINFO, na ordem (a última se repete).
    """

    def __init__(self, link, deaf=0, gnss_on=False, power_after=2, infos=(FIX,), ready_urc=False):
        self.link = link
        self.deaf = deaf
        self.gnss_on = gnss_on
        self.power_after = power_after
        self.infos = list(infos)
        self.ready_urc = ready_urc
        self.echo = True
        self.commands = []
        self._polls = None
        self._stop = threading.Event()
        self.master = None
        self.plug()

    def plug(self):
        """Um pty novo, apontado pelo link (o by-id), como o modem aparecendo na USB."""
        self.master, slave = pty.openpty()
        tty.setraw(self.master)
        if os.path.lexists(self.link):
            os.remove(self.link)
        os.symlink(os.ttyname(slave), self.link)
        self._slave = slave
        self._stop = threading.Event()
        threading.Thread(target=self._serve, args=(self.master, self._stop), daemon=True).start()

    def unplug(self):
        self._stop.set()
        os.close(self.master)
        os.close(self._slave)

    def reboot(self):
        """O modem reiniciou: o GNSS volta desligado e o eco volta."""
        self.gnss_on = False
        self._polls = None

    def _serve(self, master, stop):
        buffer = b""
        while not stop.is_set():
            try:
                data = os.read(master, 1024)
            except OSError:
                return
            buffer += data
            while b"\r" in buffer:
                line, buffer = buffer.split(b"\r", 1)
                command = line.decode().strip()
                if command:
                    self.commands.append(command)
                    reply = self.answer(command)
                    if reply is not None:
                        try:
                            os.write(master, reply.encode())
                        except OSError:
                            return

    def answer(self, command):
        if self.deaf:
            self.deaf -= 1
            return None
        echo = command + "\r\n" if self.echo else ""
        if command == "ATE0":
            self.echo = False
            return echo + "OK\r\n"
        if command == "AT":
            return echo + "OK\r\n"
        if command == "AT+CGNSSPWR?":
            if not self.gnss_on and self._polls is not None:
                self._polls -= 1
                if self._polls <= 0:
                    self.gnss_on = True
            return echo + "+CGNSSPWR: %d,0,1\r\n\r\nOK\r\n" % self.gnss_on
        if command == "AT+CGNSSPWR=1":
            self._polls = self.power_after
            if self.ready_urc:
                self.gnss_on = True
                return echo + "OK\r\n\r\n+CGNSSPWR: READY!\r\n"
            return echo + "OK\r\n"
        if command == "AT+CGNSSINFO":
            if not self.gnss_on:
                return echo + "ERROR\r\n"
            info = self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]
            return echo + info + "\r\n\r\nOK\r\n"
        if command == "AT+CCLK?":
            return echo + CCLK + "\r\n\r\nOK\r\n"
        return echo + "ERROR\r\n"


class Recorder:
    """O EventManager falso: guarda os update()."""

    def __init__(self):
        self.updates = []

    def update(self, source, data, ts=None):
        self.updates.append((source, data))

    def gps(self):
        return [data for source, data in self.updates if source == "gps"]


FAST = dict(hello_interval=0.05, power_poll=0.05, power_timeout=0.6, fix_interval=0.05,
            clock_interval=0.3, reply_timeout=0.3, offset_log_interval=0.2)


class ReaderTest(unittest.TestCase):

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="vguard-teste-")
        self.addCleanup(folder.cleanup)
        self.link = os.path.join(folder.name, "modem-at")
        patcher = patch.object(serial_port, "REOPEN_S", 0.1)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = Recorder()

    def start(self, modem, **options):
        self.addCleanup(lambda: modem._stop.set())
        settings = dict(FAST, clock=lambda: 1794346856.3)      # a Pi 0,3 s à frente do GNSS
        settings.update(options)
        reader = ModemReader(self.manager, port=self.link, **settings)
        reader.start()
        self.addCleanup(reader.stop)
        return reader

    def wait_for(self, condition, seconds=3.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_start_turns_the_gnss_on_and_reads_the_position(self):
        modem = FakeModem(self.link, deaf=3, infos=(NO_FIX, NO_FIX, FIX))
        with self.assertLogs("modem", "INFO") as logs:
            reader = self.start(modem)
            self.assertTrue(self.wait_for(lambda: {"lat": -25.436157, "lon": -49.26881, "speed": 7.3}
                                          in self.manager.gps()), self.manager.updates)
            self.assertTrue(self.wait_for(lambda: reader.operator_time is not None))
        # A porta surda no começo, o eco desligado, o GNSS ligado e então a posição.
        commands = modem.commands
        self.assertEqual(commands[:3], ["AT"] * 3)
        self.assertLess(commands.index("ATE0"), commands.index("AT+CGNSSPWR=1"))
        self.assertLess(commands.index("AT+CGNSSPWR=1"), commands.index("AT+CGNSSINFO"))
        self.assertEqual(self.manager.gps()[:2], [{}, {}])     # sem posição: só o horário
        text = "\n".join(logs.output)
        for expected in ("porta AT pronta", "GNSS ligado em", "GNSS: primeira posição em",
                         "(10 satélites GPS, HDOP 2.46)", "relógio da Pi − GNSS: +0.3 s",
                         "relógio da Pi − operadora:"):
            self.assertIn(expected, text)
        self.assertEqual(reader.gnss_time[0], 1794346856.0)
        self.assertEqual(reader.operator_time[0], parse_cclk(CCLK))

    def test_gnss_already_on_skips_the_power_command(self):
        modem = FakeModem(self.link, gnss_on=True)
        self.start(modem)
        self.assertTrue(self.wait_for(lambda: self.manager.gps()))
        self.assertNotIn("AT+CGNSSPWR=1", modem.commands)

    def test_ready_message_also_counts(self):
        modem = FakeModem(self.link, power_after=1000, ready_urc=True)
        self.start(modem)
        self.assertTrue(self.wait_for(lambda: self.manager.gps()))

    def test_gnss_that_does_not_turn_on_is_tried_again(self):
        modem = FakeModem(self.link, power_after=1000)
        with self.assertLogs("modem", "WARNING") as logs:
            self.start(modem)
            self.assertTrue(self.wait_for(lambda: modem.commands.count("AT+CGNSSPWR=1") >= 2, 3))
        self.assertIn("GNSS não ligou em 1 s; tentando de novo", logs.output[0])

    def test_error_in_the_middle_turns_the_gnss_on_again(self):
        modem = FakeModem(self.link)
        reader = self.start(modem)
        self.assertTrue(self.wait_for(lambda: reader.fixes >= 2))
        with self.assertLogs("modem", "WARNING") as logs:
            modem.reboot()                          # o modem reiniciou: GNSS desligado
            self.assertTrue(self.wait_for(lambda: modem.commands.count("AT+CGNSSPWR=1") == 2))
            fixes = reader.fixes
            self.assertTrue(self.wait_for(lambda: reader.fixes > fixes))
        self.assertIn("GNSS desligou (ERROR); ligando de novo", logs.output[0])
        self.assertIn({}, self.manager.gps())       # sem posição durante a volta

    def test_lost_position_and_back_one_line_each(self):
        modem = FakeModem(self.link, gnss_on=True, infos=(FIX, NO_FIX, NO_FIX, NO_FIX, FIX))
        with self.assertLogs("modem", "INFO") as logs:
            self.start(modem)
            self.assertTrue(self.wait_for(lambda: self.manager.gps().count({}) >= 3 and self.manager.gps()[-1]))
            time.sleep(0.2)
        text = "\n".join(logs.output)
        self.assertEqual(text.count("GNSS: sem posição"), 1)
        self.assertEqual(text.count("GNSS: posição de volta depois de"), 1)

    def test_port_that_disappears_and_comes_back(self):
        modem = FakeModem(self.link, gnss_on=True)
        reader = self.start(modem)
        self.assertTrue(self.wait_for(lambda: reader.fixes >= 1))
        modem.unplug()                              # o cabo saiu
        self.assertTrue(self.wait_for(lambda: self.manager.gps()[-1] == {}))
        modem.plug()                                # voltou, num pty novo
        fixes = reader.fixes
        self.assertTrue(self.wait_for(lambda: reader.fixes > fixes, 4), "não voltou a ler")

    def test_silent_modem_restarts_the_start(self):
        modem = FakeModem(self.link, gnss_on=True)
        reader = self.start(modem)
        self.assertTrue(self.wait_for(lambda: reader.fixes >= 1))
        with self.assertLogs("modem", "WARNING") as logs:
            modem.deaf = 10**6                      # parou de responder
            self.assertTrue(self.wait_for(lambda: reader._state == "hello"))
        self.assertIn("modem não respondeu ao", logs.output[0])
        modem.deaf = 0
        fixes = reader.fixes
        self.assertTrue(self.wait_for(lambda: reader.fixes > fixes, 3))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
