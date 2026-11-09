"""
Testes da bancada guiada da visão (tools/vision_bench.py): a leitura do log e
a sequência dos pedidos, sem câmera, sem janela e sem o OpenCV.

    cd tools && python3 test_vision_bench.py
"""

import unittest

from vision_bench import (GREEN, ORANGE, RED, Script, ascii_text, clock, parse_line,
                          status_lines)


class ParseLineTest(unittest.TestCase):
    """Linhas copiadas do log real da Pi em 09/11."""

    def test_fps(self):
        line = "2026-11-09 18:27:20,343 INFO vision: visão: 15.0 FPS em 10 s\n"
        self.assertEqual(parse_line(line), ("fps", 15.0))

    def test_pi(self):
        line = "2026-11-09 18:27:09,511 INFO pi: Pi: 44.3 °C, throttled 0x0\n"
        self.assertEqual(parse_line(line), ("pi", (44.3, "0x0")))

    def test_event(self):
        # O EventManager de hoje devolve um UUID; o do ticket 2, um inteiro.
        line = ("[EVENTO REGISTRADO] ID=06f0a5ac-ae2e-4584-93fd-c23ee9d264a9 Tipo='fadiga' "
                "Score=0.7 Tamanho=40255B\n")
        self.assertEqual(parse_line(line), ("event", "fadiga"))
        line = "[EVENTO REGISTRADO] ID=3 Tipo='distracao' Score=0.82 Tamanho=40074B\n"
        self.assertEqual(parse_line(line), ("event", "distracao"))

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
    """Bancada de 600 s, 120 s olhando para a frente, 5 s de aviso, 60 s de espera e 4 s de resultado."""

    def make(self, total=600):
        script = Script(total, 120, 60)
        script.begin(1000.0)
        return script

    def test_waits_for_space(self):
        script = Script(600, 120, 60)
        script.tick(5000.0)
        self.assertEqual(script.phase, "intro")
        self.assertFalse(script.finished(9999.0))
        self.assertEqual(script.view(5000.0)["title"], "PRONTOS?")
        script.event("distracao", 5001.0)          # antes do ESPAÇO: fica de fora
        self.assertEqual(len(script.outside), 1)
        script.begin(6000.0)
        self.assertEqual(script.phase, "normal")
        self.assertTrue(script.finished(6600.0))

    def test_full_run_passes(self):
        script = self.make()
        view = script.view(1030.0)
        self.assertEqual((view["title"], view["color"]), ("OLHE PARA A FRENTE", GREEN))
        self.assertEqual(view["footer"], "Em 1:30: pedir para bocejar")

        script.tick(1120.0)
        view = script.view(1121.0)
        self.assertEqual((script.phase, view["title"], view["color"]), ("prep", "PREPARE-SE", ORANGE))
        self.assertEqual(view["detail"], "Daqui a 4 s: pedir para bocejar.")

        script.tick(1125.0)
        view = script.view(1126.0)
        self.assertEqual((script.phase, view["title"]), ("ask", "PECA PARA BOCEJAR"))
        self.assertIn("ate aparecer OK", view["detail"])
        self.assertEqual(view["footer"], "Esperando a deteccao...")

        script.event("fadiga", 1130.0)
        view = script.view(1131.0)
        self.assertEqual((script.phase, view["title"], view["color"]), ("result", "OK! PODE FECHAR A BOCA", GREEN))
        self.assertEqual(script.results, {"fadiga": "OK"})

        script.tick(1134.0)
        self.assertEqual((script.index, script.phase), (2, "normal"))
        self.assertEqual(script.view(1134.0)["footer"], "Em 2:00: pedir para virar o rosto")

        script.tick(1254.0)
        script.tick(1259.0)
        self.assertEqual(script.view(1259.0)["title"], "PECA PARA VIRAR O ROSTO")
        script.event("distracao", 1262.0)
        self.assertEqual(script.view(1262.0)["title"], "OK! PODE VOLTAR")
        script.tick(1266.0)
        self.assertTrue(script.last_step)
        self.assertEqual(script.view(1300.0)["footer"], "Fim em 5:00")
        self.assertTrue(script.passed())
        self.assertEqual(script.outside, [])

    def test_event_during_prep_counts(self):
        script = self.make()
        script.tick(1120.0)
        script.event("fadiga", 1122.0)
        self.assertEqual(script.results, {"fadiga": "OK"})
        self.assertEqual(script.phase, "result")

    def test_hint_after_20_s_and_no_countdown(self):
        script = self.make()
        script.tick(1120.0)
        script.tick(1125.0)
        self.assertNotIn("MAIS", script.view(1144.0)["detail"])
        self.assertIn("MAIS", script.view(1145.0)["detail"])
        self.assertNotIn(":", script.view(1145.0)["footer"])     # sem contagem no pedido

    def test_ask_times_out(self):
        script = self.make()
        script.tick(1120.0)
        script.tick(1125.0)
        script.tick(1184.0)
        self.assertEqual(script.phase, "ask")
        script.tick(1185.0)
        view = script.view(1185.0)
        self.assertEqual((view["title"], view["color"]), ("NAO DETECTOU", RED))
        self.assertEqual(script.results["fadiga"], "FALHOU")
        script.tick(1189.0)
        self.assertEqual((script.index, script.phase), (2, "normal"))
        self.assertFalse(script.passed())

    def test_event_outside_a_request(self):
        script = self.make()
        script.event("distracao", 1010.0)
        self.assertEqual(script.recent_outside(1012.0), "distracao")
        self.assertIsNone(script.recent_outside(1020.0))
        script.tick(1120.0)
        script.tick(1125.0)
        script.event("distracao", 1126.0)   # pediu bocejo: a distração fica de fora
        self.assertEqual([event for event, _ in script.outside], ["distracao", "distracao"])
        self.assertEqual(script.phase, "ask")

    def test_key_n(self):
        script = self.make()
        script.skip(1005.0)
        self.assertEqual(script.phase, "prep")
        script.skip(1006.0)
        self.assertEqual(script.phase, "ask")
        script.skip(1007.0)
        self.assertEqual(script.results["fadiga"], "PULADO")
        self.assertEqual(script.view(1007.0)["title"], "NAO DETECTOU")
        script.skip(1008.0)
        self.assertEqual((script.index, script.phase), (2, "normal"))
        self.assertFalse(script.passed())

    def test_last_step_stays(self):
        script = self.make()
        for now in range(1001, 1020):
            script.skip(float(now))
        self.assertTrue(script.last_step)
        script.tick(1599.0)
        self.assertTrue(script.last_step)

    def test_normal_countdown_never_passes_the_end(self):
        script = self.make(total=100)
        self.assertEqual(script.view(1050.0)["footer"], "Em 0:50: pedir para bocejar")

    def test_step_line(self):
        script = self.make()
        self.assertEqual(script.step_line(1083.0), "Passo 1 de 5     1:23 / 10:00")
        self.assertEqual(script.step_line(2000.0), "Passo 1 de 5     10:00 / 10:00")

    def test_all_texts_are_ascii(self):
        script = self.make()
        texts = []
        for now in range(1000, 1700, 1):
            script.tick(float(now))
            view = script.view(float(now))
            texts += [view["title"], view["detail"], view["footer"], script.step_line(float(now))]
        texts.append(Script(600, 120, 60).view(0.0)["detail"])
        for text in texts:
            self.assertEqual(ascii_text(text), text)


class TextTest(unittest.TestCase):

    def test_ascii(self):
        self.assertEqual(ascii_text("visão até 53.0 °C"), "visao ate 53.0 C")

    def test_clock(self):
        self.assertEqual(clock(0), "0:00")
        self.assertEqual(clock(599.6), "10:00")
        self.assertEqual(clock(83), "1:23")

    def test_status_lines(self):
        script = Script(600, 120, 60)
        script.begin(0.0)
        script.tick(120.0)
        script.event("fadiga", 122.0)
        script.event("fadiga", 130.0)
        status = {"fps": 15.0, "temp": 52.1, "throttled": "0x0", "problem": None, "summary": None}
        self.assertEqual(status_lines(script, status), [
            "Bocejo: OK   Rosto virado: falta",
            "Eventos fora do pedido: fadiga",
            "Visao 15.0 FPS   Pi 52.1 C   throttled 0x0",
        ])


if __name__ == "__main__":
    unittest.main()
