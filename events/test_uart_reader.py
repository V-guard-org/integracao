"""
Verificação do leitor da UART contra o ESP32 falso do firmware_telemetria,
num pty (só Linux ou macOS). Executar:

    python3 test_uart_reader.py

O ESP32 falso é procurado no repositório vizinho
(../firmware_telemetria/host) ou em VGUARD_FIRMWARE.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import uart_reader
from event_manager import EventManager
from uart_reader import UartReader, drop_stale

FIRMWARE = Path(os.environ.get(
    "VGUARD_FIRMWARE",
    Path(__file__).resolve().parents[2] / "firmware_telemetria" / "host"
))


def test_drop_stale():
    sample = {"rpm": 2100.0, "speed": 60, "coolant": 88,
              "age_ms": {"rpm": 120, "speed": 80, "coolant": 900}}
    assert drop_stale(sample) is sample

    # Só o PID velho sai.
    sample["age_ms"]["coolant"] = 5000
    assert drop_stale(sample) == {
        "rpm": 2100.0, "speed": 60, "age_ms": {"rpm": 120, "speed": 80}
    }

    # Tudo velho vira OBD parado; a amostra vazia passa como está.
    assert drop_stale({"rpm": 2100.0, "age_ms": {"rpm": 65535}}) == {}
    assert drop_stale({}) == {}


def test_with_fake_esp32():
    data_dir = tempfile.mkdtemp()
    port = os.path.join(data_dir, "esp32")
    manager = EventManager(data_dir)

    # O leitor sobe antes do ESP32: precisa insistir até a porta existir.
    uart_reader.REOPEN_S = 0.2
    reader = UartReader(manager, port)
    reader.start()
    time.sleep(0.5)

    # O carro para de responder 4 s depois de o ESP32 falso subir.
    fake = subprocess.Popen(
        [sys.executable, str(FIRMWARE / "fake_esp32.py"),
         "--path", port, "--car-off-after", "4"],
        stderr=subprocess.DEVNULL
    )
    try:
        # STATUS, 1º PONG e, ~1,2 s depois do 1º PING, o OBD.
        time.sleep(3.5)
        on = manager.record_event("fadiga", 0.9)

        # A taxa nova vai pela porta, no TAXAS (RF29).
        reader.decoder.set_rates(imu=50)

        # OBD segue 2 s com as idades crescendo, depois o {} (§3.4).
        time.sleep(5.0)
        off = manager.record_event("distracao", 0.9)
        stats = reader.decoder.stats()
    finally:
        reader.stop()
        fake.terminate()
        fake.wait()

    counters = stats["counters"]
    assert stats["synced"] and counters["boots"] == 1
    assert counters["corrupt"] == 0 and counters["lost"] == 0
    assert stats["esp32"]["imu_rate"] == 50

    # Telemetria consolidada chegou ao SD, uma linha por segundo.
    rows = manager.pending_telemetry(10**6)
    assert len(rows) >= 7 and all("imu" in r["data"] for r in rows[1:])

    manager.close()   # fecha as janelas, para os eventos entrarem na fila
    events = {e["id"]: e["data"] for e in EventManager(data_dir).pending_events()}

    # Carro ligado: o evento tem IMU e OBD completos, com a idade.
    assert {"ax", "az_peak"} <= set(events[on]["imu"])
    assert {"rpm", "speed", "age_ms"} <= set(events[on]["obd"])

    # Carro desligado: o OBD do evento fica só com o horário, sem valor velho.
    assert set(events[off]["obd"]) == {"ts"}

    print("OK")


if __name__ == "__main__":
    test_drop_stale()
    test_with_fake_esp32()
