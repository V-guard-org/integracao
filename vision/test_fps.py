"""
Testes da medição do FPS da visão (fps.py), com instantes inventados. Executar:

    python3 test_fps.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fps import FpsMeter


def feed(meter, instants):
    """Passa os instantes ao medidor e devolve as médias que ele fechou."""
    return [fps for fps in map(meter.frame, instants) if fps is not None]


class FpsMeterTest(unittest.TestCase):
    def test_steady_stream_gives_one_average_every_ten_seconds(self):
        # 14 quadros por segundo, de 100 s a 125 s: fecha em 110 s e em 120 s.
        instants = [100 + i / 14 for i in range(14 * 25 + 1)]
        self.assertEqual([round(fps, 3) for fps in feed(FpsMeter(), instants)], [14.0, 14.0])

    def test_run_average_includes_the_unfinished_period(self):
        # 10 s a 10 FPS e depois 5 s a 20 FPS: 200 quadros em 15 s.
        instants = [i / 10 for i in range(101)] + [10 + i / 20 for i in range(1, 101)]
        meter = FpsMeter()
        self.assertEqual([round(fps, 3) for fps in feed(meter, instants)], [10.0])
        self.assertAlmostEqual(meter.average, 200 / 15)

    def test_run_average_is_zero_before_two_frames(self):
        meter = FpsMeter()
        self.assertEqual(meter.average, 0.0)
        meter.frame(5.0)
        self.assertEqual(meter.average, 0.0)


if __name__ == "__main__":
    unittest.main()
