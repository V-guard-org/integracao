"""
Modulo de analise temporal e deteccao de estado do motorista do V-GUARD.

Processa metricas instantaneas (FrameMetrics) ao longo do tempo usando relogio
monotonico injetavel, aplicando histerese/tolerancia a ruido (grace period),
trava de episodio (latch) e cooldown independente por tipo de evento.
"""

from dataclasses import dataclass
from typing import Optional
import time

from config import (
    DISTRACTION_DURATION_S,
    EAR_THRESHOLD,
    EVENT_COOLDOWN_S,
    EYES_CLOSED_DURATION_S,
    GRACE_PERIOD_S,
    MAR_THRESHOLD,
    PITCH_THRESHOLD,
    YAW_THRESHOLD,
    YAWN_DURATION_S,
)
from metrics import FrameMetrics


# ==============================================================================
# 1. ESTRUTURA DE SAÍDA (DetectionResult)
# ==============================================================================

@dataclass(frozen=True)
class DetectionResult:
    """
    Resultado imutavel da avaliacao temporal do estado do condutor:
    - driver_state: Estado visual atual ("NORMAL", "PISCANDO", "BOCEJANDO", "FADIGA", "DISTRAIDO", "SEM_ROSTO")
    - should_record: True exclusivamente no frame exato em que o evento deve ser persistido
    - event_type: "fadiga" ou "distracao" quando should_record=True, ou None caso contrario
    - score: Severidade continua entre 0.0 e 1.0 (iniciando em 0.70 ao cruzar o limiar)
    - eyes_closed_duration: Tempo acumulado com olhos fechados (segundos)
    - yawn_duration: Tempo acumulado com abertura bucal de bocejo (segundos)
    - distraction_duration: Tempo acumulado de desvio de cabeca (segundos)
    """
    driver_state: str
    should_record: bool
    event_type: Optional[str]
    score: float
    eyes_closed_duration: float
    yawn_duration: float
    distraction_duration: float


# ==============================================================================
# 2. RASTREADOR TEMPORAL COM TOLERÂNCIA A RUÍDO (Histerese)
# ==============================================================================

class TemporalConditionTracker:
    """
    Rastreia a duracao continua de uma condicao temporal com tolerancia a ruido (grace period)
    e controle de trava de disparo por episodio.
    """

    def __init__(self, threshold_duration: float, grace_period_s: float):
        self.threshold_duration: float = threshold_duration
        self.grace_period_s: float = grace_period_s
        self.start_time: Optional[float] = None
        self.last_active_time: Optional[float] = None
        self.duration: float = 0.0
        self.episode_triggered: bool = False

    def update(self, is_active: bool, now: float) -> float:
        """
        Atualiza o estado temporal com base na presenca da condicao no instante 'now'.
        Retorna a duracao acumulada em segundos.
        """
        if is_active:
            if self.start_time is None:
                # Inicio de nova acumulacao
                self.start_time = now
            elif (
                self.last_active_time is not None
                and (now - self.last_active_time) > self.grace_period_s
            ):
                # Interrupcao anterior ultrapassou a tolerancia: reinicia o episodio
                self.start_time = now
                self.episode_triggered = False

            self.last_active_time = now
            self.duration = max(0.0, now - self.start_time)
        else:
            if self.last_active_time is not None:
                gap = now - self.last_active_time
                if gap > self.grace_period_s:
                    # Tolerancia de ruido expirou: zera acumulador e libera novo episodio
                    self.start_time = None
                    self.last_active_time = None
                    self.duration = 0.0
                    self.episode_triggered = False
                else:
                    # Dentro da tolerancia de ruido: preserva a duracao do ultimo instante ativo
                    if self.start_time is not None:
                        self.duration = max(0.0, self.last_active_time - self.start_time)
                    else:
                        self.duration = 0.0
            else:
                self.duration = 0.0

        return self.duration

    @property
    def is_accumulating(self) -> bool:
        """Retorna True se a condicao esta ativa ou dentro da janela de tolerancia (grace period)."""
        return self.start_time is not None

    @property
    def is_threshold_reached(self) -> bool:
        """Verifica se a duracao atual atingiu ou ultrapassou o limiar de evento."""
        return self.duration >= self.threshold_duration

    def should_trigger_episode(self) -> bool:
        """
        Verifica se o limiar foi atingido e trava o episodio para evitar re-disparos
        enquanto a condicao permanecer ativa nos frames seguintes.
        """
        if self.is_threshold_reached and not self.episode_triggered:
            self.episode_triggered = True
            return True
        return False

    def reset(self) -> None:
        """Reseta completamente o estado do rastreador."""
        self.start_time = None
        self.last_active_time = None
        self.duration = 0.0
        self.episode_triggered = False


# ==============================================================================
# 3. CLASSE PRINCIPAL: DriverStateDetector
# ==============================================================================

class DriverStateDetector:
    """
    Processador de analise temporal e deteccao de eventos de seguranca do condutor.
    Consome FrameMetrics e gera DetectionResult de forma deterministica via relogio monotonico.
    """

    def __init__(
        self,
        ear_threshold: float = EAR_THRESHOLD,
        mar_threshold: float = MAR_THRESHOLD,
        yaw_threshold: float = YAW_THRESHOLD,
        pitch_threshold: float = PITCH_THRESHOLD,
        eyes_closed_duration_s: float = EYES_CLOSED_DURATION_S,
        yawn_duration_s: float = YAWN_DURATION_S,
        distraction_duration_s: float = DISTRACTION_DURATION_S,
        grace_period_s: float = GRACE_PERIOD_S,
        event_cooldown_s: float = EVENT_COOLDOWN_S,
    ):
        self.ear_threshold = ear_threshold
        self.mar_threshold = mar_threshold
        self.yaw_threshold = yaw_threshold
        self.pitch_threshold = pitch_threshold

        self.eyes_closed_duration_s = eyes_closed_duration_s
        self.yawn_duration_s = yawn_duration_s
        self.distraction_duration_s = distraction_duration_s

        self.event_cooldown_s = event_cooldown_s

        # Rastreadores temporais por condicao com histerese
        self.eyes_tracker = TemporalConditionTracker(
            eyes_closed_duration_s, grace_period_s
        )
        self.yawn_tracker = TemporalConditionTracker(
            yawn_duration_s, grace_period_s
        )
        self.distraction_tracker = TemporalConditionTracker(
            distraction_duration_s, grace_period_s
        )

        # Controle de cooldown independente por tipo de evento
        self.last_event_time: dict[str, float] = {
            "fadiga": -float("inf"),
            "distracao": -float("inf"),
        }

    def update(self, metrics: Optional[FrameMetrics], now: float) -> DetectionResult:
        """
        Avalia as metricas do frame no instante 'now' (tempo monotonico em segundos).
        Retorna o resultado de deteccao com o estado e indicativo de gravacao.
        """
        # Caso 1: Rosto nao detectado no frame
        if metrics is None:
            eyes_dur = self.eyes_tracker.update(False, now)
            yawn_dur = self.yawn_tracker.update(False, now)
            distr_dur = self.distraction_tracker.update(False, now)

            return DetectionResult(
                driver_state="SEM_ROSTO",
                should_record=False,
                event_type=None,
                score=0.0,
                eyes_closed_duration=eyes_dur,
                yawn_duration=yawn_dur,
                distraction_duration=distr_dur,
            )

        # Caso 2: Rosto detectado - avalia condicoes booleanas instantaneas
        is_eyes_closed = metrics.ear < self.ear_threshold
        is_yawn = metrics.mar > self.mar_threshold
        is_distracted = (
            abs(metrics.yaw) > self.yaw_threshold
            or abs(metrics.pitch) > self.pitch_threshold
        )

        # Atualiza acumuladores temporais
        eyes_dur = self.eyes_tracker.update(is_eyes_closed, now)
        yawn_dur = self.yawn_tracker.update(is_yawn, now)
        distr_dur = self.distraction_tracker.update(is_distracted, now)

        # Verifica se algum rastreador cruzou o limiar de disparo neste frame
        eyes_crossed = self.eyes_tracker.should_trigger_episode()
        yawn_crossed = self.yawn_tracker.should_trigger_episode()
        distraction_crossed = self.distraction_tracker.should_trigger_episode()

        should_record = False
        event_type: Optional[str] = None

        # Prioridade 1: Fadiga (olhos fechados ou bocejo sustentado)
        if eyes_crossed or yawn_crossed:
            if (now - self.last_event_time["fadiga"]) >= self.event_cooldown_s:
                should_record = True
                event_type = "fadiga"
                self.last_event_time["fadiga"] = now

        # Prioridade 2: Distracao (somente se nao estiver disparando fadiga)
        if not should_record and distraction_crossed:
            # Desvio de atencao so gera evento se o motorista nao estiver com fadiga ativa
            if not (
                self.eyes_tracker.is_threshold_reached
                or self.yawn_tracker.is_threshold_reached
            ):
                if (now - self.last_event_time["distracao"]) >= self.event_cooldown_s:
                    should_record = True
                    event_type = "distracao"
                    self.last_event_time["distracao"] = now

        # Determinacao do estado visual do condutor e severidade (score)
        driver_state: str
        score: float = 0.0

        if (
            self.eyes_tracker.is_threshold_reached
            or self.yawn_tracker.is_threshold_reached
        ):
            driver_state = "FADIGA"
            # Calcula score continuo escalando de 0.70 a 1.0
            if self.eyes_tracker.is_threshold_reached:
                excess = max(0.0, eyes_dur - self.eyes_closed_duration_s)
                score = min(1.0, 0.70 + 0.30 * (excess / 1.5))
            else:
                excess = max(0.0, yawn_dur - self.yawn_duration_s)
                score = min(1.0, 0.70 + 0.30 * (excess / 1.5))

        elif self.distraction_tracker.is_threshold_reached:
            driver_state = "DISTRAIDO"
            excess = max(0.0, distr_dur - self.distraction_duration_s)
            score = min(1.0, 0.70 + 0.30 * (excess / 1.5))

        elif self.eyes_tracker.is_accumulating:
            driver_state = "PISCANDO"
            score = 0.0

        elif self.yawn_tracker.is_accumulating:
            driver_state = "BOCEJANDO"
            score = 0.0

        else:
            driver_state = "NORMAL"
            score = 0.0

        # Regra contratual: se should_record=False, event_type deve ser None
        if not should_record:
            event_type = None

        return DetectionResult(
            driver_state=driver_state,
            should_record=should_record,
            event_type=event_type,
            score=round(score, 3),
            eyes_closed_duration=round(eyes_dur, 3),
            yawn_duration=round(yawn_dur, 3),
            distraction_duration=round(distr_dur, 3),
        )
