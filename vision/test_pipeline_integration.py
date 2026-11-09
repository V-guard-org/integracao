"""
Teste de integracao do pipeline de visao computacional (main.py) com o Mock do EventManager:
1. Validacao da inicializacao do MediaPipe e execucao do VisionPipeline em modo headless.
2. Validacao do relogio monotonico e timestamps estritamente crescentes.
3. Validacao do disparo sob demanda e codificacao JPEG em memoria do frame original.
4. Validacao da assinatura e integridade dos dados enviados para event_manager.record_event(type, score, jpeg, ts).
"""

from dataclasses import replace
import sys
import time
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List
from types import SimpleNamespace
from unittest.mock import Mock, patch
import cv2
import numpy as np

# Garante importacao local
sys.path.insert(0, str(Path(__file__).resolve().parent))

from detector import DetectionResult
from main import VisionPipeline
from calibration import CalibrationProfile
from metrics import FrameMetrics


class MockEventManager:
    """Mock que replica fielmente o contrato de record_event do EventManager do V-GUARD."""

    def __init__(self):
        self.recorded_events: List[Dict[str, Any]] = []

    def record_event(self, type: str, score: float, jpeg: bytes, ts: float) -> str:
        """
        Assinatura esperada pelo repositório gerenciador_eventos:
        manager.record_event(type, score, jpeg, ts) -> event_id
        """
        assert isinstance(type, str), f"type deve ser str, obteve {type}"
        assert type in ("fadiga", "distracao"), f"type invalido: {type}"
        assert isinstance(score, float), f"score deve ser float, obteve {score}"
        assert 0.0 <= score <= 1.0, f"score fora dos limites [0, 1]: {score}"
        assert isinstance(jpeg, bytes), f"jpeg deve ser bytes, obteve {type(jpeg)}"
        assert len(jpeg) > 0, "jpeg bytes nao pode ser vazio"
        assert isinstance(ts, float), f"ts deve ser float, obteve {ts}"

        event_id = f"evt_{len(self.recorded_events) + 1:04d}"
        self.recorded_events.append(
            {
                "id": event_id,
                "type": type,
                "score": score,
                "jpeg": jpeg,
                "ts": ts,
            }
        )
        return event_id


def test_jpeg_encoding_and_contract():
    """Valida a codificacao JPEG sob demanda e a conformidade dos bytes da imagem."""
    print("\n[1] Validando codificacao JPEG em memoria e chamada do EventManager...")
    mock_mgr = MockEventManager()

    # Cria um frame sintético puro 640x480 (sem overlays)
    raw_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.circle(raw_frame, (320, 240), 80, (0, 255, 0), -1)

    # Simula o bloco de codificacao do VisionPipeline
    success, buffer = cv2.imencode(".jpg", raw_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    assert success, "Falha na codificacao JPEG"
    jpeg_bytes = buffer.tobytes()

    # Verifica os bytes magicos do JPEG (\xff\xd8\xff)
    assert jpeg_bytes[:2] == b"\xff\xd8", "Formato de bytes nao e JPEG valido"

    # Testa reconstrucao da imagem a partir dos bytes
    decoded = cv2.imdecode(np.frombuffer(jpeg_bytes, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == (480, 640, 3), f"Formato decodificado incorreto: {decoded.shape}"

    # Dispara chamada ao EventManager
    event_id = mock_mgr.record_event(
        type="fadiga",
        score=0.85,
        jpeg=jpeg_bytes,
        ts=time.time(),
    )

    assert event_id == "evt_0001"
    assert len(mock_mgr.recorded_events) == 1
    ev = mock_mgr.recorded_events[0]
    assert ev["type"] == "fadiga"
    assert ev["score"] == 0.85
    assert len(ev["jpeg"]) == len(jpeg_bytes)
    print(f"    -> OK: Evento registrado com sucesso! (ID={event_id}, Imagem={len(jpeg_bytes)}B)")


def test_vision_pipeline_end_to_end():
    """Cria um arquivo de video sintetico e executa o VisionPipeline completo em headless."""
    print("\n[2] Validando execucao ponta-a-ponta do VisionPipeline (modo headless)...")
    mock_mgr = MockEventManager()

    # Gera arquivo de video temporario de teste com 10 quadros
    temp_video_path = Path(__file__).resolve().parent / "temp_pipeline_test.avi"
    fourcc = cv2.VideoWriter_fourcc(*"MJPG")
    writer = cv2.VideoWriter(str(temp_video_path), fourcc, 15.0, (640, 480))

    try:
        for i in range(10):
            frame = np.full((480, 640, 3), 50 + i * 15, dtype=np.uint8)
            cv2.putText(frame, f"Frame {i}", (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            writer.write(frame)
        writer.release()

        # Instancia o pipeline com o mock injetado
        pipeline = VisionPipeline(
            event_manager=mock_mgr,
            headless=True,
            source=str(temp_video_path),
            frame_width=640,
            frame_height=480,
            max_frames=10,
        )

        # Força o detector a simular should_record=True no frame 5 para validar o disparo end-to-end
        original_update = pipeline.detector.update
        call_count = [0]

        def mock_detector_update(metrics, now):
            call_count[0] += 1
            if call_count[0] == 5:
                return DetectionResult(
                    driver_state="FADIGA",
                    should_record=True,
                    event_type="fadiga",
                    score=0.88,
                    eyes_closed_duration=1.2,
                    yawn_duration=0.0,
                    distraction_duration=0.0,
                )
            return original_update(metrics, now)

        pipeline.detector.update = mock_detector_update

        # Executa o pipeline
        recorded_events = pipeline.run()

        # Valida que o pipeline processou e enviou o evento para o EventManager
        assert recorded_events == 1, f"Deveria ter registrado 1 evento, obteve {recorded_events}"
        assert len(mock_mgr.recorded_events) == 1, "MockEventManager deveria conter 1 evento"

        evt = mock_mgr.recorded_events[0]
        assert evt["type"] == "fadiga"
        assert evt["score"] == 0.88
        assert len(evt["jpeg"]) > 1000, "JPEG deve conter dados de imagem reais"
        assert evt["jpeg"][:2] == b"\xff\xd8", "Cabecalho JPEG invalido"
        assert pipeline.summary().endswith("; eventos: fadiga 1"), pipeline.summary()

        print("    -> OK: VisionPipeline executou os 10 frames e despachou o evento ao MockEventManager!")

    finally:
        # Limpeza do arquivo de teste temporario
        if temp_video_path.exists():
            temp_video_path.unlink()


def test_csi_frame_to_event():
    """CSI substituta, MediaPipe real e JPEG: protege ordem BGR e limpeza."""
    print("\n[3] Validando captura CSI integrada e cores do JPEG sem HUD...")
    manager = MockEventManager()
    camera = Mock()
    # RGB888 da Picamera2 entrega um array BGR ao OpenCV.
    raw = np.full((480, 640, 3), (25, 90, 210), dtype=np.uint8)
    camera.capture_array.side_effect = lambda stream: raw.copy()
    factory = Mock(return_value=camera)
    with patch.dict(sys.modules, {"picamera2": SimpleNamespace(Picamera2=factory)}):
        pipeline = VisionPipeline(event_manager=manager, headless=True,
                                  source="picamera2", max_frames=2)
        pipeline.detector.update = Mock(side_effect=[
            DetectionResult(driver_state="FADIGA", should_record=True,
                            event_type="fadiga", score=0.88,
                            eyes_closed_duration=1.2, yawn_duration=0.0,
                            distraction_duration=0.0),
            DetectionResult(driver_state="NORMAL", should_record=False,
                            event_type=None, score=0.0,
                            eyes_closed_duration=0.0, yawn_duration=0.0,
                            distraction_duration=0.0),
        ])
        assert pipeline.run() == 1
    decoded = cv2.imdecode(np.frombuffer(manager.recorded_events[0]["jpeg"], np.uint8),
                           cv2.IMREAD_COLOR)
    assert decoded.shape == raw.shape
    assert np.allclose(decoded.mean(axis=(0, 1)), (25, 90, 210), atol=3)
    camera.stop.assert_called_once()
    camera.close.assert_called_once()
    print("    -> OK: CSI -> MediaPipe -> evento, cores BGR e fechamento preservados.")


def run_all_tests():
    print("=" * 65)
    print("TESTE DE INTEGRAÇÃO: main.py + EventManager Mock (V-GUARD)")
    print("=" * 65)

    test_jpeg_encoding_and_contract()
    test_vision_pipeline_end_to_end()
    test_csi_frame_to_event()
    test_calibration_blocks_events_and_loaded_pose_reaches_detector()
    test_webcam_failure_is_error_but_video_end_is_not()
    test_headless_pipeline_logs_fps_every_ten_seconds()
    test_guided_check_records_no_events()

    print("\n" + "=" * 65)
    print("TODOS OS TESTES DE INTEGRAÇÃO PASSARAM COM SUCESSO!")
    print("=" * 65)


def test_calibration_blocks_events_and_loaded_pose_reaches_detector():
    """Assistente bloqueia persistência e perfil recarregado corrige a pose no loop real."""
    raw = FrameMetrics(.21, .1, .155, .005, -23.4, 37.1, -12)
    frame = np.zeros((480, 640, 3), np.uint8)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'calibration-driver.json'
        manager = MockEventManager()
        pipeline = VisionPipeline(manager, calibration_path=path, max_frames=4)
        camera, landmarker = Mock(), Mock()
        camera.read.side_effect = lambda: (True, frame.copy())
        landmarker.detect_for_video.return_value = SimpleNamespace(face_landmarks=[[object()]])
        pipeline.detector.update = Mock(return_value=DetectionResult(
            'FADIGA', True, 'fadiga', .9, 2, 0, 0))
        with patch.object(pipeline, '_open_capture', return_value=camera), \
             patch.object(pipeline, '_init_landmarker', return_value=landmarker), \
             patch('main.extract_all_metrics', return_value=raw), \
             patch('main.open_window'), \
             patch('main.draw_landmarks'), patch('main.cv2.imshow'), \
             patch('main.cv2.waitKey', return_value=-1), patch('main.cv2.destroyAllWindows'):
            assert pipeline.run() == 0
        assert not manager.recorded_events
        camera.release.assert_called_once()
        CalibrationProfile.from_neutral([raw] * 30, 640, 480,
            context='source=picamera2;camera=0;infrared=False').save(path)
        loaded = VisionPipeline(manager, headless=True, calibration_path=path, max_frames=40)
        with patch.object(loaded, '_open_capture', return_value=camera), \
             patch.object(loaded, '_init_landmarker', return_value=landmarker), \
             patch('main.extract_all_metrics', return_value=raw), \
             patch('main.time.monotonic', side_effect=[i / 10 for i in range(50)]):
            assert loaded.run() == 0
        assert loaded.detector.distraction_tracker.duration == 0
        assert not manager.recorded_events
        with patch('main.open_capture') as capture:
            try:
                VisionPipeline(manager, headless=True, calibration_path=path, calibrate=True)
                raise AssertionError('Calibracao interativa nao pode ser headless')
            except ValueError:
                pass
            capture.assert_not_called()


def test_webcam_failure_is_error_but_video_end_is_not():
    """C525 desconectada sai com erro (para ser reiniciada); vídeo que acaba sai normal."""
    camera = Mock()
    camera.read.return_value = (False, None)
    for source, should_fail in ((0, True), ("/dev/v4l/by-id/usb-046d_C525-video-index0", True),
                                ("viagem.mp4", False)):
        pipeline = VisionPipeline(MockEventManager(), headless=True, source=source)
        with patch.object(pipeline, '_open_capture', return_value=camera), \
             patch.object(pipeline, '_init_landmarker', return_value=Mock()):
            try:
                pipeline.run()
                assert not should_fail, f"{source}: falha da webcam deveria virar erro"
            except RuntimeError as error:
                assert should_fail and "parou de enviar quadros" in str(error), error


def test_headless_pipeline_logs_fps_every_ten_seconds():
    """Sem tela, a média de 10 s vai ao log e fica no vision_fps (ticket 1b)."""
    camera, landmarker = Mock(), Mock()
    frame = np.zeros((480, 640, 3), np.uint8)
    camera.read.side_effect = lambda: (True, frame)
    landmarker.detect_for_video.return_value = SimpleNamespace(face_landmarks=[])
    # 16 quadros por segundo durante 11 s: a média de 10 s fecha uma vez.
    instants = [1 + i / 16 for i in range(176)]
    pipeline = VisionPipeline(MockEventManager(), headless=True, source=0, max_frames=len(instants))
    assert pipeline.vision_fps == 0.0
    with patch.object(pipeline, '_open_capture', return_value=camera), \
         patch.object(pipeline, '_init_landmarker', return_value=landmarker), \
         patch('main.time.monotonic', side_effect=[0.0] + instants), \
         unittest.TestCase().assertLogs("vision", level="INFO") as logs:
        pipeline.run()
    assert pipeline.vision_fps == 16.0, pipeline.vision_fps
    assert "INFO:vision:visão: 16.0 FPS em 10 s" in logs.output, logs.output
    assert pipeline.summary() == "visão 16.0 FPS em média; eventos: nenhum", pipeline.summary()


def test_guided_check_records_no_events():
    """
    A conferência guiada (Issue #7) começa no V, passa as 3 provas com o detector
    de verdade e não grava evento; depois dela, a gravação volta.

    Em 09/11 o pipeline zerava o detector a cada quadro durante as provas, e o
    bocejo e o rosto virado nunca passavam: este teste usa o DriverStateDetector
    real, para pegar esse tipo de erro.
    """
    neutral = FrameMetrics(.30, .28, .29, .05, -23.4, 37.1, -12)
    turned = replace(neutral, yaw=neutral.yaw - 40)      # 40° para o lado, a partir da referência
    yawning = replace(neutral, mar=.80)
    frame = np.zeros((480, 640, 3), np.uint8)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'calibration-driver.json'
        CalibrationProfile.from_neutral([neutral] * 30, 640, 480,
            context='source=picamera2;camera=0;infrared=False').save(path)
        manager = MockEventManager()
        # 0,1 s por quadro; depois da conferência (~25 s), 10 s de bocejo para gravar.
        pipeline = VisionPipeline(manager, calibration_path=path, max_frames=400)
        camera, landmarker = Mock(), Mock()
        camera.read.side_effect = lambda: (True, frame.copy())
        landmarker.detect_for_video.return_value = SimpleNamespace(face_landmarks=[[object()]])

        def metrics(*_):
            """A pessoa faz o que a prova em curso pede; depois da conferência, boceja."""
            check = pipeline.check
            if check is None:
                return neutral
            if check.phase == "run" and check.index == 0:
                return turned
            if check.phase == "run" and check.index == 1:
                return yawning
            if check.phase == "done":
                return yawning
            return neutral

        keys = iter([ord('v')] + [255] * 1000)
        clock = iter([i / 20 for i in range(2000)])     # 2 leituras do relógio por quadro
        shown = []
        with patch.object(pipeline, '_open_capture', return_value=camera), \
             patch.object(pipeline, '_init_landmarker', return_value=landmarker), \
             patch('main.extract_all_metrics', side_effect=metrics), \
             patch('main.open_window'), patch('main.draw_landmarks'), \
             patch('main.cv2.imshow', side_effect=lambda *a: shown.append(
                 (pipeline.check is not None and pipeline.check.blocking, len(manager.recorded_events)))), \
             patch('main.cv2.waitKey', side_effect=lambda *_: next(keys)), \
             patch('main.cv2.destroyAllWindows'), \
             patch('main.time.monotonic', side_effect=lambda: next(clock)), \
             unittest.TestCase().assertLogs("vision", level="INFO") as logs:
            pipeline.run()
        check = pipeline.check
        assert check.results == {"lado": "OK", "bocejo": "OK", "estrada": "OK"}, check.results
        guided = [line for line in logs.output if "conferência guiada" in line]
        assert guided == ["INFO:vision:conferência guiada: começou (nenhum evento é gravado até o fim)",
                          "INFO:vision:conferência guiada: Lado OK | Bocejo OK | Estrada OK"], guided
        assert check.passed
        during = [count for blocking, count in shown if blocking]
        assert during and set(during) == {0}, "a conferência gravou evento"
        assert [event["type"] for event in manager.recorded_events] == ["fadiga"], \
            "depois da conferência a gravação volta"

if __name__ == "__main__":
    run_all_tests()
