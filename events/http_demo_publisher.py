"""Publica os dados da demonstração na API HTTP usada pelo dashboard atual."""

import http.client
import json
import logging
import threading
import time
import urllib.parse

import sync

log = logging.getLogger("events.demo")


class HttpDemoPublisher:
    # O HTTP não mantém uma sessão com o servidor. O EventSender verifica o
    # enlace e usa o resultado de cada requisição para decidir se deve tentar de novo.
    connected = True

    def __init__(self, manager):
        self.manager = manager
        self.connection_changed = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._sent = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="demo-http", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join()

    def summary(self):
        return f"HTTP da demonstração: {self._sent} linhas de telemetria confirmadas"

    def publish_event(self, payload):
        event = json.loads(payload)
        self._request("PUT", f"/events/{event['event_id']}", payload.encode())
        return True

    def _run(self):
        next_health = 0.0
        while not self._stop.is_set():
            link = sync.active_link()
            if link is not None:
                try:
                    if time.monotonic() >= next_health:
                        self._request("POST", "/health", json.dumps(sync.health(self.manager, link)).encode())
                        next_health = time.monotonic() + 60
                    batch = self.manager.pending_telemetry(limit=100)
                    if batch:
                        self._request("POST", "/telemetry", json.dumps({"items": batch}).encode())
                        self.manager.ack_telemetry([row["id"] for row in batch])
                        self._sent += len(batch)
                except (OSError, http.client.HTTPException) as error:
                    log.warning("envio HTTP de telemetria ou saúde: %s", error)
            self._stop.wait(5)

    def _request(self, method, path, body):
        url = urllib.parse.urlsplit(sync.BACKEND_URL)
        connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(url.netloc, timeout=10)
        try:
            connection.request(method, url.path.rstrip("/") + path, body, {
                "Content-Type": "application/json",
                "Authorization": "Bearer " + sync.device_token(),
                "X-Device-Id": sync.DEVICE_ID,
            })
            response = connection.getresponse()
            response.read()
            if not 200 <= response.status < 300:
                raise OSError(f"{path}: HTTP {response.status}")
        finally:
            connection.close()
