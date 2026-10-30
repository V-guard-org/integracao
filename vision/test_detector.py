"""Regressões temporais: olhos auxiliares, eventos de pose e bocejo."""
from dataclasses import replace
import unittest

from detector import DriverStateDetector
from metrics import FrameMetrics


NORMAL = FrameMetrics(.3, .3, .3, .01, 0, 0, 0)


def sequence(detector, metrics, duration=3, step=.1):
    return [detector.update(metrics, now=round(i * step, 6))
            for i in range(round(duration / step) + 1)]


class DetectorTests(unittest.TestCase):
    def test_low_ear_and_calibrated_closure_alone_do_not_trigger(self):
        for closure in (None, True, False):
            raw = replace(NORMAL, ear=.09, ear_left=.09, ear_right=.09, eye_closure=closure)
            results = sequence(DriverStateDetector(), raw)
            self.assertTrue(all(r.driver_state == 'NORMAL' and not r.should_record for r in results))
            self.assertFalse(results[-1].eye_support)

    def test_sustained_distraction_fires_once_at_two_seconds(self):
        results = sequence(DriverStateDetector(), replace(NORMAL, yaw=30))
        fired = [(i, r) for i, r in enumerate(results) if r.should_record]
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][0], 20)
        self.assertEqual(fired[0][1].event_type, 'distracao')
        self.assertEqual(fired[0][1].score, .7)

    def test_eyes_only_add_capped_support_after_pose_threshold(self):
        raw = replace(NORMAL, pitch=25, eye_closure=True)
        results = sequence(DriverStateDetector(), raw)
        self.assertTrue(all(not r.should_record and not r.eye_support for r in results[:20]))
        self.assertEqual(results[20].event_type, 'distracao')
        self.assertEqual(results[20].score, .75)
        self.assertTrue(results[20].eye_support)
        self.assertLessEqual(results[-1].score, 1)

    def test_unknown_eye_signal_immediately_removes_support(self):
        detector = DriverStateDetector()
        raw = replace(NORMAL, pitch=25, eye_closure=True)
        sequence(detector, raw, 2)
        result = detector.update(replace(raw, eye_closure=None), 2.1)
        self.assertEqual(result.driver_state, 'DISTRAIDO')
        self.assertFalse(result.eye_support)
        self.assertEqual(result.eyes_closed_duration, 0)
        result = detector.update(raw, 2.2)
        self.assertFalse(result.eye_support)

    def test_yawn_still_triggers_fatigue(self):
        results = sequence(DriverStateDetector(), replace(NORMAL, mar=.75))
        fired = [(i, r) for i, r in enumerate(results) if r.should_record]
        self.assertEqual([(i, r.event_type) for i, r in fired], [(15, 'fadiga')])

    def test_yawn_priority_does_not_consume_later_distraction_event(self):
        detector = DriverStateDetector()
        results = sequence(detector, replace(NORMAL, yaw=30, mar=.75))
        self.assertEqual([r.event_type for r in results if r.should_record], ['fadiga'])
        detector.update(replace(NORMAL, yaw=30), 3.1)
        result = detector.update(replace(NORMAL, yaw=30), 3.2)
        self.assertTrue(result.should_record)
        self.assertEqual(result.event_type, 'distracao')

    def test_low_fps_stall_reset_and_episode_reset(self):
        detector = DriverStateDetector()
        raw = replace(NORMAL, yaw=30)
        results = sequence(detector, raw, 2.4, .2)  # 5 FPS: duração em segundos
        self.assertEqual(sum(r.should_record for r in results), 1)
        result = detector.update(raw, 10)  # pausa não conta como observação
        self.assertFalse(result.should_record)
        self.assertEqual(result.distraction_duration, 0)
        detector.reset()
        self.assertEqual(detector.update(raw, 10.1).distraction_duration, 0)

    def test_glitch_no_face_and_recovery(self):
        detector = DriverStateDetector()
        raw = replace(NORMAL, yaw=30)
        sequence(detector, raw, 1, .05)
        self.assertEqual(detector.update(None, 1.05).driver_state, 'SEM_ROSTO')
        self.assertGreater(detector.update(raw, 1.1).distraction_duration, 1)
        detector.update(None, 1.2)
        detector.update(None, 1.4)
        self.assertEqual(detector.update(raw, 1.5).distraction_duration, 0)


if __name__ == '__main__':
    unittest.main()
