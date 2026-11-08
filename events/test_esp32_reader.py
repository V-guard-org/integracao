"""
Testes do leitor do ESP32 (protocolo de texto, proposta §5). Executar:

    python3 test_esp32_reader.py

A conferência (Esp32Link) roda sem porta, com relógios inventados. O teste
de integração usa um ESP32 falso num pty (só Linux ou macOS).
"""

import logging
import os
import pty
import select
import tempfile
import time
import unittest

import esp32_reader
import serial_port
from esp32_reader import Esp32Link, Esp32Reader, frame, parse_line, parse_obd_hz
from event_manager import EventManager

WALL = 1793980812.0   # time.time() inventado, 2026-11-07


class FakeManager:
    def __init__(self):
        self.updates = []

    def update(self, source, data, ts=None):
        self.updates.append((source, data, ts))

    def of(self, source):
        return [data for s, data, _ in self.updates if s == source]


class Esp32:
    """Monta as linhas do ESP32 com a seq andando sozinha."""

    def __init__(self):
        self.seq = 0

    def line(self, kind, *fields, seq=None):
        seq = self.seq if seq is None else seq
        self.seq = seq + 1
        return frame(",".join([kind, str(seq), "1793980812440"] + [str(f) for f in fields]))

    def status(self, state="ATIVO", obd="LENDO", obd_hz="12.5", **kw):
        return self.line("S", 3, state, obd, "OK", "OK", 97, obd_hz, 0, **kw)

    def accel(self, ax=0.12, ay=-0.05, az=9.79, **kw):
        return self.line("A", ax, ay, az, **kw)

    def obd(self, field, value, **kw):
        return self.line("O", field, value, **kw)


class Harness:
    """Um Esp32Link com os relógios na mão do teste."""

    def __init__(self, clock_ok=True, obd_hz="12.5"):
        self.manager = FakeManager()
        self.clock = clock_ok
        self.link = Esp32Link(self.manager, clock_ok=lambda: self.clock, obd_hz=obd_hz)
        self.now = 100.0
        self.sent = []

    def feed(self, data=b"", advance=0.0):
        self.now += advance
        out = self.link.feed(data, self.now, WALL + self.now)
        self.sent += [line for line in out.split(b"\n") if line]
        return out

    def accept(self, esp32):
        """Passa das 10 linhas válidas seguidas com $A."""
        for _ in range(esp32_reader.LINES_TO_ACCEPT):
            self.feed(esp32.accel())
        assert self.link.accepted


def body(line):
    return line.rstrip(b"\n")


class Moldura(unittest.TestCase):

    def test_frame(self):
        self.assertEqual(frame("H,1793980812437"), b"$H,1793980812437*52\n")
        self.assertEqual(frame("C,obd_hz,5"), b"$C,obd_hz,5*52\n")

    def test_valid_lines(self):
        line = parse_line(b"$A,57,1793980812457,0.12,-0.05,9.79*6D")
        self.assertEqual((line.type, line.seq), ("A", 57))
        self.assertEqual(line.fields[3:], ["0.12", "-0.05", "9.79"])
        # \r antes do \n é aceito.
        self.assertEqual(parse_line(b"$O,61,1793980813020,rpm,876.25*02\r").seq, 61)

    def test_rejected_lines(self):
        self.assertEqual(parse_line(b"A,57,0,0.12*00"), "sem $")
        self.assertEqual(parse_line(b"$A,57,0,0.12"), "sem *hh")
        self.assertEqual(parse_line(b"$A,57,1793980812457,0.12,-0.05,9.79*6d"), "sem *hh")
        self.assertEqual(parse_line(b"$A,57,1793980812457,0.12,-0.05,9.79*6C"), "checksum errado")
        self.assertEqual(parse_line(body(frame("L,x,0,AVISO,y"))), "sem a seq")
        self.assertEqual(parse_line(b"ets Jun  8 2016 00:22:57"), "sem $")

    def test_120_characters_without_crlf(self):
        # $ + corpo + *hh = 120: aceita; 121: recusa.
        ok = body(frame("L,1,0,AVISO," + "x" * (120 - 4 - len("L,1,0,AVISO,"))))
        self.assertEqual(len(ok), 120)
        self.assertNotIsInstance(parse_line(ok + b"\r"), str)
        long = body(frame("L,1,0,AVISO," + "x" * (121 - 4 - len("L,1,0,AVISO,"))))
        self.assertEqual(parse_line(long), "mais de 120 caracteres")

    def test_obd_hz(self):
        self.assertEqual(parse_obd_hz("12.5"), 12.5)
        self.assertEqual(parse_obd_hz("5"), 5.0)
        self.assertEqual(parse_obd_hz("20"), 20.0)
        for text in ("0", "0.9", "21", "12.55", "abc", "", "-5"):
            self.assertIsNone(parse_obd_hz(text), text)


class Contagens(unittest.TestCase):

    def test_boot_text_is_not_corrupt(self):
        h, esp32 = Harness(), Esp32()
        h.feed(b"ets Jun  8 2016 00:22:57\r\n\r\nrst:0x1 (POWERON_RESET)\r\n$S,99,0,1,ESP")
        h.feed(b"\n" + esp32.line("B", "0.10.0-ca26fc4-sim", "LIGOU"))
        self.assertEqual((h.link.corrupt, h.link.ignored), (0, 4))

    def test_restart_after_boot_text(self):
        h, esp32 = Harness(), Esp32()
        h.accept(esp32)
        # Linha cortada pelo reinício, texto da ROM e o $B com seq 0.
        h.feed(b"$A,10,17939808\nets Jun  8 2016\r\n" + esp32.line("B", "x", "LIGOU", seq=0))
        self.assertEqual((h.link.corrupt, h.link.lost, h.link.restarts), (0, 0, 1))
        self.assertFalse(h.link.accepted)

    def test_corrupt_between_valid_lines(self):
        h, esp32 = Harness(), Esp32()
        h.feed(esp32.accel())
        h.feed(b"$A,1,0,0.1*00\n")
        h.feed(esp32.accel(seq=2))
        self.assertEqual((h.link.corrupt, h.link.lost), (1, 1))

    def test_gap_is_lost(self):
        h, esp32 = Harness(), Esp32()
        h.feed(esp32.accel())
        h.feed(esp32.accel(seq=5))
        self.assertEqual(h.link.lost, 4)

    def test_seq_wraps(self):
        h, esp32 = Harness(), Esp32()
        h.feed(esp32.accel(seq=0xFFFFFFFF))
        h.feed(esp32.accel(seq=0))
        self.assertEqual((h.link.lost, h.link.restarts), (0, 0))

    def test_seq_backwards_is_restart(self):
        h, esp32 = Harness(), Esp32()
        h.feed(esp32.accel(seq=500))
        h.feed(esp32.accel(seq=3))
        self.assertEqual((h.link.lost, h.link.restarts), (0, 1))

    def test_unknown_type_and_bad_fields(self):
        h, esp32 = Harness(), Esp32()
        h.accept(esp32)
        h.feed(esp32.line("Z", "novo", "tipo"))
        self.assertEqual(h.link.corrupt, 0)
        h.feed(esp32.line("A", "x", 1, 2))
        h.feed(esp32.line("O", "speed", "6.5"))
        self.assertEqual(h.link.corrupt, 2)

    def test_open_discard(self):
        # A rajada de blocos repetidos do CP2102 logo depois de abrir a porta.
        h, esp32 = Harness(), Esp32()
        h.link.port_opened(h.now)
        block = b"$O,134059577,-0.01,0.01,9.84*66\n"   # 32 bytes, como na bancada
        burst = esp32.accel(seq=40) + block * 300
        h.feed(burst, advance=0.05)
        h.feed(block * 10, advance=0.1)
        h.feed(b",9.81*48\n" + esp32.accel(seq=60), advance=0.1)
        h.feed(esp32.accel(), advance=0.02)
        self.assertEqual((h.link.corrupt, h.link.lost, h.link.ignored), (0, 0, 1))
        self.assertEqual(h.link.discarded_bytes, len(burst) + len(block) * 10)
        self.assertEqual(h.link.counts, {"A": 2})

    def test_line_without_newline_grows_too_much(self):
        h, esp32 = Harness(), Esp32()
        h.feed(esp32.accel())
        h.feed(b"$" + b"x" * 1000)
        h.feed(b"\n" + esp32.accel())
        self.assertEqual(h.link.corrupt, 2)


class Validacao(unittest.TestCase):

    def test_samples_after_10_lines(self):
        h, esp32 = Harness(), Esp32()
        for _ in range(9):
            h.feed(esp32.accel())
        self.assertEqual(h.manager.of("imu"), [])
        h.feed(esp32.accel(ax=1.25))
        self.assertEqual(h.manager.of("imu"), [{"ax": 1.25, "ay": -0.05, "az": 9.79}])
        # O ts é a hora da Pi na chegada.
        self.assertEqual(h.manager.updates[-1][2], WALL + h.now)

    def test_isolated_error_after_acceptance_keeps_samples(self):
        h, esp32 = Harness(), Esp32()
        h.accept(esp32)
        h.feed(b"$A,lixo*00\n")
        h.feed(esp32.accel(seq=esp32.seq + 3))
        self.assertTrue(h.link.accepted)
        self.assertEqual(len(h.manager.of("imu")), 2)

    def test_error_before_acceptance_restarts_count(self):
        h, esp32 = Harness(), Esp32()
        for _ in range(5):
            h.feed(esp32.accel())
        h.feed(esp32.accel(seq=esp32.seq + 1))   # lacuna
        for _ in range(8):
            h.feed(esp32.accel())
        self.assertFalse(h.link.accepted)
        h.feed(esp32.accel())
        self.assertTrue(h.link.accepted)

    def test_reopen_restarts_count(self):
        h, esp32 = Harness(), Esp32()
        h.accept(esp32)
        h.link.port_closed(WALL)
        h.link.port_opened(h.now)
        self.assertFalse(h.link.accepted)
        h.feed(advance=esp32_reader.OPEN_DISCARD_S)
        h.feed(b"lixo do meio de uma linha\n")
        h.accept(esp32)
        self.assertEqual((h.link.corrupt, h.link.lost), (0, 0))


class Hora(unittest.TestCase):

    def test_first_valid_line_sends_hour(self):
        h, esp32 = Harness(), Esp32()
        h.feed()
        self.assertEqual(h.sent, [])   # nada antes de alguma linha
        h.feed(esp32.status(state="ATIVO"), advance=0.5)
        self.assertEqual(h.sent, [body(frame("H,%d" % int((WALL + h.now) * 1000)))])

    def test_boot_and_waiting_status(self):
        h, esp32 = Harness(), Esp32()
        h.feed(esp32.line("B", "0.10.0", "LIGOU"))
        self.assertEqual(h.link.hours_sent, 1)
        # O $S que já vinha a caminho não repete o $H.
        h.feed(esp32.status(state="ESPERA", obd="PARADO"), advance=1)
        self.assertEqual(h.link.hours_sent, 1)
        h.feed(esp32.status(state="ESPERA", obd="PARADO"), advance=1)
        self.assertEqual(h.link.hours_sent, 2)
        h.feed(esp32.status(), advance=1)
        h.feed(esp32.line("B", "0.10.0", "LIGOU", seq=0), advance=0.1)
        self.assertEqual(h.link.hours_sent, 3)

    def test_every_60_s(self):
        h, esp32 = Harness(), Esp32()
        h.feed(esp32.status())
        for _ in range(59):
            h.feed(esp32.status(), advance=1)
        self.assertEqual(h.link.hours_sent, 1)
        h.feed(esp32.status(), advance=1)
        self.assertEqual(h.link.hours_sent, 2)

    def test_waits_for_clock(self):
        h, esp32 = Harness(clock_ok=False), Esp32()
        with self.assertLogs("esp32", "WARNING") as logs:
            for _ in range(3):
                h.feed(esp32.status(state="ESPERA", obd="PARADO"), advance=1)
        self.assertEqual(h.sent, [])
        self.assertEqual(sum("não sincronizada" in m for m in logs.output), 1)
        h.clock = True
        h.feed(advance=1)
        self.assertEqual(h.link.hours_sent, 1)
        self.assertTrue(h.sent[0].startswith(b"$H,"))

    def test_no_hour_to_silent_port(self):
        h, esp32 = Harness(), Esp32()
        h.feed(esp32.status())
        h.feed(advance=61)
        self.assertEqual(h.link.hours_sent, 1)


class Taxa(unittest.TestCase):

    def test_rate_differs(self):
        h, esp32 = Harness(obd_hz="5"), Esp32()
        h.feed(esp32.status(), advance=0)
        self.assertIn(b"$C,obd_hz,5*52", h.sent)
        h.feed(esp32.status(), advance=1)
        self.assertEqual(h.link.rates_sent, 1)   # pausa de 2 s
        h.feed(esp32.status(), advance=1)
        self.assertEqual(h.link.rates_sent, 2)
        h.feed(esp32.status(obd_hz="5.0"), advance=2)
        self.assertEqual(h.link.rates_sent, 2)

    def test_only_in_active(self):
        h, esp32 = Harness(obd_hz="5"), Esp32()
        h.feed(esp32.status(state="ESPERA", obd="PARADO"))
        self.assertEqual(h.link.rates_sent, 0)

    def test_one_decimal(self):
        h, esp32 = Harness(obd_hz="7.5"), Esp32()
        h.feed(esp32.status())
        self.assertIn(body(frame("C,obd_hz,7.5")), h.sent)

    def test_invalid_rate_sends_nothing(self):
        with self.assertLogs("esp32", "ERROR"):
            h, esp32 = Harness(obd_hz="25"), Esp32()
        h.feed(esp32.status())
        self.assertEqual(h.link.rates_sent, 0)


class Amostras(unittest.TestCase):

    def test_obd_types_and_age(self):
        h, esp32 = Harness(), Esp32()
        h.accept(esp32)
        h.feed(esp32.obd("rpm", "876.25"))
        h.feed(esp32.obd("speed", "60"), advance=0.08)
        h.feed(esp32.obd("voltage", "13.800"), advance=0.08)
        sample = h.manager.of("obd")[-1]
        self.assertEqual(sample, {"rpm": 876.25, "speed": 60, "voltage": 13.8,
                                  "age_ms": {"rpm": 160, "speed": 80, "voltage": 0}})
        self.assertIsInstance(sample["speed"], int)

    def test_unknown_obd_field_ignored(self):
        h, esp32 = Harness(), Esp32()
        h.accept(esp32)
        h.feed(esp32.obd("fuel", "33"))
        self.assertEqual(h.manager.of("obd"), [])

    def test_stale_obd_value_dropped(self):
        h, esp32 = Harness(), Esp32()
        h.accept(esp32)
        h.feed(esp32.obd("iat", "30"))
        h.feed(esp32.obd("rpm", "900.00"), advance=3.1)
        self.assertEqual(h.manager.of("obd")[-1], {"rpm": 900.0, "age_ms": {"rpm": 0}})


class SemDado(unittest.TestCase):

    def test_obd_not_reading(self):
        h, esp32 = Harness(), Esp32()
        h.accept(esp32)
        h.feed(esp32.obd("rpm", "876.25"))
        h.feed(esp32.status(obd="SEM_RESPOSTA"))
        self.assertEqual(h.manager.of("obd")[-1], {})
        # Os valores guardados foram apagados.
        h.feed(esp32.obd("speed", "10"))
        self.assertEqual(h.manager.of("obd")[-1], {"speed": 10, "age_ms": {"speed": 0}})

    def test_status_without_accel(self):
        h, esp32 = Harness(), Esp32()
        h.accept(esp32)
        h.feed(esp32.status())
        self.assertNotIn({}, h.manager.of("imu"))
        h.feed(esp32.status(), advance=1)
        self.assertEqual(h.manager.of("imu")[-1], {})

    def test_esp32_disappears(self):
        h, esp32 = Harness(), Esp32()
        h.accept(esp32)
        h.feed(advance=2.9)
        self.assertTrue(h.link.connected(h.now))
        with self.assertLogs("esp32", "WARNING"):
            h.feed(advance=0.2)
        self.assertFalse(h.link.connected(h.now))
        self.assertEqual((h.manager.of("imu")[-1], h.manager.of("obd")[-1]), ({}, {}))
        # Uma vez só.
        count = len(h.manager.updates)
        h.feed(advance=5)
        h.link.port_closed(WALL)
        self.assertEqual(len(h.manager.updates), count)

    def test_nothing_before_acceptance(self):
        h, esp32 = Harness(), Esp32()
        h.feed(esp32.status(state="ESPERA", obd="PARADO"))
        h.feed(advance=4)
        self.assertEqual(h.manager.updates, [])


class Log(unittest.TestCase):

    def test_rare_lines_and_changes(self):
        h, esp32 = Harness(), Esp32()
        with self.assertLogs("esp32", "INFO") as logs:
            h.feed(esp32.line("B", "0.10.0-ca26fc4-sim", "LIGOU"))
            h.feed(esp32.status(state="ESPERA", obd="PARADO"))
            h.feed(esp32.status(state="ATIVO", obd="ESCUTANDO"))
            h.feed(esp32.line("P", "183A8001", "00000001", "40000000"))
            h.feed(esp32.line("L", "AVISO", "PID 0F sem resposta"))
            h.feed(esp32.line("L", "ERRO", "IMU sem resposta"))
        text = "\n".join(logs.output)
        self.assertIn("firmware 0.10.0-ca26fc4-sim, motivo LIGOU", text)
        self.assertIn("ESP32 estado: ESPERA → ATIVO", text)
        self.assertIn("ESP32 obd: PARADO → ESCUTANDO", text)
        self.assertIn("183A8001 00000001 40000000", text)
        self.assertIn("WARNING:esp32:ESP32: PID 0F sem resposta", text)
        self.assertIn("ERROR:esp32:ESP32: IMU sem resposta", text)


class FakeEsp32OnPty(unittest.TestCase):
    """O Esp32Reader de verdade, com a thread e a porta, contra um ESP32 falso num pty."""

    def test_reader_and_event_manager(self):
        serial_port.REOPEN_S = 0.2
        data_dir = tempfile.mkdtemp()
        manager = EventManager(data_dir)
        master, slave = pty.openpty()
        reader = Esp32Reader(manager, os.ttyname(slave), clock_ok=lambda: True, obd_hz="12.5")
        reader.start()
        # Ao abrir a porta, o tty.setraw descarta o que já estava no buffer.
        time.sleep(0.5)
        esp32 = Esp32()
        received = b""

        def read_master(timeout):
            nonlocal received
            ready, _, _ = select.select([master], [], [], timeout)
            if ready:
                received += os.read(master, 4096)

        try:
            os.write(master, b"ets Jun  8 2016\r\n" + esp32.line("B", "0.10.0-sim", "LIGOU"))
            os.write(master, esp32.status(state="ESPERA", obd="PARADO"))
            read_master(1.0)
            self.assertRegex(received, rb"^\$H,\d{13}\*[0-9A-F]{2}\n")

            # 3 s em ATIVO: 50 $A e 12,5 $O por segundo, como o esp32-sim.
            fields = ["rpm", "speed", "coolant", "load", "map", "iat", "throttle", "voltage"]
            values = ["876.25", "0", "47", "21.6", "33", "30", "14.9", "13.800"]
            start = time.monotonic()
            for tick in range(150):
                chunk = esp32.accel()
                if tick % 4 == 0:
                    chunk += esp32.obd(fields[tick // 4 % 8], values[tick // 4 % 8])
                if tick % 50 == 0:
                    chunk += esp32.status()
                os.write(master, chunk)
                time.sleep(max(0, start + (tick + 1) * 0.02 - time.monotonic()))
                read_master(0)
            self.assertEqual(reader.health(), {"connected": True, "lost": 0, "corrupt": 0})
            event = manager.record_event("fadiga", 0.9)

            # O ESP32 some: 3 s depois, connected falso.
            time.sleep(3.3)
            self.assertFalse(reader.health()["connected"])
            gone = manager.record_event("distracao", 0.9)
        finally:
            reader.stop()
            os.close(master)
            os.close(slave)

        rows = manager.pending_telemetry(10**6)
        manager.close()
        self.assertGreaterEqual(len(rows), 3)
        # A primeira linha é a do primeiro update (o EventManager grava na hora).
        self.assertTrue(all({"imu", "obd"} <= set(r["data"]) for r in rows[1:3]))

        events = {e["id"]: e["data"] for e in EventManager(data_dir).pending_events()}
        self.assertEqual(set(events[event]["obd"]) - {"ts", "age_ms"}, set(fields))
        # Com o ESP32 desaparecido, o evento não leva valor antigo: só o horário.
        self.assertEqual((set(events[gone]["imu"]), set(events[gone]["obd"])), ({"ts"}, {"ts"}))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
