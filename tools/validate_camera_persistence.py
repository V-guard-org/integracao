"""Teste opt-in: câmera real e evento SINTÉTICO identificado, sem UART/backend.

Usa vguard.run(), FaceLandmarker, detector temporal e EventManager reais.
Apenas as métricas entregues ao detector são substituídas em memória para
provocar um episódio determinístico; o JPEG vem do frame real do pipeline.
Não é uma avaliação da acurácia da detecção de fadiga.
"""

import argparse
from dataclasses import asdict, replace
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import sqlite3
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vguard
from detector import DriverStateDetector
from metrics import FrameMetrics
import cv2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True, help="Diretório novo exclusivo deste teste")
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--source", default="0", help="índice da webcam C525 (padrão: 0) ou picamera2 (CSI)")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--camera-num", type=int, default=0)
    parser.add_argument("--infrared", action="store_true")
    parser.add_argument("--camera-python")
    args = parser.parse_args()
    if args.frames < 60:
        parser.error("use pelo menos 60 frames para atravessar o limiar temporal")
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=False)
    manifest = {
        "synthetic": True,
        "description": "Métricas de bocejo injetadas no detector; JPEG da câmera real",
        "source": args.source,
        "infrared": args.infrared,
        "camera_num": args.camera_num,
        "requested_fps": args.fps,
        "camera_python": args.camera_python,
        "event_type": "teste_integracao_sintetico_fadiga",
        "backend_enabled": False,
        "uart_enabled": False,
    }
    (data / "test-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    synthetic = FrameMetrics(ear_left=0.3, ear_right=0.3, ear=0.3, mar=0.75, pitch=0, yaw=0, roll=0)
    original_update = DriverStateDetector.update
    frames = 0
    face_frames = 0

    def controlled_update(detector, real_metrics, now):
        nonlocal frames, face_frames
        frames += 1
        if real_metrics is not None:
            assert all(math.isfinite(value) for key, value in asdict(real_metrics).items()
                       if key != "eye_closure")
            face_frames += 1
        result = original_update(detector, synthetic, now)
        if result.should_record:
            result = replace(result, event_type=manifest["event_type"])
        return result

    started = time.perf_counter()
    with patch.dict(os.environ, {"VGUARD_DATA": str(data)}), \
         patch.object(DriverStateDetector, "update", controlled_update):
        camera_args = ["--headless", "--source", args.source, "--no-uart", "--no-gps", "--no-sync",
                       "--fps", str(args.fps), "--camera-num", str(args.camera_num),
                       "--max-frames", str(args.frames)]
        if args.infrared:
            camera_args.append("--infrared")
        if args.camera_python:
            camera_args.extend(["--camera-python", args.camera_python])
        count = vguard.run(camera_args)
    elapsed = time.perf_counter() - started
    assert frames == args.frames, (frames, args.frames)
    assert count == 1, count
    db = sqlite3.connect("file:" + str(data / "vguard.db") + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok", integrity
        events = [dict(row) for row in db.execute("SELECT * FROM events")]
        assert len(events) == 1, len(events)
        event = events[0]
        assert event["type"] == manifest["event_type"]
        assert event["meta_synced"] == event["photo_synced"] == 0
        assert event["window_closed"] == 1
        assert json.loads(event["data"]) == {}
    finally:
        db.close()
    photo = data / "photos" / event["photo"]
    jpeg = photo.read_bytes()
    assert jpeg.startswith(b"\xff\xd8") and jpeg.endswith(b"\xff\xd9")
    image = cv2.imread(str(photo))
    assert image is not None and image.shape == (480, 640, 3)
    assert image.std() > 1, "JPEG sem variação de pixels"
    report = {
        **manifest, "frames": frames, "frames_with_real_face_metrics": face_frames,
        "elapsed_seconds": elapsed, "effective_fps": frames / elapsed,
        "rss_peak_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
        "temperature_c": int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000,
        "event": event, "sqlite_integrity": integrity,
        "photo_path": str(photo), "photo_bytes": len(jpeg),
        "photo_sha256": hashlib.sha256(jpeg).hexdigest(), "photo_shape": list(image.shape),
    }
    (data / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
