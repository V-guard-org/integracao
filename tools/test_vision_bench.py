"""
Testes da bancada guiada da visão (tools/vision_bench.py): a leitura do log e
a sequência dos pedidos, sem câmera, sem janela e sem o OpenCV.

    cd tools && python3 test_vision_bench.py
"""

import unittest

from vision_bench import ACTION, NORMAL, Script, ascii_text, clock, parse_line, status_lines


class ParseLineTest(unittest.TestCase):
    """Linhas copiadas do log real da Pi em 09/11."""

    def test_fps(self):
        line = "2026-11-09 18:27:20,343 INFO vision: visão: 15.0 FPS em 10 s\n"
        self.assertEqual(parse_line(line), ("fps", 15.0))

    def test_pi(self):
        line = "2026-11-09 18:27:09,511 INFO pi: Pi: 44.3 °C, throttled 0x0\n"
        self.assertEqual(parse_line(line), ("pi", (44.3, "0x0")))

    def test_event(self):
        line = "[EVENTO REGISTRADO] ID=3 Tipo='fadiga' Score=0.82 Tamanho=40074B\n"
        self.assertEqual(parse_line(line), ("event", "fadiga"))

    def test_summary(self):
        line = ("2026-11-09 18:28:37,482 INFO vguard: resumo: visão 15.0 FPS em média; "
                "eventos: nenhum; Pi até 53.0 °C, throttled sem avisos\n")
        self.assertEqual(parse_line(line), ("summary", "visão 15.0 FPS em média; eventos: nenhum; "
                                                       "Pi até 53.0 °C, throttled sem avisos"))

    def test_problems(self):
        for line in ("RuntimeError: A webcam 0 parou de enviar quadros\n",
                     "Traceback (most recent call last):\n",
                     "2026-11-09 18:30:00,000 WARNING pi: Pi: undervoltage (subtensão) ligou, throttled 0x50005\n"):
            self.assertEqual(parse_line(line)[0], "problem", line)

    def test_noise_is_ignored(self):
        for line in ("WARNING: All log messages before absl::InitializeLog() is called are written to STDERR\n",
                     "W0000 00:00:1794259177.332881   20183 face_landmarker_graph.cc:174] Sets ...\n",
                     "Error in cpuinfo: prctl(PR_SVE_GET_VL) failed\n",
                     "[V-GUARD Vision] Iniciando pipeline (source=0, headless=True)...\n"):
            self.assertIsNone(parse_line(line), line)


class ScriptTest(unittest.TestCase):

    def make(self, total=600, normal=120, timeout=60):
        return Script(1000.0, total, normal, timeout)

    def test_starts_looking_ahead(self):
        script = self.make()
        self.assertEqual(script.step[0], NORMAL)
        self.assertEqual(script.countdown(1030.0), 90.0)

    def test_full_run_passes(self):
        script = self.make()
        script.tick(1120.0)
        self.assertEqual(script.step[:2], (ACTION, "fadiga"))
        script.event("fadiga", 1125.0)
        self.assertEqual(script.results, {"fadiga": "OK"})
        self.assertEqual(script.step[0], NORMAL)
        script.tick(1244.0)
        self.assertEqual(script.step[0], NORMAL)       # ainda nos 120 s olhando para a frente
        script.tick(1245.0)
        self.assertEqual(script.step[:2], (ACTION, "distracao"))
        script.event("distracao", 1250.0)
        self.assertTrue(script.last_step)
        self.assertTrue(script.passed())
        self.assertEqual(script.countdown(1300.0), 300.0)  # o último passo vai até o fim
        self.assertFalse(script.finished(1599.0))
        self.assertTrue(script.finished(1600.0))

    def test_action_times_out(self):
        script = self.make()
        script.tick(1120.0)
        script.tick(1179.0)
        self.assertEqual(script.step[0], ACTION)
        script.tick(1180.0)
        self.assertEqual(script.results["fadiga"], "FALHOU")
        self.assertEqual(script.step[0], NORMAL)
        self.assertFalse(script.passed())

    def test_event_outside_a_request(self):
        script = self.make()
        script.event("distracao", 1010.0)
        script.tick(1120.0)
        script.event("distracao", 1121.0)   # pediu bocejo: a distração fica de fora
        self.assertEqual([event for event, _ in script.outside], ["distracao", "distracao"])
        self.assertEqual(script.step[:2], (ACTION, "fadiga"))

    def test_key_n_asks_now_and_skips(self):
        script = self.make()
        script.skip(1005.0)
        self.assertEqual(script.step[:2], (ACTION, "fadiga"))
        self.assertEqual(script.countdown(1015.0), 50.0)
        script.skip(1016.0)
        self.assertEqual(script.results["fadiga"], "PULADO")
        self.assertFalse(script.passed())

    def test_last_step_stays(self):
        script = self.make()
        for now in (1001.0, 1002.0, 1003.0, 1004.0, 1005.0, 1006.0):
            script.skip(now)
        self.assertTrue(script.last_step)
        script.tick(1599.0)
        self.assertTrue(script.last_step)

    def test_countdown_never_passes_the_end(self):
        script = self.make(total=100)
        self.assertEqual(script.countdown(1050.0), 50.0)

    def test_not_asked_is_not_passed(self):
        script = self.make()
        self.assertFalse(script.passed())


class TextTest(unittest.TestCase):

    def test_ascii(self):
        self.assertEqual(ascii_text("visão até 53.0 °C"), "visao ate 53.0 C")

    def test_clock(self):
        self.assertEqual(clock(0), "0:00")
        self.assertEqual(clock(599.6), "10:00")
        self.assertEqual(clock(83), "1:23")

    def test_status_lines(self):
        script = Script(0.0, 600, 120, 60)
        script.tick(120.0)
        script.event("fadiga", 125.0)
        script.event("fadiga", 130.0)
        status = {"fps": 15.0, "temp": 52.1, "throttled": "0x0", "problem": None, "summary": None}
        self.assertEqual(status_lines(script, status), [
            "Bocejo: OK   Rosto virado: aguardando",
            "Eventos fora do pedido: fadiga",
            "Visao 15.0 FPS   Pi 52.1 C   throttled 0x0",
        ])


if __name__ == "__main__":
    unittest.main()
