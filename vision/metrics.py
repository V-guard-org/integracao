"""
Funcoes matematicas puras e sem estado (stateless) para extracao de metricas faciais:
- EAR (Eye Aspect Ratio) com correcao de proporcao em pixels
- MAR (Mouth Aspect Ratio) em pixels
- Head Pose (Pitch, Yaw, Roll) via cv2.solvePnP e decomposicao de Euler (cv2.RQDecomp3x3)
"""

from dataclasses import dataclass
import math
from typing import Any, Sequence, Tuple
import cv2
import numpy as np

from config import (
    FACE_3D_MODEL,
    HEAD_POSE_LANDMARK_INDICES,
    LEFT_EYE_INDICES,
    MOUTH_INDICES,
    RIGHT_EYE_INDICES,
)


# ==============================================================================
# 1. CONTRATO DE SAÍDA (FrameMetrics)
# ==============================================================================

@dataclass(frozen=True)
class FrameMetrics:
    """
    Contrato imutavel contendo as metricas calculadas para um frame:
    - ear_left: Abertura do olho esquerdo
    - ear_right: Abertura do olho direito
    - ear: Media de abertura dos dois olhos
    - mar: Abertura bucal
    - pitch: Inclinacao vertical da cabeca em graus (+ para baixo, - para cima)
    - yaw: Rotacao horizontal da cabeca em graus (+ para direita, - para esquerda)
    - roll: Inclinacao lateral em graus (+ inclinado para a direita, - para a esquerda)
    """
    ear_left: float
    ear_right: float
    ear: float
    mar: float
    pitch: float
    yaw: float
    roll: float
    # None = sinal ocular sem referência/confiabilidade; só a calibração preenche.
    eye_closure: bool | None = None


# ==============================================================================
# 2. AUXILIARES GEOMÉTRICOS E DESNORMALIZAÇÃO
# ==============================================================================

def to_pixel_coords(landmark: Any, width: int, height: int) -> Tuple[float, float]:
    """
    Converte coordenadas normalizadas [0.0, 1.0] (NormalizedLandmark do MediaPipe)
    para coordenadas em pixels reais.
    """
    return float(landmark.x * width), float(landmark.y * height)


# ==============================================================================
# 3. FUNÇÕES MATEMÁTICAS PURAS
# ==============================================================================

def _calculate_single_eye_ear(
    landmarks: Sequence[Any], indices: Sequence[int], width: int, height: int
) -> float:
    """
    Calcula o EAR para um unico olho a partir de 6 pontos em pixels:
    EAR = (|p2 - p6| + |p3 - p5|) / (2 * |p1 - p4|)
    """
    p1, p2, p3, p4, p5, p6 = (to_pixel_coords(landmarks[i], width, height) for i in indices)

    vertical_1 = math.dist(p2, p6)
    vertical_2 = math.dist(p3, p5)
    horizontal = math.dist(p1, p4)

    if horizontal <= 1e-6:
        return 0.0

    return (vertical_1 + vertical_2) / (2.0 * horizontal)


def calculate_ear(
    landmarks: Sequence[Any], width: int, height: int
) -> Tuple[float, float, float]:
    """
    Calcula o EAR em pixels para ambos os olhos e a media.
    Retorna (ear_left, ear_right, ear_avg).
    """
    ear_right = _calculate_single_eye_ear(landmarks, RIGHT_EYE_INDICES, width, height)
    ear_left = _calculate_single_eye_ear(landmarks, LEFT_EYE_INDICES, width, height)
    ear_avg = (ear_left + ear_right) / 2.0

    return ear_left, ear_right, ear_avg


def calculate_mar(landmarks: Sequence[Any], width: int, height: int) -> float:
    """
    Calcula o MAR (Mouth Aspect Ratio) em pixels usando os 4 pontos cardeais:
    MAR = |p_top - p_bottom| / |p_left - p_right|
    """
    p_left = to_pixel_coords(landmarks[MOUTH_INDICES[0]], width, height)
    p_right = to_pixel_coords(landmarks[MOUTH_INDICES[1]], width, height)
    p_top = to_pixel_coords(landmarks[MOUTH_INDICES[2]], width, height)
    p_bottom = to_pixel_coords(landmarks[MOUTH_INDICES[3]], width, height)

    horizontal = math.dist(p_left, p_right)
    vertical = math.dist(p_top, p_bottom)

    if horizontal <= 1e-6:
        return 0.0

    return vertical / horizontal


def calculate_head_pose(
    landmarks: Sequence[Any], width: int, height: int
) -> Tuple[float, float, float]:
    """
    Estima a orientacao da cabeca em graus (Pitch, Yaw, Roll) via solvePnP.
    
    Matriz intrinseca aproximada da camera:
    - fx = fy = width
    - cx = width / 2, cy = height / 2
    - Coeficientes de distorcao nulos
    
    Usa cv2.solvePnP com o modelo 3D canônico e cv2.RQDecomp3x3 para decomposicao
    estavel dos angulos de Euler em graus:
    - Olhar reto para frente resulta em pitch ~ 0°, yaw ~ 0°, roll ~ 0°.
    
    Retorna (pitch, yaw, roll).
    """
    pts_2d = np.array(
        [
            to_pixel_coords(landmarks[idx], width, height)
            for idx in HEAD_POSE_LANDMARK_INDICES
        ],
        dtype=np.float64,
    )

    focal_length = float(width)
    center_x = float(width) / 2.0
    center_y = float(height) / 2.0

    camera_matrix = np.array(
        [
            [focal_length, 0.0, center_x],
            [0.0, focal_length, center_y],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    dist_coeffs = np.zeros((4, 1), dtype=np.float64)

    success, rvec, _ = cv2.solvePnP(
        FACE_3D_MODEL,
        pts_2d,
        camera_matrix,
        dist_coeffs,
        flags=cv2.SOLVEPNP_ITERATIVE,
    )

    if not success:
        # Uma falha não pode virar uma amostra de pose neutra na calibração.
        return math.nan, math.nan, math.nan

    rotation_matrix, _ = cv2.Rodrigues(rvec)
    angles, _, _, _, _, _ = cv2.RQDecomp3x3(rotation_matrix)

    pitch, yaw, roll = map(float, angles)
    return pitch, yaw, roll


def extract_all_metrics(
    landmarks: Sequence[Any], width: int, height: int
) -> FrameMetrics:
    """
    Funcao fachada sem estado que calcula todas as metricas geometricas do frame:
    EAR (esquerdo, direito, medio), MAR e Head Pose (Pitch, Yaw, Roll).
    """
    ear_left, ear_right, ear_avg = calculate_ear(landmarks, width, height)
    mar = calculate_mar(landmarks, width, height)
    pitch, yaw, roll = calculate_head_pose(landmarks, width, height)

    return FrameMetrics(
        ear_left=ear_left,
        ear_right=ear_right,
        ear=ear_avg,
        mar=mar,
        pitch=pitch,
        yaw=yaw,
        roll=roll,
    )
