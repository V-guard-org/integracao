"""
Testes da bancada guiada da visão (tools/vision_bench.py): a leitura do log e
a sequência dos pedidos, sem câmera, sem janela e sem o OpenCV.

    cd tools && python3 test_vision_bench.py
"""

import unittest

import os
import tempfile

from vision_bench import (GREEN, KILL, ORANGE, RED, Script, ascii_text, build_steps, clock, criteria,
                          ending, parse_line, parse_summary, principal_python, status_lines,
                          telemetry_report, vision_pid)


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
        view = script.view(1300.0)
        self.assertEqual((view["title"], view["color"]), ("PEDIDOS FEITOS", GREEN))
        self.assertEqual(view["detail"], "N: terminar a bancada agora.")
        self.assertEqual(view["footer"], "Ou fim sozinho em 5:00")
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

    def test_key_n_on_the_last_part_ends_the_bench(self):
        script = self.make()
        for now in range(1001, 1009):
            script.skip(float(now))
        self.assertTrue(script.last_step)
        self.assertFalse(script.finished(1008.5))
        script.skip(1009.0)                        # N na última parte: termina (Issue #12)
        self.assertTrue(script.finished(1009.0))
        self.assertTrue(script.terminated)
        self.assertEqual(script.end, 1009.0)
        script.skip(1010.0)                        # um N a mais não muda nada
        self.assertEqual(script.end, 1009.0)

    def test_key_n_before_the_last_part_does_not_end(self):
        script = self.make()
        script.skip(1001.0)
        self.assertFalse(script.terminated)
        self.assertFalse(script.finished(1001.0))
        self.assertEqual(script.end, 1600.0)

    def test_last_part_ends_alone_at_the_maximum(self):
        script = self.make()
        for now in range(1001, 1009):
            script.skip(float(now))
        script.tick(1599.0)
        self.assertTrue(script.last_step)
        self.assertFalse(script.finished(1599.0))
        self.assertTrue(script.finished(1600.0))
        self.assertFalse(script.terminated)

    def test_keys_line(self):
        script = Script(600, 120, 60)
        self.assertEqual(script.keys_line(), "ESPACO: comecar     Q: sair")
        script.begin(1000.0)
        self.assertEqual(script.keys_line(), "N: adiantar     Q: interromper")
        for now in range(1001, 1009):
            script.skip(float(now))
        self.assertEqual(script.keys_line(), "N: terminar     Q: interromper")

    def test_ending(self):
        script = self.make()
        self.assertEqual(ending(script, crashed=False, interrupted=False), "fim pelo tempo")
        self.assertEqual(ending(script, crashed=False, interrupted=True), "interrompida com o Q")
        self.assertEqual(ending(script, crashed=True, interrupted=True), "o principal saiu antes")
        for now in range(1001, 1010):
            script.skip(float(now))
        self.assertEqual(ending(script, crashed=False, interrupted=False), "terminada com o N")

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
            texts += [view["title"], view["detail"], view["footer"], script.step_line(float(now)),
                      script.keys_line()]
        texts.append(Script(600, 120, 60).view(0.0)["detail"])
        for text in texts:
            self.assertEqual(ascii_text(text), text)


class PassoCTest(unittest.TestCase):
    """O passo C (Issue #10): o ESP32, o kill -9 na visão e a telemetria."""

    def test_principal_lines_are_read(self):
        self.assertEqual(parse_line("2026-11-09 20:38:25,306 INFO vision.process: [EVENTO REGISTRADO] "
                                    "ID=7f3c Tipo='fadiga' Score=0.7 Tamanho=40255B\n"), ("event", "fadiga"))
        self.assertEqual(parse_line("2026-11-09 20:37:57,994 WARNING vision.process: visão caiu (código -9); "
                                    "nova tentativa em 2 s\n"), ("fell", -9))
        self.assertEqual(parse_line("2026-11-09 20:38:32,001 INFO vision.process: visão de volta depois de "
                                    "1 tentativa (6 s fora)\n"), ("back", None))

    def test_kill_step_comes_after_the_turned_face(self):
        steps = build_steps(kill=True)
        self.assertEqual([step["kind"] for step in steps],
                         ["normal", "action", "normal", "action", "normal", KILL, "normal"])
        self.assertEqual(len(build_steps()), 5)

    def test_kill_step_texts_and_ok(self):
        script = Script(600, 120, 60, steps=build_steps(kill=True), name="passo C")
        script.begin(0.0)
        self.assertEqual(script.step_line(0.0), "Passo 1 de 7     0:00 / 10:00")
        script.index, script.phase, script.phase_start = 4, "normal", 300.0
        self.assertEqual(script.view(330.0)["footer"], "Em 1:30: matar a visao (kill -9)")
        script.tick(420.0)
        view = script.view(421.0)
        self.assertEqual((view["title"], view["detail"]), ("PREPARE-SE", "Daqui a 4 s: a visao vai ser morta (kill -9)."))
        script.tick(425.0)
        view = script.view(426.0)
        self.assertEqual((view["title"], view["footer"]), ("VISAO MORTA (KILL -9)", "Esperando a visao voltar sozinha..."))
        script.event("volta", 432.0)
        view = script.view(432.5)
        self.assertEqual((view["title"], view["detail"], view["color"]),
                         ("OK! A VISAO VOLTOU", "A visao voltou sozinha.", GREEN))
        self.assertEqual(script.expected(), ["fadiga", "distracao", "volta"])
        self.assertEqual(script.label("volta"), "Visao de volta")
        for text in (view["title"], view["detail"], view["footer"]):
            self.assertTrue(text.isascii())

    def test_summary_numbers(self):
        summary = parse_summary("visão 14.3 FPS em média; eventos: fadiga 1, distracao 1; visão reiniciada 1 vez; "
                                "ESP32: lost 0, corrupt 2; Pi até 62.8 °C, throttled sem avisos")
        self.assertEqual(summary, {"fps": 14.3, "restarts": 1, "lost": 0, "corrupt": 2,
                                   "temp_max": 62.8, "throttled_ok": True, "sent": None, "unsent": None})
        self.assertEqual(parse_summary(None)["fps"], None)
        sent = parse_summary("visão 13.5 FPS em média; envio: 1 evento enviado, 0 pendentes; Pi até 50.0 °C")
        self.assertEqual((sent["sent"], sent["unsent"]), (1, 0))
        sent = parse_summary("envio: 5 eventos enviados, 2 pendentes")
        self.assertEqual((sent["sent"], sent["unsent"]), (5, 2))

    def test_telemetry_report(self):
        rows = [(100.0 + i, {"imu": {}, "obd": {"age_ms": {"rpm": 200 + i}}}) for i in range(10)]
        rows += [(111.8, {"imu": {}, "obd": {}}), (112.8, {"imu": {}})]       # lacuna de 1,8 s
        report = telemetry_report(rows, 100.0, 120.0, kill_at=108.0)
        self.assertEqual(report["rows"], 12)
        self.assertEqual(report["both"], 11)
        self.assertAlmostEqual(report["max_gap"], 2.8)
        self.assertAlmostEqual(report["kill_gap"], 2.8)
        self.assertEqual(report["max_age_ms"], 209)
        self.assertEqual(telemetry_report(rows, 200.0, 300.0)["rows"], 0)

    def test_criteria(self):
        script = Script(600, 120, 60, steps=build_steps(kill=True))
        script.results = {"fadiga": "OK", "distracao": "OK", "volta": "OK"}
        summary = parse_summary("visão 14.3 FPS em média; eventos: fadiga 1; visão reiniciada 1 vez; "
                                "ESP32: lost 0, corrupt 0; Pi até 62.8 °C, throttled sem avisos")
        telemetry = {"rows": 594, "both": 593, "max_gap": 1.02, "kill_gap": 1.02, "max_age_ms": 561}
        items = criteria(summary, telemetry, script, 600, esp32=True, kill=True)
        self.assertTrue(all(passed for _, passed, _ in items), items)
        self.assertEqual(len(items), 11)
        telemetry["kill_gap"] = 3.5
        summary["corrupt"] = 1
        failed = [name for name, passed, _ in criteria(summary, telemetry, script, 600, esp32=True, kill=True)
                  if not passed]
        self.assertEqual(failed, ["ESP32 sem perdas", "Telemetria inteira no kill -9"])
        self.assertEqual(len(criteria(summary, telemetry, Script(600, 120, 60), 600)), 5)

    def test_server_criterion(self):
        script = Script(600, 120, 60)
        script.results = {"fadiga": "OK", "distracao": "OK"}
        text = "visão 14.3 FPS em média; envio: %s; Pi até 50.0 °C, throttled sem avisos"
        for sending, passed in (("2 eventos enviados, 0 pendentes", True), ("1 evento enviado, 1 pendente", False)):
            with self.subTest(sending=sending):
                items = criteria(parse_summary(text % sending), None, script, 120, server=True)
                self.assertEqual(items[-1][:2], ("Eventos enviados ao servidor", passed))
        items = criteria(parse_summary("visão 14.3 FPS em média"), None, script, 120, server=True)
        self.assertEqual(items[-1], ("Eventos enviados ao servidor", False, "-"))

    def test_principal_python(self):
        self.assertEqual(principal_python({"VGUARD_PYTHON": "/opt/py"}), "/opt/py")
        self.assertTrue(principal_python({}))

    def test_vision_pid_finds_the_child_of_any_thread(self):
        with tempfile.TemporaryDirectory() as proc:
            def write(path, data):
                full = os.path.join(proc, path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "wb") as stream:
                    stream.write(data)
            write("100/task/100/children", b"")
            write("100/task/105/children", b"200 300 ")                  # a thread "vision" iniciou
            write("200/cmdline", b"vcgencmd\0measure_temp\0")
            write("300/cmdline", b"/venv/bin/python\0-u\0/repo/vision/main.py\0--channel-fd\0")
            self.assertEqual(vision_pid(100, proc), 300)
            self.assertIsNone(vision_pid(999, proc))


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
