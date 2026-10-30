"""Contratos CSI sem hardware; executar na Pi: python3 -B vision/test_capture.py."""

import sys
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from capture import CameraProcessCapture, Picamera2Capture, _HEADER, _MAGIC, open_capture


class CsiCaptureTests(unittest.TestCase):
    def setUp(self):
        self.camera = Mock()
        self.factory = Mock(return_value=self.camera)
        self.module_patch = patch.dict(sys.modules, {
            "picamera2": SimpleNamespace(Picamera2=self.factory),
        })
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def test_bgr_frame_reaches_pipeline_without_channel_swap(self):
        frame = SimpleNamespace(size=640 * 480 * 3)
        self.camera.capture_array.return_value = frame
        cap = open_capture("picamera2", 640, 480, fps=20, camera_num=1)
        self.factory.assert_called_once_with(camera_num=1)
        config = self.camera.create_video_configuration.call_args.kwargs
        self.assertEqual(config["main"], {"size": (640, 480), "format": "RGB888"})
        self.assertEqual(config["controls"]["FrameRate"], 20)
        self.assertTrue(config["controls"]["AeEnable"])
        self.assertNotIn("ColourGains", config["controls"])
        self.assertFalse(config["queue"])
        ok, actual = cap.read()
        self.assertTrue(ok)
        self.assertIs(actual, frame)
        self.camera.capture_array.assert_called_once_with("main")
        cap.release()
        cap.release()
        self.camera.stop.assert_called_once()
        self.camera.close.assert_called_once()
        self.assertEqual(cap.read(), (False, None))

    def test_ir_keeps_auto_exposure_and_fixes_colour_gains(self):
        cap = Picamera2Capture(640, 480, infrared=True)
        controls = self.camera.create_video_configuration.call_args.kwargs["controls"]
        self.assertTrue(controls["AeEnable"])
        self.assertFalse(controls["AwbEnable"])
        self.assertEqual(controls["ColourGains"], (1.0, 1.0))
        cap.release()

    def test_configuration_failure_closes_camera(self):
        self.camera.configure.side_effect = RuntimeError("bad config")
        with self.assertRaisesRegex(RuntimeError, "bad config"):
            Picamera2Capture(640, 480)
        self.camera.start.assert_not_called()
        self.camera.close.assert_called_once()

    def test_start_failure_closes_camera(self):
        self.camera.start.side_effect = RuntimeError("CSI unavailable")
        with self.assertRaisesRegex(RuntimeError, "CSI unavailable"):
            Picamera2Capture(640, 480)
        self.camera.close.assert_called_once()

    def test_stop_failure_still_closes_camera(self):
        cap = Picamera2Capture(640, 480)
        self.camera.stop.side_effect = RuntimeError("stop failed")
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            cap.release()
        self.camera.close.assert_called_once()
        cap.release()

    def test_capture_failure_propagates(self):
        cap = Picamera2Capture(640, 480)
        self.camera.capture_array.side_effect = RuntimeError("sensor disconnected")
        with self.assertRaisesRegex(RuntimeError, "sensor disconnected"):
            cap.read()
        cap.release()

    def test_empty_frame_is_an_error(self):
        cap = Picamera2Capture(640, 480)
        for frame in (None, SimpleNamespace(size=0)):
            self.camera.capture_array.return_value = frame
            with self.assertRaisesRegex(RuntimeError, "frame vazio"):
                cap.read()
        cap.release()

    def test_missing_picamera2_has_installation_guidance(self):
        with patch.dict(sys.modules, {"picamera2": None}):
            with self.assertRaisesRegex(RuntimeError, "python3-picamera2"):
                Picamera2Capture(640, 480)


class OpenCvCaptureTests(unittest.TestCase):
    def setUp(self):
        self.cv2 = Mock()
        self.module_patch = patch.dict(sys.modules, {"cv2": self.cv2, "picamera2": None})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def test_video_does_not_require_picamera2_or_change_properties(self):
        cap = open_capture("test.mp4", 640, 480)
        self.cv2.VideoCapture.assert_called_once_with("test.mp4")
        cap.set.assert_not_called()

    def test_webcam_receives_dimensions_and_fps(self):
        cap = open_capture(0, 640, 480, fps=15)
        cap.set.assert_any_call(self.cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set.assert_any_call(self.cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set.assert_any_call(self.cv2.CAP_PROP_FPS, 15)
        # C525: V4L2 direto no Linux e 1 buffer, para ler sempre o quadro mais novo.
        cap.set.assert_any_call(self.cv2.CAP_PROP_BUFFERSIZE, 1)
        if sys.platform.startswith("linux"):
            self.cv2.VideoCapture.assert_called_once_with(0, self.cv2.CAP_V4L2)

    def test_v4l2_device_path_is_a_webcam(self):
        cap = open_capture("/dev/v4l/by-id/usb-046d_C525-video-index0", 640, 480)
        cap.set.assert_any_call(self.cv2.CAP_PROP_BUFFERSIZE, 1)

    def test_failed_open_releases_capture(self):
        self.cv2.VideoCapture.return_value.isOpened.return_value = False
        with self.assertRaisesRegex(RuntimeError, "fonte de video"):
            open_capture(0, 640, 480)
        self.cv2.VideoCapture.return_value.release.assert_called_once()


class CameraProcessTests(unittest.TestCase):
    def setUp(self):
        self.process = Mock()
        self.process.poll.return_value = None
        self.popen_patch = patch("capture.subprocess.Popen", return_value=self.process)
        self.popen = self.popen_patch.start()
        self.addCleanup(self.popen_patch.stop)

    def make_capture(self):
        return open_capture("picamera2", 640, 480, infrared=True,
                            camera_python="/usr/bin/python3")

    def test_worker_receives_camera_options_and_bgr_frame_is_writable(self):
        cap = self.make_capture()
        command = self.popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/python3")
        self.assertIn("--infrared", command)
        with patch.object(cap, "_read_exact", side_effect=[_HEADER.pack(_MAGIC, 2, 1, 6),
                                                         bytes([25, 90, 210] * 2)]):
            ok, frame = cap.read()
        self.assertTrue(ok)
        self.assertEqual(frame.shape, (1, 2, 3))
        self.assertEqual(frame[0, 0].tolist(), [25, 90, 210])
        self.assertTrue(frame.flags.writeable)
        cap.release()
        cap.release()
        self.process.terminate.assert_called_once()
        self.process.stdout.close.assert_called_once()

    def test_invalid_header_fails_before_reading_frame(self):
        cap = self.make_capture()
        for header in (_HEADER.pack(b"oops", 2, 1, 6), _HEADER.pack(_MAGIC, 2, 1, 5)):
            with patch.object(cap, "_read_exact", return_value=header) as read:
                with self.assertRaisesRegex(RuntimeError, "Cabeçalho"):
                    cap.read()
                self.assertEqual(read.call_count, 1)
        cap.release()

    def test_unresponsive_worker_is_killed(self):
        cap = self.make_capture()
        self.process.wait.side_effect = [subprocess.TimeoutExpired("camera", 3), 0]
        cap.release()
        self.process.kill.assert_called_once()
        self.process.stdout.close.assert_called_once()

    def test_eof_and_timeout_are_errors(self):
        cap = self.make_capture()
        with patch("capture.select.select", return_value=([self.process.stdout], [], [])), \
             patch("capture.os.read", return_value=b""):
            with self.assertRaisesRegex(RuntimeError, "encerrou"):
                cap._read_exact(16, float("inf"))
        with patch("capture.select.select", return_value=([], [], [])):
            with self.assertRaisesRegex(RuntimeError, "Timeout"):
                cap._read_exact(16, float("inf"))
        cap.release()


if __name__ == "__main__":
    unittest.main()
