"""Regressões da montagem lateral, assistente e perfil. Sem câmera física."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from calibration import (CHECKS, KEYS_LIMIT, LINE_LIMIT, CalibrationProfile, CalibrationSession,
                         GuidedCheck, relative_pose, wrap)
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


def check_prompt(test, prompt):
    """Cada tela: só ASCII, linhas curtas e, fora do rodapé, alguma instrução."""
    texts = (prompt.step, *prompt.lines, prompt.keys, prompt.feedback, prompt.results)
    for text in texts:
        test.assertTrue(text.isascii(), text)
    for line in prompt.lines:
        test.assertLessEqual(len(line), LINE_LIMIT, line)
    test.assertLessEqual(len(prompt.keys), KEYS_LIMIT, prompt.keys)
    test.assertTrue(prompt.step)
    if not prompt.compact:
        test.assertTrue(prompt.lines, prompt)
    test.assertIn(prompt.tone, ("info", "ok", "error"))


class PromptTests(unittest.TestCase):
    """A tela da calibração (Issue #7): o que fazer em cada estado, sem cortar texto."""

    def setUp(self):
        self.opened = [sample() for _ in range(31)]

    def collect(self, session, start, raw):
        for tick in range(31):
            session.update(raw, start + tick / 10, 640, 480)

    def test_every_state_says_what_to_do(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration-driver.json"
            session = CalibrationSession(path)
            seen = {}

            def look(now):
                prompt = session.prompt(now)
                check_prompt(self, prompt)
                seen[session.state] = prompt
                return prompt

            prompt = look(0)
            self.assertEqual(prompt.step, "Passo 1 de 2: postura")
            self.assertIn("C: comecar", prompt.keys)
            session.key(ord('c'), 0)
            self.assertEqual(look(0.2).countdown, 3)
            self.assertEqual(look(2.5).countdown, 1)
            for now in (3, 3.3, 3.6, 3.9, 4.2, 4.5):       # a 0,3 s: sem lacuna na coleta
                session.update(sample(), now, 640, 480)
            prompt = look(4.5)
            self.assertEqual(session.state, "neutral")
            self.assertAlmostEqual(prompt.progress, .5)
            session.update(None, 4.6, 640, 480)
            self.assertEqual(look(4.6).lines[0], "Rosto fora da imagem.")
            self.collect(session, 5, sample())
            prompt = look(9)
            self.assertEqual((session.state, prompt.step, prompt.tone), ("wait_closed", "Passo 2 de 2: olhos (opcional)", "ok"))
            self.assertIn("F: medir os olhos", prompt.keys)
            session.key(ord('f'), 9)
            self.assertIn("Feche os olhos", " ".join(look(9.5).lines))
            self.collect(session, 12, sample(.09, .07))
            self.assertEqual(session.state, "wait_ready")
            look(16)
            self.assertTrue(session.key(ord('s'), 16))
            prompt = look(16)
            self.assertTrue(prompt.compact)
            self.assertIn("V: conferir", prompt.keys)
            self.assertEqual(set(seen), {"wait_neutral", "neutral_delay", "neutral", "wait_closed",
                                         "closed_delay", "wait_ready", "ready"})

    def test_errors_explain_what_to_do(self):
        session = CalibrationSession(recalibrate=True)
        session.key(ord('c'), 0)
        self.collect(session, 3, sample())
        session.key(ord('f'), 7)
        self.collect(session, 10, sample())          # olhos "fechados" iguais aos abertos
        prompt = session.prompt(14)
        check_prompt(self, prompt)
        self.assertEqual((session.state, prompt.tone), ("wait_closed", "error"))
        self.assertIn("parecidos", " ".join(prompt.lines))
        self.assertIn("S: salvar so a postura", prompt.keys)

        unstable = CalibrationSession(recalibrate=True)
        unstable.key(ord('c'), 0)
        for tick in range(31):
            pose = (-20, 40, -12) if tick % 2 else (0, 0, 0)
            unstable.update(sample(pose=pose), 3 + tick / 10, 640, 480)
        prompt = unstable.prompt(7)
        check_prompt(self, prompt)
        self.assertEqual((unstable.state, prompt.tone), ("error", "error"))
        self.assertEqual(prompt.lines[-1], "Aperte C e olhe para a estrada.")

    def test_eyes_can_be_measured_again_after_success(self):
        session = CalibrationSession(recalibrate=True)
        session.key(ord('c'), 0)
        self.collect(session, 3, sample())
        session.key(ord('f'), 7)
        self.collect(session, 10, sample(.09, .07))
        self.assertEqual(session.state, "wait_ready")
        session.key(ord('f'), 14)
        self.assertEqual(session.state, "closed_delay")
        self.assertFalse(session.key(ord('x'), 15))

    def test_wrap(self):
        self.assertEqual(wrap("A cabeca mexeu com os olhos fechados", 20),
                         ("A cabeca mexeu com", "os olhos fechados"))
        self.assertEqual(wrap(""), ())


class GuidedCheckTests(unittest.TestCase):
    """A conferência guiada (Issue #7), com estados do detector inventados."""

    def run_check(self, check, start, states, metrics=None):
        """Um quadro a cada 0,1 s: devolve o instante seguinte."""
        now = start
        for state in states:
            check.begin_frame(now)
            check.observe(state, metrics or sample(), now)
            now = round(now + .1, 3)
        return now

    def test_three_checks_pass(self):
        check = GuidedCheck(0)
        self.assertTrue(check.blocking)
        self.assertTrue(check.begin_frame(1))            # preparação: zerar o detector
        now = self.run_check(check, 3, ["NORMAL"] * 5 + ["DISTRAIDO"])
        self.assertEqual(check.results, {"lado": "OK"})
        self.assertEqual(check.phase, "prep")
        now = self.run_check(check, now + 3, ["BOCEJANDO"] * 5 + ["FADIGA"])
        self.assertEqual(check.results["bocejo"], "OK")
        now = self.run_check(check, now + 3, ["NORMAL"] * 31)
        self.assertEqual(check.results["estrada"], "OK")
        self.assertFalse(check.blocking)
        self.assertTrue(check.passed)
        prompt = check.prompt(now)
        check_prompt(self, prompt)
        self.assertEqual((prompt.tone, prompt.results), ("ok", "Lado OK | Bocejo OK | Estrada OK"))
        self.assertTrue(check.prompt(now + 8).compact)

    def test_timeout_fails_and_moves_on(self):
        check = GuidedCheck(0)
        now = self.run_check(check, 3, ["NORMAL"] * 101)
        self.assertEqual(check.results, {"lado": "FALHOU"})
        self.assertEqual((check.index, check.phase), (1, "prep"))
        prompt = check.prompt(now)
        check_prompt(self, prompt)
        self.assertEqual(prompt.tone, "error")

    def test_state_during_prep_does_not_count(self):
        check = GuidedCheck(0)
        self.run_check(check, 0, ["DISTRAIDO"] * 29)     # ainda na preparação (3 s)
        self.assertEqual(check.results, {})
        self.assertEqual(check.phase, "prep")

    def test_road_needs_three_seconds_in_a_row(self):
        check = GuidedCheck(0)
        check.index = 2
        now = self.run_check(check, 3, ["NORMAL"] * 25 + ["BOCEJANDO"] + ["NORMAL"] * 25)
        self.assertNotIn("estrada", check.results)
        self.run_check(check, now, ["NORMAL"] * 6)
        self.assertEqual(check.results["estrada"], "OK")

    def test_feedback_and_lost_face(self):
        check = GuidedCheck(0, yaw_limit=25, mar_limit=.6)
        self.run_check(check, 3, ["NORMAL"], metrics=sample(pose=(0, -12.4, 0)))
        prompt = check.prompt(3.1)
        check_prompt(self, prompt)
        self.assertEqual(prompt.feedback, "Lado: 12 de 25 graus")
        check.observe("SEM_ROSTO", None, 3.2)
        self.assertEqual(check.prompt(3.2).feedback, "Rosto fora da imagem: vire menos.")
        check.index = 1
        check.observe("BOCEJANDO", replace(sample(), mar=.45), 3.3)
        self.assertEqual(check.prompt(3.3).feedback, "Boca: 0.45 de 0.60")

    def test_every_prompt_fits(self):
        check = GuidedCheck(0)
        now = 0.0
        while check.blocking and now < 60:
            check.begin_frame(now)
            check.observe("NORMAL", sample(), now)
            check_prompt(self, check.prompt(now))
            now = round(now + .5, 3)
        self.assertEqual(len(CHECKS), 3)
        check_prompt(self, check.prompt(now))


if __name__ == '__main__':
    unittest.main()
