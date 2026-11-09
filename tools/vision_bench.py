"""
Bancada guiada da visão, o passo 0 (Issue #6): roda a visão sem tela por
10 min e mostra numa janela da Pi, em letra grande, o que quem lê a tela
deve pedir agora a quem está na frente da câmera: olhar para a frente,
bocejar ou virar o rosto. Cada pedido espera o evento aparecer no log da
visão e mostra OK assim que ele chega.

    .venv/bin/python tools/vision_bench.py
    .venv/bin/python tools/vision_bench.py --seconds 120 --normal 20

Roda na tela da Pi, pelo Raspberry Pi Connect. A visão continua sem tela
(vguard.py --headless), como pede o critério; a janela só lê o log dela.
Teclas na janela: ESPAÇO começa os 10 min, N adianta o passo (ou pula o
pedido atual) e Q encerra antes. O log inteiro vai para o terminal e para
<dados>/bancada-visao-<data>.log.

Sai com o código 1 se a visão saiu sozinha, se a bancada foi encerrada
antes ou se algum pedido não gerou o evento.
"""

import argparse
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

NORMAL, ACTION = "normal", "action"

# Os textos são lidos em voz alta por quem está na tela. Só ASCII: a fonte do
# OpenCV não tem acento.
STEPS = (
    {"kind": NORMAL},
    {"kind": ACTION, "event": "fadiga", "label": "Bocejo", "verb": "bocejar",
     "ask": "PECA PARA BOCEJAR",
     "how": "Boca bem aberta, segurando, ate aparecer OK aqui.",
     "hint": "Ainda nao detectou: abrir MAIS a boca e segurar.",
     "ok": "OK! PODE FECHAR A BOCA"},
    {"kind": NORMAL},
    {"kind": ACTION, "event": "distracao", "label": "Rosto virado", "verb": "virar o rosto",
     "ask": "PECA PARA VIRAR O ROSTO",
     "how": "Para o lado (a janela), segurando, ate aparecer OK aqui.",
     "hint": "Ainda nao detectou: virar MAIS o rosto e segurar.",
     "ok": "OK! PODE VOLTAR"},
    {"kind": NORMAL},
)

GREEN, ORANGE, RED, WHITE = "green", "orange", "red", "white"


class Script:
    """
    A sequência da bancada, sem janela nem processo: recebe o horário
    (monotônico) e os eventos, e diz o que mostrar. Os tempos ficam aqui;
    quem lê a tela só repete o que aparece.

    Fases: "intro" (até o ESPAÇO), "normal" (olhando para a frente), e, em
    cada pedido, "prep" (aviso de alguns segundos), "ask" (até o evento) e
    "result" (OK ou NAO DETECTOU, por alguns segundos).
    """

    def __init__(self, total_s, normal_s, ask_timeout_s, prep_s=5.0, result_s=4.0,
                 hint_after_s=20.0, steps=STEPS):
        self.total_s = total_s
        self.normal_s = normal_s
        self.ask_timeout_s = ask_timeout_s
        self.prep_s = prep_s
        self.result_s = result_s
        self.hint_after_s = hint_after_s
        self.steps = steps
        self.start = self.end = None
        self.index = 0
        self.phase = "intro"
        self.phase_start = None
        self.results = {}      # evento esperado -> "OK", "FALHOU" ou "PULADO"
        self.outside = []      # (tipo, quando) dos eventos fora de um pedido

    # ------------------------------------------------------------ estado

    @property
    def step(self):
        return self.steps[self.index]

    @property
    def last_step(self):
        return self.index + 1 >= len(self.steps)

    @property
    def started(self):
        return self.start is not None

    def expected(self):
        return [step["event"] for step in self.steps if step["kind"] == ACTION]

    def label(self, event):
        for step in self.steps:
            if step.get("event") == event:
                return step["label"]
        return event

    def finished(self, now):
        return self.started and now >= self.end

    def passed(self):
        return all(self.results.get(name) == "OK" for name in self.expected())

    # ------------------------------------------------------------ entradas

    def begin(self, now):
        """ESPAÇO: começa a contar os 10 min."""
        if not self.started:
            self.start, self.end = now, now + self.total_s
            self._enter(0, "normal", now)

    def tick(self, now):
        """Passa à fase seguinte quando o tempo da atual acaba."""
        elapsed = None if self.phase_start is None else now - self.phase_start
        if self.phase == "normal" and not self.last_step and elapsed >= self.normal_s:
            self._enter(self.index + 1, "prep", now)
        elif self.phase == "prep" and elapsed >= self.prep_s:
            self._enter(self.index, "ask", now)
        elif self.phase == "ask" and elapsed >= self.ask_timeout_s:
            self.results[self.step["event"]] = "FALHOU"
            self._enter(self.index, "result", now)
        elif self.phase == "result" and elapsed >= self.result_s:
            self._enter(self.index + 1, "normal", now)

    def event(self, event_type, now):
        """Um evento gravado pela visão: cumpre o pedido atual ou fica de fora."""
        # Na preparação também vale: quem lê a tela pode pedir um pouco antes.
        if self.phase in ("prep", "ask") and event_type == self.step["event"]:
            self.results[event_type] = "OK"
            self._enter(self.index, "result", now)
        else:
            self.outside.append((event_type, now))

    def skip(self, now):
        """Tecla N: adianta a fase atual; num pedido, marca como pulado."""
        if self.phase == "normal" and not self.last_step:
            self._enter(self.index + 1, "prep", now)
        elif self.phase == "prep":
            self._enter(self.index, "ask", now)
        elif self.phase == "ask":
            self.results[self.step["event"]] = "PULADO"
            self._enter(self.index, "result", now)
        elif self.phase == "result":
            self._enter(self.index + 1, "normal", now)

    def _enter(self, index, phase, now):
        self.index, self.phase, self.phase_start = index, phase, now

    # ------------------------------------------------------------ o que mostrar

    def view(self, now):
        """
        O que a janela mostra agora: o título grande, a cor, a frase de baixo
        e a linha da contagem. Tudo em ASCII.
        """
        step = self.step
        if self.phase == "intro":
            return {"title": "PRONTOS?", "color": WHITE,
                    "detail": "Na camera: olhar sempre para a frente, nunca para a tela.",
                    "footer": "Na tela: falar em voz alta o que aparecer.  ESPACO: comecar"}
        elapsed = now - self.phase_start
        if self.phase == "normal":
            if self.last_step:
                return {"title": "OLHE PARA A FRENTE", "color": GREEN,
                        "detail": "Ultima parte: nada mais a pedir.",
                        "footer": "Fim em %s" % clock(max(0.0, self.end - now))}
            next_verb = self.steps[self.index + 1]["verb"]
            left = min(self.normal_s - elapsed, self.end - now)
            return {"title": "OLHE PARA A FRENTE", "color": GREEN,
                    "detail": "Nada a pedir agora.",
                    "footer": "Em %s: pedir para %s" % (clock(max(0.0, left)), next_verb)}
        if self.phase == "prep":
            left = max(1, int(self.prep_s - elapsed + 0.999))
            return {"title": "PREPARE-SE", "color": ORANGE,
                    "detail": "Daqui a %d s: pedir para %s." % (left, step["verb"]),
                    "footer": ""}
        if self.phase == "ask":
            detail = step["hint"] if elapsed >= self.hint_after_s else step["how"]
            return {"title": step["ask"], "color": ORANGE, "detail": detail,
                    "footer": "Esperando a deteccao..."}
        if self.results.get(step["event"]) == "OK":
            return {"title": step["ok"], "color": GREEN,
                    "detail": "%s detectado e gravado." % step["label"],
                    "footer": "Volte a olhar para a frente."}
        return {"title": "NAO DETECTOU", "color": RED,
                "detail": "%s: %s. Seguindo para o proximo passo." % (
                    step["label"], self.results.get(step["event"], "FALHOU")),
                "footer": "Volte a olhar para a frente."}

    def step_line(self, now):
        """A linha de cima: o passo e o tempo."""
        if not self.started:
            return "Bancada da visao (passo 0): 10 min sem tela"
        return "Passo %d de %d     %s / %s" % (
            self.index + 1, len(self.steps), clock(min(now, self.end) - self.start), clock(self.total_s))

    def recent_outside(self, now, window_s=6.0):
        """O último evento fora de um pedido, se foi há pouco (para avisar na tela)."""
        if self.outside and now - self.outside[-1][1] < window_s:
            return self.outside[-1][0]
        return None


_EVENT = re.compile(r"\[EVENTO REGISTRADO\] ID=\S+ Tipo='(\w+)'")   # UUID hoje, inteiro no ticket 2
_FPS = re.compile(r"visão: ([\d.]+) FPS")
_PI = re.compile(r"Pi: ([\d.]+) °C, throttled (0x[0-9a-fA-F]+)")
_SUMMARY = re.compile(r"resumo: (.*)")
_PROBLEM = re.compile(r" (WARNING|ERROR) |^\w*Error: |^Traceback")


def parse_line(line):
    """Uma linha do log da visão vira (tipo, valor), ou None se não interessa à janela."""
    match = _EVENT.search(line)
    if match:
        return "event", match.group(1)
    match = _FPS.search(line)
    if match:
        return "fps", float(match.group(1))
    match = _PI.search(line)
    if match:
        return "pi", (float(match.group(1)), match.group(2))
    match = _SUMMARY.search(line)
    if match:
        return "summary", match.group(1).strip()
    if _PROBLEM.search(line):
        return "problem", line.strip()
    return None


def ascii_text(text):
    """Tira acentos e símbolos que a fonte do OpenCV não desenha (°, ã...)."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def clock(seconds):
    seconds = int(round(seconds))
    return "%d:%02d" % (seconds // 60, seconds % 60)


def status_lines(script, status):
    """As linhas de baixo da janela: os pedidos, os eventos de fora e a visão."""
    results = ["%s: %s" % (script.label(name), script.results.get(name, "falta"))
               for name in script.expected()]
    outside = ", ".join(event for event, _ in script.outside) or "nenhum"
    vision = "Visao %s FPS   Pi %s   throttled %s" % (
        "carregando" if status["fps"] is None else "%.1f" % status["fps"],
        "-" if status["temp"] is None else "%.1f C" % status["temp"],
        status["throttled"] or "-")
    return ["   ".join(results), "Eventos fora do pedido: %s" % outside, vision]


# ---------------------------------------------------------------- janela (OpenCV)

WIDTH, HEIGHT = 960, 540
BGR = {GREEN: (80, 220, 80), ORANGE: (0, 190, 255), RED: (80, 80, 255), WHITE: (255, 255, 255)}
GRAY = (170, 170, 170)


def _put(cv2, img, text, y, scale, color, thickness=2, x=None):
    """Escreve uma linha, diminuindo a letra até caber na largura."""
    text = ascii_text(text)
    font = cv2.FONT_HERSHEY_SIMPLEX
    (width, _), _ = cv2.getTextSize(text, font, scale, thickness)
    while width > WIDTH - 40 and scale > 0.4:
        scale -= 0.05
        (width, _), _ = cv2.getTextSize(text, font, scale, thickness)
    if x is None:
        x = max(20, (WIDTH - width) // 2)
    cv2.putText(img, text, (x, y), font, scale, color, thickness, cv2.LINE_AA)


def draw(cv2, np, script, status, now):
    img = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    view = script.view(now)
    color = BGR[view["color"]]
    _put(cv2, img, script.step_line(now), 40, 0.7, GRAY, 1, x=20)
    cv2.rectangle(img, (0, 70), (WIDTH, 300), tuple(c // 5 for c in color), -1)
    _put(cv2, img, view["title"], 170, 2.2, color, 5)
    _put(cv2, img, view["detail"], 250, 1.0, BGR[WHITE], 2)
    _put(cv2, img, view["footer"], 345, 0.9, BGR[WHITE], 2)
    outside = script.recent_outside(now)
    if outside:
        _put(cv2, img, "Evento fora do pedido: %s (olhou para baixo ou para a tela?)" % outside,
             385, 0.7, BGR[RED], 2)
    y = 425
    for line in status_lines(script, status):
        _put(cv2, img, line, y, 0.65, GRAY, 1, x=20)
        y += 30
    if status["problem"]:
        _put(cv2, img, status["problem"], 515, 0.5, BGR[RED], 1, x=20)
    keys = "ESPACO: comecar     Q: encerrar" if not script.started else "N: adiantar     Q: encerrar"
    _put(cv2, img, keys, HEIGHT - 8, 0.5, GRAY, 1, x=WIDTH - 360)
    return img


def draw_final(cv2, np, script, status, crashed, stopped_early):
    img = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    ok = script.passed() and not crashed and not stopped_early
    _put(cv2, img, "FIM: PASSOU" if ok else "FIM: CONFERIR", 120, 2.0, BGR[GREEN if ok else RED], 5)
    lines = []
    if crashed:
        lines.append("A visao saiu sozinha antes do fim.")
    if stopped_early:
        lines.append("Bancada encerrada antes do fim (Q).")
    lines += status_lines(script, status)
    if status["summary"]:
        lines.append("Resumo: " + status["summary"])
    lines.append("Avise o Claude. Q: fechar a janela")
    y = 200
    for line in lines:
        _put(cv2, img, line, y, 0.65, BGR[WHITE], 1, x=20)
        y += 40
    return img


# ---------------------------------------------------------------- processo da visão

def _pump(stream, lines, log_path):
    """Lê o log da visão: copia no arquivo e no terminal e entrega à janela."""
    with open(log_path, "a", encoding="utf-8") as log_file:
        for line in stream:
            log_file.write(line)
            log_file.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
            lines.put(line)
    lines.put(None)


def _drain(lines, script, status):
    """Trata as linhas que chegaram. Devolve True quando o log da visão acabou."""
    while True:
        try:
            line = lines.get_nowait()
        except queue.Empty:
            return False
        if line is None:
            return True
        parsed = parse_line(line)
        if parsed is None:
            continue
        kind, value = parsed
        if kind == "event":
            script.event(value, time.monotonic())
        elif kind == "fps":
            status["fps"] = value
        elif kind == "pi":
            status["temp"], status["throttled"] = value
        elif kind == "summary":
            status["summary"] = value
        else:
            status["problem"] = value


def parse_args(argv=None):
    data = os.environ.get("VGUARD_DATA") or os.path.join(os.path.dirname(REPO), "dados-integracao")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=data, help="pasta de dados da bancada (padrão: %(default)s)")
    parser.add_argument("--calibration-file", help="perfil do motorista (padrão: <dados>/calibration-driver.json)")
    parser.add_argument("--source", default="0", help="câmera, como no vguard.py (padrão: %(default)s)")
    parser.add_argument("--seconds", type=float, default=600,
                        help="duração da bancada, a partir do ESPAÇO (padrão: %(default)s)")
    parser.add_argument("--normal", type=float, default=120,
                        help="segundos olhando para a frente antes de cada pedido (padrão: %(default)s)")
    parser.add_argument("--ask-timeout", type=float, default=60,
                        help="segundos de espera do evento em cada pedido (padrão: %(default)s)")
    args = parser.parse_args(argv)
    args.data = os.path.abspath(args.data)
    if args.calibration_file is None:
        args.calibration_file = os.path.join(args.data, "calibration-driver.json")
    if not os.path.exists(args.calibration_file):
        parser.error("perfil da calibração não encontrado: %s (calibre antes pelo "
                     "tools/test_camera_gui.sh --calibrate)" % args.calibration_file)
    return args


def main(argv=None):
    args = parse_args(argv)
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    import cv2
    import numpy as np

    os.makedirs(args.data, exist_ok=True)
    log_path = os.path.join(args.data, time.strftime("bancada-visao-%Y%m%d-%H%M%S.log"))
    command = [sys.executable, "-u", "-B", os.path.join(REPO, "vguard.py"), "--headless",
               "--source", args.source, "--no-esp32", "--no-gps", "--no-sync",
               "--calibration-file", args.calibration_file]
    env = dict(os.environ, VGUARD_DATA=args.data, PYTHONIOENCODING="utf-8")
    child = subprocess.Popen(command, cwd=REPO, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", bufsize=1)
    lines = queue.Queue()
    reader = threading.Thread(target=_pump, args=(child.stdout, lines, log_path), daemon=True)
    reader.start()

    script = Script(args.seconds, args.normal, args.ask_timeout)
    status = {"fps": None, "temp": None, "throttled": None, "problem": None, "summary": None}
    window = "V-GUARD - bancada da visao"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, WIDTH, HEIGHT)
    launched = time.monotonic()
    crashed = stopped_early = False
    try:
        while True:
            if _drain(lines, script, status):
                crashed = True
                break
            now = time.monotonic()
            if script.finished(now):
                break
            script.tick(now)
            cv2.imshow(window, draw(cv2, np, script, status, now))
            key = cv2.waitKey(200) & 0xFF
            if key == ord(" "):
                script.begin(time.monotonic())
            elif key in (ord("n"), ord("N")) and script.started:
                script.skip(time.monotonic())
            elif key in (ord("q"), ord("Q")):
                stopped_early = True
                break
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        reader.join(timeout=5)
    _drain(lines, script, status)

    results = ", ".join("%s %s" % (name, script.results.get(name, "nao pedido")) for name in script.expected())
    outside = ", ".join(event for event, _ in script.outside) or "nenhum"
    measured = 0.0 if not script.started else min(time.monotonic(), script.end) - script.start
    print("\nBancada da visão: %s de %s (visão rodando por %s)" % (
        clock(measured), clock(args.seconds), clock(time.monotonic() - launched)))
    print("A visão saiu sozinha: %s (código %s)" % ("sim" if crashed else "não", child.returncode))
    print("Pedidos: %s" % results)
    print("Eventos fora do pedido: %s" % outside)
    print("Resumo da visão: %s" % (status["summary"] or "não chegou"))
    print("Log: %s" % log_path)

    cv2.imshow(window, draw_final(cv2, np, script, status, crashed, stopped_early))
    while cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) >= 1:
        if (cv2.waitKey(200) & 0xFF) in (ord("q"), ord("Q")):
            break
    cv2.destroyAllWindows()
    return 0 if script.passed() and not crashed and not stopped_early else 1


if __name__ == "__main__":
    sys.exit(main())
