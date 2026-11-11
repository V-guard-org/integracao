"""
V-GUARD — o envio dos eventos ao servidor (ticket 4, Issue #18; contrato 2.1).

Desde o ticket 4, o evento vai pelo MQTT e só a foto vai pelo HTTPS:

    1. quando a janela fecha (ts + 12 s), o evento vai ao vguard/<device>/eventos,
       com QoS 1: event_id, ts, type, score, has_photo, data (o retrato) e
       window (±10 s, da telemetria do SQLite). Espera o PUBACK;
    2. 2 s depois, a foto vai num PUT <VGUARD_BACKEND>/events/<event_id>/photo,
       com o JPEG cru. O 200 dela é o que confirma o evento: o backend só
       aceita a foto com o evento já gravado.

    200          confirmado: sai da fila
    409          o evento ainda não está no banco: novo PUT em 30 s; depois de
                 3 respostas 409, espera crescente e o evento é publicado de novo
    400, 413     recusado em definitivo (FAILED): sai da fila e fica no cartão
    401          o token está errado: continua pendente, com a espera
    5xx, rede,
    sem PUBACK   continua pendente, com a espera (60 s, dobrando até 10 min)

O event_id é um UUID calculado da chave (device_id, ts em ms, type): o mesmo
evento dá sempre o mesmo, inclusive depois de reiniciar, e o servidor não
duplica. Um evento por vez, do mais antigo ao mais novo. Roda numa thread do
principal, ao lado do publicador:

    EventSender(manager, publisher).start()
"""

import http.client
import json
import logging
import math
import threading
import time
import urllib.parse
import uuid

import sync
from event_manager import CONFIRMED, FAILED

log = logging.getLogger("events.sender")

# A espera depois de uma falha: começa em FIRST_WAIT e dobra até MAX_WAIT
# (contrato). Volta a FIRST_WAIT quando o servidor confirma de novo.
FIRST_WAIT = 60.0
MAX_WAIT = 600.0

# Uma foto de 50 a 100 KB leva de 0,6 a 5 s no 4G, conforme o sinal (§7).
HTTP_TIMEOUT = 30.0

# A janela de ±10 s, e a margem para a linha do último segundo ser gravada:
# o evento sai a partir de ts + WINDOW + WINDOW_MARGIN.
WINDOW = 10.0
WINDOW_MARGIN = 2.0

# Do PUBACK ao PUT da foto, para o consumidor gravar o evento.
PUT_DELAY = 2.0

# O 409 (evento ainda não gravado): a espera até o próximo PUT, e quantos
# 409 seguidos encerram a rodada (o evento é publicado de novo na próxima).
CONFLICT_WAIT = 30.0
CONFLICT_LIMIT = 3

# O broker do piloto descarta, sem avisar, uma mensagem acima de 16 KiB.
MAX_MESSAGE = 15 * 1024

# Respostas em que o servidor recusa a foto em si: reenviar não adianta.
REJECTED = (400, 413)

# Os resultados de send_pending().
DONE, NO_LINK, NOT_YET, FAILURE = "done", "no_link", "not_yet", "failure"


def plural(count, one, many):
    return "%d %s" % (count, one if count == 1 else many)


def event_id(device, ts, type):
    """O UUID do evento, calculado da chave (contrato 2.1)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "vguard:%s:%d:%s" % (device, round(ts * 1000), type)))


def window_entries(rows):
    """
    As linhas de telemetria [{ts, data}] no formato da janela do contrato:
    uma entrada {ts, source, data} por sensor e por linha, sem o ts do sensor
    e sem o age_ms. O sensor só com o horário fica de fora.
    """
    entries = []
    for row in rows:
        for source, sample in row["data"].items():
            fields = {k: v for k, v in sample.items() if k not in ("ts", "age_ms")}
            if fields:
                entries.append({"ts": row["ts"], "source": source, "data": fields})
    return entries


def event_message(event, device, rows, limit=MAX_MESSAGE):
    """
    O texto JSON do evento para o tópico eventos. Acima do limite, a janela
    perde as pontas (o segundo mais longe do evento primeiro) até caber.
    Devolve (texto, entradas da janela, segundos da janela, cortada).
    """
    rows = sorted(rows, key=lambda row: row["ts"])
    message = {
        "event_id": event_id(device, event["ts"], event["type"]),
        "ts": event["ts"],
        "type": event["type"],
        "score": event["score"],
        "has_photo": True,
        "data": event["telemetria"],
        "window": [],
    }
    trimmed = False
    while True:
        message["window"] = window_entries(rows)
        text = json.dumps(message, separators=(",", ":"))
        if len(text.encode()) <= limit or not rows:
            break
        trimmed = True
        # Tira a linha mais longe do evento (a do começo, no empate).
        if abs(rows[-1]["ts"] - event["ts"]) > abs(rows[0]["ts"] - event["ts"]):
            rows.pop()
        else:
            rows.pop(0)
    seconds = len({entry["ts"] for entry in message["window"]})
    return text, len(message["window"]), seconds, trimmed


class EventSender:
    """
    Quem manda os eventos ao servidor: start(), stop(), sent (os confirmados
    desde o início), summary() e history (por evento: o event_id e os status
    do PUT, para a server_bench).
    """

    def __init__(self, manager, publisher, link=sync.active_link, url=None, token=None, device=None,
                 first_wait=FIRST_WAIT, max_wait=MAX_WAIT, timeout=HTTP_TIMEOUT,
                 window_margin=WINDOW_MARGIN, put_delay=PUT_DELAY, conflict_wait=CONFLICT_WAIT,
                 clock=time.time):
        self.manager = manager
        self.publisher = publisher
        self.link = link
        self.url = url or sync.BACKEND_URL
        self.token = token          # None: sync.device_token() a cada pedido
        self.device = device or sync.DEVICE_ID
        self.first_wait = first_wait
        self.max_wait = max_wait
        self.timeout = timeout
        self.window_margin = window_margin
        self.put_delay = put_delay
        self.conflict_wait = conflict_wait
        self.clock = clock

        self.sent = 0
        self.history = {}               # id local -> {event_id, ts, type, window, size, puts}
        self.wait = first_wait          # a espera da próxima falha
        self.retry_in = None            # a espera depois da última falha
        self._attempts = 0              # tentativas falhas no episódio em curso
        self._failing = False
        self._no_link = None            # o motivo da espera sem enlace já avisado
        self._published = None          # o id local do evento publicado e ainda sem o 200
        self._conflicts = 0             # 409 seguidos do evento em curso
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ interface

    def start(self):
        if not self._token():
            log.warning("token vazio (VGUARD_TOKEN_FILE ou VGUARD_TOKEN): o servidor vai recusar as fotos (401)")
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
            self.publisher.connection_changed.clear()
            outcome, delay = self.send_pending()
            if outcome == FAILURE:
                # Durante a espera, um evento novo não acorda o envio: ele sai
                # junto com os outros, no fim dela (a franquia do 4G).
                self._stop.wait(self.retry_in)
            elif outcome == NOT_YET:
                self._stop.wait(delay)      # a janela do mais antigo fechando
            elif outcome == NO_LINK:
                # Confere de novo a cada FIRST_WAIT, com um evento novo ou
                # quando o MQTT reconecta.
                self._wait_for_news(self.first_wait)
            else:
                self._wait_for_news(None)

    def _wait_for_news(self, timeout):
        end = None if timeout is None else time.monotonic() + timeout
        while not self._stop.is_set():
            step = 1.0 if end is None else min(1.0, end - time.monotonic())
            if step <= 0 or self.manager.event_recorded.wait(step) \
                    or self.publisher.connection_changed.is_set():
                return

    def send_pending(self):
        """
        Envia os eventos pendentes, um por vez, do mais antigo ao mais novo.
        Devolve (resultado, espera): DONE (a fila esvaziou), NOT_YET (a janela
        do mais antigo ainda não fechou; espera em segundos), NO_LINK ou
        FAILURE (esperar retry_in e tentar de novo).
        """
        while not self._stop.is_set():
            events = self.manager.pending_events(limit=1)
            if not events:
                return DONE, None
            event = events[0]

            ready_at = event["ts"] + WINDOW + self.window_margin
            if self.clock() < ready_at:
                return NOT_YET, ready_at - self.clock()

            reason = None
            if self.link() is None:
                reason = "sem enlace"
            elif not self.publisher.connected:
                reason = "MQTT desconectado"
            if reason:
                if self._no_link != reason:
                    self._no_link = reason
                    log.info("envio de eventos: %s; %s no cartão", reason,
                             plural(self.manager.pending_counts()["events"], "evento espera", "eventos esperam"))
                return NO_LINK, None
            self._no_link = None

            outcome = self._send(event)
            if outcome is not None:
                return outcome, None
        return DONE, None

    def _send(self, event):
        """Uma rodada de um evento. Devolve None para seguir ao próximo, ou FAILURE."""
        started = time.monotonic()
        record = self.history.setdefault(event["id"], {
            "event_id": event_id(self.device, event["ts"], event["type"]),
            "ts": event["ts"], "type": event["type"], "window": 0, "size": 0, "puts": []})

        try:
            with open(event["photo"], "rb") as f:
                jpeg = f.read()
        except FileNotFoundError:
            # Apagada do cartão à mão: nunca vai subir. Sem isto, a foto
            # ausente travaria a fila para sempre.
            log.warning("foto do evento %d não existe mais no cartão: fica como recusado", event["id"])
            self.manager.ack_event(event["id"], FAILED)
            return None

        if self._published != event["id"]:
            # As linhas dos segundos que contêm ts - 10 e ts + 10, e as do meio: 21.
            rows = self.manager.telemetry_window(math.floor(event["ts"] - WINDOW),
                                                 math.floor(event["ts"] + WINDOW))
            text, entries, seconds, trimmed = event_message(event, self.device, rows)
            if trimmed:
                log.warning("evento %d: a janela passou de %d KB e perdeu as pontas (ficaram %d s)",
                            event["id"], MAX_MESSAGE // 1024, seconds)
            if not self.publisher.publish_event(text):
                self._failed("o broker não confirmou o evento (sem PUBACK)")
                return FAILURE
            self._published = event["id"]
            self._conflicts = 0
            record.update(window=seconds, size=len(text.encode()))
            if self._stop.wait(self.put_delay):
                return None

        while True:
            try:
                status = self._put(record["event_id"], jpeg)
            except (OSError, http.client.HTTPException) as error:
                self._failed(str(error) or type(error).__name__)
                return FAILURE
            record["puts"].append(status)

            if 200 <= status < 300:
                self.manager.ack_event(event["id"], CONFIRMED)
                self.sent += 1
                self._published = None
                self._recovered()
                log.info("evento %d enviado (%s, janela %d s / %.1f KB, foto %d KB, %.1f s)",
                         event["id"], event["type"], record["window"], record["size"] / 1024,
                         round(len(jpeg) / 1024), time.monotonic() - started)
                return None
            if status == 409:
                self._conflicts += 1
                if self._conflicts < CONFLICT_LIMIT:
                    if self._stop.wait(self.conflict_wait):
                        return None
                    continue
                log.warning("evento %d: o servidor não gravou o evento (409 x%d); publicado de novo na próxima rodada",
                            event["id"], self._conflicts)
                self._published = None      # a próxima rodada publica de novo
                self._failed("HTTP 409 x%d" % self._conflicts, quiet=True)
                return FAILURE
            if status in REJECTED:
                self.manager.ack_event(event["id"], FAILED)
                self._published = None
                self._recovered()
                log.warning("servidor recusou a foto do evento %d (HTTP %d): fica no cartão", event["id"], status)
                return None
            self._failed("HTTP %d" % status, token=status == 401)
            return FAILURE

    def _token(self):
        return sync.device_token() if self.token is None else self.token

    def _put(self, uuid_text, jpeg):
        """Um PUT da foto. Devolve o código HTTP."""
        url = urllib.parse.urlsplit(self.url)
        connection = (http.client.HTTPSConnection if url.scheme == "https"
                      else http.client.HTTPConnection)(url.netloc, timeout=self.timeout)
        try:
            connection.request("PUT", "%s/events/%s/photo" % (url.path.rstrip("/"), uuid_text), jpeg, {
                "Content-Type": "image/jpeg",
                "Authorization": "Bearer " + self._token(),
                "X-Device-Id": self.device,
            })
            response = connection.getresponse()
            response.read()
            return response.status
        finally:
            connection.close()

    # ------------------------------------------------------------------ o log por episódio

    def _failed(self, reason, token=False, quiet=False):
        self._attempts += 1
        self.retry_in = self.wait
        self.wait = min(self.wait * 2, self.max_wait)
        if self._failing:
            return          # uma linha por episódio
        self._failing = True
        if token:
            log.error("token recusado (HTTP 401): confira o VGUARD_TOKEN_FILE; nova tentativa em %g s", self.retry_in)
        elif not quiet:
            log.warning("envio de eventos falhou (%s); nova tentativa em %g s", reason, self.retry_in)

    def _recovered(self):
        if self._failing:
            log.info("envio de eventos de volta depois de %s",
                     plural(self._attempts, "tentativa", "tentativas"))
        self._failing = False
        self._attempts = 0
        self.wait = self.first_wait
