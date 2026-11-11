"""
Testes do gerenciador de eventos no modelo novo (ticket 2, Issue #14): a
telemetria, o evento com o retrato e a foto, a fila de envio, a queda de
energia, o corte abrupto e o banco do modelo antigo. Cada pasta temporária é
apagada no fim. A partir de events/:

    python3 test_event_manager.py

O corte de energia de verdade é puxar a fonte da Pi com o gravador rodando e
conferir depois:

    python3 test_event_manager.py writer <pasta>
    python3 test_event_manager.py check <pasta>
"""

import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import event_manager
from event_manager import FAILED, EventManager

JPEG = b"\xff\xd8\xff\xe0 foto de teste \xff\xd9"
BIG_JPEG = JPEG * 5000      # ~100 KB, o tamanho de uma foto de verdade

# O esquema do modelo antigo (até 09/11), para os testes do banco antigo.
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id     INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,
    data   TEXT NOT NULL,
    synced INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    ts           REAL NOT NULL,
    score        REAL,
    data         TEXT NOT NULL,
    photo        TEXT,
    meta_synced  INTEGER NOT NULL DEFAULT 0,
    photo_synced INTEGER NOT NULL DEFAULT 0,
    window_closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS event_window (
    event_id TEXT NOT NULL REFERENCES events (id),
    ts       REAL NOT NULL,
    source   TEXT NOT NULL,
    data     TEXT NOT NULL
);
"""


class Folder(unittest.TestCase):
    """Uma pasta de dados nova por teste, apagada no fim."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="vguard-teste-")
        self.addCleanup(directory.cleanup)
        self.data_dir = directory.name

    def open(self):
        manager = EventManager(self.data_dir)
        self.addCleanup(manager.close)
        return manager

    def names(self):
        return sorted(os.listdir(self.data_dir))


class TelemetryTest(Folder):

    def test_one_line_per_full_second(self):
        manager = self.open()
        seen = []
        manager.on_telemetry = seen.append
        for i in range(300):            # 10 Hz por 30 s, começando no meio de um segundo
            ts = 1000.55 + i / 10
            manager.update("imu", {"ax": i}, ts)
            manager.update("gps", {"lat": -25.4, "lon": -49.2}, ts)
        rows = manager.pending_telemetry()
        # Do segundo 1000 ao 1029; o 1030 ainda está em montagem.
        self.assertEqual([row["ts"] for row in rows], [1000.0 + n for n in range(30)])
        # A última leitura de [1001, 1002): a de 1001.95 (i = 14).
        self.assertEqual(rows[1]["data"]["imu"], {"ts": 1000.55 + 14 / 10, "ax": 14})
        self.assertEqual(set(rows[1]["data"]), {"imu", "gps"})
        self.assertEqual(seen, rows)            # um aviso por linha, depois de gravada
        manager.ack_telemetry([row["id"] for row in rows])
        self.assertEqual(manager.pending_counts(), {"telemetry": 0, "events": 0})

    def test_only_sensors_read_in_that_second(self):
        manager = self.open()
        manager.update("gps", {"lat": -25.4}, 1000.2)
        manager.update("imu", {"ax": 1}, 1001.3)
        manager.update("imu", {}, 1001.6)       # sensor sem dado: só o horário
        manager.update("imu", {"ax": 2}, 1003.1)    # o 1002 ficou vazio: sem linha
        manager.update("imu", {"ax": 3}, 1004.0)
        rows = manager.pending_telemetry()
        self.assertEqual([(row["ts"], row["data"]) for row in rows], [
            (1000.0, {"gps": {"ts": 1000.2, "lat": -25.4}}),
            (1001.0, {"imu": {"ts": 1001.6}}),
            (1003.0, {"imu": {"ts": 1003.1, "ax": 2}}),
        ])

    def test_late_reading_of_a_closed_second_only_goes_to_the_snapshot(self):
        manager = self.open()
        manager.update("imu", {"ax": 1}, 1001.1)
        manager.update("gps", {"lat": -25.4}, 1002.0)
        manager.update("obd", {"rpm": 800.0}, 1001.9)   # chegou depois do 1002
        manager.update("imu", {"ax": 2}, 1003.0)
        self.assertEqual([row["data"] for row in manager.pending_telemetry()],
                         [{"imu": {"ts": 1001.1, "ax": 1}}, {"gps": {"ts": 1002.0, "lat": -25.4}}])
        manager.record_event("fadiga", 0.9, JPEG, 1003.0)
        self.assertIn("obd", manager.pending_events()[0]["telemetria"])

    def test_close_writes_the_second_being_assembled(self):
        manager = self.open()
        manager.update("imu", {"ax": 1}, 1000.5)
        manager.close()
        manager = self.open()
        self.assertEqual([row["ts"] for row in manager.pending_telemetry()], [1000.0])

    def test_window_by_ts(self):
        manager = self.open()
        for second in range(1000, 1031):
            manager.update("imu", {"ax": second}, second + 0.5)
        window = manager.telemetry_window(1005.0, 1015.0)
        self.assertEqual([row["ts"] for row in window], [1005.0 + n for n in range(11)])
        self.assertEqual(window[0]["data"], {"imu": {"ts": 1005.5, "ax": 1005}})

    def test_a_failing_notice_does_not_stop_the_recording(self):
        manager = self.open()
        manager.on_telemetry = lambda row: 1 / 0
        with self.assertLogs("events", "ERROR"):
            manager.update("imu", {"ax": 1}, 1000.0)
            manager.update("imu", {"ax": 2}, 1001.0)
        self.assertEqual(len(manager.pending_telemetry()), 1)


class EventTest(Folder):

    def test_event_with_integer_id_ts_in_ms_snapshot_and_photo(self):
        manager = self.open()
        manager.update("imu", {"ax": 0.11}, 1000.0)
        event_id = manager.record_event("fadiga", 0.82, JPEG, 1000.4374)
        self.assertIsInstance(event_id, int)
        event, = manager.pending_events()
        self.assertEqual(event["id"], event_id)
        self.assertEqual(event["ts"], 1000.437)
        self.assertEqual((event["type"], event["score"]), ("fadiga", 0.82))
        self.assertEqual(event["telemetria"], {"imu": {"ts": 1000.0, "ax": 0.11}})
        self.assertEqual(os.path.basename(event["photo"]), "1000437-fadiga.jpg")
        with open(event["photo"], "rb") as f:
            self.assertEqual(f.read(), JPEG)
        self.assertEqual(manager.pending_counts(), {"telemetry": 0, "events": 1})   # a linha do 1000 ainda em montagem

    def test_cooldown_of_5_s_per_type(self):
        manager = self.open()
        self.assertIsNotNone(manager.record_event("fadiga", 0.9, JPEG, 1000.0))
        self.assertIsNone(manager.record_event("fadiga", 0.9, JPEG, 1004.9))
        self.assertIsNotNone(manager.record_event("distracao", 0.7, JPEG, 1004.9))
        self.assertIsNotNone(manager.record_event("fadiga", 0.9, JPEG, 1005.0))
        self.assertEqual(len(manager.pending_events()), 3)

    def test_jpeg_is_required(self):
        manager = self.open()
        for jpeg in (None, b""):
            with self.subTest(jpeg=jpeg), self.assertRaises(ValueError):
                manager.record_event("fadiga", 0.9, jpeg, 1000.0)
        self.assertEqual(manager.pending_events(), [])
        self.assertEqual(os.listdir(manager.photo_dir), [])

    def test_snapshot_leaves_out_samples_older_than_3_s(self):
        manager = self.open()
        manager.update("gps", {"lat": -25.4}, 996.9)    # 3,1 s antes: fica de fora
        manager.update("obd", {"rpm": 800.0}, 997.0)    # 3,0 s antes: entra
        manager.update("imu", {}, 999.5)                # sensor sem dado: só o horário
        manager.record_event("fadiga", 0.9, JPEG, 1000.0)
        self.assertEqual(manager.pending_events()[0]["telemetria"],
                         {"obd": {"ts": 997.0, "rpm": 800.0}, "imu": {"ts": 999.5}})

    def test_snapshot_without_sensors_is_empty(self):
        manager = self.open()
        manager.record_event("distracao", 0.7, JPEG, 1000.0)
        self.assertEqual(manager.pending_events()[0]["telemetria"], {})

    def test_repeated_ts_and_type_keeps_the_photo(self):
        # Só com o relógio voltando para trás, depois de reiniciar.
        manager = self.open()
        manager.record_event("fadiga", 0.9, JPEG, 1000.0)
        manager.close()
        manager = self.open()
        with self.assertLogs("events", "WARNING"):
            self.assertIsNone(manager.record_event("fadiga", 0.8, JPEG + b"2", 1000.0))
        event, = manager.pending_events()
        with open(event["photo"], "rb") as f:
            self.assertEqual(f.read(), JPEG)


class QueueTest(Folder):

    def test_order_ack_confirmed_and_failed(self):
        manager = self.open()
        first = manager.record_event("fadiga", 0.9, JPEG, 1000.0)
        second = manager.record_event("distracao", 0.7, JPEG, 1001.0)
        third = manager.record_event("fadiga", 0.8, JPEG, 1010.0)
        self.assertEqual([e["id"] for e in manager.pending_events()], [first, second, third])
        self.assertEqual([e["id"] for e in manager.pending_events(limit=2)], [first, second])
        manager.ack_event(first)                # o servidor confirmou
        manager.ack_event(second, FAILED)       # recusou: sai da fila e fica no cartão
        self.assertEqual([e["id"] for e in manager.pending_events()], [third])
        self.assertEqual(manager.pending_counts(), {"telemetry": 0, "events": 1})
        self.assertEqual(len(os.listdir(manager.photo_dir)), 3)

    def test_power_cut_keeps_the_queue_and_removes_leftovers(self):
        manager = self.open()
        manager.update("imu", {"ax": 1}, 1000.0)
        ids = [manager.record_event("fadiga", 0.9, JPEG, 1000.0),
               manager.record_event("distracao", 0.7, JPEG, 1000.5)]
        # Restos de um corte no meio da gravação de uma foto.
        for name in ("meia-foto.jpg.tmp", "sem-evento.jpg"):
            with open(os.path.join(manager.photo_dir, name), "wb") as f:
                f.write(JPEG)
        reopened = self.open()                  # sem close: como se a energia caísse
        self.assertEqual([e["id"] for e in reopened.pending_events()], ids)
        self.assertEqual(sorted(os.listdir(reopened.photo_dir)), ["1000000-fadiga.jpg", "1000500-distracao.jpg"])
        # A linha do segundo em montagem se perde no corte, como deve.
        self.assertEqual(reopened.pending_counts(), {"telemetry": 0, "events": 2})

    def test_cleanup_only_at_the_warning_and_only_what_was_confirmed(self):
        manager = self.open()
        manager.update("imu", {"ax": 1}, 1000.0)
        done = manager.record_event("fadiga", 0.9, JPEG, 1000.0)
        waiting = manager.record_event("distracao", 0.7, JPEG, 1000.5)
        manager.ack_event(done)
        manager.ack_telemetry([row["id"] for row in manager.pending_telemetry()])
        with patch.multiple(event_manager, STORAGE_WARN=1.01, STORAGE_CRITICAL=1.02):
            manager.cleanup()                   # abaixo do alerta: nada sai
        self.assertEqual(len(os.listdir(manager.photo_dir)), 2)
        with patch.object(event_manager, "STORAGE_WARN", 0.0):
            manager.cleanup()
        self.assertEqual(os.listdir(manager.photo_dir), ["1000500-distracao.jpg"])
        self.assertEqual([e["id"] for e in manager.pending_events()], [waiting])
        self.assertEqual(manager._db.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0], 0)


class OldModelTest(Folder):

    def make_old_model(self, events):
        """
        Um banco do modelo antigo, como a bancada deixava, com as fotos. A
        cópia é feita com a conexão aberta: é o retrato de um corte de
        energia, com as linhas confirmadas ainda só no -wal.
        """
        photo_dir = os.path.join(self.data_dir, "photos")
        os.makedirs(photo_dir, exist_ok=True)
        source = tempfile.TemporaryDirectory(prefix="vguard-antigo-")
        self.addCleanup(source.cleanup)
        db = sqlite3.connect(os.path.join(source.name, "vguard.db"))
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.executescript(OLD_SCHEMA)
        sizes = {}
        with db:
            for i in range(events):
                event_id = "8f1c2d3e-0000-4000-8000-%012d" % i
                db.execute("INSERT INTO events (id, type, ts, score, data, photo) VALUES (?, ?, ?, ?, ?, ?)",
                           (event_id, "fadiga", 1000.0 + 10 * i, 0.9, "{}", event_id + ".jpg"))
                db.execute("INSERT INTO event_window VALUES (?, ?, ?, ?)", (event_id, 1000.0, "imu", "{}"))
                content = JPEG * (i + 1)
                with open(os.path.join(photo_dir, event_id + ".jpg"), "wb") as f:
                    f.write(content)
                sizes[event_id + ".jpg"] = len(content)
        self.assertGreater(os.path.getsize(os.path.join(source.name, "vguard.db-wal")), 0)
        for name in ("vguard.db", "vguard.db-wal"):
            shutil.copy(os.path.join(source.name, name), os.path.join(self.data_dir, name))
        db.close()
        return sizes

    def old_counts(self, name):
        db = sqlite3.connect(os.path.join(self.data_dir, name))
        try:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            return [db.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] for table in ("events", "event_window")]
        finally:
            db.close()

    def test_old_model_goes_aside_whole(self):
        sizes = self.make_old_model(events=3)
        with open(os.path.join(self.data_dir, "photos", "meia.jpg.tmp"), "wb") as f:
            f.write(JPEG)                       # um resto de corte vai junto, sem ser apagado
        with self.assertLogs("events", "WARNING") as logs:
            manager = self.open()
        self.assertEqual(logs.output, ["WARNING:events:banco do modelo antigo movido para vguard-antigo.db e "
                                       "photos-antigo/ (3 eventos, 3 fotos); nada foi apagado"])
        old = os.path.join(self.data_dir, "photos-antigo")
        self.assertEqual({name: os.path.getsize(os.path.join(old, name))
                          for name in os.listdir(old) if name.endswith(".jpg")}, sizes)
        self.assertIn("meia.jpg.tmp", os.listdir(old))
        self.assertEqual(self.old_counts("vguard-antigo.db"), [3, 3])
        # O banco novo começa vazio, no modelo novo, e não vai para o lado de novo.
        self.assertEqual(manager.pending_events(), [])
        self.assertEqual(os.listdir(manager.photo_dir), [])
        self.assertEqual(manager._db.execute("PRAGMA user_version").fetchone()[0], 1)
        manager.close()
        self.open()
        self.assertEqual([name for name in self.names() if "antigo" in name], ["photos-antigo", "vguard-antigo.db"])

    def test_second_old_model_never_overwrites_the_first(self):
        self.make_old_model(events=1)
        with self.assertLogs("events", "WARNING") as logs:
            self.open().close()
        self.assertIn("(1 evento, 1 foto)", logs.output[0])
        first = os.path.join(self.data_dir, "vguard-antigo.db")
        size = os.path.getsize(first)
        # Outro banco antigo na mesma pasta (copiado de outra pasta, por exemplo).
        for name in ("vguard.db", "vguard.db-wal", "vguard.db-shm"):
            if os.path.exists(os.path.join(self.data_dir, name)):
                os.remove(os.path.join(self.data_dir, name))
        shutil.rmtree(os.path.join(self.data_dir, "photos"))
        self.make_old_model(events=2)
        with self.assertLogs("events", "WARNING") as logs:
            self.open()
        stamped = [name for name in self.names() if re.fullmatch(r"vguard-antigo-\d{8}-\d{6}\.db", name)]
        self.assertEqual(len(stamped), 1, self.names())
        self.assertIn(stamped[0].replace("vguard", "photos")[:-3], self.names())
        self.assertIn("(2 eventos, 2 fotos)", logs.output[0])
        self.assertEqual(os.path.getsize(first), size)
        self.assertEqual(self.old_counts("vguard-antigo.db"), [1, 1])
        self.assertEqual(self.old_counts(stamped[0]), [2, 2])

    def test_file_without_tables_only_gets_the_new_schema(self):
        open(os.path.join(self.data_dir, "vguard.db"), "wb").close()
        manager = self.open()
        self.assertFalse([name for name in self.names() if "antigo" in name])
        self.assertEqual(manager._db.execute("PRAGMA user_version").fetchone()[0], 1)


class AbruptCutTest(Folder):
    """
    O SIGKILL corta o processo, não a energia: valida a recuperação do banco
    e das fotos, mas não o fsync.
    """

    def test_sigkill_while_writing(self):
        for _ in range(8):
            process = subprocess.Popen([sys.executable, "-B", os.path.abspath(__file__), "writer", self.data_dir])
            time.sleep(random.uniform(0.5, 1.2))
            self.assertIsNone(process.poll(), "o gravador saiu sozinho")
            process.send_signal(signal.SIGKILL)
            process.wait()
            events = check(self.data_dir)
        self.assertGreater(events, 0)
        total = sum(os.path.getsize(os.path.join(root, name))
                    for root, _, names in os.walk(self.data_dir) for name in names)
        self.assertLess(total, 50 * 1024 * 1024)    # poucos MB, e não os ~3 GB de antes


def writer(data_dir):
    """Grava telemetria e eventos sem parar, até ser morto."""
    manager = EventManager(data_dir)
    # Continua de onde a rodada anterior parou: cada evento tem um ts novo.
    start = manager._db.execute("SELECT COALESCE(MAX(ts), 0) FROM telemetry").fetchone()[0]
    i = 0
    while True:
        i += 1
        ts = start + i * 0.1
        manager.update("imu", {"ax": i}, ts)
        if i % 50 == 0:
            manager.record_event("fadiga", 0.9, BIG_JPEG, ts)
        time.sleep(0.001)       # sem a pausa, o teste antigo gravava ~3 GB (08/11)


def check(data_dir):
    """Reabre a pasta e confere que tudo o que ficou está íntegro. Devolve o número de eventos."""
    manager = EventManager(data_dir)
    try:
        db = manager._db
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # Toda foto no disco pertence a um evento, e vice-versa, inteira.
        photos = [row[0] for row in db.execute("SELECT photo FROM events")]
        assert sorted(os.listdir(manager.photo_dir)) == sorted(photos)
        for name in photos:
            assert os.path.getsize(os.path.join(manager.photo_dir, name)) == len(BIG_JPEG)
        # A fila volta inteira: todo evento gravado está pendente.
        assert manager.pending_counts()["events"] == len(photos)
        assert len(manager.pending_events(limit=10**6)) == len(photos)
        return len(photos)
    finally:
        manager.close()


if __name__ == "__main__":
    if sys.argv[1:2] == ["writer"]:
        writer(sys.argv[2])
    elif sys.argv[1:2] == ["check"]:
        print("OK: %d eventos íntegros" % check(sys.argv[2]))
    else:
        unittest.main()
