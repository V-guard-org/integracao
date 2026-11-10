"""
V-GUARD — Gerenciador de eventos local-first, no modelo do contrato com o
servidor (ticket 2, Issue #14).

Tudo é gravado no cartão SD antes de qualquer tentativa de envio (RF14):

    dados/
    ├── vguard.db                       SQLite: a telemetria (1 linha por segundo) e os eventos
    └── photos/<ts em ms>-<tipo>.jpg    a foto de cada evento (RNF13)

Um evento é o que o servidor recebe no POST /api/events: o ts (ao
milissegundo), o tipo, o score, o retrato da telemetria naquele instante e a
foto, que vão juntos. A chave no servidor é (device_id, ts, type).

Quem escreve: a visão (record_event, pelo VisionProcess) e os leitores do
ESP32 e do GPS (update). Quem lê: o envio (pending_* e ack_*).

Um banco do modelo antigo (UUID e janela de 10 s, até 09/11) vai para o lado
na abertura, com as fotos, sem converter e sem apagar nada: vguard-antigo.db
e photos-antigo/.
"""

import json
import logging
import os
import shutil
import sqlite3
import threading
import time

log = logging.getLogger("events")


# ============================================================
# CONFIGURAÇÃO
# ============================================================

# Intervalo de consolidação da telemetria gravada no SD (RNF24).
TELEMETRY_INTERVAL = 1.0

# Tempo mínimo entre dois eventos do mesmo tipo. Sem isso, a visão
# geraria um evento por quadro enquanto a boca estivesse aberta.
EVENT_COOLDOWN = 5.0

# O retrato do evento deixa de fora a amostra de um sensor mais velha que
# isto, contando a partir do ts do evento.
SNAPSHOT_MAX_AGE = 3.0

# Ocupação do cartão SD (fração de 0 a 1) que dispara o alerta e o
# estado crítico (RNF22). A limpeza só atua a partir do alerta.
STORAGE_WARN = 0.80
STORAGE_CRITICAL = 0.95

# Estados de envio gravados nas colunas synced. "Enviando" não é gravado:
# se a energia cair no meio de um envio, o item continua PENDING.
PENDING = 0
CONFIRMED = 1   # o servidor confirmou o armazenamento
FAILED = 2      # o servidor recusou em definitivo; fica no SD, sai da fila

# O PRAGMA user_version do modelo novo. O modelo antigo ficava no 0.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id     INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,
    data   TEXT NOT NULL,
    synced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS telemetry_ts ON telemetry (ts);
CREATE INDEX IF NOT EXISTS telemetry_pending ON telemetry (id) WHERE synced = 0;

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,       -- ao milissegundo
    type       TEXT NOT NULL,
    score      REAL NOT NULL,
    telemetria TEXT NOT NULL,       -- o retrato, em JSON, com o nome do campo do contrato
    photo      TEXT NOT NULL,       -- o nome do arquivo em photos/
    synced     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (ts, type)
);
CREATE INDEX IF NOT EXISTS events_pending ON events (ts) WHERE synced = 0;
"""


# ============================================================
# BANCO DO MODELO ANTIGO
# ============================================================

def set_aside_old_model(data_dir):
    """
    Se o vguard.db da pasta é do modelo antigo (o user_version 0 com alguma
    tabela), ele e a photos/ vão para o lado, inteiros, sem converter.
    Devolve (banco, pasta das fotos ou None, eventos, fotos), ou None se não
    havia banco antigo.

    Na abertura, o EventManager apaga as fotos que não estão no banco: com um
    banco novo e vazio, apagaria todas as fotos antigas. Por isso as fotos
    saem primeiro. Se a energia cair entre os dois passos, o banco antigo
    continua no lugar e é reconhecido de novo na abertura seguinte.
    """
    path = os.path.join(data_dir, "vguard.db")
    if not os.path.exists(path):
        return None
    db = sqlite3.connect(path)
    try:
        if db.execute("PRAGMA user_version").fetchone()[0] != 0:
            return None
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not tables:
            return None     # um arquivo sem tabelas só recebe o esquema novo
        events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0] if "events" in tables else 0
        # Um corte de energia deixa transações confirmadas só no -wal: elas
        # passam para o banco antes de ele mudar de nome.
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()

    new_db, new_photos = _free_names(data_dir)
    photo_dir = os.path.join(data_dir, "photos")
    photos = 0
    if os.path.isdir(photo_dir):
        photos = sum(1 for name in os.listdir(photo_dir) if name.endswith(".jpg"))
        os.rename(photo_dir, new_photos)
    else:
        new_photos = None
    for suffix in ("-wal", "-shm", ""):     # o banco por último
        if os.path.exists(path + suffix):
            os.rename(path + suffix, new_db + suffix)
    _fsync_dir(data_dir)
    return new_db, new_photos, events, photos


def _free_names(data_dir):
    """vguard-antigo.db e photos-antigo/ ou, se já existirem, com a data e a hora: nunca sobrescreve."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffixes = ["-antigo", "-antigo-" + stamp] + ["-antigo-%s-%d" % (stamp, n) for n in range(2, 100)]
    for suffix in suffixes:
        db = os.path.join(data_dir, "vguard%s.db" % suffix)
        photos = os.path.join(data_dir, "photos" + suffix)
        if not any(os.path.exists(p) for p in (db, db + "-wal", db + "-shm", photos)):
            return db, photos
    raise RuntimeError("nenhum nome livre para o banco antigo em %s" % data_dir)


def plural(count, one, many):
    return "%d %s" % (count, one if count == 1 else many)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ============================================================
# GERENCIADOR
# ============================================================

class EventManager:

    def __init__(self, data_dir="dados"):
        self.data_dir = data_dir
        self.photo_dir = os.path.join(data_dir, "photos")
        os.makedirs(data_dir, exist_ok=True)

        # Antes de abrir o banco e de limpar as fotos órfãs.
        moved = set_aside_old_model(data_dir)
        if moved:
            old_db, old_photos, events, photos = moved
            where = os.path.basename(old_db)
            if old_photos:
                where += " e %s/" % os.path.basename(old_photos)
            log.warning("banco do modelo antigo movido para %s (%s, %s); nada foi apagado",
                        where, plural(events, "evento", "eventos"), plural(photos, "foto", "fotos"))
        os.makedirs(self.photo_dir, exist_ok=True)

        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            os.path.join(data_dir, "vguard.db"),
            check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row

        # WAL + FULL: uma queda de energia perde no máximo a transação
        # em andamento, sem corromper o banco (RNF06).
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)
        self._db.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)

        self._remove_orphans()

        self._latest = {}       # fonte -> última amostra, com o ts dela
        self._last_flush = 0.0
        self._last_event = {}   # tipo -> ts do último evento

    # --------------------------------------------------------
    # TELEMETRIA
    # --------------------------------------------------------

    def update(self, source, data, ts=None):
        """
        Recebe uma amostra de "gps", "imu" ou "obd" (dict). Um dict vazio é
        o sensor sem dado no momento: fica só o horário.

        Pode ser chamado na frequência do sensor: só uma linha
        consolidada por TELEMETRY_INTERVAL vai para o SD.
        """
        ts = time.time() if ts is None else ts

        with self._lock:
            self._latest[source] = {"ts": ts, **data}

            if ts - self._last_flush < TELEMETRY_INTERVAL:
                return

            # Só as fontes com leitura nova desde a última linha.
            fresh = {
                s: d for s, d in self._latest.items()
                if d["ts"] > self._last_flush
            }
            with self._db:
                self._db.execute(
                    "INSERT INTO telemetry (ts, data) VALUES (?, ?)",
                    (ts, json.dumps(fresh))
                )
            self._last_flush = ts

    # --------------------------------------------------------
    # EVENTOS
    # --------------------------------------------------------

    def record_event(self, type, score, jpeg, ts=None):
        """
        Registra um evento ("fadiga", "distracao") com o retrato da
        telemetria e a foto (os bytes do JPEG, obrigatória).

        O ts é arredondado ao milissegundo: a chave no servidor é
        (device_id, ts, type), e o reenvio tem de mandar o mesmo número.

        Devolve o id local (inteiro), ou None dentro do EVENT_COOLDOWN do
        último evento do mesmo tipo.
        """
        if not jpeg:
            raise ValueError("o evento precisa da foto (JPEG)")
        ts = round(time.time() if ts is None else ts, 3)

        with self._lock:
            if ts - self._last_event.get(type, float("-inf")) < EVENT_COOLDOWN:
                return None

            photo = "%d-%s.jpg" % (round(ts * 1000), type)
            if self._db.execute("SELECT 1 FROM events WHERE ts = ? AND type = ?", (ts, type)).fetchone() \
                    or os.path.exists(os.path.join(self.photo_dir, photo)):
                # Só com o relógio voltando para trás. O servidor também
                # ignoraria o repetido, e a foto já gravada não é sobrescrita.
                log.warning("evento %s em %.3f já gravado: o repetido não foi gravado", type, ts)
                return None

            # O retrato: a última amostra de cada sensor, menos as velhas.
            snapshot = {
                source: sample for source, sample in self._latest.items()
                if ts - sample["ts"] <= SNAPSHOT_MAX_AGE
            }

            # A foto vai para o disco ANTES da linha no banco: o banco
            # nunca aponta para um arquivo que não existe.
            self._write_photo(photo, jpeg)
            with self._db:
                cursor = self._db.execute(
                    "INSERT INTO events (ts, type, score, telemetria, photo)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (ts, type, float(score), json.dumps(snapshot), photo)
                )
            self._last_event[type] = ts
            return cursor.lastrowid

    def _write_photo(self, name, jpeg):
        path = os.path.join(self.photo_dir, name)
        tmp = path + ".tmp"

        with open(tmp, "wb") as f:
            f.write(jpeg)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, path)
        _fsync_dir(self.photo_dir)

    # --------------------------------------------------------
    # FILA DE ENVIO
    # --------------------------------------------------------

    # A fila É o banco: pendente = linha com synced = 0. Sobrevive a
    # reinicializações sem nenhum passo de recuperação (RNF25).
    # Chamar ack_* somente após a resposta do servidor (RF22).

    def pending_telemetry(self, limit=500):
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, data FROM telemetry WHERE synced = 0"
                " ORDER BY id LIMIT ?",
                (limit,)
            ).fetchall()

        return [
            {"id": r["id"], "ts": r["ts"], "data": json.loads(r["data"])}
            for r in rows
        ]

    def pending_events(self, limit=100):
        """
        Os eventos à espera do envio, do mais antigo ao mais novo: id, ts,
        type, score, telemetria (o retrato) e photo (o caminho do JPEG). O
        evento e a foto vão juntos, no mesmo POST.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, type, score, telemetria, photo FROM events"
                " WHERE synced = 0 ORDER BY ts, id LIMIT ?",
                (limit,)
            ).fetchall()

        return [
            {"id": r["id"], "ts": r["ts"], "type": r["type"], "score": r["score"],
             "telemetria": json.loads(r["telemetria"]),
             "photo": os.path.join(self.photo_dir, r["photo"])}
            for r in rows
        ]

    def ack_telemetry(self, ids, state=CONFIRMED):
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE telemetry SET synced = ? WHERE id = ?",
                [(state, i) for i in ids]
            )

    def ack_event(self, event_id, state=CONFIRMED):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE events SET synced = ? WHERE id = ?",
                (state, event_id)
            )

    def pending_counts(self):
        """
        Quantidade de registros pendentes, para a saúde do dispositivo (RF32).
        """
        with self._lock:
            row = self._db.execute(
                "SELECT"
                " (SELECT COUNT(*) FROM telemetry WHERE synced = 0),"
                " (SELECT COUNT(*) FROM events WHERE synced = 0)"
            ).fetchone()

        return {"telemetry": row[0], "events": row[1]}

    # --------------------------------------------------------
    # ARMAZENAMENTO
    # --------------------------------------------------------

    def storage_status(self):
        """
        Ocupação do cartão SD, para a saúde do dispositivo (RF32).
        """
        usage = shutil.disk_usage(self.data_dir)
        used = usage.used / usage.total

        if used >= STORAGE_CRITICAL:
            level = "critical"
        elif used >= STORAGE_WARN:
            level = "warning"
        else:
            level = "ok"

        return {"used": used, "free_bytes": usage.free, "level": level}

    def cleanup(self):
        """
        Abaixo de STORAGE_WARN não faz nada. A partir dele, apaga o que o
        servidor já confirmou; o pendente e o recusado nunca são apagados
        (RNF23).

        Chamar periodicamente (ex.: a cada minuto).
        """
        if self.storage_status()["level"] == "ok":
            return

        with self._lock:
            with self._db:
                self._db.execute("DELETE FROM telemetry WHERE synced = ?", (CONFIRMED,))
                self._db.execute("DELETE FROM events WHERE synced = ?", (CONFIRMED,))

            # As fotos desses eventos ficaram sem linha no banco.
            self._remove_orphans()

    def _remove_orphans(self):
        """
        Apaga arquivos em photos/ sem evento no banco: gravações
        interrompidas (.tmp), fotos cujo evento não chegou a ser gravado
        e fotos de eventos removidos pela limpeza.
        """
        known = {
            row[0] for row in
            self._db.execute("SELECT photo FROM events")
        }

        for name in os.listdir(self.photo_dir):
            if name not in known:
                os.remove(os.path.join(self.photo_dir, name))

    # --------------------------------------------------------

    def close(self):
        with self._lock:
            self._db.close()
