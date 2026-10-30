"""
Verificação da sincronização contra um backend falso local. Executar:

    python test_sync.py
    python3 test_sync.py
"""

import http.server
import json
import os
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

    manager = EventManager(tempfile.mkdtemp())
    for i in range(300):
        ts = 1000.0 + i / 10
        manager.update("imu", {"ax": i}, ts)
        if i == 100:
            first = manager.record_event("fadiga", 0.9, JPEG, ts)
        if i == 110:
            bad = manager.record_event("distracao", 0.7, JPEG, ts)

    link = [None]
    service = sync.Sync(manager, link=lambda: link[0])

    # Sem enlace: nada sai, tudo pendente.
    assert service.sync_once() is None and Backend.received == []
    assert manager.pending_counts() == {
        "telemetry": 30, "events": 2, "photos": 2
    }

    # 4G: saúde, telemetria, metadados (com janela) e a foto do evento aceito.
    # O backend recusa um dos eventos (422): vira FAILED e sai da fila.
    link[0] = "cellular"
    Backend.fault = ("/events/" + bad, 422)
    assert service.sync_once() == "cellular"

    assert len(json.loads(received("/telemetry")[0])["items"]) == 30
    event, = [json.loads(body) for body in received("/events/" + first)]
    assert event["type"] == "fadiga" and len(event["window"]) == 201
    assert event["has_photo"] == 1        # a foto existe na Pi
    assert received("/events/" + bad) == []
    assert received("/photo") == [JPEG]   # a foto sobe pelo 4G, logo depois dos dados
    assert manager.pending_counts() == {
        "telemetry": 0, "events": 0, "photos": 0
    }
    assert service.last_contact is not None

    # A saúde vai no começo e, como a fila mudou, de novo no fim, já com
    # as pendências depois do envio.
    first_health, last_health = [json.loads(body) for body in received("/health")]
    assert first_health["pending"] == {"telemetry": 30, "events": 2, "photos": 2}
    assert last_health["pending"] == {"telemetry": 0, "events": 0, "photos": 0}

    # Nada é reenviado no ciclo seguinte, nem o evento recusado.
    count = len(Backend.received)
    service.sync_once()
    assert len(Backend.received) == count + 1   # só a saúde

    # Upload da foto interrompido (4G fraco): a foto fica pendente, mas o ciclo
    # termina normalmente e os dados do mesmo ciclo já foram confirmados.
    late = manager.record_event("fadiga", 0.9, JPEG + b"3", 1300.0)
    manager.update("imu", {"ax": 0}, 1320.0)     # fecha a janela
    Backend.fault = ("/photo", "drop")
    assert service.sync_once() == "cellular"     # não levanta: foto atrasa, dado não
    assert len(received("/events/" + late)) == 1
    assert received("/photo") == [JPEG]
    assert [i for i, _ in manager.pending_photos()] == [late]

    # ...e é reenviada inteira no ciclo seguinte, agora pelo Wi-Fi.
    link[0] = "wifi"
    assert service.sync_once() == "wifi"
    assert received("/photo") == [JPEG, JPEG + b"3"]
    assert manager.pending_photos() == []

    # Foto apagada do cartão à mão: sai da fila e não trava as seguintes.
    gone = manager.record_event("fadiga", 0.8, JPEG, 1500.0)
    manager.record_event("distracao", 0.8, JPEG + b"2", 1506.0)
    manager.update("imu", {"ax": 0}, 1520.0)     # fecha as janelas
    os.remove(os.path.join(manager.photo_dir, gone + ".jpg"))
    assert service.sync_once() == "wifi"
    assert received("/photo")[-1] == JPEG + b"2"
    assert manager.pending_photos() == []

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
    print("OK")


if __name__ == "__main__":
    main()
