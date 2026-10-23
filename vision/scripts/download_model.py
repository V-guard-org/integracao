"""
Script de download do modelo oficial MediaPipe Face Landmarker.

Modelo: face_landmarker.task
Origem: Google MediaPipe Models Repository
Destino: vision/models/face_landmarker.task (para baixar de novo, apague o arquivo)
"""

import sys
import urllib.request
from pathlib import Path

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)


def download_model() -> Path:
    """
    Baixa o modelo face_landmarker.task caso ainda nao exista localmente.
    """
    base_dir = Path(__file__).resolve().parent.parent
    models_dir = base_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    dest_file = models_dir / "face_landmarker.task"

    if dest_file.exists():
        size_mb = dest_file.stat().st_size / (1024 * 1024)
        print(f"[OK] Modelo ja presente em: {dest_file} ({size_mb:.2f} MB)")
        return dest_file

    print(f"Baixando modelo Face Landmarker de:\n  {MODEL_URL}")
    print(f"Destino:\n  {dest_file}")

    try:
        urllib.request.urlretrieve(MODEL_URL, dest_file)
        print("[SUCESSO] Download concluido com sucesso!")
    except Exception as e:
        if dest_file.exists():
            dest_file.unlink()
        print(f"\n[ERRO] Falha ao baixar o modelo: {e}", file=sys.stderr)
        sys.exit(1)

    return dest_file


if __name__ == "__main__":
    download_model()
