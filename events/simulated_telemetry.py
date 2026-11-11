"""Telemetria fictícia para demonstrar a visão sem os sensores do veículo."""

import math
import threading
import time


class SimulatedTelemetry:
    def __init__(self, manager):
        self.manager = manager
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        started_at = time.monotonic()
        self._publish(0.0)
        self._thread = threading.Thread(target=self._run, args=(started_at,), name="simulated-telemetry", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join()

    def _run(self, started_at):
        while not self._stop.wait(0.2):
            self._publish(time.monotonic() - started_at)

    def _publish(self, elapsed):
        # Trajeto em linha reta com velocidade oscilando entre 30 e 50 km/h.
        frequency = math.tau / 30
        phase = frequency * elapsed
        speed = 40 + 10 * math.sin(phase)
        distance = (40 * elapsed + 10 * (1 - math.cos(phase)) / frequency) / 3.6
        imu = {"ax": round(10 * frequency * math.cos(phase) / 3.6, 2), "ay": 0.0, "az": 9.81}
        obd = {"speed": round(speed), "rpm": round(800 + 30 * speed),
               "load": round(20 + speed / 4, 1), "coolant": 88, "map": 45,
               "iat": 30, "throttle": 20.0, "voltage": 13.9}
        obd["age_ms"] = dict.fromkeys(obd, 0)
        gps = {"lat": -23.55, "lon": -46.65 + distance / 102_000, "speed": obd["speed"]}
        ts = time.time()
        for source, sample in (("imu", imu), ("obd", obd), ("gps", gps)):
            self.manager.update(source, sample, ts)
