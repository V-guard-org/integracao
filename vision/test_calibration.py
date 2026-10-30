"""Regressões da montagem lateral, assistente e perfil. Sem câmera física."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from calibration import CalibrationProfile, CalibrationSession, relative_pose
from detector import DriverStateDetector
from metrics import FrameMetrics, calculate_head_pose


def sample(left=.32, right=.25, pose=(-20, 40, -12)):
    return FrameMetrics(left, right, (left + right) / 2, .01, *pose)


def compose(reference, change):
    # Matriz independente: Rodrigues para rotações nos eixos e RQDecomp3x3.
    def rotation(angles):
        p, y, r = np.radians(angles)
        rx = cv2.Rodrigues(np.array([p, 0., 0.]))[0]
        ry = cv2.Rodrigues(np.array([0., y, 0.]))[0]
        rz = cv2.Rodrigues(np.array([0., 0., r]))[0]
        return rz @ ry @ rx
    return cv2.RQDecomp3x3(rotation(reference) @ rotation(change))[0]


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.opened = [sample() for _ in range(31)]
        self.closed = [sample(.09, .07) for _ in range(31)]
        self.profile = CalibrationProfile.from_neutral(self.opened, 640, 480)

    def test_relative_rotation_cancels_mounting_and_preserves_motion(self):
        reference = (-23.4, 37.1, -12)
        for motion in ((0, 0, 0), (12, 30, 5), (-25, -35, 8), (0, 0, 170)):
            with self.subTest(motion=motion):
                np.testing.assert_allclose(relative_pose(reference, compose(reference, motion)), motion, atol=1e-6)

    def test_normal_screenshot_metrics_do_not_trigger_events_after_pose_calibration(self):
        for raw in (sample(.21, .10, (-23.4, 37.1, -12)),
                    sample(.43, .35, (-13.3, 42.2, -11.2))):
            detector = DriverStateDetector()
            for tick in range(61):
                result = detector.update(self.profile.apply(raw), tick / 10)
                self.assertEqual(result.driver_state, "NORMAL")
                self.assertFalse(result.should_record)

    def test_eye_separation_and_ambiguous_or_occluded_eye(self):
        calibrated = self.profile.with_eyes(self.opened, self.closed)
        self.assertIs(calibrated.apply(sample()).eye_closure, False)
        self.assertIs(calibrated.apply(sample(.09, .07)).eye_closure, True)
        for raw in (sample(.09, .25), sample(.2, .16), sample(0, 0),
                    sample(.09, .07, compose(self.profile.neutral_pose, (0, 40, 0)))):
            self.assertIsNone(calibrated.apply(raw).eye_closure)

    def test_eye_samples_overlap_or_head_moves_keep_pose_usable(self):
        for closed in (self.opened, [sample(.09, .07, (-5.2, 23.2, -13.5))] * 31):
            with self.assertRaises(ValueError):
                self.profile.with_eyes(self.opened, closed)
        self.assertIsNone(self.profile.left_eye)
        self.assertAlmostEqual(self.profile.apply(sample()).yaw, 0)

    def test_unstable_reference_rejected(self):
        with self.assertRaises(ValueError):
            CalibrationProfile.from_neutral(self.opened + [sample(pose=(0, 0, 0))] * 20, 640, 480)

    def test_profile_round_trip_and_invalid_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration-driver.json"
            for profile in (self.profile, self.profile.with_eyes(self.opened, self.closed)):
                profile.save(path)
                self.assertEqual(CalibrationProfile.load(path), profile)
            for data in ({"version": 999}, {"version": 2, "neutral_pose": None}, [],
                         {"version": 2, "width": 640, "height": 480, "neutral_pose": [0, float('nan'), 0]}):
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    CalibrationProfile.load(path)

    def collect(self, session, start, raw):
        for tick in range(31):
            self.assertIsNone(session.update(raw, start + tick / 10, 640, 480))

    def test_guided_flow_requires_keys_and_never_returns_metrics_while_collecting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration-driver.json"
            session = CalibrationSession(path)
            self.assertIsNone(session.update(sample(), 0, 640, 480))
            session.key(ord('c'), 0)
            self.assertIsNone(session.update(sample(), 2, 640, 480))
            self.collect(session, 3, sample())
            self.assertEqual(session.state, "wait_closed")
            self.assertFalse(path.exists())
            session.key(ord('f'), 7)
            self.collect(session, 10, sample(.09, .07))
            self.assertEqual(session.state, "wait_ready")
            self.assertFalse(path.exists())
            session.key(ord('s'), 14)
            self.assertTrue(path.exists())
            self.assertFalse(session.active)
            self.assertIs(session.update(sample(), 14, 640, 480).eye_closure, False)
            loaded = CalibrationSession(path)
            self.assertAlmostEqual(loaded.update(sample(), 0, 640, 480).yaw, 0)
            session.key(ord('c'), 15)
            self.assertTrue(session.active)
            self.assertIsNone(session.update(sample(), 15, 640, 480))

    def test_failed_eyes_can_be_skipped(self):
        session = CalibrationSession(recalibrate=True)
        session.key(ord('c'), 0)
        self.collect(session, 3, sample())
        session.key(ord('f'), 7)
        self.collect(session, 10, sample())
        self.assertEqual(session.state, "wait_closed")
        session.key(ord('s'), 14)
        self.assertFalse(session.active)
        self.assertIsNone(session.update(sample(.09, .07), 14, 640, 480).eye_closure)

    def test_lost_face_and_stalled_stream_restart_collection(self):
        session = CalibrationSession(recalibrate=True)
        session.key(ord('c'), 0)
        for tick in range(20):
            session.update(sample(), 3 + tick / 10, 640, 480)
        session.update(None, 5, 640, 480)
        self.assertEqual(session.opened, [])
        session.update(sample(), 6, 640, 480)
        session.update(sample(), 7, 640, 480)
        self.assertEqual(len(session.opened), 1)
        self.assertEqual(session.state, "neutral")

    def test_resolution_context_and_failed_pose_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'calibration-driver.json'
            self.profile.save(path)
            with self.assertRaises(ValueError):
                CalibrationSession(path, context='infrared=True')
            session = CalibrationSession(path)
            with self.assertRaises(ValueError):
                session.update(sample(), 0, 1280, 720)
        raw = replace(sample(), pitch=float('nan'))
        self.assertIsNone(CalibrationSession().update(raw, 0, 640, 480))
        from types import SimpleNamespace
        with patch('metrics.cv2.solvePnP', return_value=(False, None, None)):
            pose = calculate_head_pose([SimpleNamespace(x=.5, y=.5)] * 478, 640, 480)
        self.assertTrue(all(np.isnan(pose)))


if __name__ == '__main__':
    unittest.main()
