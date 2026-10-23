"""
Script de download do modelo oficial MediaPipe Face Landmarker.

Modelo: face_landmarker.task
Origem: Google MediaPipe Models Repository
Destino padrao: edge/vguard/vision/models/face_landmarker.task
"""

import argparse
import sys
import urllib.request
from pathlib import Path

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/"
    "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
)


def download_model(force: bool = False) -> Path:
    """
    Baixa o modelo face_landmarker.task caso ainda nao exista localmente.
    """
    base_dir = Path(__file__).resolve().parent.parent
    models_dir = base_dir / "models"
    models_dir.mkdir(parents=True, exist_ok=True)

    dest_file = models_dir / "face_landmarker.task"

    if dest_file.exists() and not force:
        size_mb = dest_file.stat().st_size / (1024 * 1024)
        print(f"[OK] Modelo ja presente em: {dest_file} ({size_mb:.2f} MB)")
        return dest_file

    print(f"Baixando modelo Face Landmarker de:\n  {MODEL_URL}")
    print(f"Destino:\n  {dest_file}")

    def reporthook(block_num, block_size, total_size):
        downloaded = block_num * block_size
        if total_size > 0:
            percent = min(100.0, downloaded * 100 / total_size)
            downloaded_mb = downloaded / (1024 * 1024)
            total_mb = total_size / (1024 * 1024)
            sys.stdout.write(
                f"\rProgresso: {percent:5.1f}% [{downloaded_mb:.2f} / {total_mb:.2f} MB]"
            )
            sys.stdout.flush()

    try:
        urllib.request.urlretrieve(MODEL_URL, dest_file, reporthook=reporthook)
        print("\n[SUCESSO] Download concluido com sucesso!")
    except Exception as e:
        if dest_file.exists():
            dest_file.unlink()
        print(f"\n[ERRO] Falha ao baixar o modelo: {e}", file=sys.stderr)
        sys.exit(1)

    return dest_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download do modelo MediaPipe Face Landmarker para o V-GUARD"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Forca o download mesmo se o arquivo ja existir",
    )
    args = parser.parse_args()

    download_model(force=args.force)
