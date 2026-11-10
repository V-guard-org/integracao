"""
Verificação da sincronização contra um backend falso local. Executar:

    python test_sync.py
    python3 test_sync.py
"""

import http.server
import json
import tempfile
import threading
import time

import sync
from event_manager import EventManager

JPEG = b"\xff\xd8\xff\xe0 foto de teste \xff\xd9"


class Backend(http.server.BaseHTTPRequestHandler):

    received = []   # (caminho, corpo) do que foi confirmado
    fault = None    # (sufixo do caminho, "drop", "slow" ou status HTTP), uma vez só

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        assert self.headers["Authorization"] == "Bearer segredo"

        status = 200
        if Backend.fault and self.path.endswith(Backend.fault[0]):
            status, Backend.fault = Backend.fault[1], None

        if status == "drop":
            # Conexão cai no meio do envio, sem resposta.
            self.close_connection = True
            return

        if status == "slow":
            # Grava, mas responde depois do tempo de espera do Sync: a
            # confirmação se perde e o Sync precisa reenviar (T34.2).
            Backend.received.append((self.path, body))
            time.sleep(sync.HTTP_TIMEOUT * 2)
            try:
                self.send_response(200)
                self.end_headers()
            except OSError:
                pass    # o Sync já desistiu e fechou a conexão
            return

        if status == 200:
            Backend.received.append((self.path, body))

        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT

    def log_message(self, *args):
        pass


def received(suffix):
    return [body for path, body in Backend.received if path.endswith(suffix)]


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    sync.BACKEND_URL = "http://127.0.0.1:%d/api" % server.server_port
    sync.DEVICE_TOKEN = "segredo"

    # Backend na mesma máquina (notebook): conta como Wi-Fi.
    assert sync.active_link() == "wifi"

    folder = tempfile.TemporaryDirectory(prefix="vguard-teste-")
    manager = EventManager(folder.name)
    for i in range(300):
        ts = 1000.0 + i / 10
        manager.update("imu", {"ax": i}, ts)
        if i == 100:
            manager.record_event("fadiga", 0.9, JPEG, ts)
        if i == 110:
            manager.record_event("distracao", 0.7, JPEG, ts)

    link = [None]
    service = sync.Sync(manager, link=lambda: link[0])

    # Sem enlace: nada sai, tudo pendente.
    assert service.sync_once() is None and Backend.received == []
    assert manager.pending_counts() == {"telemetry": 30, "events": 2}

    # 4G: a saúde e a telemetria. Os eventos esperam no SQLite pelo
    # POST /api/events do ticket 3 (Issue #14).
    link[0] = "cellular"
    assert service.sync_once() == "cellular"

    assert len(json.loads(received("/telemetry")[0])["items"]) == 30
    assert [path for path, _ in Backend.received if "/events" in path or "photo" in path] == []
    assert manager.pending_counts() == {"telemetry": 0, "events": 2}
    assert service.last_contact is not None

    # A saúde vai no começo e, como a fila mudou, de novo no fim, já com
    # as pendências depois do envio.
    first_health, last_health = [json.loads(body) for body in received("/health")]
    assert first_health["pending"] == {"telemetry": 30, "events": 2}
    assert last_health["pending"] == {"telemetry": 0, "events": 2}

    # Nada é reenviado no ciclo seguinte.
    count = len(Backend.received)
    link[0] = "wifi"
    assert service.sync_once() == "wifi"
    assert len(Backend.received) == count + 1   # só a saúde

    # Servidor lento (T34.2): grava, mas responde depois do tempo de espera.
    # O item continua pendente e volta no ciclo seguinte; a chave por ts no
    # backend faz o reenvio não duplicar.
    sync.HTTP_TIMEOUT = 0.5
    manager.update("imu", {"ax": 0}, 1900.0)
    Backend.fault = ("/telemetry", "slow")
    try:
        service.sync_once()
        assert False, "deveria ter falhado"
    except TimeoutError:
        pass
    assert manager.pending_counts()["telemetry"] == 1
    assert service.sync_once() == "wifi"
    assert manager.pending_counts()["telemetry"] == 0
    slow, resent = [json.loads(b)["items"] for b in received("/telemetry")[-2:]]
    assert slow == resent     # o mesmo lote, inteiro

    # Backend respondendo 500: o dado novo continua pendente.
    manager.update("imu", {"ax": 0}, 2000.0)
    Backend.fault = ("/telemetry", 500)
    try:
        service.sync_once()
        assert False, "deveria ter falhado"
    except sync.Retry:
        pass
    assert manager.pending_counts()["telemetry"] == 1

    # Backend fora do ar: idem.
    server.shutdown()
    server.server_close()
    try:
        service.sync_once()
        assert False, "deveria ter falhado"
    except OSError:
        pass
    assert manager.pending_counts()["telemetry"] == 1

    manager.close()
    folder.cleanup()
    print("OK")


if __name__ == "__main__":
    main()
