"""Decoder for the messages the V-GUARD ESP32 sends to the Raspberry Pi.

One file, Python standard library only (3.10+). The protocol is in
docs/PROTOCOLO.md and this API in docs/DECODIFICADOR.md.
"""

import binascii
import collections
import copy
import logging
import struct
import threading
import time

VERSION = "1.0.0"
PROTOCOL_VERSIONS = (1,)            # the versions in the 1st byte of the STATUS this file reads

# The time sync (DECODIFICADOR.md §3.8). They change only after a measurement (TESTES.md §7.3).
PING_INTERVAL_S = 1.0
MAX_RTT_S = 0.020           # from D12: a pair is off by at most half its round trip
BUCKET_S = 10               # of each 10 s, only the fastest pair is kept
BUCKETS = 12                # pairs in the line: the last 2 min
MIN_SPAN_S = 30             # pairs closer than this give only the offset
LINK_TIMEOUT_S = 3.0
PONG_WINDOW_S = 3.0         # a PONG counts only for a PING sent within this

STATUS = 0x01
IMU = 0x02
OBD = 0x03
PONG = 0x04
LOG = 0x05
PING = 0x81
TAXAS = 0x82

_HEADER = struct.Struct("<BHH")     # type, boot, seq
_CRC_SIZE = 2
_MAX_MESSAGE = 255                  # header, body and CRC, before COBS
_MAX_BUFFER = 258                   # 255 bytes become at most 257 after COBS, plus the 0x00

# STATUS up to heap_min; the firmware text takes the rest (PROTOCOLO.md §3.1)
_STATUS = struct.Struct("<BQBBBBBBB12s9II")

_LOG = struct.Struct("<QB")        # t_us, level; the UTF-8 text takes the rest (§3.5)
_LOG_ERROR = 1

_IMU = struct.Struct("<Q12h")      # t_us, then mean and peak of ax ay az gx gy gz (§3.2)
_OBD = struct.Struct("<QB")        # t_us, n; the n values follow (§3.3)
_OBD_VALUE = struct.Struct("<BHB")  # pid, age, size; the raw bytes follow
_PONG = struct.Struct("<HQ")       # seq_ping, t_us (§3.4)
_TAXAS = struct.Struct("<BB")      # imu, obd (§4.2)

_MIN_BODY = {STATUS: _STATUS.size, IMU: _IMU.size, OBD: _OBD.size, PONG: _PONG.size,
             LOG: _LOG.size}

# The rates the ESP32 accepts, in samples/s, and the ones it starts with (PROTOCOLO.md §4.2)
_IMU_RATES = (100, 50, 25, 20, 10)
_OBD_RATES = (1, 2, 5, 10)
_DEFAULT_RATES = {"imu": 25, "obd": 5}

_IMU_KEYS = ("ax", "ay", "az", "gx", "gy", "gz")
_IMU_DIVISORS = (100, 100, 100, 10, 10, 10)    # 0.01 m/s² and 0.1 °/s on the wire

# PID: the name in the sample, the raw bytes the formula uses, and the formula (DECODIFICADOR.md §3.3).
# A new PID is one more line here, and a new VERSION.
_OBD_FORMULAS = {
    0x04: ("load", 1, lambda raw: round(raw[0] * 100 / 255, 1)),
    0x05: ("coolant", 1, lambda raw: raw[0] - 40),
    0x0B: ("map", 1, lambda raw: raw[0]),
    0x0C: ("rpm", 2, lambda raw: (256 * raw[0] + raw[1]) / 4),
    0x0D: ("speed", 1, lambda raw: raw[0]),
    0x0F: ("iat", 1, lambda raw: raw[0] - 40),
    0x11: ("throttle", 1, lambda raw: round(raw[0] * 100 / 255, 1)),
    0x42: ("voltage", 2, lambda raw: (256 * raw[0] + raw[1]) / 1000),
}

_esp32_logger = logging.getLogger("esp32")
_log = logging.getLogger(__name__)

_PLATFORMS = ("notebook", "esp32")
_RESET_REASONS = ("unknown", "power_on", "external_pin", "software", "panic", "watchdog", "brownout")
_IMU_STATES = ("ok", "failed")
_OBD_STATES = ("waiting_pi", "listening", "discovering", "reading", "no_response",
               "wrong_bitrate", "line_stuck", "can_failure", "locked")
_ESP32_COUNTERS = ("dropped", "rejected", "imu_failures", "obd_sent", "obd_answered",
                   "obd_timeouts", "blocked", "can_errors", "bus_off")


def crc16(data):
    """CRC-16/CCITT-FALSE: polynomial 0x1021, start 0xFFFF, no reflection, no final XOR."""
    return binascii.crc_hqx(data, 0xFFFF)


def cobs_encode(data):
    out = bytearray()
    block = bytearray()
    for byte in data:
        if byte == 0:
            out.append(len(block) + 1)
            out += block
            block.clear()
        else:
            block.append(byte)
            if len(block) == 254:
                out.append(255)
                out += block
                block.clear()
    out.append(len(block) + 1)
    out += block
    return bytes(out)


def cobs_decode(data):
    """The inverse of cobs_encode, without the final 0x00. ValueError if the bytes are not COBS."""
    out = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        end = i + code
        if code == 0 or end > len(data):
            raise ValueError("invalid COBS")
        out += data[i + 1:end]
        i = end
        if code < 255 and i < len(data):
            out.append(0)
    return bytes(out)


def encode_message(msg_type, boot, seq, body=b""):
    """The bytes on the wire: COBS(header | body | CRC16) followed by 0x00."""
    data = _HEADER.pack(msg_type, boot, seq) + body
    return cobs_encode(data + struct.pack("<H", crc16(data))) + b"\x00"


class Decoder:
    """Turns the ESP32's bytes into samples and prepares the bytes the Pi sends.

    It never opens the port: the reader passes in what it read and writes out
    what data_to_send() returns (DECODIFICADOR.md §2.2).
    """

    def __init__(self, clock=time):
        self._clock = clock
        # stats() and set_rates() may come from any thread. Reentrant, in case a logging
        # handler calls stats() while feed() logs.
        self._lock = threading.RLock()
        self._seq = 0                   # of the messages the Pi sends
        self._next_ping_at = None
        self._ping_now = False          # a new boot: one more PING, off the 1 s schedule
        self._requested = dict(_DEFAULT_RATES)
        self._rates_pending = False     # a STATUS showed other rates than the requested ones
        self._buffer = b""
        self._skipping = False          # dropped an overlong buffer; wait for the next 0x00
        self._counters = {"lost": 0, "corrupt": 0, "unsynced": 0, "unknown": 0, "skipped_values": 0,
                          "slow_pongs": 0, "imu": 0, "obd": 0, "empty": 0, "boots": 0}
        self._boot = None
        self._incompatible = False      # this boot's STATUS has a protocol version we do not know
        self._last_seq = None
        self._last_valid_at = None
        self._esp32 = None
        self._pings = {}                # seq → instant each PING went out
        self._sync = _Sync()
        self._live = {"imu": False, "obd": False}   # the last sample delivered had values

    def feed(self, data):
        """Bytes read from the port. Returns the samples ready for update()."""
        with self._lock:
            now = self._clock.monotonic()
            # Read at each delivery, so a step of chrony counts at once.
            wall_offset = self._clock.time() - now
            *frames, rest = (self._buffer + data).split(b"\x00")
            if self._skipping:
                if frames:
                    frames.pop(0)
                    self._skipping = False
                else:
                    rest = b""
            if len(rest) > _MAX_BUFFER:
                self._counters["corrupt"] += 1
                self._skipping = True
                rest = b""
            self._buffer = rest
            samples = []
            if self._last_valid_at is not None and now - self._last_valid_at >= LINK_TIMEOUT_S:
                # The ESP32 went silent (DECODIFICADOR.md §3.4): no t_us to convert, so the Pi's time now.
                samples += [(source, now + wall_offset, {}) for source in self._newly_stopped(("imu", "obd"))]
            for frame in frames:
                if frame:                   # back-to-back zeros carry no message
                    for source, t_us, values in self._handle_frame(frame, now):
                        samples.append((source, self._sync.to_monotonic(t_us) + wall_offset, values))
            if self._incompatible:
                # A firmware we cannot read: for the Pi, both sources stopped, at its time now.
                samples += [(source, now + wall_offset, {}) for source in self._newly_stopped(("imu", "obd"))]
            return samples

    def data_to_send(self):
        """Everything pending for the ESP32, to be written right away."""
        with self._lock:
            now = self._clock.monotonic()
            out = bytearray()
            due = self._next_ping_at is None or now >= self._next_ping_at
            if due or self._ping_now:
                self._pings = {seq: at for seq, at in self._pings.items() if now - at < PONG_WINDOW_S}
                self._pings[self._seq] = now
                out += self._message(PING)
                self._ping_now = False
            if due:
                self._next_ping_at = (self._next_ping_at or now) + PING_INTERVAL_S
                if self._next_ping_at <= now:   # the reader stalled; no burst to catch up
                    self._next_ping_at = now + PING_INTERVAL_S
            if self._rates_pending:
                out += self._message(TAXAS, _TAXAS.pack(self._requested["imu"], self._requested["obd"]))
                self._rates_pending = False
            return bytes(out)

    def set_rates(self, imu=None, obd=None):
        """The rates the ESP32 should send at, in samples/s; None keeps that one as it is."""
        for source, value, accepted in (("imu", imu, _IMU_RATES), ("obd", obd, _OBD_RATES)):
            if value is not None and not (type(value) is int and value in accepted):     # no True, no 25.0
                raise ValueError(f"the {source} rate must be one of {accepted}, not {value!r}")
        with self._lock:
            self._requested = {"imu": self._requested["imu"] if imu is None else imu,
                               "obd": self._requested["obd"] if obd is None else obd}

    def stats(self):
        """The link state, the decoder's counters and the ESP32's last STATUS."""
        with self._lock:
            now = self._clock.monotonic()
            return {
                "connected": self._last_valid_at is not None
                             and now - self._last_valid_at < LINK_TIMEOUT_S,
                "synced": self._synced(),
                "incompatible": self._incompatible,
                "boot": self._boot,
                "requested_rates": dict(self._requested),
                "sync": self._sync.health(now),
                "counters": dict(self._counters),
                "esp32": copy.deepcopy(self._esp32),
            }

    def _handle_frame(self, frame, now):
        """The samples in one frame, as (source, t_us, values)."""
        try:
            data = cobs_decode(frame)
        except ValueError:
            data = b""
        payload, crc = data[:-_CRC_SIZE], data[-_CRC_SIZE:]
        if (len(payload) < _HEADER.size or len(data) > _MAX_MESSAGE
                or int.from_bytes(crc, "little") != crc16(payload)):
            self._counters["corrupt"] += 1
            return []
        msg_type, boot, seq = _HEADER.unpack_from(payload)
        body = payload[_HEADER.size:]
        silent = self._last_valid_at is None or now - self._last_valid_at >= LINK_TIMEOUT_S
        step = None if self._last_seq is None else (seq - self._last_seq) & 0xFFFF
        if boot != self._boot:
            self._boot = boot
            self._counters["boots"] += 1
            self._esp32 = None
            self._incompatible = False
            self._sync = _Sync()        # the ESP32's clock started again from zero
            self._ping_now = True       # so its 1st pair comes in milliseconds (PROTOCOLO.md §6.2)
        elif not silent and 0 < step < 0x8000:
            # A repeated or earlier seq is a restart, not 65535 lost messages.
            self._counters["lost"] += step - 1
        self._last_seq = seq
        self._last_valid_at = now

        if msg_type == STATUS and body and body[0] not in PROTOCOL_VERSIONS:
            if not self._incompatible:
                self._incompatible = True
                _log.error("the ESP32 (boot 0x%04X) speaks protocol version %d, and esp32_decoder %s "
                           "knows %s: no samples from it until this file is updated",
                           boot, body[0], VERSION, PROTOCOL_VERSIONS)
        elif self._incompatible and msg_type in (IMU, OBD):
            # Their layout may have changed with the version: counted, not read.
            self._counters["unsynced"] += 1
        elif msg_type in _MIN_BODY and len(body) < _MIN_BODY[msg_type]:
            # The CRC matched, so the header counted above; only the body is unreadable.
            self._counters["corrupt"] += 1
        elif msg_type == STATUS:
            return self._handle_status(body)
        elif msg_type == LOG:
            _t_us, level = _LOG.unpack_from(body)
            _esp32_logger.log(logging.ERROR if level == _LOG_ERROR else logging.WARNING,
                              "%s", body[_LOG.size:].decode("utf-8", "replace"))
        elif msg_type == PONG:
            seq_ping, t_us = _PONG.unpack_from(body)
            sent_at = self._pings.pop(seq_ping, None)
            if sent_at is None or now - sent_at >= PONG_WINDOW_S:
                pass                    # no PING of ours, or too old to tell the time
            elif now - sent_at > MAX_RTT_S:
                self._counters["slow_pongs"] += 1
            else:
                self._sync.add(sent_at, now, t_us)
        elif msg_type == IMU:
            t_us, *values = _IMU.unpack_from(body)
            return self._deliver("imu", t_us, _imu_sample(values))
        elif msg_type == OBD:
            parsed = _parse_obd(body)
            if parsed is None:
                self._counters["corrupt"] += 1
            else:
                t_us, values, skipped = parsed
                self._counters["skipped_values"] += skipped
                if values["age_ms"]:    # some value left; {} would mean it stopped (DECODIFICADOR.md §3.4)
                    return self._deliver("obd", t_us, values)
        else:
            self._counters["unknown"] += 1
        return []

    def _handle_status(self, body):
        """Shows the STATUS in stats(), asks for the rates again, and stops the sources it shows stopped."""
        self._esp32 = _parse_status(body)
        # No ACK: the STATUS confirms, and a TAXAS goes until it shows the rates (PROTOCOLO.md §6.4).
        in_use = (self._esp32["imu_rate"], self._esp32["obd_rate"])
        self._rates_pending = in_use != (self._requested["imu"], self._requested["obd"])
        if not self._synced():
            return []
        # The 1st synced STATUS that shows a source stopped gives its {} (DECODIFICADOR.md §3.4).
        stopped = [source for source, stop in (("imu", self._esp32["imu_state"] == "failed"),
                                                ("obd", self._esp32["obd_state"] != "reading"))
                   if stop]
        t_us = _STATUS.unpack_from(body)[1]
        return [(source, t_us, {}) for source in self._newly_stopped(stopped)]

    def _deliver(self, source, t_us, values):
        """The sample, if this boot has a STATUS and a pair to convert its t_us (PROTOCOLO.md §6.2)."""
        if not self._synced():
            self._counters["unsynced"] += 1
            return []
        self._counters[source] += 1
        self._live[source] = True
        return [(source, t_us, values)]

    def _newly_stopped(self, sources):
        """Those that get their {} now: once per stop, so only if their last sample had values."""
        stopped = [source for source in sources if self._live[source]]
        for source in stopped:
            self._live[source] = False
            self._counters["empty"] += 1
        return stopped

    def _synced(self):
        return self._esp32 is not None and self._sync.ready

    def _message(self, msg_type, body=b""):
        data = encode_message(msg_type, 0, self._seq, body)
        self._seq = (self._seq + 1) & 0xFFFF
        return data


_Pair = collections.namedtuple("_Pair", "rtt t_us mid")    # mid: the Pi's instant halfway through


class _Sync:
    """Turns the ESP32's t_us into the Pi's monotonic clock, from the PING/PONG pairs (§3.8)."""

    def __init__(self):
        self._best = {}                 # each 10 s of the ESP32's clock → its fastest pair
        self._last = None               # the last pair accepted, and when its PONG arrived
        self._last_arrival = None
        self._origin_us = None          # the line: the Pi's instant origin_at at origin_us,
        self._origin_at = None          # then slope seconds per µs
        self._slope = None
        self._drift_ppm = None

    @property
    def ready(self):
        return self._slope is not None

    def add(self, sent_at, arrived_at, t_us):
        pair = _Pair(arrived_at - sent_at, t_us, (sent_at + arrived_at) / 2)
        self._last, self._last_arrival = pair, arrived_at
        bucket = t_us // (BUCKET_S * 1_000_000)
        if bucket not in self._best or pair.rtt < self._best[bucket].rtt:
            self._best[bucket] = pair
            if len(self._best) > BUCKETS:
                del self._best[min(self._best)]
            self._fit()

    def _fit(self):
        oldest, newest = self._best[min(self._best)], self._best[max(self._best)]
        if newest.t_us - oldest.t_us < MIN_SPAN_S * 1_000_000:
            # 1 ms of noise over 10 s would tilt the line by 100 ppm, more than a crystal drifts.
            self._origin_us, self._origin_at, self._slope = newest.t_us, newest.mid, 1e-6
            self._drift_ppm = None
            return
        # Least squares, around the mean so that hours of t_us keep their precision.
        pairs = self._best.values()
        mean_us = sum(pair.t_us for pair in pairs) / len(pairs)
        mean_at = sum(pair.mid for pair in pairs) / len(pairs)
        self._slope = (sum((pair.t_us - mean_us) * (pair.mid - mean_at) for pair in pairs)
                       / sum((pair.t_us - mean_us) ** 2 for pair in pairs))
        self._origin_us, self._origin_at = mean_us, mean_at
        self._drift_ppm = (1e-6 / self._slope - 1) * 1e6   # positive: the ESP32's clock runs fast

    def to_monotonic(self, t_us):
        return self._origin_at + (t_us - self._origin_us) * self._slope

    def health(self, now):
        last = self._last
        return {
            "pairs": len(self._best),
            "rtt_ms": None if last is None else round(last.rtt * 1000, 2),
            "age_s": None if last is None else round(now - self._last_arrival, 1),
            "drift_ppm": None if self._drift_ppm is None else round(self._drift_ppm, 1),
        }


def _imu_sample(values):
    keys = _IMU_KEYS + tuple(key + "_peak" for key in _IMU_KEYS)
    return {key: value / divisor for key, value, divisor in zip(keys, values, _IMU_DIVISORS * 2)}


def _parse_obd(body):
    """(t_us, sample, values skipped), or None if the values run past the end of the body."""
    t_us, count = _OBD.unpack_from(body)
    sample, ages = {}, {}
    skipped = 0
    offset = _OBD.size
    for _ in range(count):
        if offset + _OBD_VALUE.size > len(body):
            return None
        pid, age, size = _OBD_VALUE.unpack_from(body, offset)
        offset += _OBD_VALUE.size
        if offset + size > len(body):
            return None
        name, used, formula = _OBD_FORMULAS.get(pid, (None, None, None))
        if formula is None or size < used:
            skipped += 1
        else:
            sample[name] = formula(body[offset:offset + size])
            ages[name] = age
        offset += size
    sample["age_ms"] = ages
    return t_us, sample, skipped


def _parse_status(body):
    (protocol, t_us, platform, sources, reset, imu_rate, obd_rate, imu_state, obd_state,
     pids, *rest) = _STATUS.unpack_from(body)
    counters, heap_min = rest[:-1], rest[-1]
    return {
        "protocol": protocol,
        "uptime_s": t_us / 1e6,
        "platform": _code_name(_PLATFORMS, platform),
        "imu_simulated": bool(sources & 0x01),
        "reset_reason": _code_name(_RESET_REASONS, reset),
        "imu_rate": imu_rate,
        "obd_rate": obd_rate,
        "imu_state": _code_name(_IMU_STATES, imu_state),
        "obd_state": _code_name(_OBD_STATES, obd_state),
        "supported_pids": [pid for pid in range(0x01, 0x61)
                           if pids[(pid - 1) // 8] & (0x80 >> ((pid - 1) % 8))],
        "counters": dict(zip(_ESP32_COUNTERS, counters)),
        "heap_min": heap_min,
        "firmware": body[_STATUS.size:].decode("ascii", "replace"),
    }


def _code_name(names, code):
    return names[code] if code < len(names) else "unknown"
