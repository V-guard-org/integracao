"""
Testes do publicador MQTT (ticket 4, Issue #18; contrato 2.1) com um
mosquitto de verdade, iniciado pelo teste numa porta livre, e o paho-mqtt. Na
Pi, no venv-principal (o mosquitto fica em /usr/sbin, com o serviço desligado):

    cd events && /home/vguard/vguard/venv-principal/bin/python test_mqtt_publisher.py

Sem o paho ou sem o mosquitto, os testes são pulados, com o motivo.
"""

import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from event_manager import CONFIRMED, EventManager
from event_sender import EventSender, event_id
from fake_server import FakeServer
from mqtt_publisher import MqttPublisher

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

MOSQUITTO = shutil.which("mosquitto") or next(
    (path for path in ("/usr/sbin/mosquitto", "/usr/local/sbin/mosquitto") if os.path.exists(path)), None)
MOSQUITTO_PASSWD = shutil.which("mosquitto_passwd")
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 40 + b"\xff\xd9"
DEVICE = "teste-01"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(condition, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


class Broker:
    """Um mosquitto só para o teste, na porta dada, sem persistência."""

    def __init__(self, folder, port, password_file=None):
        self.port = port
        self.config = os.path.join(folder, "mosquitto-%d.conf" % port)
        with open(self.config, "w") as f:
            f.write("listener %d 127.0.0.1\npersistence false\n" % port)
            if password_file:
                f.write("allow_anonymous false\npassword_file %s\n" % password_file)
            else:
                f.write("allow_anonymous true\n")
        self.process = None

    def start(self):
        self.process = subprocess.Popen([MOSQUITTO, "-c", self.config],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_for(self._listening, 5):
            raise RuntimeError("o mosquitto não abriu a porta %d" % self.port)
        return self

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(5)

    def _listening(self):
        try:
            socket.create_connection(("127.0.0.1", self.port), 0.2).close()
            return True
        except OSError:
            return False


class Listener:
    """Um cliente que assina vguard/# e guarda (tópico, qos, retain, JSON)."""

    def __init__(self, port):
        self.messages = []
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ouvinte-%d" % time.monotonic_ns())
        self.ready = threading.Event()
        self.client.on_connect = lambda c, u, f, rc, p: (c.subscribe("vguard/#", qos=1), self.ready.set())
        self.client.on_message = lambda c, u, m: self.messages.append(
            (m.topic, m.qos, m.retain, json.loads(m.payload)))
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()
        self.ready.wait(5)
        time.sleep(0.2)                     # a assinatura chega ao broker

    def topic(self, name):
        return [m for m in self.messages if m[0] == "vguard/%s/%s" % (DEVICE, name)]

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()


@unittest.skipIf(mqtt is None, "sem o paho-mqtt (requirements-principal.txt)")
@unittest.skipIf(MOSQUITTO is None, "sem o mosquitto (sudo apt install mosquitto)")
class BrokerTest(unittest.TestCase):
    """O broker, o EventManager e o ouvinte de cada teste."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="vguard-teste-")
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.port = free_port()
        self.broker = Broker(self.folder, self.port).start()
        self.addCleanup(self.broker.stop)
        self.manager = EventManager(os.path.join(self.folder, "dados"))
        self.addCleanup(self.manager.close)
        self.listener = Listener(self.port)
        self.addCleanup(self.listener.close)

    def publisher(self, **options):
        settings = dict(device=DEVICE, host="127.0.0.1", port=self.port, ca="", user="", password_file="",
                        link=lambda: "wifi", health_interval=60, reconnect_max=1)
        settings.update(options)
        publisher = MqttPublisher(self.manager, **settings)
        publisher.start()
        self.addCleanup(publisher.stop)
        self.assertTrue(wait_for(lambda: publisher.connected, 5), "não conectou")
        return publisher

    def rows(self, first, last):
        """Leituras de 10 Hz do segundo first ao last (gravam as linhas de first a last - 1)."""
        for i in range((last - first) * 10 + 1):
            self.manager.update("imu", {"ax": i}, first + i / 10)

    def record_event(self, ts=1000.0):
        return self.manager.record_event("fadiga", 0.82, JPEG, ts)


class PublisherTest(BrokerTest):

    def test_telemetry_one_message_per_line_qos_0(self):
        publisher = self.publisher()
        self.rows(1000, 1010)
        self.assertTrue(wait_for(lambda: len(self.listener.topic("telemetria")) == 10, 3))
        messages = self.listener.topic("telemetria")
        self.assertEqual([m[3]["ts"] for m in messages], [1000.0 + n for n in range(10)])
        self.assertEqual({(m[1], m[2]) for m in messages}, {(0, False)})    # QoS 0, sem retain
        self.assertEqual(set(messages[0][3]), {"ts", "data"})
        self.assertEqual(messages[0][3]["data"]["imu"]["ax"], 9)            # a última de [1000, 1001)
        self.assertEqual(self.manager.pending_counts()["telemetry"], 0)     # publicadas: CONFIRMED
        self.assertEqual(publisher.published, 10)

    def test_health_right_after_connecting_and_every_interval(self):
        self.record_event()
        self.publisher(health_interval=0.5)
        self.assertTrue(wait_for(lambda: len(self.listener.topic("saude")) >= 3, 3))
        topic, qos, retain, health = self.listener.topic("saude")[0]
        self.assertEqual((qos, retain), (0, False))
        self.assertEqual(set(health), {"ts", "link", "pending", "storage", "temperature", "cellular_bytes"})
        self.assertEqual((health["link"], health["pending"]), ("wifi", {"events": 1}))

    def test_disconnected_lines_stay_pending_and_it_comes_back_alone(self):
        publisher = self.publisher()
        self.rows(1000, 1003)
        self.assertTrue(wait_for(lambda: publisher.published == 3, 3))

        with self.assertLogs("events.mqtt", "INFO") as logs:
            self.broker.stop()              # o túnel caiu
            self.assertTrue(wait_for(lambda: not publisher.connected, 5))
            self.rows(1003, 1006)
            self.assertEqual(publisher.not_published, 3)
            self.assertEqual(self.manager.pending_counts()["telemetry"], 3)    # ficam no cartão

            health_before = publisher.health_sent
            self.broker.start()             # voltou, na mesma porta
            self.assertTrue(wait_for(lambda: publisher.connected, 5), "não voltou sozinho")
            self.assertTrue(wait_for(lambda: publisher.health_sent > health_before, 3),
                            "sem a saúde depois de conectar")
            self.rows(1006, 1008)
            self.assertTrue(wait_for(lambda: publisher.published == 5, 3))
        text = "\n".join(logs.output)
        self.assertIn("MQTT caiu", text)
        self.assertIn("MQTT de volta depois de", text)
        self.assertEqual([state for _, state in publisher.history], ["conectado", "caiu", "conectado"])
        self.assertEqual(publisher.summary(), "mqtt: 5 linhas publicadas, 3 não publicadas (desconectado), %d saúdes"
                         % publisher.health_sent)

    def test_event_qos_1_waits_for_the_puback(self):
        publisher = self.publisher()
        self.assertTrue(publisher.publish_event(json.dumps({"event_id": "x", "ts": 1000.0})))
        self.assertTrue(wait_for(lambda: self.listener.topic("eventos"), 3))
        topic, qos, retain, payload = self.listener.topic("eventos")[0]
        self.assertEqual((qos, retain, payload["ts"]), (1, False, 1000.0))
        self.broker.stop()
        self.assertTrue(wait_for(lambda: not publisher.connected, 5))
        self.assertFalse(publisher.publish_event("{}"))

    @unittest.skipIf(MOSQUITTO_PASSWD is None, "sem o mosquitto_passwd")
    def test_user_and_password_from_file(self):
        self.broker.stop()
        passwords = os.path.join(self.folder, "senhas")
        subprocess.run([MOSQUITTO_PASSWD, "-c", "-b", passwords, "pi-teste", "segredo"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(passwords, 0o600)
        self.broker = Broker(self.folder, free_port(), password_file=passwords).start()
        self.addCleanup(self.broker.stop)
        secret = os.path.join(self.folder, "senha")
        with open(secret, "w") as f:
            f.write("segredo\n")            # a quebra de linha do fim não conta
        self.port = self.broker.port
        self.publisher(user="pi-teste", password_file=secret)

        with open(secret, "w") as f:
            f.write("errada")
        wrong = MqttPublisher(self.manager, device="outro", host="127.0.0.1", port=self.port, ca="",
                              user="pi-teste", password_file=secret, link=lambda: "wifi")
        with self.assertLogs("events.mqtt", "ERROR") as logs:
            wrong.start()
            self.assertFalse(wait_for(lambda: wrong.connected, 1.5))
        wrong.stop()
        self.assertIn("MQTT recusou a conexão", logs.output[0])


class EndToEndTest(BrokerTest):
    """O evento pelo broker de verdade até o consumidor do servidor falso, e a foto pelo PUT."""

    def test_event_with_window_and_photo_confirmed(self):
        server = FakeServer(os.path.join(self.folder, "servidor"), mqtt=("127.0.0.1", self.port)).start()
        self.addCleanup(server.stop)
        publisher = self.publisher()
        for second in range(990, 1000):
            self.manager.update("imu", {"ax": second}, second + 0.5)
        local_id = self.record_event(1000.437)
        for second in range(1000, 1013):
            self.manager.update("imu", {"ax": second}, second + 0.5)

        sender = EventSender(self.manager, publisher, link=lambda: "wifi", url=server.url, token="t",
                             device=DEVICE, timeout=2, put_delay=0.5, conflict_wait=0.5, clock=lambda: 10**9)
        self.assertEqual(sender.send_pending()[0], "done")
        uuid_text = event_id(DEVICE, 1000.437, "fadiga")
        record = server.events[uuid_text]
        self.assertEqual(len({entry["ts"] for entry in record["window"]}), 21)
        self.assertEqual(record["bytes"], len(JPEG))
        self.assertEqual(self.manager._db.execute("SELECT synced FROM events WHERE id = ?",
                                                  (local_id,)).fetchone()[0], CONFIRMED)
        self.assertEqual(sender.history[local_id]["puts"], [200])
        self.assertTrue(wait_for(lambda: server.telemetry.get(DEVICE), 3))     # o consumidor conta a telemetria


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
