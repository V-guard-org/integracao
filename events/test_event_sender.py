"""
Testes do envio dos eventos (ticket 3, Issue #16), contra o servidor falso e
com o EventManager de verdade numa pasta temporária. A partir de events/:

    python3 test_event_sender.py
"""

import json
import logging
import os
import tempfile
import time
import unittest

from event_manager import CONFIRMED, FAILED, PENDING, EventManager
from event_sender import DONE, FAILURE, NO_LINK, EventSender
from fake_server import MAX_PHOTO, FakeServer

# Um "JPEG" com todos os bytes, inclusive \r e \n, para conferir que a foto chega inteira.
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 40 + b"\xff\xd9"


class SenderTest(unittest.TestCase):

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="vguard-teste-")
        self.addCleanup(folder.cleanup)
        self.manager = EventManager(os.path.join(folder.name, "dados"))
        self.addCleanup(self.manager.close)
        self.server = FakeServer(os.path.join(folder.name, "servidor"), token="segredo", slow_s=1.0).start()
        self.addCleanup(self.server.stop)
        self.link = "wifi"
        self.sender = EventSender(self.manager, link=lambda: self.link, url=self.server.url,
                                  token="segredo", device="vguard-teste", timeout=0.5)

    def record(self, type="fadiga", ts=1000.0, score=0.82, jpeg=JPEG):
        return self.manager.record_event(type, score, jpeg, ts)

    def state(self, event_id):
        return self.manager._db.execute("SELECT synced FROM events WHERE id = ?", (event_id,)).fetchone()[0]


class RequestTest(SenderTest):

    def test_request_follows_the_contract(self):
        self.manager.update("imu", {"ax": 0.11, "ay": -0.02, "az": 9.8}, 999.9)
        event_id = self.record(ts=1000.4374)
        self.assertEqual(self.sender.send_pending(), DONE)
        path, headers = self.server.requests[0]
        self.assertEqual(path, "/api/events")
        self.assertEqual((headers["Authorization"], headers["X-Device-Id"]), ("Bearer segredo", "vguard-teste"))
        self.assertTrue(headers["Content-Type"].startswith("multipart/form-data; boundary="))
        record, = self.server.received()
        self.assertEqual((record["ts"], record["type"], record["score"]), (1000.437, "fadiga", 0.82))
        self.assertEqual(record["telemetria"], {"imu": {"ts": 999.9, "ax": 0.11, "ay": -0.02, "az": 9.8}})
        with open(os.path.join(self.server.folder, record["foto"]), "rb") as f:
            self.assertEqual(f.read(), JPEG)
        self.assertEqual(self.state(event_id), CONFIRMED)
        self.assertEqual((self.sender.sent, self.manager.pending_counts()["events"]), (1, 0))

    def test_dados_has_exactly_the_four_fields_of_the_contract(self):
        self.record()
        self.sender.send_pending()
        with open(os.path.join(self.server.folder, "eventos.jsonl"), encoding="utf-8") as f:
            record = json.loads(f.readline())
        self.assertEqual({k for k in record if k in ("ts", "type", "score", "telemetria")},
                         {"ts", "type", "score", "telemetria"})

    def test_one_at_a_time_oldest_first(self):
        ids = [self.record("distracao", 1001.0), self.record("fadiga", 1000.0), self.record("fadiga", 1010.0)]
        self.assertEqual(self.sender.send_pending(), DONE)
        self.assertEqual([(r["type"], r["ts"]) for r in self.server.received()],
                         [("fadiga", 1000.0), ("distracao", 1001.0), ("fadiga", 1010.0)])
        self.assertEqual([self.state(i) for i in ids], [CONFIRMED] * 3)


class ResponseTest(SenderTest):

    def test_resend_of_a_stored_event_is_200_without_duplicate(self):
        event_id = self.record()
        self.sender.send_pending()
        self.manager.ack_event(event_id, PENDING)       # como se a confirmação tivesse se perdido
        self.assertEqual(self.sender.send_pending(), DONE)
        self.assertEqual(len(self.server.received()), 1)
        self.assertEqual(self.state(event_id), CONFIRMED)

    def test_lost_response_stays_pending_and_the_resend_does_not_duplicate(self):
        event_id = self.record()
        self.server.faults = ["slow"]                   # grava, mas responde depois do tempo de espera
        self.assertEqual(self.sender.send_pending(), FAILURE)
        self.assertEqual(self.state(event_id), PENDING)
        self.assertEqual(len(self.server.received()), 1)
        time.sleep(1.0)                                 # o servidor termina a resposta atrasada
        self.assertEqual(self.sender.send_pending(), DONE)
        self.assertEqual(len(self.server.received()), 1)
        self.assertEqual(self.state(event_id), CONFIRMED)

    def test_400_and_413_become_failed_and_the_queue_moves_on(self):
        rejected = self.record("fadiga", 1000.0)
        too_big = self.record("distracao", 1001.0, jpeg=b"\xff\xd8" + b"x" * MAX_PHOTO)
        accepted = self.record("fadiga", 1010.0)
        self.server.faults = [400]
        with self.assertLogs("events.sender", "WARNING") as logs:
            self.assertEqual(self.sender.send_pending(), DONE)
        self.assertEqual([self.state(i) for i in (rejected, too_big, accepted)], [FAILED, FAILED, CONFIRMED])
        self.assertIn("servidor recusou o evento %d (HTTP 400): fica no cartão" % rejected, logs.output[0])
        self.assertIn("(HTTP 413)", logs.output[1])
        # Recusado sai da fila, mas a foto fica no cartão.
        self.assertEqual(len(os.listdir(self.manager.photo_dir)), 3)

    def test_401_500_and_dropped_connection_stay_pending(self):
        event_id = self.record()
        for fault in (401, 500, 503, "drop"):
            with self.subTest(fault=fault):
                self.server.faults = [fault]
                self.assertEqual(self.sender.send_pending(), FAILURE)
                self.assertEqual(self.state(event_id), PENDING)
        self.assertEqual(self.server.received(), [])
        self.assertEqual(self.sender.send_pending(), DONE)

    def test_wrong_token_is_401(self):
        self.record()
        sender = EventSender(self.manager, link=lambda: "wifi", url=self.server.url, token="errado",
                             device="vguard-teste", timeout=0.5)
        with self.assertLogs("events.sender", "ERROR") as logs:
            self.assertEqual(sender.send_pending(), FAILURE)
        self.assertIn("token recusado (HTTP 401): confira o VGUARD_TOKEN", logs.output[0])

    def test_wait_grows_from_60_s_to_10_min_and_returns_to_60_s(self):
        self.record()
        sender = EventSender(self.manager, link=lambda: "wifi", url=self.server.url, token="segredo",
                             device="vguard-teste", timeout=0.5)
        waits = []
        for _ in range(6):
            self.server.faults = [500]
            sender.send_pending()
            waits.append(sender.retry_in)
        self.assertEqual(waits, [60, 120, 240, 480, 600, 600])
        sender.send_pending()                           # o servidor voltou
        self.assertEqual(sender.wait, 60)

    def test_one_log_line_per_failure_episode(self):
        self.record()
        with self.assertLogs("events.sender", "INFO") as logs:
            for _ in range(3):
                self.server.faults = [503]
                self.sender.send_pending()
            self.sender.send_pending()
        self.assertEqual(logs.output[0], "WARNING:events.sender:envio de eventos falhou (HTTP 503); "
                                         "nova tentativa em 60 s")
        self.assertEqual(logs.output[1], "INFO:events.sender:envio de eventos de volta depois de 3 tentativas")
        self.assertTrue(logs.output[2].startswith("INFO:events.sender:evento 1 enviado (fadiga, "))
        self.assertEqual(len(logs.output), 3)


class QueueTest(SenderTest):

    def test_without_link_nothing_goes_out(self):
        self.record()
        self.link = None
        with self.assertLogs("events.sender", "INFO") as logs:
            self.assertEqual(self.sender.send_pending(), NO_LINK)
        self.assertIn("sem enlace; 1 evento espera no cartão", logs.output[0])
        self.assertEqual(self.server.requests, [])
        self.link = "cellular"
        self.assertEqual(self.sender.send_pending(), DONE)

    def test_missing_photo_becomes_failed_and_does_not_block(self):
        gone = self.record("fadiga", 1000.0)
        kept = self.record("distracao", 1001.0)
        os.remove(self.manager.pending_events()[0]["photo"])
        with self.assertLogs("events.sender", "WARNING"):
            self.assertEqual(self.sender.send_pending(), DONE)
        self.assertEqual((self.state(gone), self.state(kept)), (FAILED, CONFIRMED))

    def test_summary(self):
        self.record("fadiga", 1000.0)
        self.record("distracao", 1001.0)
        self.server.faults = [200, 500]
        self.sender.send_pending()
        self.assertEqual(self.sender.summary(), "envio: 1 evento enviado, 1 pendente")


class ThreadTest(SenderTest):
    """O laço de verdade, com a thread e as esperas encurtadas."""

    def wait_for(self, condition, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.05)
        return False

    def test_new_event_goes_out_right_away_but_not_during_the_wait(self):
        sender = EventSender(self.manager, link=lambda: "wifi", url=self.server.url, token="segredo",
                             device="vguard-teste", timeout=0.5, first_wait=2.0)
        sender.start()
        self.addCleanup(sender.stop)
        self.record("fadiga", 1000.0)
        self.assertTrue(self.wait_for(lambda: len(self.server.received()) == 1, 1.5), "não saiu logo")

        self.server.faults = [500]
        failed_at = time.monotonic()
        self.record("distracao", 1001.0)                # falha: a espera de 2 s começa
        self.assertTrue(self.wait_for(lambda: sender.retry_in == 2.0, 1.5))
        self.record("fadiga", 1010.0)                   # não acorda o envio durante a espera
        self.assertFalse(self.wait_for(lambda: len(self.server.received()) > 1, 1.0))
        self.assertTrue(self.wait_for(lambda: len(self.server.received()) == 3, 3.0), "não saiu depois da espera")
        self.assertGreaterEqual(time.monotonic() - failed_at, 2.0)
        self.assertEqual(self.manager.pending_counts()["events"], 0)

    def test_pending_events_from_before_go_out_at_start(self):
        self.record()
        sender = EventSender(self.manager, link=lambda: "wifi", url=self.server.url, token="segredo",
                             device="vguard-teste", timeout=0.5)
        sender.start()
        self.addCleanup(sender.stop)
        self.assertTrue(self.wait_for(lambda: self.manager.pending_counts()["events"] == 0, 1.5))

    def test_stop_returns_quickly(self):
        sender = EventSender(self.manager, link=lambda: "wifi", url=self.server.url, token="segredo",
                             device="vguard-teste", timeout=0.5)
        sender.start()
        time.sleep(0.2)
        started = time.monotonic()
        sender.stop()
        self.assertLess(time.monotonic() - started, 1.5)


class FakeServerTest(unittest.TestCase):

    def test_stored_events_survive_a_restart(self):
        with tempfile.TemporaryDirectory(prefix="vguard-teste-") as folder:
            manager = EventManager(os.path.join(folder, "dados"))
            server = FakeServer(os.path.join(folder, "servidor")).start()
            sender = EventSender(manager, link=lambda: "wifi", url=server.url, token="t", device="d", timeout=0.5)
            event_id = manager.record_event("fadiga", 0.9, JPEG, 1000.0)
            sender.send_pending()
            server.stop()
            again = FakeServer(os.path.join(folder, "servidor")).start()
            manager.ack_event(event_id, PENDING)
            sender.url = again.url
            self.assertEqual(sender.send_pending(), DONE)
            self.assertEqual(len(again.received()), 1)
            again.stop()
            manager.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)     # os avisos esperados não poluem a saída
    unittest.main()
