"""
V-GUARD — o envio dos eventos ao servidor (ticket 3, Issue #16).

Cada evento gravado pelo EventManager vai ao servidor num POST /api/events,
com a foto junto, como manda o contrato (contrato-pi-servidor.md):

    POST <VGUARD_BACKEND>/events
    Authorization: Bearer <VGUARD_TOKEN>
    X-Device-Id: <VGUARD_DEVICE>
    multipart/form-data: dados (JSON: ts, type, score, telemetria) + foto (JPEG)

Um evento por vez, do mais antigo ao mais novo, logo depois da detecção. Sem
enlace ou com o servidor fora, o evento espera no cartão. A tentativa seguinte
vem depois de 60 s, dobrando até 10 min, para não gastar a franquia do 4G.

    2xx (200)    confirmado: sai da fila
    400, 413     recusado em definitivo (FAILED): sai da fila e fica no cartão
    401          o token está errado: continua pendente, com a espera
    5xx, rede    continua pendente, com a espera

O enlace é o do sync.py (active_link): o Wi-Fi autorizado, ou o 4G do modem no
modo placa de rede (usb0). Roda numa thread do principal, ao lado do Sync:

    EventSender(manager).start()
"""

import http.client
import json
import logging
import os
import threading
import time
import urllib.parse
import uuid

import sync
from event_manager import CONFIRMED, FAILED

log = logging.getLogger("events.sender")

# A espera depois de uma falha: começa em FIRST_WAIT e dobra até MAX_WAIT
# (contrato). Volta a FIRST_WAIT quando o servidor responde de novo.
FIRST_WAIT = 60.0
MAX_WAIT = 600.0

# Uma foto de 50 a 100 KB leva de 0,6 a 5 s no 4G, conforme o sinal (§7).
HTTP_TIMEOUT = 30.0

# Respostas em que o servidor recusa o evento em si: reenviar não adianta.
REJECTED = (400, 413)

# Os resultados de send_pending().
DONE, NO_LINK, FAILURE = "done", "no_link", "failure"


def plural(count, one, many):
    return "%d %s" % (count, one if count == 1 else many)


def multipart(boundary, dados, jpeg, filename):
    """O corpo multipart/form-data do contrato: a parte dados (JSON) e a parte foto (JPEG)."""
    mark = b"--" + boundary.encode()
    return b"".join([
        mark, b"\r\n",
        b'Content-Disposition: form-data; name="dados"\r\n',
        b"Content-Type: application/json\r\n\r\n",
        json.dumps(dados).encode(), b"\r\n",
        mark, b"\r\n",
        b'Content-Disposition: form-data; name="foto"; filename="', filename.encode(), b'"\r\n',
        b"Content-Type: image/jpeg\r\n\r\n",
        jpeg, b"\r\n",
        mark, b"--\r\n",
    ])


class EventSender:
    """
    Quem manda os eventos ao servidor: start(), stop(), sent (os enviados
    desde o início) e summary(), para o resumo do principal.
    """

    def __init__(self, manager, link=sync.active_link, url=None, token=None, device=None,
                 first_wait=FIRST_WAIT, max_wait=MAX_WAIT, timeout=HTTP_TIMEOUT):
        self.manager = manager
        self.link = link
        self.url = url or sync.BACKEND_URL
        self.token = sync.DEVICE_TOKEN if token is None else token
        self.device = device or sync.DEVICE_ID
        self.first_wait = first_wait
        self.max_wait = max_wait
        self.timeout = timeout

        self.sent = 0
        self.wait = first_wait          # a espera da próxima falha
        self.retry_in = None            # a espera depois da última falha
        self._attempts = 0              # tentativas falhas no episódio em curso
        self._failing = False
        self._no_link = False
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ interface

    def start(self):
        if not self.token:
            log.warning("VGUARD_TOKEN vazio: o servidor vai recusar os eventos (401)")
        self._thread = threading.Thread(target=self.run, name="event-sender", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def summary(self):
        """Para o resumo do principal. Chamar antes de fechar o banco."""
        pending = self.manager.pending_counts()["events"]
        return "envio: %s, %s" % (plural(self.sent, "evento enviado", "eventos enviados"),
                                  plural(pending, "pendente", "pendentes"))

    # ------------------------------------------------------------------ o laço

    def run(self):
        while not self._stop.is_set():
            self.manager.event_recorded.clear()
            outcome = self.send_pending()
            if outcome == FAILURE:
                # Durante a espera, um evento novo não acorda o envio: ele sai
                # junto com os outros, no fim dela (a franquia do 4G).
                self._stop.wait(self.retry_in)
            else:
                # Sem enlace, ele é conferido de novo a cada FIRST_WAIT.
                self._wait_for_event(self.first_wait if outcome == NO_LINK else None)

    def _wait_for_event(self, timeout):
        end = None if timeout is None else time.monotonic() + timeout
        while not self._stop.is_set():
            step = 1.0 if end is None else min(1.0, end - time.monotonic())
            if step <= 0 or self.manager.event_recorded.wait(step):
                return

    def send_pending(self):
        """
        Envia os eventos pendentes, um por vez, do mais antigo ao mais novo.
        Devolve DONE (a fila esvaziou), NO_LINK ou FAILURE (esperar e tentar
        de novo).
        """
        while not self._stop.is_set():
            events = self.manager.pending_events(limit=1)
            if not events:
                return DONE
            if self.link() is None:
                if not self._no_link:
                    self._no_link = True
                    log.info("envio de eventos: sem enlace; %s no cartão",
                             plural(self.manager.pending_counts()["events"], "evento espera", "eventos esperam"))
                return NO_LINK
            self._no_link = False
            event = events[0]
            started = time.monotonic()
            try:
                status, size = self._post(event)
            except FileNotFoundError:
                # Apagada do cartão à mão: nunca vai subir. Sem isto, a foto
                # ausente travaria a fila para sempre.
                log.warning("foto do evento %d não existe mais no cartão: fica como recusado", event["id"])
                self.manager.ack_event(event["id"], FAILED)
                continue
            except (OSError, http.client.HTTPException) as error:
                self._failed(str(error) or type(error).__name__)
                return FAILURE

            if 200 <= status < 300:
                self.manager.ack_event(event["id"], CONFIRMED)
                self.sent += 1
                self._recovered()
                log.info("evento %d enviado (%s, %d KB, %.1f s)", event["id"], event["type"],
                         round(size / 1024), time.monotonic() - started)
            elif status in REJECTED:
                self.manager.ack_event(event["id"], FAILED)
                self._recovered()
                log.warning("servidor recusou o evento %d (HTTP %d): fica no cartão", event["id"], status)
            else:
                self._failed("HTTP %d" % status, token=status == 401)
                return FAILURE
        return DONE

    def _post(self, event):
        """Um POST /api/events. Devolve (o código HTTP, o tamanho do corpo)."""
        with open(event["photo"], "rb") as f:
            jpeg = f.read()
        dados = {"ts": event["ts"], "type": event["type"], "score": event["score"],
                 "telemetria": event["telemetria"]}
        boundary = uuid.uuid4().hex
        body = multipart(boundary, dados, jpeg, os.path.basename(event["photo"]))
        url = urllib.parse.urlsplit(self.url)
        connection = (http.client.HTTPSConnection if url.scheme == "https"
                      else http.client.HTTPConnection)(url.netloc, timeout=self.timeout)
        try:
            connection.request("POST", url.path.rstrip("/") + "/events", body, {
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Authorization": "Bearer " + self.token,
                "X-Device-Id": self.device,
            })
            response = connection.getresponse()
            response.read()
            return response.status, len(body)
        finally:
            connection.close()

    # ------------------------------------------------------------------ o log por episódio

    def _failed(self, reason, token=False):
        self._attempts += 1
        self.retry_in = self.wait
        self.wait = min(self.wait * 2, self.max_wait)
        if self._failing:
            return          # uma linha por episódio
        self._failing = True
        if token:
            log.error("token recusado (HTTP 401): confira o VGUARD_TOKEN; nova tentativa em %g s", self.retry_in)
        else:
            log.warning("envio de eventos falhou (%s); nova tentativa em %g s", reason, self.retry_in)

    def _recovered(self):
        if self._failing:
            log.info("envio de eventos de volta depois de %s",
                     plural(self._attempts, "tentativa", "tentativas"))
        self._failing = False
        self._attempts = 0
        self.wait = self.first_wait
