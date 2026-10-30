"""
Modulo de analise temporal e deteccao de estado do motorista do V-GUARD.

Processa metricas instantaneas (FrameMetrics) ao longo do tempo usando relogio
monotonico injetavel, aplicando histerese/tolerancia a ruido (grace period),
e trava de episodio (latch). O cooldown entre eventos fica no EventManager.
"""

from dataclasses import dataclass
from typing import Optional

from config import (
    DISTRACTION_DURATION_S,
    EAR_THRESHOLD,
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
    - driver_state: Estado visual atual (NORMAL, BOCEJANDO, FADIGA, DISTRAIDO, SEM_ROSTO)
    - should_record: True exclusivamente no frame exato em que o evento deve ser persistido
    - event_type: "fadiga" ou "distracao" quando should_record=True, ou None caso contrario
    - score: Severidade continua entre 0.0 e 1.0 (iniciando em 0.70 ao cruzar o limiar)
    - eyes_closed_duration: Tempo de sinal ocular baixo validado pela calibração (segundos)
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
    eye_support: bool = False


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
        self.last_update_time: Optional[float] = None
        self.previously_active = False

    def update(self, is_active: bool, now: float) -> float:
        """
        Atualiza o estado temporal com base na presenca da condicao no instante 'now'.
        Retorna a duracao acumulada em segundos.
        """
        if is_active:
            if self.start_time is None:
                # Inicio de nova acumulacao
                self.start_time = now
            elif (self.last_update_time is not None and (
                now - self.last_update_time > 0.5
                or (not self.previously_active and self.last_active_time is not None
                    and now - self.last_active_time > self.grace_period_s)
            )):
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

        self.last_update_time = now
        self.previously_active = is_active
        return self.duration

    def reset(self):
        self.start_time = self.last_active_time = self.last_update_time = None
        self.duration = 0.0
        self.episode_triggered = self.previously_active = False

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
    ):
        self.ear_threshold = ear_threshold  # compatibilidade: não decide fechamento isoladamente
        self.mar_threshold = mar_threshold
        self.yaw_threshold = yaw_threshold
        self.pitch_threshold = pitch_threshold

        self.eyes_closed_duration_s = eyes_closed_duration_s
        self.yawn_duration_s = yawn_duration_s
        self.distraction_duration_s = distraction_duration_s

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

    def reset(self) -> None:
        """Descarta episódios anteriores ao entrar ou sair de uma calibração."""
        for tracker in (self.eyes_tracker, self.yawn_tracker, self.distraction_tracker):
            tracker.reset()

    def update(self, metrics: Optional[FrameMetrics], now: float) -> DetectionResult:
        """
        Avalia as metricas do frame no instante 'now' (tempo monotonico em segundos).
        Retorna o resultado de deteccao com o estado e indicativo de gravacao.
        """
        # Caso 1: Rosto nao detectado no frame
        if metrics is None:
            self.eyes_tracker.reset()
            eyes_dur = 0.0
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
        is_eyes_closed = metrics.eye_closure is True
        if metrics.eye_closure is None:
            self.eyes_tracker.reset()
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
        yawn_crossed = self.yawn_tracker.should_trigger_episode()

        # Olhos são auxiliares: nunca disparam fadiga nem antecipam distração.
        fatigue = self.yawn_tracker.is_threshold_reached
        distraction_crossed = (not fatigue and self.distraction_tracker.should_trigger_episode())
        event_type: Optional[str] = None
        if yawn_crossed:
            event_type = "fadiga"
        elif distraction_crossed and not fatigue:
            event_type = "distracao"
        should_record = event_type is not None

        # Estado visual e severidade: 0.70 ao cruzar o limiar, ate 1.0 apos +1.5s
        def _score(duration: float, limit: float) -> float:
            return min(1.0, 0.70 + 0.30 * max(0.0, duration - limit) / 1.5)

        score = 0.0
        eye_support = (is_distracted and is_eyes_closed
                       and self.distraction_tracker.is_threshold_reached
                       and self.eyes_tracker.is_threshold_reached and not fatigue)
        if self.yawn_tracker.is_threshold_reached:
            driver_state = "FADIGA"
            score = _score(yawn_dur, self.yawn_duration_s)
        elif self.distraction_tracker.is_threshold_reached:
            driver_state = "DISTRAIDO"
            score = _score(distr_dur, self.distraction_duration_s)
            # Bônus experimental limitado; score não representa probabilidade.
            score = min(1.0, score + (0.05 if eye_support else 0.0))
        elif self.yawn_tracker.is_accumulating:
            driver_state = "BOCEJANDO"
        else:
            driver_state = "NORMAL"

        return DetectionResult(
            driver_state=driver_state,
            should_record=should_record,
            event_type=event_type,
            score=round(score, 3),
            eyes_closed_duration=round(eyes_dur, 3),
            yawn_duration=round(yawn_dur, 3),
            distraction_duration=round(distr_dur, 3),
            eye_support=eye_support,
        )
