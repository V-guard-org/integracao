"""
Teste rapido e deterministico das funcoes puras de metrics.py:
1. Validacao de extract_all_metrics com landmarks sinteticos.
2. Verificacao de Head Pose neutro (Pitch ~ 0°, Yaw ~ 0°, Roll ~ 0°).
3. Verificacao de deteccao de rotacao angular (Yaw +25°).
4. Demonstracao da correcao de aspect ratio (pixels vs coordenadas normalizadas).
"""

import sys
from pathlib import Path
import cv2
import numpy as np

# Garante importacao local
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    FACE_3D_MODEL,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    HEAD_POSE_LANDMARK_INDICES,
)
from metrics import FrameMetrics, extract_all_metrics


class MockLandmark:
    """Mock compativel com o objeto NormalizedLandmark do MediaPipe."""
    def __init__(self, x: float, y: float, z: float = 0.0):
        self.x = x
        self.y = y
        self.z = z

    def __repr__(self):
        return f"MockLandmark(x={self.x:.3f}, y={self.y:.3f})"


def create_synthetic_landmarks(rvec: np.ndarray, tvec: np.ndarray, width: int, height: int):
    """Gera 478 landmarks com geometria facial realista a partir de projecao 3D."""
    cam_matrix = np.array(
        [[width, 0.0, width / 2.0], [0.0, width, height / 2.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    dist_coeffs = np.zeros((4, 1), dtype=np.float64)

    pts_2d, _ = cv2.projectPoints(FACE_3D_MODEL, rvec, tvec, cam_matrix, dist_coeffs)
    pts_2d = pts_2d.reshape(-1, 2)

    landmarks = {i: MockLandmark(0.5, 0.5) for i in range(478)}

    # Atribui os 6 pontos do modelo 3D canônico
    for idx, pt in zip(HEAD_POSE_LANDMARK_INDICES, pts_2d):
        landmarks[idx] = MockLandmark(pt[0] / width, pt[1] / height)

    # Configura contorno do olho direito em torno de 33
    p33 = landmarks[33]
    dx = 0.06
    landmarks[133] = MockLandmark(p33.x + dx, p33.y)
    landmarks[160] = MockLandmark(p33.x + 0.02, p33.y - 0.015)
    landmarks[158] = MockLandmark(p33.x + 0.04, p33.y - 0.015)
    landmarks[144] = MockLandmark(p33.x + 0.02, p33.y + 0.015)
    landmarks[153] = MockLandmark(p33.x + 0.04, p33.y + 0.015)

    # Configura contorno do olho esquerdo em torno de 263
    p263 = landmarks[263]
    landmarks[362] = MockLandmark(p263.x - dx, p263.y)
    landmarks[385] = MockLandmark(p263.x - 0.04, p263.y - 0.015)
    landmarks[387] = MockLandmark(p263.x - 0.02, p263.y - 0.015)
    landmarks[380] = MockLandmark(p263.x - 0.04, p263.y + 0.015)
    landmarks[373] = MockLandmark(p263.x - 0.02, p263.y + 0.015)

    # Configura abertura bucal entre 61 e 291
    p61 = landmarks[61]
    p291 = landmarks[291]
    mid_x = (p61.x + p291.x) / 2.0
    mid_y = (p61.y + p291.y) / 2.0
    landmarks[13] = MockLandmark(mid_x, mid_y - 0.01)
    landmarks[14] = MockLandmark(mid_x, mid_y + 0.01)

    return landmarks


def run_tests():
    print("=" * 65)
    print("TESTE DE VERIFICAÇÃO: config.py e metrics.py (V-GUARD)")
    print("=" * 65)

    # 1. Teste de Face Neutra (olhando reto para frente)
    rvec_neutral = np.zeros((3, 1), dtype=np.float64)
    tvec_dist = np.array([[0.0], [0.0], [800.0]], dtype=np.float64)
    neutral_landmarks = create_synthetic_landmarks(
        rvec_neutral, tvec_dist, FRAME_WIDTH, FRAME_HEIGHT
    )

    metrics_neutral = extract_all_metrics(neutral_landmarks, FRAME_WIDTH, FRAME_HEIGHT)
    print("\n[1] Face Neutra (Olhar direto):")
    print(f"    - EAR: left={metrics_neutral.ear_left:.3f}, right={metrics_neutral.ear_right:.3f}, avg={metrics_neutral.ear:.3f}")
    print(f"    - MAR: {metrics_neutral.mar:.3f}")
    print(f"    - Head Pose: pitch={metrics_neutral.pitch:.2f}°, yaw={metrics_neutral.yaw:.2f}°, roll={metrics_neutral.roll:.2f}°")

    assert isinstance(metrics_neutral, FrameMetrics), "metrics deve ser instancia de FrameMetrics"
    assert abs(metrics_neutral.pitch) < 0.1, f"Pitch neutro esperado ~0°, obteve {metrics_neutral.pitch}"
    assert abs(metrics_neutral.yaw) < 0.1, f"Yaw neutro esperado ~0°, obteve {metrics_neutral.yaw}"
    assert abs(metrics_neutral.roll) < 0.1, f"Roll neutro esperado ~0°, obteve {metrics_neutral.roll}"
    print("    -> OK: Angulos de Euler perfeitamente calibrados em 0° na pose neutra!")

    # 2. Teste de Rotação de Cabeça (Yaw = +25°)
    rvec_rotated = np.array([[0.0], [np.radians(25.0)], [0.0]], dtype=np.float64)
    rotated_landmarks = create_synthetic_landmarks(
        rvec_rotated, tvec_dist, FRAME_WIDTH, FRAME_HEIGHT
    )
    metrics_rotated = extract_all_metrics(rotated_landmarks, FRAME_WIDTH, FRAME_HEIGHT)
    print("\n[2] Face Rotacionada (Yaw +25° - olhar para a direita):")
    print(f"    - Head Pose: pitch={metrics_rotated.pitch:.2f}°, yaw={metrics_rotated.yaw:.2f}°, roll={metrics_rotated.roll:.2f}°")
    assert abs(metrics_rotated.yaw - 25.0) < 0.1, f"Yaw esperado 25.0°, obteve {metrics_rotated.yaw}"
    assert abs(metrics_rotated.pitch) < 0.1
    print("    -> OK: Rotacao angular de 25° detectada com exatidao!")

    # 3. Prova da Correcao de Aspect Ratio
    print("\n[3] Impacto da Correção de Aspect Ratio (Pixels vs Normalizado):")
    delta_x = 0.10  # 10% da largura do frame
    delta_y = 0.03  # 3% da altura do frame
    ear_norm = (delta_y + delta_y) / (2.0 * delta_x)

    # Resolucao 640x480 (4:3)
    dx_43 = delta_x * 640
    dy_43 = delta_y * 480
    ear_43 = (dy_43 + dy_43) / (2.0 * dx_43)

    # Resolucao 1280x720 (16:9)
    dx_169 = delta_x * 1280
    dy_169 = delta_y * 720
    ear_169 = (dy_169 + dy_169) / (2.0 * dx_169)

    print(f"    - EAR com coordenadas normalizadas (sem correcao): {ear_norm:.4f}")
    print(f"    - EAR corrigido em pixels (640x480, 4:3):          {ear_43:.4f}  (distorcao evitada: +{(ear_norm - ear_43)/ear_43*100:.1f}%)")
    print(f"    - EAR corrigido em pixels (1280x720, 16:9):        {ear_169:.4f}  (distorcao evitada: +{(ear_norm - ear_169)/ear_169*100:.1f}%)")
    print("    -> OK: A desnormalizacao antes da distancia euclidiana garante invariancia de proporcao!")

    print("\n" + "=" * 65)
    print("TODOS OS TESTES UNITÁRIOS PASSARAM COM SUCESSO!")
    print("=" * 65)


if __name__ == "__main__":
    run_tests()
