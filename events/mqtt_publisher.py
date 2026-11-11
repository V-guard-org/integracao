"""
V-GUARD — o publicador MQTT (ticket 4, Issue #18; contrato 2.1).

    vguard/<device>/telemetria   QoS 0   cada linha de 1/s do EventManager
    vguard/<device>/saude        QoS 0   a cada 60 s e logo depois de cada conexão
    vguard/<device>/eventos      QoS 1   o evento com a janela, pelo EventSender

Sem retain. Uma linha de telemetria publicada com o MQTT desconectado não é
guardada para depois (QoS 0, decidido): ela continua pendente no cartão.

A conexão volta sozinha (o paho tenta de novo de 1 s, dobrando até 30 s),
por exemplo quando o túnel SSH até a VM volta. Contra o túnel local, a
tentativa não gasta o 4G.

Configuração (variáveis de ambiente):

    VGUARD_DEVICE               o device_id do tópico (sync.py)
    VGUARD_MQTT_HOST/PORT       o broker (padrão 127.0.0.1:1883)
    VGUARD_MQTT_CA              a CA do TLS; vazio = sem TLS
    VGUARD_MQTT_USER            o usuário; padrão: o VGUARD_DEVICE
    VGUARD_MQTT_PASSWORD_FILE   arquivo com a senha (não vai à linha de comando)

Precisa do paho-mqtt (requirements-principal.txt, no venv-principal). Ele só
é importado no start(): sem ele, o resto do principal continua importável.

    MqttPublisher(manager).start()
"""

import json
import logging
import os
import threading
import time

import sync

log = logging.getLogger("events.mqtt")


# ============================================================
# CONFIGURAÇÃO
# ============================================================

HOST = os.environ.get("VGUARD_MQTT_HOST", "127.0.0.1")
PORT = int(os.environ.get("VGUARD_MQTT_PORT", "1883"))
CA = os.environ.get("VGUARD_MQTT_CA", "")
USER = os.environ.get("VGUARD_MQTT_USER", "")
PASSWORD_FILE = os.environ.get("VGUARD_MQTT_PASSWORD_FILE", "")

KEEPALIVE = 60
RECONNECT_MIN = 1
RECONNECT_MAX = 30

HEALTH_INTERVAL = 60.0

# Quanto o envio espera o PUBACK de um evento.
PUBACK_TIMEOUT = 30.0


def plural(count, one, many):
    return "%d %s" % (count, one if count == 1 else many)


def read_secret(path):
    """O conteúdo de um arquivo de segredo, sem a quebra de linha do fim. Vazio sem arquivo."""
    if not path:
        return ""
    with open(os.path.expanduser(path)) as f:
        return f.read().rstrip("\r\n")


# ============================================================
# PUBLICADOR
# ============================================================

class MqttPublisher:
    """
    start(), stop(), connected, publish_event(), summary(). O EventSender
    espera em connection_changed para tentar de novo logo que a conexão volta.
    """

    def __init__(self, manager, device=None, host=None, port=None, ca=None, user=None,
                 password_file=None, link=sync.active_link, health_interval=HEALTH_INTERVAL,
                 reconnect_max=RECONNECT_MAX):
        self.manager = manager
        self.device = device or sync.DEVICE_ID
        self.host = host or HOST
        self.port = port or PORT
        self.ca = os.path.expanduser(CA if ca is None else ca)
        self.user = user or USER or self.device
        self.password_file = PASSWORD_FILE if password_file is None else password_file
        self.link = link
        self.health_interval = health_interval
        self.reconnect_max = reconnect_max

        self.topic = {name: "vguard/%s/%s" % (self.device, name)
                      for name in ("telemetria", "saude", "eventos")}

        self.published = 0          # linhas de telemetria publicadas
        self.not_published = 0      # linhas com o MQTT desconectado
        self.health_sent = 0
        self.connection_changed = threading.Event()
        self.history = []           # (horário, "conectado" | "caiu"), para a server_bench

        self._connected = False
        self._down_since = None     # time.monotonic() da queda em curso
        self._failing = False       # já avisou a falha de conexão deste episódio
        self._ever_connected = False
        self._client = None
        self._stop = threading.Event()
        self._health_now = threading.Event()    # a saúde logo depois de conectar
        self._health_thread = None

    @property
    def connected(self):
        return self._connected

    # ------------------------------------------------------------------ interface

    def start(self):
        import paho.mqtt.client as mqtt     # só aqui: o venv 3.11 da visão não tem o paho

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id="vguard-%s" % self.device,
                             protocol=mqtt.MQTTv311, clean_session=True)
        password = read_secret(self.password_file)
        if self.user:
            client.username_pw_set(self.user, password or None)
        if self.ca:
            client.tls_set(ca_certs=self.ca)
        client.reconnect_delay_set(RECONNECT_MIN, self.reconnect_max)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_connect_fail = self._on_connect_fail
        self._client = client
        self._down_since = time.monotonic()

        client.connect_async(self.host, self.port, KEEPALIVE)
        client.loop_start()         # reconecta sozinho, também na primeira conexão
        self.manager.on_telemetry = self.publish_telemetry

        self._health_thread = threading.Thread(target=self._health_loop, name="mqtt-health", daemon=True)
        self._health_thread.start()

    def stop(self):
        self._stop.set()
        self._health_now.set()
        self.manager.on_telemetry = None
        if self._health_thread is not None:
            self._health_thread.join()
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()
        self._connected = False

    def summary(self):
        return "mqtt: %s, %s, %s" % (
            plural(self.published, "linha publicada", "linhas publicadas"),
            plural(self.not_published, "não publicada (desconectado)", "não publicadas (desconectado)"),
            plural(self.health_sent, "saúde", "saúdes"))

    # ------------------------------------------------------------------ telemetria e saúde

    def publish_telemetry(self, row):
        """O aviso do EventManager: uma linha de 1/s, {id, ts, data}."""
        if not self._connected:
            self.not_published += 1
            return
        payload = json.dumps({"ts": row["ts"], "data": row["data"]}, separators=(",", ":"))
        if self._publish(self.topic["telemetria"], payload, 0) is None:
            self.not_published += 1
            return
        self.published += 1
        self.manager.ack_telemetry([row["id"]])

    def publish_health(self):
        if not self._connected:
            return False
        payload = json.dumps(sync.health(self.manager, self.link()), separators=(",", ":"))
        if self._publish(self.topic["saude"], payload, 0) is None:
            return False
        self.health_sent += 1
        return True

    def _health_loop(self):
        # Fora da thread do paho: o active_link() pode levar alguns segundos.
        while True:
            self._health_now.wait(self.health_interval)
            self._health_now.clear()
            if self._stop.is_set():
                return
            try:
                self.publish_health()
            except Exception:
                log.exception("a saúde não foi publicada")

    # ------------------------------------------------------------------ eventos

    def publish_event(self, payload, timeout=PUBACK_TIMEOUT):
        """
        Publica o evento (o JSON do contrato, em texto) com QoS 1 e espera o
        PUBACK. Devolve True com o PUBACK; False desconectado ou sem ele.
        """
        if not self._connected:
            return False
        info = self._publish(self.topic["eventos"], payload, 1)
        if info is None:
            return False
        try:
            info.wait_for_publish(timeout)
        except (RuntimeError, ValueError):
            return False
        return info.is_published()

    def _publish(self, topic, payload, qos):
        """Um PUBLISH, sem retain. Devolve o MQTTMessageInfo, ou None se o paho recusou."""
        try:
            info = self._client.publish(topic, payload, qos=qos, retain=False)
        except (OSError, ValueError, RuntimeError) as error:
            log.debug("publicação em %s falhou: %s", topic, error)
            return None
        return info if info.rc == 0 else None

    # ------------------------------------------------------------------ conexão (thread do paho)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            if not self._failing:
                self._failing = True
                log.error("MQTT recusou a conexão (%s); nova tentativa em até %d s",
                          reason_code, self.reconnect_max)
            return
        self._connected = True
        self._failing = False
        self.history.append((time.time(), "conectado"))
        down = time.monotonic() - self._down_since if self._down_since is not None else 0
        if self._ever_connected:
            log.warning("MQTT de volta depois de %.0f s", down)
        else:
            log.info("MQTT conectado (%s:%d, %s)", self.host, self.port, self.topic["telemetria"])
        self._ever_connected = True
        self._down_since = None
        self.connection_changed.set()
        self._health_now.set()      # o dashboard vê a "última comunicação" logo

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        was_connected = self._connected
        self._connected = False
        if not was_connected or self._stop.is_set():
            return
        self._down_since = time.monotonic()
        self._failing = True        # as tentativas seguintes não repetem o aviso
        self.history.append((time.time(), "caiu"))
        log.warning("MQTT caiu (%s); reconectando", reason_code)
        self.connection_changed.set()

    def _on_connect_fail(self, client, userdata):
        if not self._failing and not self._stop.is_set():
            self._failing = True
            log.warning("MQTT sem conexão com %s:%d; tentando de novo a cada %d s, no máximo",
                        self.host, self.port, self.reconnect_max)
