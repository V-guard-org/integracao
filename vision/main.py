"""
V-GUARD - Modulo de Visao Computacional (Orquestrador Principal).

Pipeline de execucao:
1. Captura de quadros da camera / arquivo de video (resolucao padrao 640x480).
2. Deteccao de landmarks faciais via MediaPipe Face Landmarker (timestamps monotonicos reais).
3. Extracao de metricas geometricas puras (EAR, MAR e Head Pose via solvePnP) com metrics.py.
4. Analise temporal e maquina de estados do condutor via detector.py.
5. Invocacao do EventManager do V-GUARD (com codificacao JPEG em memoria) sob demanda.
6. Interface grafica opcional (--debug / modo visual) com suporte a execucao headless no Raspberry Pi.
"""

import argparse
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
        "PISCANDO": (255, 255, 0),    # Ciano
        "BOCEJANDO": (0, 255, 255),   # Amarelo
        "FADIGA": (0, 0, 255),        # Vermelho
        "DISTRAIDO": (0, 0, 255),     # Vermelho
        "SEM_ROSTO": (0, 140, 255),   # Laranja
    }
    state_color = state_colors.get(detection.driver_state, (255, 255, 255))

    y = 35
    cv2.putText(
        frame,
        f"Estado: {detection.driver_state}",
        (20, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        state_color,
        2,
    )

    y += 25
    if metrics is not None:
        cv2.putText(
            frame,
            f"EAR: {metrics.ear:.3f} (L:{metrics.ear_left:.2f} R:{metrics.ear_right:.2f})",
            (20, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (220, 220, 220),
            1,
        )
        y += 20
        cv2.putText(
            frame,
            f"MAR: {metrics.mar:.3f}",
            (20, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (220, 220, 220),
            1,
        )
        y += 20
        cv2.putText(
            frame,
            f"Pose: P:{metrics.pitch:+.1f} Y:{metrics.yaw:+.1f} R:{metrics.roll:+.1f}",
            (20, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (220, 220, 220),
            1,
        )
    else:
        cv2.putText(
            frame,
            "Rosto nao detectado",
            (20, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (0, 140, 255),
            1,
        )
        y += 40

    y += 22
    cv2.putText(
        frame,
        f"T. Olhos Fechados: {detection.eyes_closed_duration:.2f}s",
        (20, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.42,
        (180, 180, 180),
        1,
    )
    y += 18
    cv2.putText(
        frame,
        f"T. Bocejo: {detection.yawn_duration:.2f}s",
        (20, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.42,
        (180, 180, 180),
        1,
    )
    y += 18
    cv2.putText(
        frame,
        f"T. Distracao: {detection.distraction_duration:.2f}s",
        (20, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.42,
        (180, 180, 180),
        1,
    )
    y += 22
    cv2.putText(
        frame,
        f"FPS: {fps:.1f}",
        (20, y),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (0, 255, 128),
        1,
    )

    # Banner de alerta em caso de evento recente
    if alert_text:
        banner_h = 40
        cv2.rectangle(
            frame,
            (0, h - banner_h),
            (w, h),
            (0, 0, 200),
            -1,
        )
        cv2.putText(
            frame,
            alert_text,
            (20, h - 12),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
        )


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
        event_manager: Any = None,
        headless: bool = False,
        source: Union[int, str] = 0,
        model_path: Union[str, Path] = MODEL_PATH,
        frame_width: int = FRAME_WIDTH,
        frame_height: int = FRAME_HEIGHT,
        max_frames: Optional[int] = None,
    ):
        self.event_manager = event_manager
        self.headless = headless
        self.source = source
        self.model_path = Path(model_path)
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.max_frames = max_frames

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

    def _open_capture(self) -> cv2.VideoCapture:
        """Abre a captura de video pela webcam ou arquivo."""
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            raise RuntimeError(f"Nao foi possivel abrir a fonte de video: {self.source}")

        # Tenta definir resolucao recomendada caso seja dispositivo de camera
        if isinstance(self.source, int) or (
            isinstance(self.source, str) and self.source.isdigit()
        ):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.frame_width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.frame_height)

        return cap

    def run(self) -> int:
        """
        Executa o loop principal de processamento de video.
        Retorna o numero total de eventos registrados durante a sessao.
        """
        cap = self._open_capture()
        landmarker = self._init_landmarker()

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
                if result.face_landmarks and len(result.face_landmarks) > 0:
                    landmarks = result.face_landmarks[0]
                    frame_metrics = extract_all_metrics(landmarks, w, h)

                # Processamento temporal e avaliacao de estado do motorista
                detection = self.detector.update(frame_metrics, now_mono)

                # Disparo de evento sob demanda (exclusivamente quando should_record for True)
                # Otimizacao Raspberry Pi: codifica o frame puro antes dos overlays, sem frame.copy() continuo
                if detection.should_record and detection.event_type is not None:
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
                        self.events_recorded_count += 1

                        # 2. Despacho para o EventManager (se injetado) ou log no terminal
                        if self.event_manager is not None:
                            try:
                                event_id = self.event_manager.record_event(
                                    type=detection.event_type,
                                    score=event_score,
                                    jpeg=jpeg_bytes,
                                    ts=event_ts,
                                )
                                print(
                                    f"[EVENTO REGISTRADO] ID={event_id} Tipo='{detection.event_type}' "
                                    f"Score={event_score} Tamanho={len(jpeg_bytes)}B"
                                )
                            except Exception as e:
                                print(f"[ERRO] Falha ao registrar evento no EventManager: {e}", file=sys.stderr)
                        else:
                            print(
                                f"[EVENTO DISPARADO (STANDALONE)] Tipo='{detection.event_type}' "
                                f"Score={event_score} Tamanho={len(jpeg_bytes)}B Ts={event_ts:.3f}"
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

                    draw_hud(frame, frame_metrics, detection, fps, alert_banner)

                    cv2.imshow("V-GUARD - Visao Computacional", frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        print("[V-GUARD Vision] Encerramento solicitado pelo usuario ('q').")
                        break

                # Parada opcional por limite de frames (usado em testes automatizados)
                if self.max_frames is not None and frame_count >= self.max_frames:
                    break

        finally:
            cap.release()
            landmarker.close()
            if not self.headless:
                cv2.destroyAllWindows()
            print(f"[V-GUARD Vision] Pipeline finalizado. Total de frames: {frame_count}, Eventos: {self.events_recorded_count}")

        return self.events_recorded_count


# ==============================================================================
# 3. INTERFACE DE LINHA DE COMANDO (CLI)
# ==============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
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
        default="0",
        help="Indice da camera (ex: 0) ou caminho de arquivo de video para testes",
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
    return parser.parse_args()


def main():
    args = parse_args()

    # Trata indice numerico de camera vs arquivo de video
    source: Union[int, str] = int(args.source) if args.source.isdigit() else args.source

    pipeline = VisionPipeline(
        event_manager=None,
        headless=args.headless,
        source=source,
        frame_width=args.width,
        frame_height=args.height,
    )
    pipeline.run()


if __name__ == "__main__":
    main()