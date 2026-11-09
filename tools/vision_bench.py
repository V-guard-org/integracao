"""
Bancada guiada da visão, o passo 0 (Issue #6): roda a visão sem tela por
10 min e mostra numa janela da Pi, em letra grande, o que a pessoa na frente
da câmera deve fazer agora: olhar para a frente, bocejar ou virar o rosto.
Cada pedido espera o evento aparecer no log da visão e só então segue.

    .venv/bin/python tools/vision_bench.py
    .venv/bin/python tools/vision_bench.py --seconds 120 --normal 20

Roda na tela da Pi, pelo Raspberry Pi Connect. A visão continua sem tela
(vguard.py --headless), como pede o critério; a janela só lê o log dela.
Teclas na janela: N pede a próxima ação já (ou pula a atual) e Q encerra
antes. O log inteiro vai para o terminal e para
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

# (tipo, evento esperado, instrução, detalhe). Só ASCII: a fonte do OpenCV não tem acento.
LOOK_AHEAD = "Como se estivesse dirigindo. Nao olhe para baixo."
STEPS = (
    (NORMAL, None, "Olhe para a frente", LOOK_AHEAD),
    (ACTION, "fadiga", "AGORA: BOCEJE", "Abra bem a boca e segure uns 3 segundos."),
    (NORMAL, None, "Volte a olhar para a frente", LOOK_AHEAD),
    (ACTION, "distracao", "AGORA: VIRE O ROSTO", "Olhe para o lado (a janela) e segure uns 3 segundos."),
    (NORMAL, None, "Olhe para a frente ate o fim", LOOK_AHEAD),
)
LABELS = {"fadiga": "Bocejo", "distracao": "Rosto virado"}


class Script:
    """
    A sequência dos pedidos, sem janela nem processo: recebe o horário
    (monotônico) e os eventos, e diz o que mostrar. Os tempos de espera
    ficam aqui, e a pessoa na frente da câmera só segue a janela.
    """

    def __init__(self, start, total_s, normal_s, action_timeout_s, steps=STEPS):
        self.end = start + total_s
        self.normal_s = normal_s
        self.action_timeout_s = action_timeout_s
        self.steps = steps
        self.index = 0
        self.step_start = start
        self.results = {}      # evento esperado -> "OK", "FALHOU" ou "PULADO"
        self.outside = []      # (tipo, quando) dos eventos fora de um pedido
        self.last_ok = None    # (tipo, quando) do último pedido cumprido

    @property
    def step(self):
        return self.steps[self.index]

    @property
    def last_step(self):
        return self.index + 1 >= len(self.steps)

    def expected(self):
        return [step[1] for step in self.steps if step[0] == ACTION]

    def finished(self, now):
        return now >= self.end

    def passed(self):
        return all(self.results.get(name) == "OK" for name in self.expected())

    def tick(self, now):
        """Passa ao pedido seguinte quando o tempo do atual acaba."""
        kind, expected = self.step[0], self.step[1]
        if kind == NORMAL and not self.last_step and now - self.step_start >= self.normal_s:
            self._next(now)
        elif kind == ACTION and now - self.step_start >= self.action_timeout_s:
            self.results[expected] = "FALHOU"
            self._next(now)

    def event(self, event_type, now):
        """Um evento gravado pela visão: cumpre o pedido atual ou fica de fora."""
        kind, expected = self.step[0], self.step[1]
        if kind == ACTION and event_type == expected:
            self.results[expected] = "OK"
            self.last_ok = (event_type, now)
            self._next(now)
        else:
            self.outside.append((event_type, now))

    def skip(self, now):
        """Tecla N: olhando para a frente, pede a próxima ação já; num pedido, o pula."""
        kind, expected = self.step[0], self.step[1]
        if kind == ACTION:
            self.results[expected] = "PULADO"
        self._next(now)

    def countdown(self, now):
        """Segundos até o próximo pedido, até o fim do pedido atual ou até o fim da bancada."""
        if self.step[0] == ACTION:
            left = self.action_timeout_s - (now - self.step_start)
        elif self.last_step:
            left = self.end - now
        else:
            left = self.normal_s - (now - self.step_start)
        return max(0.0, min(left, self.end - now))

    def _next(self, now):
        if not self.last_step:
            self.index += 1
            self.step_start = now


# O ID é o UUID de hoje ou o inteiro do ticket 2: qualquer coisa sem espaço.
_EVENT = re.compile(r"\[EVENTO REGISTRADO\] ID=\S+ Tipo='(\w+)'")
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
    results = []
    for name in script.expected():
        results.append("%s: %s" % (LABELS.get(name, name), script.results.get(name, "aguardando")))
    outside = ", ".join(event for event, _ in script.outside) or "nenhum"
    vision = "Visao %s FPS   Pi %s   throttled %s" % (
        "-" if status["fps"] is None else "%.1f" % status["fps"],
        "-" if status["temp"] is None else "%.1f C" % status["temp"],
        status["throttled"] or "-")
    return ["   ".join(results), "Eventos fora do pedido: %s" % outside, vision]


# ---------------------------------------------------------------- janela (OpenCV)

WIDTH, HEIGHT = 960, 540
WHITE, GRAY, GREEN, ORANGE, RED = (255, 255, 255), (170, 170, 170), (80, 220, 80), (0, 190, 255), (80, 80, 255)


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


def draw(cv2, np, script, status, now, started):
    img = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    kind, _, instruction, detail = script.step
    _put(cv2, img, "Bancada da visao (passo 0)   %s / %s" % (
        clock(now - started), clock(script.end - started)), 40, 0.7, GRAY, 1, x=20)
    color = ORANGE if kind == ACTION else GREEN
    if kind == ACTION:
        cv2.rectangle(img, (0, 80), (WIDTH, 290), (0, 60, 90), -1)
    _put(cv2, img, instruction, 180, 2.2, color, 5)
    _put(cv2, img, detail, 250, 1.0, WHITE, 2)
    if kind == ACTION:
        wait = "Esperando o evento... %s" % clock(script.countdown(now))
    elif script.last_step:
        wait = "Fim em %s" % clock(script.countdown(now))
    else:
        wait = "Proximo pedido em %s" % clock(script.countdown(now))
    _put(cv2, img, wait, 330, 0.9, WHITE, 2)
    if script.last_ok and now - script.last_ok[1] < 10:
        _put(cv2, img, "OK: evento %s gravado" % script.last_ok[0], 380, 0.9, GREEN, 2)
    y = 425
    for line in status_lines(script, status):
        _put(cv2, img, line, y, 0.65, GRAY, 1, x=20)
        y += 30
    if status["problem"]:
        _put(cv2, img, status["problem"], 515, 0.5, RED, 1, x=20)
    _put(cv2, img, "N: pedir agora / pular     Q: encerrar", HEIGHT - 8, 0.5, GRAY, 1, x=WIDTH - 420)
    return img


def draw_final(cv2, np, script, status, crashed, stopped_early):
    img = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
    ok = script.passed() and not crashed and not stopped_early
    _put(cv2, img, "FIM: PASSOU" if ok else "FIM: CONFERIR", 120, 2.0, GREEN if ok else RED, 5)
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
        _put(cv2, img, line, y, 0.65, WHITE, 1, x=20)
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
    parser.add_argument("--seconds", type=float, default=600, help="duração da bancada (padrão: %(default)s)")
    parser.add_argument("--normal", type=float, default=120,
                        help="segundos olhando para a frente antes de cada pedido (padrão: %(default)s)")
    parser.add_argument("--action-timeout", type=float, default=60,
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

    started = time.monotonic()
    script = Script(started, args.seconds, args.normal, args.action_timeout)
    status = {"fps": None, "temp": None, "throttled": None, "problem": None, "summary": None}
    window = "V-GUARD - bancada da visao"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, WIDTH, HEIGHT)
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
            cv2.imshow(window, draw(cv2, np, script, status, now, started))
            key = cv2.waitKey(200) & 0xFF
            if key in (ord("n"), ord("N")):
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
    print("\nBancada da visão: %s de %s" % (clock(time.monotonic() - started), clock(args.seconds)))
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
