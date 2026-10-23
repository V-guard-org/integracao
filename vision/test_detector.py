"""
Suite de testes deterministicos para edge/vguard/vision/detector.py:
1. Piscada rapida (0.25s) -> driver_state="PISCANDO", should_record=False.
2. Fechamento prolongado (1.2s) -> should_record=True, event_type="fadiga", 0.70 <= score <= 1.0 exatamente aos 1.0s, should_record=False nos instantes seguintes.
3. Falha de 1 frame (0.05s) durante fechamento ocular -> mantem acumulador vivo pelo GRACE_PERIOD_S.
4. Bocejo sustentado (1.6s) -> dispara event_type="fadiga" aos 1.5s.
5. Desvio de cabeca (yaw=30° por 2.1s) -> dispara event_type="distracao" aos 2.0s.
6. Prioridade de Fadiga sobre Distracao.
7. Cooldown de 5.0s independente por tipo de evento.
8. Tratamento de frame sem rosto (metrics is None).
"""

import sys
from pathlib import Path

# Garante importacao local
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    DISTRACTION_DURATION_S,
    EYES_CLOSED_DURATION_S,
    GRACE_PERIOD_S,
    YAWN_DURATION_S,
)
from detector import DetectionResult, DriverStateDetector
from metrics import FrameMetrics


def make_metrics(
    ear: float = 0.30,
    mar: float = 0.15,
    pitch: float = 0.0,
    yaw: float = 0.0,
    roll: float = 0.0,
) -> FrameMetrics:
    """Cria objeto FrameMetrics com valores padrao de repouso."""
    return FrameMetrics(
        ear_left=ear,
        ear_right=ear,
        ear=ear,
        mar=mar,
        pitch=pitch,
        yaw=yaw,
        roll=roll,
    )


def test_quick_blink():
    """1. Piscada rápida (0.25s) -> driver_state='PISCANDO', should_record=False."""
    detector = DriverStateDetector()
    closed_metrics = make_metrics(ear=0.10)
    normal_metrics = make_metrics(ear=0.30)

    # Simula 0.25s de olhos fechados em passos de 0.05s
    for i in range(6):
        t = i * 0.05
        res = detector.update(closed_metrics, now=t)
        assert res.driver_state == "PISCANDO", f"Esperado PISCANDO em t={t:.2f}, obteve {res.driver_state}"
        assert res.should_record is False
        assert res.event_type is None
        assert res.score == 0.0

    # Abre os olhos e aguarda expirar a tolerancia de histerese
    t = 0.30
    detector.update(normal_metrics, now=t)
    t = 0.30 + GRACE_PERIOD_S + 0.05
    res = detector.update(normal_metrics, now=t)
    assert res.driver_state == "NORMAL"
    assert res.eyes_closed_duration == 0.0
    print("[OK] Teste 1: Piscada rapida (0.25s) validada.")


def test_prolonged_eye_closure():
    """
    2. Fechamento prolongado (1.2s):
    - should_record=True, event_type='fadiga', 0.70 <= score <= 1.0 exatamente aos 1.0s.
    - should_record=False em 1.1s e 1.2s mantendo FADIGA.
    """
    detector = DriverStateDetector()
    closed_metrics = make_metrics(ear=0.10)

    recorded_at: list[float] = []
    scores_at_record: list[float] = []

    # Avanca de 0.0s a 1.2s em passos de 0.05s
    steps = int(1.2 / 0.05) + 1
    for i in range(steps):
        t = round(i * 0.05, 3)
        res = detector.update(closed_metrics, now=t)

        if t < EYES_CLOSED_DURATION_S:
            assert res.should_record is False
            assert res.driver_state == "PISCANDO"
        elif abs(t - EYES_CLOSED_DURATION_S) < 1e-4:
            # Exatamente no limiar (1.0s)
            assert res.should_record is True, f"Esperado should_record=True em t={t}"
            assert res.event_type == "fadiga"
            assert 0.70 <= res.score <= 1.0, f"Score esperado >= 0.70, obteve {res.score}"
            assert res.driver_state == "FADIGA"
            recorded_at.append(t)
            scores_at_record.append(res.score)
        else:
            # Instantes posteriores (1.05s, 1.1s, 1.2s)
            assert res.should_record is False, f"Esperado should_record=False em t={t}"
            assert res.event_type is None
            assert res.driver_state == "FADIGA"
            assert res.score >= 0.70

    assert len(recorded_at) == 1, f"Deveria gravar em exatamente 1 frame, gravou em {recorded_at}"
    assert recorded_at[0] == 1.0
    print(f"[OK] Teste 2: Fechamento prolongado (1.2s) validado. Gravado em t={recorded_at[0]}s com score={scores_at_record[0]}.")


def test_one_frame_glitch_tolerance():
    """3. Falha de 1 frame (0.05s) durante fechamento ocular -> mantem acumulador vivo."""
    detector = DriverStateDetector()
    closed_metrics = make_metrics(ear=0.10)

    # 0.0s a 0.50s: olhos fechados
    for i in range(11):
        t = round(i * 0.05, 3)
        detector.update(closed_metrics, now=t)

    # t=0.55s: falha de 1 frame (rosto perdido / metrics=None) por 0.05s <= GRACE_PERIOD_S (0.15s)
    res_glitch = detector.update(None, now=0.55)
    assert res_glitch.driver_state == "SEM_ROSTO"
    assert res_glitch.eyes_closed_duration >= 0.50, "Duracao acumulada nao deve zerar no frame com falha breve"

    # t=0.60s: olhos fechados retornam normalmente
    res_resume = detector.update(closed_metrics, now=0.60)
    assert res_resume.driver_state == "PISCANDO"
    assert res_resume.eyes_closed_duration >= 0.60, "Contagem deve continuar a partir do inicio do episodio"

    # Continua ate 1.00s e verifica disparo
    triggered = False
    for i in range(13, 22):
        t = round(i * 0.05, 3)
        res = detector.update(closed_metrics, now=t)
        if res.should_record:
            assert res.event_type == "fadiga"
            triggered = True

    assert triggered, "O fechamento deveria ter disparado evento mesmo com a falha de 1 frame"
    print("[OK] Teste 3: Tolerancia a falha de 1 frame (histerese) validada.")


def test_sustained_yawn():
    """4. Bocejo sustentado (1.6s) -> dispara event_type='fadiga' aos 1.5s."""
    detector = DriverStateDetector()
    yawn_metrics = make_metrics(mar=0.75, ear=0.30)

    recorded_at: list[float] = []
    steps = int(1.6 / 0.05) + 1
    for i in range(steps):
        t = round(i * 0.05, 3)
        res = detector.update(yawn_metrics, now=t)

        if t < YAWN_DURATION_S:
            assert res.should_record is False
            assert res.driver_state == "BOCEJANDO"
        elif abs(t - YAWN_DURATION_S) < 1e-4:
            assert res.should_record is True
            assert res.event_type == "fadiga"
            assert res.driver_state == "FADIGA"
            assert 0.70 <= res.score <= 1.0
            recorded_at.append(t)
        else:
            assert res.should_record is False
            assert res.event_type is None
            assert res.driver_state == "FADIGA"

    assert len(recorded_at) == 1
    assert recorded_at[0] == 1.5
    print(f"[OK] Teste 4: Bocejo sustentado (1.6s) validado. Gravado aos {recorded_at[0]}s.")


def test_distraction_head_pose():
    """5. Desvio de cabeca (yaw=30° por 2.1s) -> dispara event_type='distracao' aos 2.0s."""
    detector = DriverStateDetector()
    distracted_metrics = make_metrics(yaw=30.0, ear=0.30, mar=0.15)

    recorded_at: list[float] = []
    steps = int(2.1 / 0.05) + 1
    for i in range(steps):
        t = round(i * 0.05, 3)
        res = detector.update(distracted_metrics, now=t)

        if t < DISTRACTION_DURATION_S:
            assert res.should_record is False
            assert res.driver_state == "NORMAL"
        elif abs(t - DISTRACTION_DURATION_S) < 1e-4:
            assert res.should_record is True
            assert res.event_type == "distracao"
            assert res.driver_state == "DISTRAIDO"
            assert 0.70 <= res.score <= 1.0
            recorded_at.append(t)
        else:
            assert res.should_record is False
            assert res.event_type is None
            assert res.driver_state == "DISTRAIDO"

    assert len(recorded_at) == 1
    assert recorded_at[0] == 2.0
    print(f"[OK] Teste 5: Desvio de cabeca (2.1s) validado. Gravado aos {recorded_at[0]}s.")


def test_fatigue_priority_over_distraction():
    """6. Prioridade: Se fadiga e distracao ocorrerem juntas, fadiga tem precedencia."""
    detector = DriverStateDetector()
    both_metrics = make_metrics(ear=0.10, yaw=30.0)

    # Ambos acumulam a partir de t=0.0s
    for i in range(21):
        t = round(i * 0.05, 3)
        res = detector.update(both_metrics, now=t)
        if res.should_record:
            # Como olhos fechados atinge 1.0s antes de distracao (2.0s), fadiga dispara
            assert res.event_type == "fadiga"
            assert t == 1.0

    # Continua ate 2.0s em passos de 0.05s (quando distracao atingiria limiar)
    for i in range(21, 41):
        t = round(i * 0.05, 3)
        res_2s = detector.update(both_metrics, now=t)

    # Nao deve disparar distracao porque fadiga continua ativa e tem precedencia
    assert res_2s.should_record is False
    assert res_2s.driver_state == "FADIGA"
    print("[OK] Teste 6: Precedencia de fadiga sobre distracao validada.")


def test_no_face_handling():
    """7. Tratamento de frame sem rosto (metrics is None)."""
    detector = DriverStateDetector()
    res = detector.update(None, now=1.0)
    assert res.driver_state == "SEM_ROSTO"
    assert res.should_record is False
    assert res.event_type is None
    assert res.score == 0.0
    print("[OK] Teste 7: Tratamento de frame sem rosto validado.")


def run_all_tests():
    print("=" * 65)
    print("TESTE DE VERIFICAÇÃO: detector.py (V-GUARD)")
    print("=" * 65)

    test_quick_blink()
    test_prolonged_eye_closure()
    test_one_frame_glitch_tolerance()
    test_sustained_yawn()
    test_distraction_head_pose()
    test_fatigue_priority_over_distraction()
    test_no_face_handling()

    print("\n" + "=" * 65)
    print("TODOS OS 7 TESTES DO DETECTOR PASSARAM COM SUCESSO!")
    print("=" * 65)


if __name__ == "__main__":
    run_all_tests()
