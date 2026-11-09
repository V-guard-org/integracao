"""
V-GUARD - Modulo de Visao Computacional (Orquestrador Principal).

Pipeline de execucao:
1. Captura de quadros da camera / arquivo de video (resolucao padrao 640x480).
2. Deteccao de landmarks faciais via MediaPipe Face Landmarker (timestamps monotonicos reais).
3. Extracao de metricas geometricas puras (EAR, MAR e Head Pose via solvePnP) com metrics.py.
4. Analise temporal e maquina de estados do condutor via detector.py.
5. Invocacao do EventManager do V-GUARD (com codificacao JPEG em memoria) sob demanda.
6. Interface grafica opcional com suporte a execucao headless no Raspberry Pi.

Este arquivo é também o programa da visão (Issue #10, proposta §6.9): o
vguard.py o inicia como processo filho, no venv 3.11, com --channel-fd, e
recebe pelo canal o pronto, o fps e os eventos com o JPEG. Sem
--channel-fd, a visão roda sozinha e escreve as mensagens no stdout:

    python vision/main.py --source 0 --headless
"""

import argparse
from collections import Counter
from dataclasses import replace
import importlib.util
import logging
import math
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, Union

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
    MAR_THRESHOLD,
    MIN_FACE_DETECTION_CONFIDENCE,
    MIN_FACE_PRESENCE_CONFIDENCE,
    MIN_TRACKING_CONFIDENCE,
    MODEL_PATH,
    MOUTH_INDICES,
    RIGHT_EYE_INDICES,
    YAW_THRESHOLD,
)
from detector import DetectionResult, DriverStateDetector
from capture import Capture, is_webcam, open_capture
from fps import PERIOD_S as FPS_PERIOD_S, FpsMeter
from calibration import SPACE, CalibrationSession, GuidedCheck, Prompt
import options as vision_options
from options import ConfigError
import protocol
from metrics import FrameMetrics, extract_all_metrics, to_pixel_coords

log = logging.getLogger("vision")


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


WINDOW = "V-GUARD - Visao Computacional"
# A janela abre no canto de cima: sem isto, o gerenciador do Connect às vezes a
# abre com metade fora da tela (visto na bancada guiada de 09/11, Issue #6).
WINDOW_PLACE = (0, 40)

# Coluna ao lado da câmera, com o pedido da vez (Issue #7). A imagem da câmera
# fica inteira, sem nada por cima além do HUD do canto, e nada é ampliado: a
# janela tem o tamanho do quadro mais a coluna (1200x480 com 640x480).
PANEL_WIDTH = 560
PANEL_MIN_HEIGHT = 480
PANEL_BACKGROUND = (32, 32, 32)
PANEL_TONES = {                       # BGR
    "info": (0, 200, 255),            # amarelo: o pedido da vez
    "ok": (80, 220, 80),              # verde: deu certo
    "error": (80, 80, 255),           # vermelho: problema ou prova que não passou
    "neutral": (235, 235, 235),       # branco: início e monitorando
}
FONT = cv2.FONT_HERSHEY_SIMPLEX


def open_window() -> None:
    """Abre a janela da visão: tamanho fixo, sem a barra do OpenCV, no canto de cima."""
    cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE | cv2.WINDOW_GUI_NORMAL)
    cv2.imshow(WINDOW, np.full((max(FRAME_HEIGHT, PANEL_MIN_HEIGHT), FRAME_WIDTH + PANEL_WIDTH, 3),
                               PANEL_BACKGROUND, np.uint8))
    cv2.moveWindow(WINDOW, *WINDOW_PLACE)
    cv2.waitKey(1)


def _wrap_px(text: str, scale: float, thickness: int, max_width: int) -> list[str]:
    """Quebra o texto pelas palavras, para cada linha caber em max_width pixels."""
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        (width, _), _ = cv2.getTextSize(candidate, FONT, scale, thickness)
        if width <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _put(img: np.ndarray, text: str, x: int, y: int, scale: float, color, thickness: int,
         max_width: int) -> None:
    """Escreve uma linha, diminuindo a letra se ela não couber."""
    (width, _), _ = cv2.getTextSize(text, FONT, scale, thickness)
    while width > max_width and scale > 0.35:
        scale -= 0.05
        (width, _), _ = cv2.getTextSize(text, FONT, scale, thickness)
    cv2.putText(img, text, (x, y), FONT, scale, color, thickness, cv2.LINE_AA)


def draw_panel(panel: np.ndarray, prompt: Prompt, info_lines: list[str]) -> None:
    """
    A coluna ao lado da câmera, no estilo da bancada guiada: o passo, o pedido
    em letra grande numa faixa colorida, as frases curtas, o retorno ao vivo, a
    contagem ou a barra da medida e os resultados. Embaixo, as teclas e as
    linhas técnicas.
    """
    h, w = panel.shape[:2]
    color = PANEL_TONES.get(prompt.tone, PANEL_TONES["info"])
    white, gray, dim = (255, 255, 255), (190, 190, 190), (140, 140, 140)
    x, max_width = 24, w - 48

    y = 34
    _put(panel, prompt.step, x, y, 0.6, gray, 1, max_width)

    # O pedido, em letra grande, centralizado numa faixa da cor do momento
    def title_lines(scale):
        # "\n" no título marca onde quebrar; o resto quebra pela largura
        return [line for part in prompt.title.split("\n") for line in _wrap_px(part, scale, 3, max_width)]

    scale = 1.25
    title = title_lines(scale)
    if len(title) > 2:
        scale = 1.0
        title = title_lines(scale)
    line_height = int(44 * scale / 1.25) + 4
    top = y + 16
    bottom = top + 24 + line_height * len(title)
    cv2.rectangle(panel, (0, top), (w, bottom), tuple(c // 4 for c in color), -1)
    ty = top + 12
    for line in title:
        ty += line_height
        (width, _), _ = cv2.getTextSize(line, FONT, scale, 3)
        _put(panel, line, max(x, (w - width) // 2), ty, scale, color, 3, max_width)

    y = bottom + 36
    for line in prompt.detail:
        _put(panel, line, x, y, 0.72, white, 2, max_width)
        y += 30
    if prompt.feedback:
        y += 6
        _put(panel, prompt.feedback, x, y, 0.85, color, 2, max_width)
        y += 34
    if prompt.countdown:
        y += 6
        _put(panel, prompt.countdown, x, y, 0.85, color, 2, max_width)
        y += 34
    if prompt.progress is not None:
        y += 4
        cv2.rectangle(panel, (x, y - 14), (w - x, y), gray, 1)
        cv2.rectangle(panel, (x, y - 14), (x + int((w - 2 * x) * prompt.progress), y), color, -1)
        y += 30
    if prompt.results:
        _put(panel, prompt.results, x, y, 0.62, gray, 1, max_width)

    # Embaixo: as linhas técnicas e, acima delas, as teclas
    yb = h - 12
    for line in reversed(info_lines):
        _put(panel, line, x, yb, 0.42, dim, 1, max_width)
        yb -= 20
    if prompt.keys:
        cv2.line(panel, (x, yb - 4), (w - x, yb - 4), (70, 70, 70), 1)
        _put(panel, prompt.keys, x, yb - 18, 0.6, white, 1, max_width)


def compose(frame: np.ndarray, prompt: Prompt, info_lines: list[str]) -> np.ndarray:
    """A imagem da janela: o quadro da câmera (com o HUD) e a coluna ao lado."""
    h, w = frame.shape[:2]
    height = max(h, PANEL_MIN_HEIGHT)
    panel = np.full((height, PANEL_WIDTH, 3), PANEL_BACKGROUND, np.uint8)
    draw_panel(panel, prompt, info_lines)
    if h < height:
        frame = np.vstack([frame, np.full((height - h, w, 3), PANEL_BACKGROUND, np.uint8)])
    return np.hstack([frame, panel])


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
        on_ready: Optional[Callable[[], None]] = None,
        on_fps: Optional[Callable[[float], None]] = None,
    ):
        """
        `on_ready` é chamado uma vez, com a câmera aberta e o MediaPipe
        carregado; `on_fps`, a cada média de 10 s. O programa da visão os liga
        ao canal (o pronto e o fps). Um erro de configuração sai como
        ConfigError (código 78 no programa da visão).
        """
        self.event_manager = event_manager
        self.on_ready = on_ready
        self.on_fps = on_fps
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
        try:
            self.calibration = CalibrationSession(calibration_path, recalibrate=calibrate, context=context)
        except ValueError as exc:     # perfil inválido, ou de outra câmera ou outro modo IR
            raise ConfigError(f"Perfil da calibracao: {exc}") from exc
        if headless and self.calibration.active:
            raise ConfigError("Calibracao pendente: execute sem --headless e siga a tela "
                              "(tools/test_camera_gui.sh --calibrate)")

        if not self.model_path.exists():
            raise ConfigError(
                f"Modelo nao encontrado em: {self.model_path}. "
                "Execute 'python scripts/download_model.py' para baixa-lo."
            )

        # Inicializa o detector temporal
        self.detector = DriverStateDetector()
        # Conferência guiada depois da calibração (tecla V), só com a tela (Issue #7)
        self.check: Optional[GuidedCheck] = None

        # Contador de eventos disparados durante a execucao
        self.events_recorded_count = 0
        self._events_by_type: Counter = Counter()

        # FPS da visão: a média dos últimos 10 s, que a saúde lê (ticket 5).
        self.vision_fps = 0.0
        self._fps_meter = FpsMeter()

    def summary(self) -> str:
        """A parte da visão no resumo ao encerrar: FPS médio e eventos gravados por tipo."""
        events = ", ".join(f"{kind} {count}" for kind, count in self._events_by_type.items())
        return f"visão {self._fps_meter.average:.1f} FPS em média; eventos: {events or 'nenhum'}"

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

    def _start_check(self, now: float) -> None:
        self.check = GuidedCheck(now, yaw_limit=YAW_THRESHOLD, mar_limit=MAR_THRESHOLD)
        log.info("conferência guiada: começou (nenhum evento é gravado até o fim)")

    def _handle_key(self, key: int, now: float) -> None:
        """
        Teclas da tela (a coluna mostra as que valem agora): C recomeça a
        calibração; na calibração, ESPAÇO começa ou mede de novo e N pula os
        olhos; com o perfil pronto, ESPAÇO ou V começa a conferência guiada e
        N pula a prova da vez.
        """
        if key in (ord("c"), ord("C")):
            if self.check is not None:
                log.info("conferência guiada: interrompida (C)")
            self.check = None
            self.calibration.start(now)
        elif self.calibration.active or self.calibration.state == "idle":
            self.calibration.key(key, now)
        elif key in (SPACE, ord("v"), ord("V")):
            if self.check is None or not self.check.blocking:
                self._start_check(now)
        elif key in (ord("n"), ord("N")) and self.check is not None and self.check.blocking:
            self.check.skip(now)
        if self.calibration.pop_saved():
            self._start_check(now)

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
        if not self.headless:
            open_window()
        if self.on_ready is not None:
            self.on_ready()

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

                # Média de 10 s no log, também sem tela
                period_fps = self._fps_meter.frame(now_mono)
                if period_fps is not None:
                    self.vision_fps = period_fps
                    log.info("visão: %.1f FPS em %g s", period_fps, FPS_PERIOD_S)
                    if self.on_fps is not None:
                        self.on_fps(period_fps)

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
                if self.calibration.pop_saved():
                    # O perfil acabou de ser gravado: a conferência guiada começa sozinha
                    self._start_check(now_mono)
                # Na conferência guiada as provas usam o detector, mas nada é gravado
                checking = not was_calibrating and self.check is not None and self.check.blocking
                # Processamento temporal e avaliacao de estado do motorista
                if was_calibrating or (checking and self.check.begin_frame(now_mono)):
                    self.detector.reset()
                detection = self.detector.update(frame_metrics, now_mono)
                if was_calibrating:
                    detection = replace(detection, driver_state="CALIBRANDO", should_record=False,
                                        event_type=None, score=0.0)
                elif checking:
                    self.check.observe(detection.driver_state, frame_metrics, now_mono)
                    detection = replace(detection, should_record=False, event_type=None)
                if checking and not self.check.blocking:
                    log.info("conferência guiada: %s", self.check.results_line())

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
                        except BrokenPipeError:
                            raise       # o programa principal saiu: a visão sai também
                        except Exception as e:
                            event_id = None
                            print(f"[ERRO] Falha ao registrar evento no EventManager: {e}", file=sys.stderr)
                        if event_id is not None:
                            self.events_recorded_count += 1
                            self._events_by_type[detection.event_type] += 1
                            # No processo filho o evento é enviado ao principal, que o grava
                            # e escreve o [EVENTO REGISTRADO] (a bancada guiada lê essa linha).
                            label = getattr(self.event_manager, "event_label", "EVENTO REGISTRADO")
                            print(
                                f"[{label}] ID={event_id} Tipo='{detection.event_type}' "
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
                    showing_check = self.check is not None and not self.calibration.active
                    prompt = (self.check if showing_check else self.calibration).prompt(now_mono)
                    info_lines = []
                    if raw_metrics is not None:
                        info_lines.append(f"Bruto EAR L:{raw_metrics.ear_left:.3f} R:{raw_metrics.ear_right:.3f} "
                                          f"P:{raw_metrics.pitch:+.1f} Y:{raw_metrics.yaw:+.1f}")
                    ocular = "indeterminado"
                    if frame_metrics and frame_metrics.eye_closure is not None:
                        ocular = "baixo" if frame_metrics.eye_closure else "aberto"
                    reference = "relativa" if self.calibration.profile else "sem referencia"
                    info_lines.append(f"Pose {reference} | Olhos: {ocular} | "
                                      f"Reforco: {'sim' if detection.eye_support else 'nao'}")
                    cv2.imshow(WINDOW, compose(frame, prompt, info_lines))
                    key = cv2.waitKey(1) & 0xFF
                    if key in (ord("q"), ord("Q")):
                        print("[V-GUARD Vision] Encerramento solicitado pelo usuario ('q').")
                        break
                    self._handle_key(key, time.monotonic())
                    if self.calibration.active:
                        self.detector.reset()
                        alert_banner = None
                    elif self.check is not None and self.check.blocking:
                        # Sem zerar o detector: nas provas ele precisa acumular os 2 s
                        # do rosto virado e o 1,5 s do bocejo. A preparação de cada
                        # prova já o zera (GuidedCheck.begin_frame).
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
    """As opções da visão (vision/options.py) e o --channel-fd do canal com o principal."""
    parser = parser or argparse.ArgumentParser(
        description="V-GUARD - a visao (o programa filho do vguard.py; sozinha, mostra as mensagens)"
    )
    vision_options.add_arguments(parser)
    parser.add_argument("--channel-fd", type=int,
                        help="Descritor do canal com o programa principal (sem ele, as mensagens vao ao stdout)")
    args = parser.parse_args(argv)
    vision_options.check(parser, args)
    if args.source == "picamera2" and args.camera_python is None:
        args.camera_python = auto_camera_python()
    return args


def build_pipeline(args: argparse.Namespace, event_manager: Any, **hooks) -> VisionPipeline:
    """O VisionPipeline das opções da linha de comando (o programa da visão e as ferramentas)."""
    return VisionPipeline(
        event_manager=event_manager,
        headless=args.headless,
        source=int(args.source) if args.source.isdigit() else args.source,
        frame_width=args.width,
        frame_height=args.height,
        max_frames=args.max_frames,
        camera_fps=args.fps,
        camera_num=args.camera_num,
        infrared=args.infrared,
        camera_python=args.camera_python,
        calibration_path=args.calibration_file,
        calibrate=args.calibrate,
        **hooks,
    )


class ChannelSink:
    """
    O mensageiro da visão no processo filho (proposta §6.1): a mesma
    assinatura do record_event do EventManager, mas o evento vai pelo canal ao
    programa principal, que o grava. Devolve o número da mensagem, para o
    pipeline contar o envio.
    """

    event_label = "EVENTO ENVIADO"

    def __init__(self, writer: Any):
        self.writer = writer
        self.sent = 0

    def record_event(self, type: str, score: float, jpeg: bytes, ts: float) -> int:
        self.writer.event(ts, type, score, jpeg)
        self.sent += 1
        return self.sent


class ConsoleWriter:
    """Sem o principal: as mensagens do canal no stdout, a linha de JSON sem os bytes do JPEG."""

    def send(self, data: bytes) -> None:
        line, _, payload = data.partition(b"\n")
        extra = f"  (+ {len(payload)} bytes do JPEG)" if payload else ""
        print(f"[canal] {line.decode('ascii')}{extra}", flush=True)

    def ready(self) -> None:
        self.send(protocol.ready())

    def fps(self, value: float) -> None:
        self.send(protocol.fps(value))

    def event(self, ts: float, type: str, score: float, jpeg: bytes) -> None:
        self.send(protocol.event(ts, type, score, jpeg))


class Terminated(Exception):
    """SIGTERM: o principal pediu para a visão sair (ou alguém a matou por fora)."""


def _terminate(signum, frame):
    raise Terminated()


def main(argv=None) -> int:
    """
    O programa da visão. Códigos de saída (protocol.py): 0 no fim normal (Q
    na tela ou --max-frames), 78 no erro de configuração, 143 no SIGTERM e
    1 (com o traceback no log) em qualquer outra falha, que o principal
    reinicia.
    """
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.channel_fd is not None:
        # O Ctrl+C do terminal chega aos dois processos: quem cuida dele é o
        # principal, que manda o SIGTERM. O canal não passa ao auxiliar da CSI.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        os.set_inheritable(args.channel_fd, False)
        writer = protocol.Writer(args.channel_fd)
    else:
        writer = ConsoleWriter()
    signal.signal(signal.SIGTERM, _terminate)
    try:
        pipeline = build_pipeline(args, ChannelSink(writer), on_ready=writer.ready, on_fps=writer.fps)
        pipeline.run()
    except ConfigError as exc:
        log.error("visão: erro de configuração: %s", exc)
        return protocol.EXIT_CONFIG
    except Terminated:
        log.info("visão: encerrada pelo SIGTERM")
        return protocol.EXIT_TERMINATED
    except BrokenPipeError:
        log.error("visão: o canal com o programa principal fechou")
        return 1
    return protocol.EXIT_OK


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


if __name__ == "__main__":
    sys.exit(main())
