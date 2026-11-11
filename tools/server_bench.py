"""
Ensaios com o servidor (ticket 4, Issue #18): roda, por N segundos e numa
pasta nova, o leitor do ESP32, o EventManager, o publicador MQTT e o envio dos
eventos, todos de verdade, e grava eventos sintéticos com uma foto JPEG de
640×480 (tools/foto-sintetica.jpg). No fim, mostra o relatório que o servidor
pediu: os ts publicados e as lacunas, as quedas e as voltas do MQTT, e cada
evento com o event_id e os status do PUT da foto.

No venv-principal (o paho-mqtt), com as variáveis do item 1 da Issue #18
(VGUARD_DEVICE, VGUARD_MQTT_*, VGUARD_BACKEND, VGUARD_TOKEN_FILE, VGUARD_WIFI):

    /home/vguard/vguard/venv-principal/bin/python tools/server_bench.py --seconds 300 --evento-a 60
    ... --seconds 600 --evento-a 120 --evento-a 300        # o ensaio 4: derrubar o túnel no meio

Durante o ensaio: E + Enter grava um evento sintético agora; Q + Enter termina
antes. No fim, ele ainda espera (até --espera-final s) os eventos pendentes
saírem: cada um só sai 12 s depois, quando a janela fecha.

Sai com o código 1 se algum evento ficou sem o 200 da foto.
"""

import argparse
import logging
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "events"))

import esp32_reader                                      # noqa: E402
import sync                                              # noqa: E402
from esp32_reader import Esp32Reader                     # noqa: E402
from modem_reader import ModemReader                     # noqa: E402
from event_manager import CONFIRMED, EventManager        # noqa: E402
from event_sender import EventSender                     # noqa: E402
from mqtt_publisher import MqttPublisher                 # noqa: E402

PHOTO = os.path.join(HERE, "foto-sintetica.jpg")


def clock(ts):
    """O horário local de um ts, para o relatório."""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def gaps(published):
    """As lacunas na sequência de ts publicados: [(último antes, primeiro depois)]."""
    found = []
    for before, after in zip(published, published[1:]):
        if after - before > 1.0:
            found.append((before, after))
    return found


def gps_report(modem, manager, started, ended):
    """O GNSS no ensaio: a primeira posição, as linhas com gps e as horas (ticket 6)."""
    lines = ["", "GNSS (pela porta AT do modem, %s):" % modem.port]
    if modem.first_fix_after is None:
        lines.append("  nenhuma posição no ensaio")
    else:
        lines.append("  primeira posição %.0f s depois de ligar o GNSS; %d leituras com posição"
                     % (modem.first_fix_after, modem.fixes))
    rows = manager.telemetry_window(int(started), int(ended))
    with_gps = [row for row in rows if set(row["data"].get("gps", {})) - {"ts"}]
    only_ts = [row for row in rows if "gps" in row["data"] and row not in with_gps]
    lines.append("  telemetria: %d linhas com posição, %d só com o horário do gps, %d sem gps"
                 % (len(with_gps), len(only_ts), len(rows) - len(with_gps) - len(only_ts)))
    if with_gps:
        last = with_gps[-1]["data"]["gps"]
        speeds = [row["data"]["gps"]["speed"] for row in with_gps]
        lines.append("  última posição: %.6f, %.6f; velocidade de %.1f a %.1f km/h (média %.1f)"
                     % (last["lat"], last["lon"], min(speeds), max(speeds), sum(speeds) / len(speeds)))
    now_wall, now_mono = time.time(), time.monotonic()
    for name, sample in (("GNSS", modem.gnss_time), ("operadora", modem.operator_time)):
        if sample is None:
            lines.append("  hora (%s): não veio" % name)
        else:
            utc, read_at = sample
            offset = (now_wall - (now_mono - read_at)) - utc
            lines.append("  relógio da Pi − %s: %+.1f s" % (name, offset))
    return lines


def report(args, publisher, sender, manager, published, unpublished, started, ended, modem=None):
    lines = ["", "=" * 72, "RELATÓRIO PARA O SERVIDOR (ensaio de %s, %s a %s)" % (
        time.strftime("%d/%m/%Y", time.localtime(started)), clock(started), clock(ended)), "=" * 72]
    lines.append("device_id: %s; usuário MQTT: %s; broker: %s:%d; backend: %s" % (
        publisher.device, publisher.user, publisher.host, publisher.port, sender.url))

    lines.append("")
    lines.append("Telemetria (vguard/%s/telemetria, QoS 0):" % publisher.device)
    if published:
        lines.append("  publicada: ts %d (%s) a %d (%s); %d linhas" % (
            published[0], clock(published[0]), published[-1], clock(published[-1]), len(published)))
    else:
        lines.append("  publicada: nenhuma linha")
    for before, after in gaps(published):
        lines.append("  lacuna: depois do ts %d (%s), a próxima foi o %d (%s): %d s sem publicar" % (
            before, clock(before), after, clock(after), after - before - 1))
    if unpublished:
        lines.append("  não publicadas (MQTT desconectado): %d linhas, de %d (%s) a %d (%s); ficam no cartão" % (
            len(unpublished), unpublished[0], clock(unpublished[0]), unpublished[-1], clock(unpublished[-1])))
    lines.append("  saúdes publicadas (vguard/%s/saude): %d" % (publisher.device, publisher.health_sent))

    lines.append("")
    lines.append("MQTT:")
    down = None
    for when, state in publisher.history:
        if state == "caiu":
            down = when
            lines.append("  %s caiu (ts %.0f)" % (clock(when), when))
        elif down is None:
            lines.append("  %s conectado (ts %.0f)" % (clock(when), when))
        else:
            lines.append("  %s de volta (ts %.0f), %.0f s depois da queda" % (clock(when), when, when - down))
            down = None
    if not publisher.history:
        lines.append("  nunca conectou")

    lines.append("")
    lines.append("Eventos (vguard/%s/eventos, QoS 1; foto por PUT /api/events/<event_id>/photo):" % publisher.device)
    states = {row[0]: row[1] for row in manager._db.execute("SELECT id, synced FROM events")}
    for local_id, record in sorted(sender.history.items()):
        lines.append("  %d. ts %.3f (%s) %s; event_id %s" % (
            local_id, record["ts"], clock(record["ts"]), record["type"], record["event_id"]))
        lines.append("     janela %d s / %.1f KB; PUT da foto: %s; %s" % (
            record["window"], record["size"] / 1024, ", ".join(map(str, record["puts"])) or "nenhum",
            "confirmado" if states.get(local_id) == CONFIRMED else "PENDENTE"))
    for local_id in sorted(set(states) - set(sender.history)):
        lines.append("  %d. não chegou a ser enviado (PENDENTE)" % local_id)
    if not states:
        lines.append("  nenhum evento")
    if modem is not None:
        lines.extend(gps_report(modem, manager, started, ended))
    lines.append("=" * 72)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seconds", type=float, default=300, help="duração do ensaio (padrão: %(default)s)")
    parser.add_argument("--evento-a", type=float, action="append", default=[], metavar="SEGUNDOS",
                        help="grava um evento sintético neste segundo do ensaio (pode repetir)")
    parser.add_argument("--espera-final", type=float, default=120,
                        help="espera máxima, no fim, pelos eventos pendentes (padrão: %(default)s)")
    parser.add_argument("--port", default=esp32_reader.PORT,
                        help="porta do ESP32 (padrão: VGUARD_ESP32 ou /dev/vguard-esp32)")
    parser.add_argument("--no-esp32", action="store_true", help="sem o ESP32: só a saúde e os eventos")
    parser.add_argument("--no-gps", action="store_true", help="sem o leitor do modem (o GNSS pela porta AT)")
    parser.add_argument("--data-dir", help="pasta nova (padrão: dados-ensaio-<data> na pasta atual)")
    args = parser.parse_args(argv)

    data_dir = os.path.abspath(args.data_dir or time.strftime("dados-ensaio-%Y%m%d-%H%M%S"))
    if os.path.exists(os.path.join(data_dir, "vguard.db")):
        parser.error("%s já tem um vguard.db: use uma pasta nova" % data_dir)
    os.makedirs(data_dir, exist_ok=True)
    log_path = os.path.join(data_dir, "ensaio.log")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_path, encoding="utf-8")])
    with open(PHOTO, "rb") as f:
        jpeg = f.read()

    manager = EventManager(data_dir)
    publisher = MqttPublisher(manager)
    sender = EventSender(manager, publisher)
    reader = None if args.no_esp32 else Esp32Reader(manager, args.port)
    modem = None if args.no_gps else ModemReader(manager)
    logging.info("ensaio por %g s em %s: device %s, broker %s:%d (TLS %s), backend %s, token %s",
                 args.seconds, data_dir, publisher.device, publisher.host, publisher.port,
                 "sim" if publisher.ca else "não", sender.url,
                 "lido" if sync.device_token() else "VAZIO")

    published, unpublished = [], []
    started = time.time()
    publisher.start()
    original = manager.on_telemetry

    def spy(row):
        before = publisher.published
        original(row)
        (published if publisher.published > before else unpublished).append(row["ts"])

    manager.on_telemetry = spy
    sender.start()
    if reader:
        reader.start()
    if modem:
        modem.start()

    stop = threading.Event()

    def record(why):
        local_id = manager.record_event("fadiga", 0.80, jpeg)
        logging.warning("evento sintético %s gravado (%s); sai quando a janela fechar, em 12 s", local_id, why)

    def keyboard():
        for line in sys.stdin:
            command = line.strip().upper()
            if command == "E":
                record("tecla E")
            elif command == "Q":
                stop.set()
                return

    threading.Thread(target=keyboard, daemon=True).start()
    marks = sorted(args.evento_a)
    try:
        while not stop.is_set():
            elapsed = time.time() - started
            if elapsed >= args.seconds:
                break
            while marks and elapsed >= marks[0]:
                record("--evento-a %g" % marks.pop(0))
            stop.wait(0.2)

        end = time.time() + args.espera_final
        while manager.pending_counts()["events"] and time.time() < end:
            logging.info("esperando %d evento(s) pendente(s) sair(em)...", manager.pending_counts()["events"])
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        if reader:
            reader.stop()
        if modem:
            modem.stop()
        sender.stop()
        manager.on_telemetry = original
        publisher.stop()
        ended = time.time()
        text = report(args, publisher, sender, manager, published, unpublished, started, ended, modem)
        pending = manager.pending_counts()["events"]
        manager.close()
    print(text)
    with open(os.path.join(data_dir, "relatorio.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("Relatório também em %s; log em %s" % (os.path.join(data_dir, "relatorio.txt"), log_path))
    return 1 if pending else 0


if __name__ == "__main__":
    sys.exit(main())
