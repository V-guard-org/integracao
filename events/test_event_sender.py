"""
Testes do envio dos eventos (ticket 4, Issue #18; contrato 2.1): o evento pelo
MQTT, com a janela, e a foto pelo PUT que confirma o evento. Contra o servidor
falso e com o EventManager de verdade numa pasta temporária. O MQTT aqui é um
publicador falso, que entrega o evento direto ao consumidor do servidor falso
(o MQTT de verdade está no test_mqtt_publisher.py). Só a biblioteca padrão. A
partir de events/:

    python3 test_event_sender.py
"""

import json
import logging
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from event_manager import CONFIRMED, FAILED, PENDING, EventManager
from event_sender import (DONE, FAILURE, MAX_MESSAGE, NO_LINK, NOT_YET, EventSender, event_id,
                          event_message, window_entries)
from fake_server import MAX_PHOTO, FakeServer
from http_demo_publisher import HttpDemoPublisher
import simulated_telemetry

# Um "JPEG" com todos os bytes, inclusive \r e \n, para conferir que a foto chega inteira.
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 40 + b"\xff\xd9"

OBD = {"rpm": 1851.0, "speed": 21, "coolant": 87, "load": 21.6, "map": 33, "iat": 30,
       "throttle": 14.9, "voltage": 13.8}
AGE = {"age_ms": {name: 900 for name in OBD}}


class FakePublisher:
    """O publicador MQTT, sem broker: o evento vai direto ao consumidor do servidor falso."""

    def __init__(self, server, device):
        self.server = server
        self.device = device
        self.connected = True
        self.puback = True
        self.messages = []
        self.connection_changed = threading.Event()

    def publish_event(self, text):
        if not self.connected or not self.puback:
            return False
        payload = json.loads(text)
        self.messages.append(payload)
        if self.server.consumer_enabled:
            payload = dict(payload)
            self.server.store_event(payload.pop("event_id"), self.device, payload)
        return True


class SenderTest(unittest.TestCase):

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="vguard-teste-")
        self.addCleanup(folder.cleanup)
        self.manager = EventManager(os.path.join(folder.name, "dados"))
        self.addCleanup(self.manager.close)
        self.server = FakeServer(os.path.join(folder.name, "servidor"), token="segredo", slow_s=1.0).start()
        self.addCleanup(self.server.stop)
        self.publisher = FakePublisher(self.server, "vguard-teste")
        self.link = "wifi"
        self.now = 10**9                    # bem depois de todos os eventos: as janelas fechadas
        self.sender = self.new_sender()

    def new_sender(self, **options):
        settings = dict(link=lambda: self.link, url=self.server.url, token="segredo", device="vguard-teste",
                        timeout=0.5, put_delay=0, conflict_wait=0.01, clock=lambda: self.now)
        settings.update(options)
        return EventSender(self.manager, self.publisher, **settings)

    def send(self, sender=None):
        return (sender or self.sender).send_pending()[0]

    def record(self, type="fadiga", ts=1000.0, score=0.82, jpeg=JPEG):
        return self.manager.record_event(type, score, jpeg, ts)

    def state(self, local_id):
        return self.manager._db.execute("SELECT synced FROM events WHERE id = ?", (local_id,)).fetchone()[0]

    def photo_puts(self):
        return [path for method, path, _ in self.server.requests if path.endswith("/photo")]


class MessageTest(unittest.TestCase):

    def test_event_id_of_the_contract(self):
        self.assertEqual(event_id("bancada-01", 1793980812.437, "fadiga"), "79cd9979-1ca0-5bfe-946f-b8aad0c5fee1")
        self.assertEqual(event_id("bancada-01", 1793980812.4371, "fadiga"),
                         event_id("bancada-01", 1793980812.437, "fadiga"))
        self.assertNotEqual(event_id("vguard-01", 1793980812.437, "fadiga"),
                            event_id("bancada-01", 1793980812.437, "fadiga"))

    def test_window_entries_without_ts_age_ms_and_sensors_without_data(self):
        rows = [{"ts": 991.0, "data": {"imu": {"ts": 991.5, "ax": 0.1},
                                       "obd": {"ts": 991.2, "rpm": 800.0, **AGE},
                                       "gps": {"ts": 991.9}}}]
        self.assertEqual(window_entries(rows), [
            {"ts": 991.0, "source": "imu", "data": {"ax": 0.1}},
            {"ts": 991.0, "source": "obd", "data": {"rpm": 800.0}},
        ])

    def full_rows(self, ts):
        return [{"ts": float(s), "data": {"imu": {"ts": s + 0.5, "ax": 0.11, "ay": -0.02, "az": 9.8},
                                          "obd": {"ts": s + 0.2, **OBD, **AGE},
                                          "gps": {"ts": s + 0.9, "lat": -25.4284, "lon": -49.2733, "speed": 20.4}}}
                for s in range(int(ts) - 10, int(ts) + 11)]

    def test_full_window_fits_in_the_broker_limit(self):
        event = {"ts": 1000.437, "type": "fadiga", "score": 0.82,
                 "telemetria": {"obd": {"ts": 1000.2, **OBD, **AGE}}}
        text, entries, seconds, trimmed = event_message(event, "bancada-01", self.full_rows(1000.437))
        message = json.loads(text)
        self.assertEqual(set(message), {"event_id", "ts", "type", "score", "has_photo", "data", "window"})
        self.assertEqual((entries, seconds, trimmed), (63, 21, False))
        self.assertTrue(message["has_photo"])
        self.assertEqual(message["data"], event["telemetria"])     # o retrato leva o age_ms
        self.assertLess(len(text), 8 * 1024)
        self.assertLess(len(text), MAX_MESSAGE)

    def test_window_above_the_limit_loses_the_ends(self):
        event = {"ts": 1000.437, "type": "fadiga", "score": 0.82, "telemetria": {}}
        text, entries, seconds, trimmed = event_message(event, "d", self.full_rows(1000.437), limit=3000)
        self.assertTrue(trimmed)
        self.assertLessEqual(len(text), 3000)
        kept = sorted({entry["ts"] for entry in json.loads(text)["window"]})
        self.assertEqual(len(kept), seconds)
        # O que sobra fica em volta do evento.
        self.assertLessEqual(kept[0], 1000.0)
        self.assertGreaterEqual(kept[-1], 1000.0)
        self.assertLessEqual(abs((kept[0] + kept[-1]) / 2 - 1000.437), 1.0)

    def test_demo_publishes_health_and_only_confirms_accepted_telemetry(self):
        row = {"id": 1, "ts": 1000.0, "data": {"gps": {"lat": -23.55, "lon": -46.65}}}
        for failure in (None, OSError("HTTP 503")):
            with self.subTest(failure=failure):
                manager = Mock()
                manager.pending_telemetry.return_value = [row]
                publisher = HttpDemoPublisher(manager)
                health = {"link": "cellular"}
                with patch("sync.active_link", return_value="cellular"), \
                     patch("sync.health", return_value=health), \
                     patch.object(publisher._stop, "wait", side_effect=lambda _: publisher.stop()), \
                     patch.object(publisher, "_request", side_effect=[None, failure]) as request:
                    publisher._run()
                self.assertEqual(
                    [(call.args[0], call.args[1], json.loads(call.args[2])) for call in request.call_args_list],
                    [("POST", "/health", health), ("POST", "/telemetry", {"items": [row]})],
                )
                if failure is None:
                    manager.ack_telemetry.assert_called_once_with([row["id"]])
                else:
                    manager.ack_telemetry.assert_not_called()


class RequestTest(SenderTest):

    def test_demo_sends_event_with_simulated_telemetry_and_photo_by_http(self):
        simulation = simulated_telemetry.SimulatedTelemetry(self.manager)
        for second in range(989, 1012):
            with patch.object(simulated_telemetry.time, "time", return_value=second + 0.25):
                simulation._publish(second - 989)
            if second == 1000:
                local_id = self.record(ts=1000.5)

        with patch.multiple("sync", BACKEND_URL=self.server.url, DEVICE_ID="vguard-teste"), \
             patch("sync.device_token", return_value="segredo"):
            self.publisher = HttpDemoPublisher(self.manager)
            sender = self.new_sender()
            self.assertEqual(self.send(sender), DONE)

        uuid_text = event_id("vguard-teste", 1000.5, "fadiga")
        self.assertEqual([(method, path) for method, path, _ in self.server.requests], [
            ("PUT", f"/api/events/{uuid_text}"),
            ("PUT", f"/api/events/{uuid_text}/photo"),
        ])
        event = self.server.events[uuid_text]
        self.assertEqual(set(event["data"]), {"imu", "obd", "gps"})
        self.assertEqual(event["data"]["obd"]["speed"], event["data"]["gps"]["speed"])
        self.assertEqual({item["source"] for item in event["window"]}, {"imu", "obd", "gps"})
        with open(os.path.join(self.server.folder, event["foto"]), "rb") as photo:
            self.assertEqual(photo.read(), JPEG)
        self.assertEqual(self.state(local_id), CONFIRMED)

    def test_event_by_mqtt_and_photo_by_put(self):
        for second in range(985, 1000):         # a telemetria de 985 a 1015, com o evento no meio
            self.manager.update("imu", {"ax": second}, second + 0.5)
        local_id = self.record(ts=1000.4374)
        for second in range(1000, 1016):
            self.manager.update("imu", {"ax": second}, second + 0.5)
        self.assertEqual(self.send(), DONE)

        message, = self.publisher.messages
        uuid_text = event_id("vguard-teste", 1000.437, "fadiga")
        self.assertEqual(message["event_id"], uuid_text)
        self.assertEqual((message["ts"], message["type"], message["score"]), (1000.437, "fadiga", 0.82))
        self.assertEqual(message["data"], {"imu": {"ts": 1000.5 - 1, "ax": 999}})
        self.assertEqual([entry["ts"] for entry in message["window"]], [990.0 + n for n in range(21)])

        method, path, headers = self.server.requests[-1]
        self.assertEqual((method, path), ("PUT", "/api/events/%s/photo" % uuid_text))
        self.assertEqual((headers["Authorization"], headers["X-Device-Id"], headers["Content-Type"]),
                         ("Bearer segredo", "vguard-teste", "image/jpeg"))
        record = self.server.events[uuid_text]
        with open(os.path.join(self.server.folder, record["foto"]), "rb") as f:
            self.assertEqual(f.read(), JPEG)
        self.assertEqual(self.state(local_id), CONFIRMED)
        self.assertEqual(self.sender.history[local_id]["puts"], [200])
        self.assertEqual((self.sender.sent, self.manager.pending_counts()["events"]), (1, 0))

    def test_only_after_the_window_closes(self):
        self.record(ts=1000.437)
        self.now = 1012.4
        outcome, delay = self.sender.send_pending()
        self.assertEqual(outcome, NOT_YET)
        self.assertAlmostEqual(delay, 0.037, places=3)
        self.assertEqual(self.publisher.messages, [])
        self.now = 1012.437
        self.assertEqual(self.send(), DONE)

    def test_one_at_a_time_oldest_first(self):
        ids = [self.record("distracao", 1001.0), self.record("fadiga", 1000.0), self.record("fadiga", 1010.0)]
        self.assertEqual(self.send(), DONE)
        self.assertEqual([(m["type"], m["ts"]) for m in self.publisher.messages],
                         [("fadiga", 1000.0), ("distracao", 1001.0), ("fadiga", 1010.0)])
        self.assertEqual([self.state(i) for i in ids], [CONFIRMED] * 3)


class ResponseTest(SenderTest):

    def test_resend_of_a_stored_event_is_200_without_duplicate(self):
        local_id = self.record()
        self.send()
        self.manager.ack_event(local_id, PENDING)        # como se a confirmação tivesse se perdido
        self.assertEqual(self.send(), DONE)
        self.assertEqual(len(self.publisher.messages), 2)
        self.assertEqual(self.publisher.messages[0]["event_id"], self.publisher.messages[1]["event_id"])
        self.assertEqual(len(self.server.events), 1)
        self.assertEqual(self.state(local_id), CONFIRMED)

    def test_lost_response_stays_pending_and_the_resend_does_not_duplicate(self):
        local_id = self.record()
        self.server.faults = ["slow"]                   # grava, mas responde depois do tempo de espera
        self.assertEqual(self.send(), FAILURE)
        self.assertEqual(self.state(local_id), PENDING)
        time.sleep(1.0)                                 # o servidor termina a resposta atrasada
        self.assertEqual(self.send(), DONE)
        self.assertEqual(len(self.publisher.messages), 1)   # sem publicar de novo: só a foto faltava
        self.assertEqual(len(self.server.events), 1)
        self.assertEqual(self.state(local_id), CONFIRMED)

    def test_409_until_the_consumer_writes(self):
        local_id = self.record()
        self.server.faults = [409, 409]                 # o consumidor demorou um pouco
        self.assertEqual(self.send(), DONE)
        self.assertEqual(self.sender.history[local_id]["puts"], [409, 409, 200])
        self.assertEqual(len(self.publisher.messages), 1)
        self.assertEqual(self.state(local_id), CONFIRMED)

    def test_broken_consumer_gives_409_x3_and_the_event_is_published_again(self):
        local_id = self.record()
        self.server.consumer_enabled = False
        with self.assertLogs("events.sender", "WARNING") as logs:
            self.assertEqual(self.send(), FAILURE)
        self.assertIn("evento %d: o servidor não gravou o evento (409 x3)" % local_id, logs.output[0])
        self.assertEqual(self.sender.history[local_id]["puts"], [409, 409, 409])
        self.assertEqual(self.sender.retry_in, 60)
        self.assertEqual(self.state(local_id), PENDING)

        self.server.consumer_enabled = True             # o consumidor voltou
        self.assertEqual(self.send(), DONE)
        self.assertEqual(len(self.publisher.messages), 2)   # publicado de novo, com o mesmo event_id
        self.assertEqual(len(self.server.events), 1)
        self.assertEqual(self.sender.history[local_id]["puts"], [409, 409, 409, 200])
        self.assertEqual(self.state(local_id), CONFIRMED)

    def test_400_and_413_become_failed_and_the_queue_moves_on(self):
        rejected = self.record("fadiga", 1000.0)
        too_big = self.record("distracao", 1001.0, jpeg=b"\xff\xd8" + b"x" * MAX_PHOTO)
        accepted = self.record("fadiga", 1010.0)
        self.server.faults = [400]
        with self.assertLogs("events.sender", "WARNING") as logs:
            self.assertEqual(self.send(), DONE)
        self.assertEqual([self.state(i) for i in (rejected, too_big, accepted)], [FAILED, FAILED, CONFIRMED])
        self.assertIn("servidor recusou a foto do evento %d (HTTP 400): fica no cartão" % rejected, logs.output[0])
        self.assertIn("(HTTP 413)", logs.output[1])
        self.assertEqual(len(os.listdir(self.manager.photo_dir)), 3)    # a foto fica no cartão

    def test_401_500_and_dropped_connection_stay_pending(self):
        local_id = self.record()
        for fault in (401, 500, 503, "drop"):
            with self.subTest(fault=fault):
                self.server.faults = [fault]
                self.assertEqual(self.send(), FAILURE)
                self.assertEqual(self.state(local_id), PENDING)
        self.assertEqual(self.send(), DONE)
        self.assertEqual(len(self.publisher.messages), 1)

    def test_wrong_token_is_401(self):
        self.record()
        sender = self.new_sender(token="errado")
        with self.assertLogs("events.sender", "ERROR") as logs:
            self.assertEqual(self.send(sender), FAILURE)
        self.assertIn("token recusado (HTTP 401): confira o VGUARD_TOKEN_FILE", logs.output[0])

    def test_without_puback_nothing_is_put(self):
        local_id = self.record()
        self.publisher.puback = False
        with self.assertLogs("events.sender", "WARNING") as logs:
            self.assertEqual(self.send(), FAILURE)
        self.assertIn("sem PUBACK", logs.output[0])
        self.assertEqual(self.photo_puts(), [])
        self.publisher.puback = True
        self.assertEqual(self.send(), DONE)
        self.assertEqual(self.state(local_id), CONFIRMED)

    def test_wait_grows_from_60_s_to_10_min_and_returns_to_60_s(self):
        self.record()
        waits = []
        for _ in range(6):
            self.server.faults = [500]
            self.send()
            waits.append(self.sender.retry_in)
        self.assertEqual(waits, [60, 120, 240, 480, 600, 600])
        self.send()                                     # o servidor voltou
        self.assertEqual(self.sender.wait, 60)

    def test_one_log_line_per_failure_episode(self):
        self.record()
        with self.assertLogs("events.sender", "INFO") as logs:
            for _ in range(3):
                self.server.faults = [503]
                self.send()
            self.send()
        self.assertEqual(logs.output[0], "WARNING:events.sender:envio de eventos falhou (HTTP 503); "
                                         "nova tentativa em 60 s")
        self.assertEqual(logs.output[1], "INFO:events.sender:envio de eventos de volta depois de 3 tentativas")
        self.assertTrue(logs.output[2].startswith("INFO:events.sender:evento 1 enviado (fadiga, janela "))
        self.assertEqual(len(logs.output), 3)


class QueueTest(SenderTest):

    def test_without_link_nothing_goes_out(self):
        self.record()
        self.link = None
        with self.assertLogs("events.sender", "INFO") as logs:
            self.assertEqual(self.send(), NO_LINK)
        self.assertIn("sem enlace; 1 evento espera no cartão", logs.output[0])
        self.assertEqual((self.server.requests, self.publisher.messages), ([], []))
        self.link = "cellular"
        self.assertEqual(self.send(), DONE)

    def test_mqtt_disconnected_waits_on_the_card(self):
        self.record()
        self.publisher.connected = False
        with self.assertLogs("events.sender", "INFO") as logs:
            self.assertEqual(self.send(), NO_LINK)
        self.assertIn("MQTT desconectado; 1 evento espera no cartão", logs.output[0])
        self.assertEqual(self.server.requests, [])
        self.publisher.connected = True
        self.assertEqual(self.send(), DONE)

    def test_missing_photo_becomes_failed_and_does_not_block(self):
        gone = self.record("fadiga", 1000.0)
        kept = self.record("distracao", 1001.0)
        os.remove(self.manager.pending_events()[0]["photo"])
        with self.assertLogs("events.sender", "WARNING"):
            self.assertEqual(self.send(), DONE)
        self.assertEqual((self.state(gone), self.state(kept)), (FAILED, CONFIRMED))

    def test_summary(self):
        self.record("fadiga", 1000.0)
        self.record("distracao", 1001.0)
        self.server.faults = [200, 500]
        self.send()
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
        sender = self.new_sender(first_wait=2.0)
        sender.start()
        self.addCleanup(sender.stop)
        self.record("fadiga", 1000.0)
        self.assertTrue(self.wait_for(lambda: len(self.server.events) == 1, 1.5), "não saiu logo")

        self.server.faults = [500]
        failed_at = time.monotonic()
        self.record("distracao", 1001.0)                # falha: a espera de 2 s começa
        self.assertTrue(self.wait_for(lambda: sender.retry_in == 2.0, 1.5))
        self.record("fadiga", 1010.0)                   # não acorda o envio durante a espera
        self.assertFalse(self.wait_for(lambda: self.manager.pending_counts()["events"] < 2, 1.0))
        self.assertTrue(self.wait_for(lambda: self.manager.pending_counts()["events"] == 0, 3.0),
                        "não saiu depois da espera")
        self.assertGreaterEqual(time.monotonic() - failed_at, 2.0)

    def test_event_waits_for_its_window_then_goes_out(self):
        self.now = time.time()
        sender = self.new_sender(clock=time.time, window_margin=-9.5)   # a janela fecha 0,5 s depois
        sender.start()
        self.addCleanup(sender.stop)
        self.record("fadiga", time.time())
        self.assertFalse(self.wait_for(lambda: self.publisher.messages, 0.3))
        self.assertTrue(self.wait_for(lambda: self.manager.pending_counts()["events"] == 0, 2.0))

    def test_reconnection_wakes_the_sender(self):
        self.publisher.connected = False
        sender = self.new_sender(first_wait=30.0)
        sender.start()
        self.addCleanup(sender.stop)
        self.record()
        time.sleep(0.3)
        self.publisher.connected = True
        self.publisher.connection_changed.set()         # o MQTT voltou
        self.assertTrue(self.wait_for(lambda: self.manager.pending_counts()["events"] == 0, 2.5))

    def test_stop_returns_quickly(self):
        sender = self.new_sender()
        sender.start()
        time.sleep(0.2)
        started = time.monotonic()
        sender.stop()
        self.assertLess(time.monotonic() - started, 1.5)


class FakeServerTest(unittest.TestCase):

    def test_stored_events_and_photos_survive_a_restart(self):
        with tempfile.TemporaryDirectory(prefix="vguard-teste-") as folder:
            server = FakeServer(os.path.join(folder, "servidor")).start()
            self.assertEqual(server.store_event("79cd9979-1ca0-5bfe-946f-b8aad0c5fee1", "d",
                                                {"ts": 1000.0, "type": "fadiga", "window": [], "data": {}}), 200)
            self.assertEqual(server.store_photo("79cd9979-1ca0-5bfe-946f-b8aad0c5fee1",
                                                {"Authorization": "Bearer t", "X-Device-Id": "d"}, JPEG), 200)
            server.stop()
            again = FakeServer(os.path.join(folder, "servidor"))
            record, = again.events.values()
            self.assertEqual((record["type"], record["foto"]), ("fadiga", "79cd9979-1ca0-5bfe-946f-b8aad0c5fee1.jpg"))
            again.httpd.server_close()

    def test_photo_of_another_device_is_409(self):
        with tempfile.TemporaryDirectory(prefix="vguard-teste-") as folder:
            server = FakeServer(folder)
            server.store_event("79cd9979-1ca0-5bfe-946f-b8aad0c5fee1", "d", {"ts": 1000.0, "type": "fadiga"})
            self.assertEqual(server.store_photo("79cd9979-1ca0-5bfe-946f-b8aad0c5fee1",
                                                {"Authorization": "Bearer t", "X-Device-Id": "outro"}, JPEG), 409)
            server.httpd.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)     # os avisos esperados não poluem a saída
    unittest.main()
