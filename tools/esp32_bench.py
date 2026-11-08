"""
Bancada do leitor do ESP32 (Issue #3): roda o Esp32Reader de verdade, com um
EventManager numa pasta nova, por N segundos, e mostra o resumo. Usa só a
biblioteca padrão, como o events/.

    python3 tools/esp32_bench.py --port /dev/ttyUSB0 --seconds 600
    python3 tools/esp32_bench.py --port /dev/ttyUSB0 --seconds 30 --obd-hz 5
    python3 tools/esp32_bench.py --port /dev/ttyUSB0 --seconds 20 --no-clock

O ESP32 com o firmware esp32-sim (firmware_esp32: pio run -e esp32-sim -t
upload) manda a viagem simulada. O log mostra o $B, o $L, o $P, as mudanças
do $S, os $H e $C mandados e as perdas; o resumo traz as contagens e a
telemetria gravada no SQLite.

Sai com o código 1 se houve linha perdida ou corrompida, ou se nenhuma linha
válida chegou.
"""

import argparse
import logging
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "events"))

import esp32_reader                                      # noqa: E402
from esp32_reader import OBD_FIELDS, Esp32Reader         # noqa: E402
from event_manager import EventManager                   # noqa: E402


def rate(link, kind):
    """Linhas por segundo de um tipo, entre a primeira e a última."""
    count = link.counts.get(kind, 0)
    if count < 2:
        return None
    return (count - 1) / (link.last_seen[kind] - link.first_seen[kind])


def summary(reader, manager, elapsed):
    link = reader.link
    rows = manager.pending_telemetry(10**7)
    both = [r for r in rows if {"imu", "obd"} <= set(r["data"])]
    # OBD com valor, e não só o horário do "sem dado".
    with_values = [r for r in rows if set(r["data"].get("obd", {})) - {"ts"}]
    ages = {}
    for row in with_values:
        for field, age in row["data"]["obd"].get("age_ms", {}).items():
            ages[field] = max(ages.get(field, 0), age)
    kinds = ", ".join("$%s %d" % item for item in sorted(link.counts.items()))
    rates = ", ".join("$%s %.2f/s" % (k, r) for k in ("A", "O", "S")
                      if (r := rate(link, k)) is not None)
    return "\n".join([
        "  linhas válidas: %d (%s)" % (sum(link.counts.values()), kinds or "nenhuma"),
        "  taxas: %s" % (rates or "-"),
        "  lost (lacunas na seq): %d" % link.lost,
        "  corrupt (descartadas): %d" % link.corrupt,
        "  texto do boot ignorado: %d linhas; descartados ao abrir a porta: %d bytes"
        % (link.ignored, link.discarded_bytes),
        "  reinícios do ESP32: %d" % link.restarts,
        "  $H mandados: %d; $C mandados: %d" % (link.hours_sent, link.rates_sent),
        "  connected agora: %s" % reader.health()["connected"],
        "  telemetria no SQLite: %d linhas em %.0f s; com imu e obd: %d; obd com valores: %d"
        % (len(rows), elapsed, len(both), len(with_values)),
        "  maior age_ms: " + (", ".join("%s %d" % (f, ages[f]) for f in OBD_FIELDS if f in ages)
                              or "nenhum valor do OBD")
        + ("" if set(ages) >= set(OBD_FIELDS) else
           "  (faltam: %s)" % ", ".join(f for f in OBD_FIELDS if f not in ages)),
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", default=esp32_reader.PORT,
                        help="porta do ESP32 (padrão: VGUARD_ESP32 ou /dev/vguard-esp32)")
    parser.add_argument("--seconds", type=float, default=60, help="duração da captura")
    parser.add_argument("--obd-hz", default=esp32_reader.OBD_HZ,
                        help="taxa do OBD pedida pelo $C (padrão: VGUARD_OBD_HZ ou 12.5)")
    parser.add_argument("--no-clock", action="store_true",
                        help="finge a hora da Pi sem sincronizar: nenhum $H")
    parser.add_argument("--data-dir", help="pasta nova para o SQLite (padrão: uma temporária)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(relativeCreated)9.0f ms  %(levelname)-7s %(message)s")
    data_dir = args.data_dir or tempfile.mkdtemp(prefix="vguard-bench-")
    if os.path.exists(os.path.join(data_dir, "vguard.db")):
        parser.error("%s já tem um vguard.db: use uma pasta nova" % data_dir)

    manager = EventManager(data_dir)
    clock_ok = (lambda: False) if args.no_clock else esp32_reader.clock_synchronized
    reader = Esp32Reader(manager, args.port, clock_ok=clock_ok, obd_hz=args.obd_hz)
    logging.info("lendo %s por %g s; dados em %s", args.port, args.seconds, data_dir)
    start = time.monotonic()
    reader.start()
    try:
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
    elapsed = time.monotonic() - start

    print("\nResumo de %.1f s em %s:" % (elapsed, args.port))
    print(summary(reader, manager, elapsed))
    link = reader.link
    manager.close()
    return 1 if link.lost or link.corrupt or not link.counts else 0


if __name__ == "__main__":
    sys.exit(main())
