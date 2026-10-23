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


# ==============================================================================
# 2. AUXILIARES GEOMÉTRICOS E DESNORMALIZAÇÃO
# ==============================================================================

def to_pixel_coords(landmark: Any, width: int, height: int) -> Tuple[float, float]:
    """
    Converte coordenadas normalizadas [0.0, 1.0] para coordenadas em pixels reais.
    Compativel com objetos MediaPipe (NormalizedLandmark), dicionarios, tuplas ou listas.
    """
    if hasattr(landmark, "x") and hasattr(landmark, "y"):
        return float(landmark.x * width), float(landmark.y * height)
    elif isinstance(landmark, dict):
        return float(landmark["x"] * width), float(landmark["y"] * height)
    elif isinstance(landmark, (tuple, list)) and len(landmark) >= 2:
        return float(landmark[0] * width), float(landmark[1] * height)
    raise TypeError(f"Tipo de landmark nao suportado: {type(landmark)}")


def euclidean_distance_2d(p1: Tuple[float, float], p2: Tuple[float, float]) -> float:
    """
    Calcula a distancia euclidiana entre dois pontos 2D em coordenadas de pixels.
    """
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


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
    p1 = to_pixel_coords(landmarks[indices[0]], width, height)
    p2 = to_pixel_coords(landmarks[indices[1]], width, height)
    p3 = to_pixel_coords(landmarks[indices[2]], width, height)
    p4 = to_pixel_coords(landmarks[indices[3]], width, height)
    p5 = to_pixel_coords(landmarks[indices[4]], width, height)
    p6 = to_pixel_coords(landmarks[indices[5]], width, height)

    vertical_1 = euclidean_distance_2d(p2, p6)
    vertical_2 = euclidean_distance_2d(p3, p5)
    horizontal = euclidean_distance_2d(p1, p4)

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

    horizontal = euclidean_distance_2d(p_left, p_right)
    vertical = euclidean_distance_2d(p_top, p_bottom)

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
        return 0.0, 0.0, 0.0

    rotation_matrix, _ = cv2.Rodrigues(rvec)
    angles, _, _, _, _, _ = cv2.RQDecomp3x3(rotation_matrix)

    pitch = float(angles[0])
    yaw = float(angles[1])
    roll = float(angles[2])

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
