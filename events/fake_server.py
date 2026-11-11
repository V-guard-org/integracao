"""
Servidor falso do V-GUARD (ticket 4, Issue #18; contrato 2.1): o backend e o
consumidor MQTT, para os testes e a bancada.

    mosquitto -p 1883 &        # o broker local (/usr/sbin/mosquitto na Pi)
    python3 events/fake_server.py --dir /home/vguard/vguard/servidor-falso --mqtt 127.0.0.1:1883
    VGUARD_BACKEND=http://127.0.0.1:8000/api VGUARD_MQTT_PORT=1883 python3 vguard.py ...

O backend (HTTP):
- PUT /api/events/<event_id>: grava o evento (o que o consumidor chama). Um
  repetido responde 200 sem duplicar;
- PUT /api/events/<event_id>/photo: a foto, o JPEG cru. Confere os cabeçalhos
  (Authorization: Bearer e X-Device-Id). Sem o evento gravado: 409. Foto que
  não é JPEG: 400. Acima de 5 MB: 413. Com --token, outro token: 401. Um
  reenvio responde 200 sem duplicar.

O consumidor (com --mqtt <host:porta>; precisa do paho-mqtt): assina
vguard/+/telemetria, vguard/+/saude e vguard/+/eventos (QoS 1), grava os
eventos pelo mesmo caminho do PUT, conta as linhas de telemetria e guarda a
última saúde. Com o consumer_enabled em False (--sem-consumidor), os eventos
chegam ao broker e se perdem, como um consumidor quebrado: a foto dá 409.

Tudo vai para um eventos.jsonl na pasta, com as fotos ao lado: um reinício
não duplica nada. Uma linha por evento, por foto e por saúde no terminal.

Nos testes, a fila faults faz os próximos PUT da foto responderem com outro
código, sem gravar; demorarem a responder depois de gravar ("slow"); ou
caírem sem resposta ("drop").
"""

import argparse
import http.server
import json
import os
import re
import signal
import threading
import time

MAX_PHOTO = 5 * 1024 * 1024
UUID = r"[0-9a-fA-F-]{36}"


class FakeServer:
    """
    O servidor falso: url, events (event_id -> registro), telemetry
    (device -> [ts]), health (device -> a última), faults, requests,
    consumer_enabled, start() e stop().
    """

    def __init__(self, folder, port=0, host="127.0.0.1", token=None, slow_s=2.0, quiet=True, mqtt=None):
        self.folder = folder
        self.token = token
        self.slow_s = slow_s
        self.quiet = quiet
        self.mqtt = mqtt                # (host, porta) do broker, ou None
        self.faults = []                # os próximos PUT da foto: um código HTTP, "slow" ou "drop"
        self.requests = []              # (método, caminho, cabeçalhos) de cada pedido, na ordem
        self.events = {}                # event_id -> o registro guardado
        self.telemetry = {}             # device -> [ts], na ordem de chegada
        self.health = {}                # device -> a última saúde
        self.health_count = 0
        self.consumer_enabled = True
        self._lock = threading.Lock()
        self._client = None
        os.makedirs(folder, exist_ok=True)
        self._log = os.path.join(folder, "eventos.jsonl")
        if os.path.exists(self._log):
            with open(self._log, encoding="utf-8") as f:
                for line in f:
                    record = json.loads(line)
                    if record.get("kind") == "foto":
                        self.events[record["event_id"]].update(foto=record["foto"], bytes=record["bytes"],
                                                               foto_chegou=record["chegou"])
                    else:
                        self.events[record["event_id"]] = record
        handler = type("Handler", (_Handler,), {"state": self})
        self.httpd = http.server.ThreadingHTTPServer((host, port), handler)
        self.url = "http://%s:%d/api" % (host, self.httpd.server_address[1])
        self._thread = None

    def start(self):
        if self.mqtt:
            self._start_consumer()
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._client is not None:
            self._client.disconnect()
            self._client.loop_stop()

    def received(self):
        """Os eventos guardados, na ordem de chegada."""
        return sorted(self.events.values(), key=lambda record: record["chegou"])

    def say(self, text):
        if not self.quiet:
            print("[servidor falso] %s %s" % (time.strftime("%H:%M:%S"), text), flush=True)

    def _append(self, record):
        with open(self._log, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    # ------------------------------------------------------------------ o backend

    def store_event(self, uuid_text, device, event):
        """O PUT /api/events/<id>: o código da resposta."""
        try:
            ts, kind, window, data = event["ts"], event["type"], event.get("window", []), event.get("data", {})
            if not (isinstance(ts, (int, float)) and isinstance(kind, str)
                    and isinstance(window, list) and isinstance(data, dict)):
                raise TypeError("tipos")
        except (KeyError, TypeError) as error:
            self.say("evento inválido: %s (400)" % error)
            return 400
        with self._lock:
            if uuid_text in self.events:
                self.say("evento %s repetido: sem duplicar" % uuid_text[:8])
                return 200
            record = {"kind": "evento", "event_id": uuid_text, "device_id": device, "ts": ts, "type": kind,
                      "score": event.get("score"), "has_photo": event.get("has_photo"), "data": data,
                      "window": window, "foto": None, "bytes": 0, "chegou": time.time(), "foto_chegou": None}
            self._append(record)
            self.events[uuid_text] = record
        self.say("evento %s %s em %.3f de %s (janela: %d entradas; retrato: %s)" % (
            uuid_text[:8], kind, ts, device, len(window), ", ".join(sorted(data)) or "vazio"))
        return 200

    def store_photo(self, uuid_text, headers, body):
        """O PUT /api/events/<id>/photo: o código da resposta."""
        auth = headers.get("Authorization", "")
        device = headers.get("X-Device-Id")
        if not auth.startswith("Bearer ") or (self.token is not None and auth != "Bearer " + self.token):
            self.say("token recusado (401)")
            return 401
        if not device:
            self.say("pedido sem o X-Device-Id (400)")
            return 400
        if len(body) > MAX_PHOTO:
            self.say("foto de %d bytes, acima de 5 MB (413)" % len(body))
            return 413
        if not body.startswith(b"\xff\xd8"):
            self.say("a foto não é JPEG (400)")
            return 400
        with self._lock:
            record = self.events.get(uuid_text)
            if record is None or record["device_id"] != device:
                self.say("foto de %s antes do evento (409)" % uuid_text[:8])
                return 409
            if record["foto"]:
                self.say("foto de %s repetida: sem duplicar (200)" % uuid_text[:8])
                return 200
            name = "%s.jpg" % uuid_text
            with open(os.path.join(self.folder, name), "wb") as f:
                f.write(body)
            now = time.time()
            self._append({"kind": "foto", "event_id": uuid_text, "foto": name, "bytes": len(body), "chegou": now})
            record.update(foto=name, bytes=len(body), foto_chegou=now)
        self.say("foto de %s (%s em %.3f): %d KB (200)" % (
            uuid_text[:8], record["type"], record["ts"], round(len(body) / 1024)))
        return 200

    # ------------------------------------------------------------------ o consumidor MQTT

    def _start_consumer(self):
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="vguard-servidor-falso",
                             protocol=mqtt.MQTTv311, clean_session=True)
        client.reconnect_delay_set(1, 5)

        def on_connect(client, userdata, flags, reason_code, properties):
            client.subscribe([("vguard/+/telemetria", 0), ("vguard/+/saude", 0), ("vguard/+/eventos", 1)])
            self.say("consumidor conectado ao broker %s:%d" % self.mqtt)

        client.on_connect = on_connect
        client.on_message = self._on_message
        self._client = client
        client.connect_async(self.mqtt[0], self.mqtt[1], 30)
        client.loop_start()

    def _on_message(self, client, userdata, message):
        _, device, kind = message.topic.split("/", 2)
        try:
            payload = json.loads(message.payload)
        except ValueError:
            self.say("mensagem inválida em %s" % message.topic)
            return
        if kind == "telemetria":
            with self._lock:
                self.telemetry.setdefault(device, []).append(payload.get("ts"))
        elif kind == "saude":
            with self._lock:
                self.health[device] = payload
                self.health_count += 1
            self.say("saúde de %s: link %s, %s pendentes" % (
                device, payload.get("link"), payload.get("pending", {}).get("events")))
        elif kind == "eventos":
            if not self.consumer_enabled:
                self.say("evento de %s perdido: o consumidor está desligado" % device)
                return
            uuid_text = payload.pop("event_id", None)
            if not uuid_text or not re.fullmatch(UUID, uuid_text):
                self.say("evento sem event_id em %s" % message.topic)
                return
            self.store_event(uuid_text, device, payload)


class _Handler(http.server.BaseHTTPRequestHandler):
    state = None

    def do_PUT(self):
        state = self.state
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        path = self.path.rstrip("/")
        photo = re.search(r"/events/(%s)/photo$" % UUID, path)
        event = re.search(r"/events/(%s)$" % UUID, path)
        with state._lock:
            state.requests.append(("PUT", self.path, dict(self.headers)))
            fault = state.faults.pop(0) if photo and state.faults else None
        if fault == "drop":
            self.close_connection = True        # cai sem resposta
            return
        if isinstance(fault, int):
            status = fault                      # responde o código pedido, sem gravar
        elif photo:
            status = state.store_photo(photo.group(1).lower(), self.headers, body)
            if fault == "slow":
                time.sleep(state.slow_s)        # gravou, mas responde tarde
        elif event:
            try:
                status = state.store_event(event.group(1).lower(), self.headers.get("X-Device-Id"), json.loads(body))
            except ValueError:
                status = 400
        else:
            status = 404
        try:
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except OSError:
            pass                                # o cliente já desistiu

    def log_message(self, *args):
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="endereço (padrão: %(default)s)")
    parser.add_argument("--port", type=int, default=8000, help="porta (padrão: %(default)s)")
    parser.add_argument("--dir", default="servidor-falso", help="pasta dos eventos e das fotos (padrão: %(default)s)")
    parser.add_argument("--token", help="o único token aceito (sem ele, qualquer Bearer)")
    parser.add_argument("--mqtt", metavar="HOST:PORTA", help="também o consumidor, neste broker")
    parser.add_argument("--sem-consumidor", action="store_true",
                        help="os eventos do MQTT se perdem, como um consumidor quebrado (a foto dá 409)")
    args = parser.parse_args(argv)
    mqtt = None
    if args.mqtt:
        host, _, port = args.mqtt.rpartition(":")
        mqtt = (host or "127.0.0.1", int(port))
    server = FakeServer(args.dir, port=args.port, host=args.host, token=args.token, quiet=False, mqtt=mqtt)
    server.consumer_enabled = not args.sem_consumidor
    # O SIGTERM (kill) para como o Ctrl+C, com o resumo.
    signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt))
    print("[servidor falso] em %s, pasta %s, com %d eventos já guardados%s. Ctrl+C para parar."
          % (server.url, os.path.abspath(args.dir), len(server.events),
             ", consumidor no broker %s:%d" % mqtt if mqtt else ""), flush=True)
    server.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        lines = sum(len(ts) for ts in server.telemetry.values())
        print("\n[servidor falso] parado: %d eventos guardados (%d com foto); nesta execução, "
              "%d linhas de telemetria e %d saúdes" % (
                  len(server.events), sum(1 for r in server.events.values() if r["foto"]),
                  lines, server.health_count), flush=True)


if __name__ == "__main__":
    main()
