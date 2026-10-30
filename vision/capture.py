"""Captura BGR: câmera CSI via Picamera2 ou webcam/vídeo via OpenCV.

Picamera2/libcamera são importados apenas para a fonte CSI. Assim o mesmo
pipeline continua funcionando com vídeos sem dependências da Raspberry Pi.
"""

import os
from pathlib import Path
import select
import struct
import subprocess
import time
from typing import Any, Optional, Protocol, Union


_HEADER = struct.Struct("!4sIII")
_MAGIC = b"VGC1"


class Capture(Protocol):
    def read(self) -> tuple[bool, Any]: ...

    def release(self) -> None: ...


class Picamera2Capture:
    """Adapta a câmera CSI ao contrato read()/release() do pipeline."""

    def __init__(self, width: int, height: int, fps: float = 30.0,
                 camera_num: int = 0, infrared: bool = False):
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            raise RuntimeError(
                "A fonte picamera2 requer python3-picamera2/libcamera do Raspberry Pi OS. "
                "Instale com sudo apt install python3-picamera2 e use um ambiente "
                "compatível com o Python do sistema e --system-site-packages, ou "
                "selecione --camera-python /usr/bin/python3 para captura isolada."
            ) from exc

        self._camera = None
        self._started = False
        try:
            self._camera = Picamera2(camera_num=camera_num)
            controls = {"FrameRate": fps, "AeEnable": True}
            if infrared:
                # Perfil óptico, sem acionar LEDs ou o filtro IR-CUT da placa.
                controls.update(AwbEnable=False, ColourGains=(1.0, 1.0))
            config = self._camera.create_video_configuration(
                # Em libcamera, RGB888 entrega bytes B,G,R ao NumPy/OpenCV.
                main={"size": (width, height), "format": "RGB888"},
                controls=controls,
                buffer_count=4,
                queue=False,
            )
            self._camera.configure(config)
            # Sem preview nativo: o HUD opcional continua no OpenCV.
            self._camera.start()
            self._started = True
        except BaseException:
            self.release()
            raise

    def read(self) -> tuple[bool, Any]:
        if self._camera is None:
            return False, None
        # Falhas CSI propagam como erro; não são confundidas com fim de arquivo.
        frame = self._camera.capture_array("main")
        if frame is None or frame.size == 0:
            raise RuntimeError("A câmera CSI retornou um frame vazio")
        return True, frame

    def release(self) -> None:
        camera, self._camera = self._camera, None
        if camera is not None:
            try:
                if self._started:
                    camera.stop()
            finally:
                self._started = False
                camera.close()


def open_capture(source: Union[int, str], width: int, height: int,
                 fps: float = 30.0, camera_num: int = 0,
                 infrared: bool = False, camera_python: Optional[str] = None) -> Capture:
    if source == "picamera2":
        if camera_python:
            return CameraProcessCapture(camera_python, width, height, fps, camera_num, infrared)
        return Picamera2Capture(width, height, fps, camera_num, infrared)

    import cv2

    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"Nao foi possivel abrir a fonte de video: {source}")
    if isinstance(source, int):
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_FPS, fps)
    return cap


class CameraProcessCapture:
    """Captura com o Python do sistema quando libcamera e MediaPipe usam ABIs distintas.

    O filho só captura; inferência e eventos permanecem na aplicação principal.
    O pipe transmite BGR sem perdas e limita o acúmulo de frames por backpressure.
    """

    def __init__(self, executable: str, width: int, height: int, fps: float,
                 camera_num: int, infrared: bool):
        command = [executable, "-u", str(Path(__file__).resolve()),
                   "--width", str(width), "--height", str(height),
                   "--fps", str(fps), "--camera-num", str(camera_num)]
        if infrared:
            command.append("--infrared")
        self._process = subprocess.Popen(command, stdout=subprocess.PIPE, bufsize=0)

    def _read_exact(self, size: int, deadline: float) -> bytes:
        output = bytearray()
        stream = self._process.stdout
        while len(output) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([stream], [], [], max(0, remaining))[0]:
                raise RuntimeError("Timeout aguardando frame do processo CSI")
            chunk = os.read(stream.fileno(), min(size - len(output), 65536))
            if not chunk:
                raise RuntimeError("O processo CSI encerrou sem completar o frame; confira os erros acima")
            output.extend(chunk)
        return bytes(output)

    def read(self) -> tuple[bool, Any]:
        if self._process is None:
            return False, None
        import numpy as np

        deadline = time.monotonic() + 15
        magic, width, height, size = _HEADER.unpack(self._read_exact(_HEADER.size, deadline))
        if magic != _MAGIC or not width or not height or size != width * height * 3 or size > 64 * 1024 * 1024:
            raise RuntimeError("Cabeçalho BGR inválido do processo CSI")
        # A cópia torna o array gravável para o HUD, sem mudar a ordem de canais.
        frame = np.frombuffer(self._read_exact(size, deadline), dtype=np.uint8)
        return True, frame.reshape(height, width, 3).copy()

    def release(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            finally:
                process.stdout.close()


def _camera_worker() -> None:
    """Entrada interna: executada com o Python que fornece Picamera2/libcamera."""
    import argparse
    import signal
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--fps", type=float, required=True)
    parser.add_argument("--camera-num", type=int, required=True)
    parser.add_argument("--infrared", action="store_true")
    args = parser.parse_args()

    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    cap = Picamera2Capture(args.width, args.height, args.fps, args.camera_num, args.infrared)
    try:
        while True:
            _, frame = cap.read()
            if frame.ndim != 3 or frame.shape[2] != 3 or str(frame.dtype) != "uint8":
                raise RuntimeError("Picamera2 não entregou um array BGR uint8")
            height, width = frame.shape[:2]
            data = frame.tobytes()
            sys.stdout.buffer.write(_HEADER.pack(_MAGIC, width, height, len(data)))
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
    except BrokenPipeError:
        pass
    finally:
        cap.release()


if __name__ == "__main__":
    _camera_worker()
