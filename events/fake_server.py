"""
Servidor falso do V-GUARD (ticket 3, Issue #16): recebe o POST /api/events
como manda o contrato (contrato-pi-servidor.md), para os testes e a bancada.

    python3 events/fake_server.py --dir /home/vguard/vguard/servidor-falso
    VGUARD_BACKEND=http://127.0.0.1:8000/api python3 vguard.py ...

O que ele faz:
- confere os cabeçalhos (Authorization: Bearer e X-Device-Id) e o multipart:
  a parte dados (o JSON com ts, type, score e telemetria) e a parte foto (um
  JPEG de até 5 MB). Dados errados ou foto que não é JPEG: 400. Foto acima de
  5 MB: 413. Com --token, outro token: 401;
- guarda cada evento pela chave (device_id, ts, type), num eventos.jsonl na
  pasta, com a foto ao lado. Um reenvio responde 200 sem duplicar, também
  depois de reiniciar o servidor;
- escreve uma linha por evento;
- aceita o /health e o /telemetry do sync.py antigo: responde 200 e conta.

Nos testes, a fila faults faz os próximos pedidos responderem com outro
código, sem gravar; demorarem a responder depois de gravar ("slow"); ou
caírem sem resposta ("drop").
"""

import argparse
import http.server
import json
import os
import threading
import time

MAX_PHOTO = 5 * 1024 * 1024


def parse_multipart(content_type, body):
    """As partes de um multipart/form-data, por nome, em bytes. None se não der para ler."""
    if not content_type.startswith("multipart/form-data"):
        return None
    boundary = None
    for item in content_type.split(";")[1:]:
        key, _, value = item.strip().partition("=")
        if key.lower() == "boundary":
            boundary = value.strip('"')
    if not boundary:
        return None
    parts = {}
    for chunk in body.split(b"--" + boundary.encode())[1:]:
        if chunk.startswith(b"--"):
            break                                   # o fim do multipart
        head, separator, content = chunk.partition(b"\r\n\r\n")
        if not separator:
            return None
        if content.endswith(b"\r\n"):
            content = content[:-2]
        for line in head.decode("latin-1").split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                for item in line.split(";")[1:]:
                    key, _, value = item.strip().partition("=")
                    if key == "name":
                        parts[value.strip('"')] = content
    return parts


def _safe(text):
    return "".join(c for c in str(text) if c.isalnum() or c in "-_") or "x"


class FakeServer:
    """O servidor falso: url, events, faults, counts, start() e stop()."""

    def __init__(self, folder, port=0, host="127.0.0.1", token=None, slow_s=2.0, quiet=True):
        self.folder = folder
        self.token = token
        self.slow_s = slow_s
        self.quiet = quiet
        self.faults = []        # os próximos pedidos: um código HTTP, "slow" ou "drop"
        self.requests = []      # (caminho, cabeçalhos) de cada pedido, na ordem
        self.events = {}        # (device_id, ts, type) -> o registro guardado
        self.counts = {"health": 0, "telemetry": 0}
        self._lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)
        self._log = os.path.join(folder, "eventos.jsonl")
        if os.path.exists(self._log):
            with open(self._log, encoding="utf-8") as f:
                for line in f:
                    record = json.loads(line)
                    self.events[(record["device_id"], record["ts"], record["type"])] = record
        handler = type("Handler", (_Handler,), {"state": self})
        self.httpd = http.server.ThreadingHTTPServer((host, port), handler)
        self.url = "http://%s:%d/api" % (host, self.httpd.server_address[1])
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def received(self):
        """Os eventos guardados, na ordem de chegada."""
        return sorted(self.events.values(), key=lambda record: record["chegou"])

    def say(self, text):
        if not self.quiet:
            print("[servidor falso] %s %s" % (time.strftime("%H:%M:%S"), text), flush=True)

    def receive_event(self, headers, body):
        """O POST /api/events: o código da resposta."""
        auth = headers.get("Authorization", "")
        device = headers.get("X-Device-Id")
        if not auth.startswith("Bearer ") or (self.token is not None and auth != "Bearer " + self.token):
            self.say("token recusado (401)")
            return 401
        parts = parse_multipart(headers.get("Content-Type", ""), body)
        if not device or parts is None or "dados" not in parts or "foto" not in parts:
            self.say("pedido sem o X-Device-Id ou sem as partes dados e foto (400)")
            return 400
        try:
            dados = json.loads(parts["dados"])
            ts, kind, score, telemetria = dados["ts"], dados["type"], dados["score"], dados["telemetria"]
            if not (isinstance(ts, (int, float)) and isinstance(kind, str)
                    and isinstance(score, (int, float)) and isinstance(telemetria, dict)):
                raise TypeError("tipos")
        except (ValueError, KeyError, TypeError) as error:
            self.say("dados inválidos: %s (400)" % error)
            return 400
        photo = parts["foto"]
        if len(photo) > MAX_PHOTO:
            self.say("foto de %d bytes, acima de 5 MB (413)" % len(photo))
            return 413
        if not photo.startswith(b"\xff\xd8"):
            self.say("a foto não é JPEG (400)")
            return 400
        key = (device, ts, kind)
        with self._lock:
            if key in self.events:
                self.say("reenvio de %s em %.3f: já guardado, sem duplicar (200)" % (kind, ts))
                return 200
            name = "%s-%d-%s.jpg" % (_safe(device), round(ts * 1000), _safe(kind))
            with open(os.path.join(self.folder, name), "wb") as f:
                f.write(photo)
            record = {"device_id": device, "ts": ts, "type": kind, "score": score,
                      "telemetria": telemetria, "foto": name, "bytes": len(photo), "chegou": time.time()}
            with open(self._log, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            self.events[key] = record
        self.say("evento %s %.2f em %.3f (%d KB; telemetria: %s) de %s" % (
            kind, score, ts, round(len(photo) / 1024), ", ".join(sorted(telemetria)) or "vazia", device))
        return 200


class _Handler(http.server.BaseHTTPRequestHandler):
    state = None

    def do_POST(self):
        state = self.state
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with state._lock:
            state.requests.append((self.path, dict(self.headers)))
            fault = state.faults.pop(0) if state.faults else None
        if fault == "drop":
            self.close_connection = True        # cai sem resposta
            return
        if isinstance(fault, int):
            status = fault                      # responde o código pedido, sem gravar
        else:
            path = self.path.rstrip("/")
            if path.endswith("/events"):
                status = state.receive_event(self.headers, body)
            elif path.endswith("/health") or path.endswith("/telemetry"):
                with state._lock:
                    state.counts[path.rsplit("/", 1)[1]] += 1
                status = 200
            else:
                status = 404
            if fault == "slow":
                time.sleep(state.slow_s)        # gravou, mas responde tarde
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
    args = parser.parse_args(argv)
    server = FakeServer(args.dir, port=args.port, host=args.host, token=args.token, quiet=False)
    print("[servidor falso] em %s, pasta %s, com %d eventos já guardados. Ctrl+C para parar."
          % (server.url, os.path.abspath(args.dir), len(server.events)), flush=True)
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()
        print("\n[servidor falso] parado: %d eventos guardados, %d saúdes e %d lotes de telemetria nesta execução"
              % (len(server.events), server.counts["health"], server.counts["telemetry"]), flush=True)


if __name__ == "__main__":
    main()
