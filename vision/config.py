"""
Configuracoes e parametros do modulo de visao computacional do V-GUARD.

Centraliza caminhos, dimensoes de captura, indices de landmarks do MediaPipe Face Mesh,
modelo 3D canônico para estimativa de Head Pose e limiares temporais e geometricos.

NOTA: Os limiares de EAR, MAR, Yaw e Pitch sao valores INICIAIS de referencia e serao
calibrados experimentalmente nas etapas de validacao do V-GUARD.
"""

from pathlib import Path
import numpy as np

# ==============================================================================
# 1. CAMINHOS E CÂMERA
# ==============================================================================

BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = BASE_DIR / "models" / "face_landmarker.task"

# Resolução padrão de captura e inferência (equilíbrio ideal para Raspberry Pi 4)
FRAME_WIDTH: int = 640
FRAME_HEIGHT: int = 480

# Parâmetros do MediaPipe Face Landmarker
MIN_FACE_DETECTION_CONFIDENCE: float = 0.5
MIN_FACE_PRESENCE_CONFIDENCE: float = 0.5
MIN_TRACKING_CONFIDENCE: float = 0.5


# ==============================================================================
# 2. ÍNDICES DE LANDMARKS (MediaPipe Face Mesh - 478 pontos)
# ==============================================================================

# Olho direito (visão anatômica do sujeito; aparece à esquerda na imagem)
# P1: canto externo (33), P2: pálpebra superior ext (160), P3: pálpebra superior int (158),
# P4: canto interno (133), P5: pálpebra inferior int (153), P6: pálpebra inferior ext (144)
RIGHT_EYE_INDICES: list[int] = [33, 160, 158, 133, 153, 144]

# Olho esquerdo (visão anatômica do sujeito; aparece à direita na imagem)
# P1: canto interno (362), P2: pálpebra superior int (385), P3: pálpebra superior ext (387),
# P4: canto externo (263), P5: pálpebra inferior ext (373), P6: pálpebra inferior int (380)
LEFT_EYE_INDICES: list[int] = [362, 385, 387, 263, 373, 380]

# Boca (4 pontos cardeais internos dos lábios)
# [comissura_direita(33/61), comissura_esquerda(291), labio_superior(13), labio_inferior(14)]
# 61: canto labial direito (lado esquerdo da imagem)
# 291: canto labial esquerdo (lado direito da imagem)
# 13: lábio superior central interno
# 14: lábio inferior central interno
MOUTH_INDICES: list[int] = [61, 291, 13, 14]

# Pontos faciais chave utilizados no solvePnP para estimativa de Head Pose
# 1: Ponta do nariz
# 152: Queixo
# 263: Canto externo do olho esquerdo (lado direito da imagem)
# 33: Canto externo do olho direito (lado esquerdo da imagem)
# 61: Canto labial direito (lado esquerdo da imagem)
# 291: Canto labial esquerdo (lado direito da imagem)
HEAD_POSE_LANDMARK_INDICES: list[int] = [1, 152, 263, 33, 61, 291]


# ==============================================================================
# 3. MODELO 3D FACIAL CANÔNICO (FACE_3D_MODEL)
# ==============================================================================

# Coordenadas antropométricas genéricas (X, Y, Z) em milímetros correspondentes
# aos 6 landmarks em HEAD_POSE_LANDMARK_INDICES.
# Convencao do sistema de coordenadas:
# +X: direita na imagem (lado esquerdo do sujeito)
# +Y: para baixo na imagem (direcao ao queixo)
# +Z: para frente (em direcao a camera; o nariz fica em Z=0 e olhos/queixo em Z negativo)
FACE_3D_MODEL = np.array(
    [
        [0.0, 0.0, 0.0],       # 1: Ponta do nariz
        [0.0, 110.0, -50.0],    # 152: Queixo
        [75.0, -60.0, -50.0],   # 263: Canto externo olho esquerdo
        [-75.0, -60.0, -50.0],  # 33: Canto externo olho direito
        [-40.0, 60.0, -30.0],   # 61: Canto labial direito
        [40.0, 60.0, -30.0],    # 291: Canto labial esquerdo
    ],
    dtype=np.float64,
)


# ==============================================================================
# 4. LIMIARES TEMPORAIS E GEOMÉTRICOS (Para uso no detector.py)
# ==============================================================================

# Limiares geométricos de referência
EAR_THRESHOLD: float = 0.22       # Limiar abaixo do qual os olhos sao considerados fechados
MAR_THRESHOLD: float = 0.60       # Limiar acima do qual a boca e considerada aberta (bocejo)
YAW_THRESHOLD: float = 25.0       # Graus de rotação horizontal para caracterizar distração
PITCH_THRESHOLD: float = 20.0     # Graus de rotação vertical para caracterizar cabeça baixa/adormecimento

# Limiares temporais em segundos
EYES_CLOSED_DURATION_S: float = 1.0   # Fechamento contínuo para classificar como sonolência/microssono
YAWN_DURATION_S: float = 1.5          # Abertura bucal contínua para diferenciar bocejo de fala
DISTRACTION_DURATION_S: float = 2.0   # Desvio contínuo de rotação para classificar como distração

# Tolerância a ruído e controle de eventos
GRACE_PERIOD_S: float = 0.15          # Tolerância a ruído ou interrupção breve de sinal
EVENT_COOLDOWN_S: float = 5.0         # Cooldown mínimo independente entre eventos do mesmo tipo
