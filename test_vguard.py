"""Contratos de inicialização e encerramento; executar com python -B test_vguard.py."""

import threading
import unittest
from unittest.mock import Mock, patch

import vguard


class ApplicationLifecycle(unittest.TestCase):
    def test_camera_only_never_constructs_uart_or_sync(self):
        manager = Mock()
        with patch.object(vguard, "EventManager", return_value=manager), \
             patch.object(vguard, "UartReader") as uart, \
             patch.object(vguard, "Sync") as sync, \
             patch.object(vguard, "VisionPipeline") as pipeline:
            pipeline.return_value.run.return_value = 1
            self.assertEqual(vguard.run(["--headless", "--no-uart", "--no-sync", "--max-frames", "10"]), 1)
            uart.assert_not_called()
            sync.assert_not_called()
            self.assertIs(pipeline.call_args.kwargs["event_manager"], manager)
            self.assertEqual(pipeline.call_args.kwargs["max_frames"], 10)
            manager.close.assert_called_once()

    def test_initialization_failure_stops_workers_before_closing_database(self):
        order = []
        manager, reader, sync = Mock(), Mock(), Mock()
        manager.close.side_effect = lambda: order.append("database")
        reader.stop.side_effect = lambda: order.append("uart")
        sync.stop.side_effect = lambda: order.append("sync")
        with patch.object(vguard, "EventManager", return_value=manager), \
             patch.object(vguard, "UartReader", return_value=reader), \
             patch.object(vguard, "Sync", return_value=sync), \
             patch.object(vguard, "VisionPipeline", side_effect=RuntimeError("model unavailable")):
            with self.assertRaisesRegex(RuntimeError, "model unavailable"):
                vguard.run(["--headless"])
        self.assertEqual(order, ["uart", "sync", "database"])

    def test_interrupt_closes_database(self):
        manager = Mock()
        with patch.object(vguard, "EventManager", return_value=manager), \
             patch.object(vguard, "VisionPipeline") as pipeline:
            pipeline.return_value.run.side_effect = KeyboardInterrupt
            vguard.run(["--headless", "--no-uart", "--no-sync"])
            manager.close.assert_called_once()

    def test_landmarker_failure_releases_camera(self):
        pipeline = object.__new__(vguard.VisionPipeline)
        camera = Mock()
        with patch.object(pipeline, "_open_capture", return_value=camera), \
             patch.object(pipeline, "_init_landmarker", side_effect=RuntimeError("initialization failed")):
            with self.assertRaisesRegex(RuntimeError, "initialization failed"):
                pipeline.run()
        camera.release.assert_called_once()

    def test_sync_stop_waits_for_worker(self):
        service = vguard.Sync(Mock())
        exited = threading.Event()

        def worker():
            service._stop.wait()
            exited.set()

        service.run = worker
        service.start()
        service.stop()
        self.assertTrue(exited.is_set())
        self.assertFalse(service._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
