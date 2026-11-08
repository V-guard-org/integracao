"""
V-GUARD - Modulo de Visao Computacional (Orquestrador Principal).

Pipeline de execucao:
1. Captura de quadros da camera / arquivo de video (resolucao padrao 640x480).
2. Deteccao de landmarks faciais via MediaPipe Face Landmarker (timestamps monotonicos reais).
3. Extracao de metricas geometricas puras (EAR, MAR e Head Pose via solvePnP) com metrics.py.
4. Analise temporal e maquina de estados do condutor via detector.py.
5. Invocacao do EventManager do V-GUARD (com codificacao JPEG em memoria) sob demanda.
6. Interface grafica opcional com suporte a execucao headless no Raspberry Pi.

Ponto de entrada: ../vguard.py (este modulo so fornece VisionPipeline e parse_args).
"""

import argparse
from dataclasses import replace
import importlib.util
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional, Union

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

# Garante resolucao de imports locais do modulo
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    FRAME_HEIGHT,
    FRAME_WIDTH,
    LEFT_EYE_INDICES,
    MIN_FACE_DETECTION_CONFIDENCE,
    MIN_FACE_PRESENCE_CONFIDENCE,
    MIN_TRACKING_CONFIDENCE,
    MODEL_PATH,
    MOUTH_INDICES,
    RIGHT_EYE_INDICES,
)
from detector import DetectionResult, DriverStateDetector
from capture import Capture, is_webcam, open_capture
from calibration import CalibrationSession
from metrics import FrameMetrics, extract_all_metrics, to_pixel_coords


# ==============================================================================
# 1. RENDERIZAÇÃO DE OVERLAY GRÁFICO (MODO DEBUG VISUAL)
# ==============================================================================

def draw_landmarks(frame: np.ndarray, landmarks: Any, width: int, height: int) -> None:
    """Desenha os pontos-chave faciais (olhos, boca e nariz) sobre o frame."""
    key_indices = RIGHT_EYE_INDICES + LEFT_EYE_INDICES + MOUTH_INDICES + [1]
    for idx in key_indices:
        px, py = to_pixel_coords(landmarks[idx], width, height)
        cv2.circle(frame, (int(px), int(py)), 2, (0, 255, 0), -1)


def draw_hud(
    frame: np.ndarray,
    metrics: Optional[FrameMetrics],
    detection: DetectionResult,
    fps: float,
    alert_text: Optional[str] = None,
) -> None:
    """Desenha o painel de telemetria HUD e alertas sobre o frame."""
    h, w = frame.shape[:2]

    # Painel semi-transparente no canto superior esquerdo
    panel_w, panel_h = 280, 200
    sub_img = frame[10 : 10 + panel_h, 10 : 10 + panel_w]
    dark_rect = np.zeros(sub_img.shape, dtype=np.uint8)
    res = cv2.addWeighted(sub_img, 0.4, dark_rect, 0.6, 1.0)
    frame[10 : 10 + panel_h, 10 : 10 + panel_w] = res
    cv2.rectangle(frame, (10, 10), (10 + panel_w, 10 + panel_h), (80, 80, 80), 1)

    # Cores de estado
    state_colors = {
        "NORMAL": (0, 255, 0),       # Verde
        "BOCEJANDO": (0, 255, 255),   # Amarelo
        "FADIGA": (0, 0, 255),        # Vermelho
        "DISTRAIDO": (0, 0, 255),     # Vermelho
        "SEM_ROSTO": (0, 140, 255),   # Laranja
        "CALIBRANDO": (0, 255, 255),
    }
    state_color = state_colors.get(detection.driver_state, (255, 255, 255))

    gray, dim = (220, 220, 220), (180, 180, 180)
    if metrics is not None:
        face_lines = [
            (f"EAR: {metrics.ear:.3f} (L:{metrics.ear_left:.2f} R:{metrics.ear_right:.2f})", 0.45, gray, 1, 20),
            (f"MAR: {metrics.mar:.3f}", 0.45, gray, 1, 20),
            (f"Pose: P:{metrics.pitch:+.1f} Y:{metrics.yaw:+.1f} R:{metrics.roll:+.1f}", 0.45, gray, 1, 22),
        ]
    else:
        face_lines = [("Rosto nao detectado", 0.45, (0, 140, 255), 1, 62)]

    # (texto, escala, cor, espessura, espaco ate a proxima linha)
    lines = [
        (f"Estado: {detection.driver_state}", 0.65, state_color, 2, 25),
        *face_lines,
        (f"T. Sinal ocular: {detection.eyes_closed_duration:.2f}s", 0.42, dim, 1, 18),
        (f"T. Bocejo: {detection.yawn_duration:.2f}s", 0.42, dim, 1, 18),
        (f"T. Distracao: {detection.distraction_duration:.2f}s", 0.42, dim, 1, 22),
        (f"FPS: {fps:.1f}", 0.5, (0, 255, 128), 1, 0),
    ]
    y = 35
    for text, scale, color, thickness, dy in lines:
        cv2.putText(frame, text, (20, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness)
        y += dy

    # Banner de alerta em caso de evento recente
    if alert_text:
        banner_h = 40
        cv2.rectangle(frame, (0, h - banner_h), (w, h), (0, 0, 200), -1)
        cv2.putText(frame, alert_text, (20, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)


# ==============================================================================
# 2. CLASSE PRINCIPAL DO PIPELINE (VisionPipeline)
# ==============================================================================

class VisionPipeline:
    """
    Orquestrador do modulo de visao computacional do V-GUARD.
    Executa a captura, inferencia de landmarks, calculo de metricas e despacho de eventos.
    """

    def __init__(
        self,
        event_manager: Any,
        headless: bool = False,
        source: Union[int, str] = "picamera2",
        model_path: Union[str, Path] = MODEL_PATH,
        frame_width: int = FRAME_WIDTH,
        frame_height: int = FRAME_HEIGHT,
        max_frames: Optional[int] = None,
        camera_fps: float = 30.0,
        camera_num: int = 0,
        infrared: bool = False,
        camera_python: Optional[str] = None,
        calibration_path: Optional[Union[str, Path]] = None,
        calibrate: bool = False,
    ):
        self.event_manager = event_manager
        self.headless = headless
        self.source = source
        self.model_path = Path(model_path)
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.max_frames = max_frames
        self.camera_fps = camera_fps
        self.camera_num = camera_num
        self.infrared = infrared
        self.camera_python = camera_python
        context = f"source={source};camera={camera_num};infrared={infrared}"
        self.calibration = CalibrationSession(calibration_path, recalibrate=calibrate, context=context)
        if headless and self.calibration.active:
            raise ValueError("Calibracao pendente: execute sem --headless e siga C/F/S no HUD")

        if not self.model_path.exists():
            raise FileNotFoundError(
                f"Modelo nao encontrado em: {self.model_path}\n"
                "Execute 'python scripts/download_model.py' para baixa-lo."
            )

        # Inicializa o detector temporal
        self.detector = DriverStateDetector()

        # Contador de eventos disparados durante a execucao
        self.events_recorded_count = 0

    def _init_landmarker(self) -> vision.FaceLandmarker:
        """Inicializa e configura o FaceLandmarker do MediaPipe."""
        base_options = python.BaseOptions(model_asset_path=str(self.model_path))
        options = vision.FaceLandmarkerOptions(
            base_options=base_options,
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=MIN_FACE_DETECTION_CONFIDENCE,
            min_face_presence_confidence=MIN_FACE_PRESENCE_CONFIDENCE,
            min_tracking_confidence=MIN_TRACKING_CONFIDENCE,
        )
        return vision.FaceLandmarker.create_from_options(options)

    def _open_capture(self) -> Capture:
        """Abre a câmera CSI, webcam ou arquivo mantendo o contrato BGR."""
        return open_capture(self.source, self.frame_width, self.frame_height,
                            self.camera_fps, self.camera_num, self.infrared, self.camera_python)

    def run(self) -> int:
        """
        Executa o loop principal de processamento de video.
        Retorna o numero total de eventos registrados durante a sessao.
        """
        cap = self._open_capture()
        try:
            landmarker = self._init_landmarker()
        except BaseException:
            cap.release()
            raise

        print(f"[V-GUARD Vision] Iniciando pipeline (source={self.source}, headless={self.headless})...")

        last_timestamp_ms = 0
        prev_time = time.monotonic()
        fps = 0.0
        frame_count = 0

        # Controle de exibicao do banner visual de alerta
        alert_banner: Optional[str] = None
        alert_banner_expire: float = 0.0

        try:
            while True:
                ret, frame = cap.read()
                if not ret:
                    if is_webcam(self.source):
                        # Webcam não tem "fim": cabo solto ou câmera travada é erro,
                        # para o processo sair com falha e ser reiniciado.
                        raise RuntimeError(f"A webcam {self.source} parou de enviar quadros")
                    print("[V-GUARD Vision] Fim da transmissao de video ou erro de captura.")
                    break

                frame_count += 1
                now_mono = time.monotonic()
                h, w = frame.shape[:2]

                # Calculo de FPS real
                delta_t = now_mono - prev_time
                if delta_t > 0:
                    fps = 1.0 / delta_t
                prev_time = now_mono

                # Relogio rigorosamente crescente em milissegundos para o MediaPipe VIDEO mode
                timestamp_ms = max(last_timestamp_ms + 1, int(now_mono * 1000))
                last_timestamp_ms = timestamp_ms

                # Conversao BGR -> RGB e execucao no MediaPipe
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

                result = landmarker.detect_for_video(mp_image, timestamp_ms)

                # Extracao de metricas geometricas puras
                landmarks = None
                frame_metrics: Optional[FrameMetrics] = None
                if result.face_landmarks:
                    landmarks = result.face_landmarks[0]
                    frame_metrics = extract_all_metrics(landmarks, w, h)

                raw_metrics = frame_metrics
                was_calibrating = self.calibration.active
                frame_metrics = self.calibration.update(frame_metrics, now_mono, w, h)
                # Processamento temporal e avaliacao de estado do motorista
                if was_calibrating:
                    self.detector.reset()
                detection = self.detector.update(frame_metrics, now_mono)
                if was_calibrating:
                    detection = replace(detection, driver_state="CALIBRANDO", should_record=False,
                                        event_type=None, score=0.0)

                # Disparo de evento sob demanda (exclusivamente quando should_record for True)
                # Otimizacao Raspberry Pi: codifica o frame puro antes dos overlays, sem frame.copy() continuo
                if detection.should_record:
                    # 1. Codificacao do frame original puro em JPEG em memoria
                    success, buffer = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), 85],
                    )

                    if success:
                        jpeg_bytes = buffer.tobytes()
                        event_ts = time.time()
                        event_score = round(detection.score, 2)

                        # 2. Despacho para o EventManager (None = dentro do cooldown dele)
                        try:
                            event_id = self.event_manager.record_event(
                                type=detection.event_type,
                                score=event_score,
                                jpeg=jpeg_bytes,
                                ts=event_ts,
                            )
                        except Exception as e:
                            event_id = None
                            print(f"[ERRO] Falha ao registrar evento no EventManager: {e}", file=sys.stderr)
                        if event_id is not None:
                            self.events_recorded_count += 1
                            print(
                                f"[EVENTO REGISTRADO] ID={event_id} Tipo='{detection.event_type}' "
                                f"Score={event_score} Tamanho={len(jpeg_bytes)}B"
                            )

                        # Ativa o banner visual no modo grafico por 1.5s
                        alert_banner = (
                            f"EVENTO DISPARADO: {detection.event_type.upper()} "
                            f"(Score: {event_score})"
                        )
                        alert_banner_expire = now_mono + 1.5

                # Renderizacao visual (somente se nao estiver em modo headless)
                if not self.headless:
                    if landmarks is not None:
                        draw_landmarks(frame, landmarks, w, h)

                    # Limpa banner expirado
                    if now_mono > alert_banner_expire:
                        alert_banner = None

                    draw_hud(frame, raw_metrics if was_calibrating else frame_metrics,
                             detection, fps, None if was_calibrating else alert_banner)
                    cv2.rectangle(frame, (0, h - 112), (w, h - 42), (25, 25, 25), -1)
                    cv2.putText(frame, self.calibration.message, (8, h - 94),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 1)
                    if raw_metrics is not None:
                        raw_text = (f"Bruto EAR L:{raw_metrics.ear_left:.3f} R:{raw_metrics.ear_right:.3f} "
                                    f"P:{raw_metrics.pitch:+.1f} Y:{raw_metrics.yaw:+.1f}")
                        cv2.putText(frame, raw_text, (8, h - 74),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, (220, 220, 220), 1)
                    ocular = "indeterminado"
                    if frame_metrics and frame_metrics.eye_closure is not None:
                        ocular = "baixo" if frame_metrics.eye_closure else "aberto"
                    reference = "relativa" if self.calibration.profile else "sem referencia"
                    status_text = f"Pose {reference} | Olhos: {ocular} | Reforco: {'sim' if detection.eye_support else 'nao'}"
                    cv2.putText(frame, status_text, (8, h - 54),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.40, (220, 220, 220), 1)

                    cv2.imshow("V-GUARD - Visao Computacional", frame)
                    key = cv2.waitKey(1) & 0xFF
                    if key == ord("q"):
                        print("[V-GUARD Vision] Encerramento solicitado pelo usuario ('q').")
                        break
                    self.calibration.key(key, time.monotonic())
                    if self.calibration.active:
                        self.detector.reset()
                        alert_banner = None

                # Parada opcional por limite de frames (usado em testes automatizados)
                if self.max_frames is not None and frame_count >= self.max_frames:
                    break

        finally:
            try:
                cap.release()
            finally:
                landmarker.close()
                if not self.headless:
                    cv2.destroyAllWindows()
                print(f"[V-GUARD Vision] Pipeline finalizado. Total de frames: {frame_count}, Eventos: {self.events_recorded_count}")

        return self.events_recorded_count


# ==============================================================================
# 3. INTERFACE DE LINHA DE COMANDO (CLI)
# ==============================================================================

def parse_args(argv=None, parser=None):
    parser = parser or argparse.ArgumentParser(
        description="V-GUARD - Modulo de Visao Computacional (Monitoramento de Motorista)"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Desativa exibicao de janela grafica OpenCV (obrigatorio para execucao headless no Raspberry Pi)",
    )
    parser.add_argument(
        "--source",
        type=str,
        default="picamera2",
        help="picamera2 para CSI OV5647 (padrao), indice de webcam (ex: 0 para a C525), "
             "dispositivo /dev/v4l/by-id/... ou arquivo de video",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=FRAME_WIDTH,
        help=f"Largura alvo de captura (padrao: {FRAME_WIDTH})",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=FRAME_HEIGHT,
        help=f"Altura alvo de captura (padrao: {FRAME_HEIGHT})",
    )
    parser.add_argument("--fps", type=float, default=30.0,
                        help="FPS solicitado à câmera (padrao: 30; nao altera videos)")
    parser.add_argument("--camera-num", type=int, default=0,
                        help="Indice da camera CSI listado por rpicam-hello --list-cameras")
    parser.add_argument("--infrared", action="store_true",
                        help="Fixa ganhos de cor para iluminacao IR na CSI; nao aciona LEDs/IR-CUT")
    parser.add_argument("--camera-python",
                        help="Python com Picamera2/libcamera para a captura em processo auxiliar "
                             "(padrao: /usr/bin/python3 se o Python atual nao tiver picamera2)")
    parser.add_argument("--calibration-file", type=Path,
                        help="Perfil JSON do motorista e montagem; se faltar, guia C/F/S no HUD")
    parser.add_argument("--calibrate", action="store_true",
                        help="Refaz referencia de direcao e olhos opcionais pelo HUD (C/F/S)")
    args = parser.parse_args(argv)
    if args.width <= 0 or args.height <= 0:
        parser.error("--width e --height devem ser maiores que zero")
    if not math.isfinite(args.fps) or args.fps <= 0:
        parser.error("--fps deve ser finito e maior que zero")
    if args.camera_num < 0:
        parser.error("--camera-num deve ser maior ou igual a zero")
    if args.source != "picamera2" and (args.infrared or args.camera_num != 0 or args.camera_python):
        parser.error("--infrared, --camera-num e --camera-python exigem --source picamera2")
    if args.headless and args.calibrate:
        parser.error("--calibrate exige interface grafica; retire --headless")
    if args.source == "picamera2" and args.camera_python is None:
        args.camera_python = auto_camera_python()
    return args


# Raspberry Pi OS: picamera2/libcamera vêm no Python do sistema.
SYSTEM_PYTHON = "/usr/bin/python3"


def auto_camera_python() -> Optional[str]:
    """
    Python do sistema para a captura CSI quando o Python atual não tem picamera2.

    No Trixie a visão roda num ambiente 3.11 (MediaPipe) e o picamera2 só existe
    no Python 3.13 do sistema: sem isto seria preciso lembrar --camera-python.
    """
    if importlib.util.find_spec("picamera2") is not None:
        return None
    if os.path.realpath(sys.executable) == os.path.realpath(SYSTEM_PYTHON):
        return None
    return SYSTEM_PYTHON if os.path.exists(SYSTEM_PYTHON) else None
