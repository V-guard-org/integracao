"""Regressões da montagem lateral, assistente e perfil. Sem câmera física."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from calibration import (CHECKS, DETAIL_LIMIT, KEYS_LIMIT, SPACE, CalibrationProfile,
                         CalibrationSession, GuidedCheck, relative_pose, wrap)
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

    def test_guided_flow_saves_by_itself_and_never_returns_metrics_while_collecting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration-driver.json"
            session = CalibrationSession(path)
            self.assertEqual(session.state, "wait_neutral")
            self.assertIsNone(session.update(sample(), 0, 640, 480))     # sem ESPAÇO, nada começa
            self.assertEqual(session.state, "wait_neutral")
            session.key(SPACE, 0)
            now = advance(self, session, 0, sample(), "neutral_ok")
            self.assertGreaterEqual(now, 6)                              # 3 s de contagem + 3 s de medida
            now = advance(self, session, now, sample(), "closed_delay")  # 2 s de OK na tela
            now = advance(self, session, now, sample(.09, .07), "closed_ok")
            self.assertFalse(path.exists())
            now = advance(self, session, now, sample(), "ready")         # grava sozinho
            self.assertTrue(path.exists())
            self.assertTrue(session.pop_saved())
            self.assertFalse(session.pop_saved())
            self.assertIs(session.update(sample(), now, 640, 480).eye_closure, False)
            loaded = CalibrationSession(path)
            self.assertAlmostEqual(loaded.update(sample(), 0, 640, 480).yaw, 0)
            session.key(ord('c'), now)
            self.assertTrue(session.active)
            self.assertIsNone(session.update(sample(), now, 640, 480))

    def test_eyes_can_be_skipped_with_n(self):
        session = CalibrationSession(recalibrate=True)
        session.key(SPACE, 0)
        now = advance(self, session, 0, sample(), "closed_delay")
        session.key(ord('n'), now)
        self.assertEqual(session.state, "ready")
        self.assertTrue(session.pop_saved())
        self.assertIsNone(session.profile.left_eye)
        self.assertIsNone(session.update(sample(.09, .07), now, 640, 480).eye_closure)

    def test_failed_eyes_can_be_measured_again_or_skipped(self):
        session = CalibrationSession(recalibrate=True)
        session.key(SPACE, 0)
        now = advance(self, session, 0, sample(), "closed_delay")
        now = advance(self, session, now, sample(), "wait_closed")      # "fechados" iguais aos abertos
        self.assertIn("parecidos", session.error)
        session.key(SPACE, now)
        self.assertEqual(session.state, "closed_delay")
        now = advance(self, session, now, sample(), "wait_closed")
        session.key(ord('n'), now)
        self.assertEqual(session.state, "ready")
        self.assertIsNone(session.profile.left_eye)

    def test_unstable_posture_asks_to_measure_again(self):
        session = CalibrationSession(recalibrate=True)
        session.key(SPACE, 0)
        for tick in range(61):
            pose = (-20, 40, -12) if tick % 2 else (0, 0, 0)
            session.update(sample(pose=pose), tick / 10, 640, 480)
        self.assertEqual(session.state, "error")
        session.key(SPACE, 7)
        self.assertEqual(session.state, "neutral_delay")

    def test_save_error_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration-driver.json"
            path.mkdir()                                  # um diretório no lugar do arquivo
            session = CalibrationSession(path, recalibrate=True)
            session.key(SPACE, 0)
            now = advance(self, session, 0, sample(), "closed_delay")
            session.key(ord('n'), now)
            self.assertEqual(session.state, "save_error")
            self.assertFalse(session.pop_saved())
            path.rmdir()
            session.key(SPACE, now)
            self.assertEqual(session.state, "ready")
            self.assertTrue(path.is_file())

    def test_lost_face_and_stalled_stream_restart_collection(self):
        session = CalibrationSession(recalibrate=True)
        session.key(SPACE, 0)
        for tick in range(20):
            session.update(sample(), 3 + tick / 10, 640, 480)
        session.update(None, 5, 640, 480)
        self.assertEqual(session.opened, [])
        self.assertTrue(session.face_lost)
        session.update(sample(), 6, 640, 480)
        session.update(sample(), 7, 640, 480)
        self.assertEqual(len(session.opened), 1)
        self.assertEqual(session.state, "neutral")
        self.assertFalse(session.face_lost)

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


def advance(test, session, now, raw, until, limit=30.0):
    """Um quadro a cada 0,1 s até o estado `until`; na calibração, nenhum quadro devolve métricas."""
    for _ in range(int(limit * 10)):
        test.assertIsNone(session.update(raw, now, 640, 480))
        now = round(now + .1, 3)
        if session.state == until:
            return now
    test.fail(f"nao chegou a {until}: parou em {session.state}")


def check_prompt(test, prompt):
    """Cada tela: só ASCII, o pedido curto, frases que cabem na coluna e alguma instrução."""
    texts = (prompt.step, prompt.title, *prompt.detail, prompt.keys, prompt.countdown,
             prompt.feedback, prompt.results)
    for text in texts:
        test.assertTrue(text.isascii(), text)
    test.assertTrue(prompt.title)
    test.assertLessEqual(len(prompt.title), 32, prompt.title)
    test.assertTrue(prompt.detail, prompt)
    for line in (*prompt.detail, prompt.countdown, prompt.feedback):
        test.assertLessEqual(len(line), DETAIL_LIMIT, line)
    test.assertLessEqual(len(prompt.keys), KEYS_LIMIT, prompt.keys)
    test.assertLessEqual(len(prompt.results), 48, prompt.results)
    test.assertIn(prompt.tone, ("info", "ok", "error", "neutral"))


class PromptTests(unittest.TestCase):
    """A coluna da calibração (Issue #7): o pedido da vez em cada estado, sem cortar texto."""

    def test_every_state_says_what_to_do(self):
        seen = {}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration-driver.json"
            session = CalibrationSession(path)

            def look(now, title=None, tone=None):
                prompt = session.prompt(now)
                check_prompt(self, prompt)
                seen[session.state] = prompt
                if title:
                    self.assertEqual(prompt.title.replace("\n", " "), title)
                if tone:
                    self.assertEqual(prompt.tone, tone)
                return prompt

            self.assertIn("ESPACO: comecar", look(0, "PRONTOS?", "neutral").keys)
            session.key(SPACE, 0)
            self.assertEqual(look(0.2, "PECA PARA OLHAR PARA A FRENTE", "info").countdown,
                             "A medida comeca em 3")
            self.assertEqual(look(2.5).countdown, "A medida comeca em 1")
            for now in (3, 3.3, 3.6, 3.9, 4.2, 4.5):        # a 0,3 s: sem lacuna na medida
                session.update(sample(), now, 640, 480)
            self.assertAlmostEqual(look(4.5, "MEDINDO A POSTURA").progress, .5)
            session.update(None, 4.6, 640, 480)
            look(4.6, "ROSTO FORA DA IMAGEM", "error")
            now = advance(self, session, 4.7, sample(), "neutral_ok")
            look(now, "OK! POSTURA MEDIDA", "ok")
            now = advance(self, session, now, sample(), "closed_delay")
            self.assertIn("N: pular os olhos", look(now, "PECA PARA FECHAR OS OLHOS").keys)
            now = advance(self, session, now, sample(.09, .07), "closed")
            look(now + .5, "MEDINDO OS OLHOS")
            now = advance(self, session, now, sample(.09, .07), "closed_ok")
            look(now, "OK! PODE ABRIR OS OLHOS", "ok")
            now = advance(self, session, now, sample(), "ready")
            self.assertIn("V: conferir", look(now, "MONITORANDO", "neutral").keys)
        self.assertEqual(set(seen), {"wait_neutral", "neutral_delay", "neutral", "neutral_ok",
                                     "closed_delay", "closed", "closed_ok", "ready"})

    def test_problems_say_what_happened_and_what_to_do(self):
        session = CalibrationSession(recalibrate=True)
        session.key(SPACE, 0)
        now = advance(self, session, 0, sample(), "closed_delay")
        now = advance(self, session, now, sample(), "wait_closed")
        prompt = session.prompt(now)
        check_prompt(self, prompt)
        self.assertEqual((prompt.title, prompt.tone), ("OLHOS NAO MEDIDOS", "error"))
        self.assertIn("parecidos", " ".join(prompt.detail))
        self.assertIn("N: pular os olhos", prompt.keys)

        session.state, session.error = "error", "A cabeca mexeu durante a medida"
        prompt = session.prompt(now)
        check_prompt(self, prompt)
        self.assertEqual((prompt.title, prompt.tone), ("MEDIR DE NOVO", "error"))
        self.assertIn("ESPACO: medir de novo", prompt.keys)

        session.state, session.error = "save_error", "O perfil nao foi gravado: Is a directory"
        prompt = session.prompt(now)
        check_prompt(self, prompt)
        self.assertEqual(prompt.title, "FALHA AO GRAVAR")

        prompt = CalibrationSession().prompt(0)
        check_prompt(self, prompt)
        self.assertEqual(prompt.title, "SEM CALIBRACAO")

    def test_wrap(self):
        self.assertEqual(wrap("A cabeca mexeu com os olhos fechados", 20),
                         ("A cabeca mexeu com", "os olhos fechados"))
        self.assertEqual(wrap(""), ())


class GuidedCheckTests(unittest.TestCase):
    """A conferência guiada (Issue #7), com estados do detector inventados."""

    def play(self, check, start, seconds, person):
        """Um quadro a cada 0,1 s; `person(check)` diz o estado do detector naquele quadro."""
        now = start
        for _ in range(round(seconds * 10)):
            check.begin_frame(now)
            check.observe(person(check), sample(), now)
            now = round(now + .1, 3)
        return now

    def test_three_checks_pass_when_the_person_does_what_is_asked(self):
        def person(check):
            if check.phase != "run":
                return "NORMAL"
            return {"lado": "DISTRAIDO", "bocejo": "FADIGA", "estrada": "NORMAL"}[check.step.name]

        check = GuidedCheck(0)
        self.assertTrue(check.blocking)
        now = self.play(check, 0, 40, person)
        self.assertEqual(check.results, {"lado": "OK", "bocejo": "OK", "estrada": "OK"})
        self.assertEqual(check.phase, "done")
        self.assertFalse(check.blocking)
        self.assertTrue(check.passed)
        prompt = check.prompt(now)
        check_prompt(self, prompt)
        self.assertEqual((prompt.title, prompt.tone), ("CALIBRACAO PRONTA", "ok"))
        self.assertEqual(prompt.results, "Lado OK | Bocejo OK | Estrada OK")
        self.assertIn("ESPACO: conferir de novo", prompt.keys)

    def test_one_check_from_warning_to_ok(self):
        check = GuidedCheck(0)
        self.assertTrue(check.begin_frame(0.5))           # aviso: o pipeline zera o detector
        prompt = check.prompt(0.5)
        self.assertEqual((prompt.title, prompt.detail), ("PREPARE-SE", ("Daqui a 3 s: pedir para virar o rosto.",)))
        self.assertEqual(check.prompt(2.5).detail, ("Daqui a 1 s: pedir para virar o rosto.",))
        self.assertFalse(check.begin_frame(3.0))
        prompt = check.prompt(3.5)
        check_prompt(self, prompt)
        self.assertEqual(prompt.title.replace("\n", " "), "PECA PARA VIRAR O ROSTO")
        self.assertIn("ate aparecer OK aqui.", prompt.detail)
        self.assertEqual((prompt.countdown, prompt.progress), ("", None))   # sem contagem no pedido
        self.assertIn("MAIS", " ".join(check.prompt(11.0).detail))          # depois de 8 s, insiste
        check.observe("DISTRAIDO", sample(), 12.0)
        prompt = check.prompt(12.1)
        self.assertEqual((prompt.title, prompt.tone), ("OK! PODE VOLTAR", "ok"))
        self.assertTrue(check.blocking)                   # o OK ainda bloqueia os eventos
        self.assertTrue(check.begin_frame(14.5))          # 2,5 s depois: aviso da prova seguinte
        self.assertEqual(check.step.name, "bocejo")

    def test_timeout_fails_and_moves_on(self):
        check = GuidedCheck(0)
        check.begin_frame(3.0)
        check.begin_frame(22.9)
        self.assertEqual(check.phase, "run")
        check.begin_frame(23.0)
        self.assertEqual(check.results, {"lado": "FALHOU"})
        prompt = check.prompt(23.0)
        check_prompt(self, prompt)
        self.assertEqual((prompt.title, prompt.tone), ("NAO DETECTOU", "error"))
        self.assertEqual(prompt.detail, ("Seguindo para a proxima prova.",))
        check.begin_frame(25.5)
        self.assertEqual((check.step.name, check.phase), ("bocejo", "prep"))

    def test_states_outside_the_request_do_not_count(self):
        check = GuidedCheck(0)
        check.begin_frame(1.0)
        check.observe("DISTRAIDO", sample(), 1.0)         # ainda no aviso
        self.assertEqual(check.results, {})
        check.begin_frame(3.0)
        check.observe("FADIGA", sample(), 3.0)            # pediu o rosto virado
        self.assertEqual(check.results, {})
        check.observe("DISTRAIDO", sample(), 3.1)
        check.observe("FADIGA", sample(), 3.2)            # já no resultado
        self.assertEqual(check.results, {"lado": "OK"})

    def test_road_needs_three_seconds_in_a_row(self):
        check = GuidedCheck(0)
        check.index = 2
        check.begin_frame(3.0)
        for now, state in ((3.0, "NORMAL"), (5.5, "NORMAL"), (5.75, "BOCEJANDO"),
                           (6.0, "NORMAL"), (8.5, "NORMAL")):
            check.observe(state, sample(), now)
        self.assertNotIn("estrada", check.results)
        check.observe("NORMAL", sample(), 9.0)
        self.assertEqual(check.results["estrada"], "OK")

    def test_skip_with_n(self):
        check = GuidedCheck(0)
        check.skip(1.0)
        self.assertEqual(check.results, {"lado": "PULADA"})
        self.assertEqual(check.prompt(1.0).title, "PROVA PULADA")
        check.skip(1.5)                                   # N no resultado: segue já
        check.begin_frame(1.6)
        self.assertEqual((check.step.name, check.phase), ("bocejo", "prep"))
        self.assertFalse(check.passed)

    def test_feedback_and_lost_face(self):
        check = GuidedCheck(0, yaw_limit=25, mar_limit=.6)
        check.begin_frame(3.0)
        check.observe("NORMAL", sample(pose=(0, -12.4, 0)), 3.1)
        self.assertEqual(check.prompt(3.1).feedback, "Lado: 12 de 25 graus")
        check.observe("SEM_ROSTO", None, 3.2)
        self.assertEqual(check.prompt(3.2).feedback, "Rosto fora da imagem: vire menos.")
        check.index = 1
        check.observe("BOCEJANDO", replace(sample(), mar=.45), 3.3)
        self.assertEqual(check.prompt(3.3).feedback, "Boca: 0.45 de 0.60")
        check.index = 2
        check.observe("NORMAL", sample(), 3.5)
        self.assertEqual(check.prompt(4.5).feedback, "Parado: 1.0 de 3 s")

    def test_failed_check_says_to_check_again(self):
        check = GuidedCheck(0)
        check.results = {"lado": "OK", "bocejo": "FALHOU", "estrada": "OK"}
        check.phase = "done"
        prompt = check.prompt(0)
        check_prompt(self, prompt)
        self.assertEqual((prompt.title, prompt.tone), ("CONFERIR", "error"))
        self.assertFalse(check.passed)

    def test_every_prompt_fits(self):
        for person in (lambda check: "NORMAL",                       # ninguém vira nem boceja
                       lambda check: "SEM_ROSTO"):
            check = GuidedCheck(0)
            now = 0.0
            while check.blocking and now < 120:
                check.begin_frame(now)
                check.observe(person(check), None if person(check) == "SEM_ROSTO" else sample(), now)
                check_prompt(self, check.prompt(now))
                now = round(now + .5, 3)
            self.assertEqual(check.phase, "done")
            check_prompt(self, check.prompt(now))
        self.assertEqual(len(CHECKS), 3)


if __name__ == '__main__':
    unittest.main()
