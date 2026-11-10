"""
Bancada guiada da visão: o passo 0 (Issue #6) e o passo C (Issue #10). Roda
o programa principal sem tela (vguard.py --headless, com a visão num processo
filho) e mostra numa janela da Pi, em letra grande, o que quem lê a tela deve
pedir agora a quem está na frente da câmera: olhar para a frente, bocejar ou
virar o rosto. Cada pedido espera o evento aparecer no log e mostra OK assim
que ele chega. Depois do último pedido, o N termina a bancada (Issue #12);
sem ele, ela acaba sozinha nos 10 min.

    .venv/bin/python tools/vision_bench.py                    # passo 0: só a visão
    .venv/bin/python tools/vision_bench.py --passo-c          # passo C: com o ESP32 e o kill -9
    .venv/bin/python tools/vision_bench.py --seconds 120 --normal 20

No passo C (--passo-c, ou --esp32 e --kill-vision), o principal lê também o
ESP32 (o esp32-sim na USB), a ferramenta mata a visão com kill -9 no meio e
espera ela voltar sozinha, e no fim confere a telemetria no SQLite: ~1 linha
por segundo com imu e obd, nenhuma lacuna acima de 1,5 s (nem no kill -9), o
maior age_ms abaixo de 1 s e o lost/corrupt do ESP32 em 0.

O principal roda no Python dele (VGUARD_PYTHON, o venv-principal ou o atual) e
a visão no dela (VGUARD_VISION_PYTHON). Roda na tela da Pi, pelo Raspberry Pi
Connect; a janela só lê o log. Teclas: ESPAÇO começa; N adianta o passo, pula
o pedido atual ou, depois do último pedido, termina a bancada; Q interrompe,
com o fim "CONFERIR". O log inteiro vai para o terminal e para
<dados>/bancada-visao-<data>.log.

Sai com o código 1 se algum critério não passou.
"""

import argparse
import json
import os
import queue
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

NORMAL, ACTION, KILL = "normal", "action", "kill"

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
# Passo C: a ferramenta mata a visão (kill -9) e espera ela voltar sozinha.
KILL_STEP = {"kind": KILL, "event": "volta", "label": "Visao de volta", "verb": "matar a visao",
             "next": "matar a visao (kill -9)",
             "prep": "Daqui a %d s: a visao vai ser morta (kill -9).",
             "ask": "VISAO MORTA (KILL -9)",
             "how": "Na camera: continuar olhando para a frente.",
             "hint": "Ainda nao voltou: confira o log no terminal.",
             "waiting": "Esperando a visao voltar sozinha...",
             "ok": "OK! A VISAO VOLTOU",
             "done": "A visao voltou sozinha."}


def build_steps(kill=False):
    """Os passos da bancada: no passo C, o kill -9 depois do rosto virado."""
    steps = list(STEPS[:-1])
    if kill:
        steps += [{"kind": NORMAL}, KILL_STEP]
    return tuple(steps + [{"kind": NORMAL}])


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
                 hint_after_s=20.0, steps=STEPS, name="passo 0"):
        self.name = name
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
        self.terminated = False    # o N terminou a última parte antes do máximo

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
        return [step["event"] for step in self.steps if step["kind"] in (ACTION, KILL)]

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
        """
        Tecla N: adianta a fase atual; num pedido, marca como pulado. Na última
        parte, sem mais nada a pedir, termina a bancada (Issue #12).
        """
        if self.phase == "normal" and self.last_step:
            if now < self.end:
                self.end, self.terminated = now, True
        elif self.phase == "normal":
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
                return {"title": "PEDIDOS FEITOS", "color": GREEN,
                        "detail": "N: terminar a bancada agora.",
                        "footer": "Ou fim sozinho em %s" % clock(max(0.0, self.end - now))}
            following = self.steps[self.index + 1]
            left = min(self.normal_s - elapsed, self.end - now)
            return {"title": "OLHE PARA A FRENTE", "color": GREEN,
                    "detail": "Nada a pedir agora.",
                    "footer": "Em %s: %s" % (clock(max(0.0, left)),
                                             following.get("next", "pedir para " + following["verb"]))}
        if self.phase == "prep":
            left = max(1, int(self.prep_s - elapsed + 0.999))
            text = step.get("prep", "Daqui a %d s: pedir para " + step["verb"] + ".")
            return {"title": "PREPARE-SE", "color": ORANGE, "detail": text % left, "footer": ""}
        if self.phase == "ask":
            detail = step["hint"] if elapsed >= self.hint_after_s else step["how"]
            return {"title": step["ask"], "color": ORANGE, "detail": detail,
                    "footer": step.get("waiting", "Esperando a deteccao...")}
        if self.results.get(step["event"]) == "OK":
            return {"title": step["ok"], "color": GREEN,
                    "detail": step.get("done", "%s detectado e gravado." % step["label"]),
                    "footer": "Volte a olhar para a frente."}
        return {"title": "NAO DETECTOU", "color": RED,
                "detail": "%s: %s. Seguindo para o proximo passo." % (
                    step["label"], self.results.get(step["event"], "FALHOU")),
                "footer": "Volte a olhar para a frente."}

    def step_line(self, now):
        """A linha de cima: o passo e o tempo."""
        if not self.started:
            return "Bancada da visao (%s): %s sem tela" % (self.name, clock(self.total_s))
        return "Passo %d de %d     %s / %s" % (
            self.index + 1, len(self.steps), clock(min(now, self.end) - self.start), clock(self.total_s))

    def keys_line(self):
        """A linha das teclas, embaixo da janela."""
        if not self.started:
            return "ESPACO: comecar     Q: sair"
        if self.phase == "normal" and self.last_step:
            return "N: terminar     Q: interromper"
        return "N: adiantar     Q: interromper"

    def recent_outside(self, now, window_s=6.0):
        """O último evento fora de um pedido, se foi há pouco (para avisar na tela)."""
        if self.outside and now - self.outside[-1][1] < window_s:
            return self.outside[-1][0]
        return None


_EVENT = re.compile(r"\[EVENTO REGISTRADO\] ID=\S+ Tipo='(\w+)'")   # UUID hoje, inteiro no ticket 2
_FPS = re.compile(r"visão: ([\d.]+) FPS")
_PI = re.compile(r"Pi: ([\d.]+) °C, throttled (0x[0-9a-fA-F]+)")
_SUMMARY = re.compile(r"resumo: (.*)")
_BACK = re.compile(r"visão de volta")
_FELL = re.compile(r"visão caiu \(código (-?\d+)\)")
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
    if _BACK.search(line):
        return "back", None
    match = _FELL.search(line)
    if match:
        return "fell", int(match.group(1))
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
    vision = "Visao %s   Pi %s   throttled %s" % (
        "carregando..." if status["fps"] is None else "%.1f FPS" % status["fps"],
        "-" if status["temp"] is None else "%.1f C" % status["temp"],
        status["throttled"] or "-")
    if status.get("falls"):
        vision += "   visao caiu %dx" % status["falls"]
    return ["   ".join(results), "Eventos fora do pedido: %s" % outside, vision]


# ---------------------------------------------------------------- critérios

def principal_python(environ=os.environ):
    """O Python do programa principal: VGUARD_PYTHON, o venv-principal ou o atual."""
    chosen = environ.get("VGUARD_PYTHON")
    if chosen:
        return chosen
    venv = os.path.join(os.path.dirname(REPO), "venv-principal", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


def vision_pid(principal_pid, proc="/proc"):
    """O pid da visão: o filho do principal que roda o vision/main.py (Linux, pelo /proc)."""
    try:
        tasks = os.listdir(os.path.join(proc, str(principal_pid), "task"))
    except OSError:
        return None
    for task in tasks:      # o filho pertence à thread que o iniciou
        try:
            with open(os.path.join(proc, str(principal_pid), "task", task, "children")) as stream:
                children = stream.read().split()
        except OSError:
            continue
        for pid in children:
            try:
                with open(os.path.join(proc, pid, "cmdline"), "rb") as stream:
                    command = stream.read().split(b"\0")
            except OSError:
                continue
            if any(part.endswith(b"vision/main.py") for part in command):
                return int(pid)
    return None


def parse_summary(text):
    """O resumo do principal em números (None no que não apareceu)."""
    def number(pattern, kind=float):
        match = re.search(pattern, text or "")
        return kind(match.group(1)) if match else None
    return {
        "fps": number(r"visão ([\d.]+) FPS em média"),
        "restarts": number(r"visão reiniciada (\d+)", int),
        "lost": number(r"ESP32: lost (\d+)", int),
        "corrupt": number(r"ESP32: lost \d+, corrupt (\d+)", int),
        "temp_max": number(r"Pi até ([\d.]+) °C"),
        "throttled_ok": "throttled sem avisos" in (text or ""),
    }


def read_telemetry(data_dir):
    """As linhas de telemetria do SQLite da bancada: [(ts, dados)]."""
    path = os.path.join(data_dir, "vguard.db")
    if not os.path.exists(path):
        return []
    db = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return [(ts, json.loads(data)) for ts, data in db.execute("SELECT ts, data FROM telemetry ORDER BY ts")]
    except sqlite3.Error:
        return []
    finally:
        db.close()


def telemetry_report(rows, start, end, kill_at=None):
    """A telemetria entre start e end (time.time()): linhas, com imu e obd, lacunas e age_ms."""
    inside = [(ts, data) for ts, data in rows if start <= ts <= end]
    gaps = [(b - a, a) for (a, _), (b, _) in zip(inside, inside[1:])]
    near_kill = [gap for gap, at in gaps if kill_at is not None and kill_at - 5 <= at <= kill_at + 30]
    ages = [age for _, data in inside for age in data.get("obd", {}).get("age_ms", {}).values()]
    return {
        "rows": len(inside),
        "both": sum(1 for _, data in inside if {"imu", "obd"} <= set(data)),
        "max_gap": max((gap for gap, _ in gaps), default=None),
        "kill_gap": max(near_kill, default=None),
        "max_age_ms": max(ages, default=None),
    }


def ending(script, crashed, interrupted):
    """Como a bancada acabou, para a linha da duração no terminal."""
    if crashed:
        return "o principal saiu antes"
    if interrupted:
        return "interrompida com o Q"
    if script.terminated:
        return "terminada com o N"
    return "fim pelo tempo"


def criteria(summary, telemetry, script, seconds, esp32=False, kill=False):
    """Os critérios da bancada: [(nome, passou, valor)]."""
    def fmt(value, pattern):
        return "-" if value is None else pattern % value
    items = [("Visao com 12 FPS ou mais", summary["fps"] is not None and summary["fps"] >= 12,
              fmt(summary["fps"], "%.1f FPS"))]
    for name in script.expected():
        items.append((script.label(name), script.results.get(name) == "OK", script.results.get(name, "nao pedido")))
    items.append(("Temperatura abaixo de 75 C", summary["temp_max"] is not None and summary["temp_max"] < 75,
                  fmt(summary["temp_max"], "%.1f C")))
    items.append(("Nenhum aviso do throttled", summary["throttled_ok"],
                  "sem avisos" if summary["throttled_ok"] else "com avisos"))
    if esp32:
        items.append(("ESP32 sem perdas", summary["lost"] == 0 and summary["corrupt"] == 0,
                      "lost %s, corrupt %s" % (summary["lost"], summary["corrupt"])))
        enough = telemetry["rows"] >= 0.95 * seconds and telemetry["both"] >= telemetry["rows"] - 1
        items.append(("Telemetria 1/s com imu e obd", enough,
                      "%d linhas, %d com imu e obd" % (telemetry["rows"], telemetry["both"])))
        items.append(("Nenhuma lacuna acima de 1,5 s",
                      telemetry["max_gap"] is not None and telemetry["max_gap"] <= 1.5,
                      fmt(telemetry["max_gap"], "maior %.2f s")))
        items.append(("age_ms abaixo de 1 s", telemetry["max_age_ms"] is not None and telemetry["max_age_ms"] < 1000,
                      fmt(telemetry["max_age_ms"], "maior %d ms")))
        if kill:
            items.append(("Telemetria inteira no kill -9",
                          telemetry["kill_gap"] is not None and telemetry["kill_gap"] <= 1.5,
                          fmt(telemetry["kill_gap"], "maior lacuna %.2f s")))
    return items


# ---------------------------------------------------------------- janela (OpenCV)

WIDTH, HEIGHT = 960, 540              # onde se desenha
SHOWN = (1152, 648)                   # o tamanho na tela, ampliado para ler pelo Connect
PLACE = (0, 40)                       # canto de cima, abaixo do painel, para caber inteira
BACKGROUND = (28, 28, 28)             # cinza: uma janela toda preta indica que não desenhou
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
    img = np.full((HEIGHT, WIDTH, 3), BACKGROUND, np.uint8)
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
    _put(cv2, img, script.keys_line(), HEIGHT - 8, 0.5, GRAY, 1, x=WIDTH - 360)
    return img


def draw_final(cv2, np, items, status, crashed, interrupted):
    img = np.full((HEIGHT, WIDTH, 3), BACKGROUND, np.uint8)
    ok = all(passed for _, passed, _ in items) and not crashed and not interrupted
    _put(cv2, img, "FIM: PASSOU" if ok else "FIM: CONFERIR", 90, 1.8, BGR[GREEN if ok else RED], 5)
    y = 150
    if crashed:
        _put(cv2, img, "O programa principal saiu antes do fim.", y, 0.6, BGR[RED], 1, x=20)
        y += 28
    if interrupted:
        _put(cv2, img, "Bancada interrompida (Q).", y, 0.6, BGR[RED], 1, x=20)
        y += 28
    for name, passed, value in items:
        _put(cv2, img, "%-6s %s: %s" % ("OK" if passed else "FALHOU", name, value), y, 0.6,
             BGR[GREEN if passed else RED], 1, x=20)
        y += 28
    if status["summary"]:
        _put(cv2, img, "Resumo: " + status["summary"], y + 8, 0.45, GRAY, 1, x=20)
    _put(cv2, img, "Avise o Claude. Q: fechar a janela", HEIGHT - 16, 0.6, BGR[WHITE], 1, x=20)
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
        elif kind == "back":
            if script.step["kind"] == KILL:
                script.event("volta", time.monotonic())
        elif kind == "fell":
            status["falls"] += 1
        else:
            status["problem"] = value


def parse_args(argv=None):
    data = os.environ.get("VGUARD_DATA") or os.path.join(os.path.dirname(REPO), "dados-integracao")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", default=data, help="pasta de dados da bancada (padrão: %(default)s)")
    parser.add_argument("--calibration-file", help="perfil do motorista (padrão: <dados>/calibration-driver.json)")
    parser.add_argument("--source", default="0", help="câmera, como no vguard.py (padrão: %(default)s)")
    parser.add_argument("--seconds", type=float, default=600,
                        help="duração máxima, a partir do ESPAÇO; depois do último pedido, o N "
                             "termina antes (padrão: %(default)s)")
    parser.add_argument("--normal", type=float, default=120,
                        help="segundos olhando para a frente antes de cada pedido (padrão: %(default)s)")
    parser.add_argument("--ask-timeout", type=float, default=60,
                        help="segundos de espera do evento em cada pedido (padrão: %(default)s)")
    parser.add_argument("--esp32", nargs="?", const="/dev/ttyUSB0", metavar="PORTA",
                        help="lê também o ESP32 (padrão da porta: /dev/ttyUSB0) e confere a telemetria")
    parser.add_argument("--kill-vision", action="store_true",
                        help="mata a visão com kill -9 no meio e espera ela voltar sozinha")
    parser.add_argument("--passo-c", action="store_true", help="o passo C: --esp32 e --kill-vision")
    args = parser.parse_args(argv)
    if args.passo_c:
        args.esp32 = args.esp32 or "/dev/ttyUSB0"
        args.kill_vision = True
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
    command = [principal_python(), "-u", "-B", os.path.join(REPO, "vguard.py"), "--headless",
               "--source", args.source, "--no-gps", "--no-sync", "--calibration-file", args.calibration_file]
    env = dict(os.environ, VGUARD_DATA=args.data, PYTHONIOENCODING="utf-8")
    if args.esp32:
        env["VGUARD_ESP32"] = args.esp32
    else:
        command.append("--no-esp32")
    child = subprocess.Popen(command, cwd=REPO, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", bufsize=1)
    lines = queue.Queue()
    reader = threading.Thread(target=_pump, args=(child.stdout, lines, log_path), daemon=True)
    reader.start()

    name = "passo C" if args.esp32 and args.kill_vision else "passo 0"
    script = Script(args.seconds, args.normal, args.ask_timeout, steps=build_steps(args.kill_vision), name=name)
    status = {"fps": None, "temp": None, "throttled": None, "problem": None, "summary": None, "falls": 0}
    started_wall = ended_wall = killed_wall = None
    killed_step = None
    window = "V-GUARD - bancada da visao"
    # Tamanho fixo e sem a barra do OpenCV: na 1ª rodada a janela ficou toda preta,
    # provavelmente pelo zoom da roda do mouse ou pelas setas da barra.
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE | cv2.WINDOW_GUI_NORMAL)

    def show(img):
        cv2.imshow(window, cv2.resize(img, SHOWN, interpolation=cv2.INTER_LINEAR))

    # Sem isto, a janela abre onde o gerenciador quiser, às vezes com metade fora
    # da tela do Connect (a parte de baixo, quase toda escura, era o que aparecia).
    show(np.full((HEIGHT, WIDTH, 3), BACKGROUND, np.uint8))
    cv2.moveWindow(window, *PLACE)

    launched = time.monotonic()
    crashed = interrupted = False
    try:
        while True:
            if _drain(lines, script, status):
                crashed = True
                break
            now = time.monotonic()
            if script.finished(now):
                break
            script.tick(now)
            if script.step["kind"] == KILL and script.phase == "ask" and killed_step != script.index:
                killed_step = script.index
                pid = vision_pid(child.pid)
                if pid is None:
                    status["problem"] = "kill -9: a visao nao estava rodando"
                else:
                    os.kill(pid, signal.SIGKILL)
                    killed_wall = time.time()
                    print("\n[bancada] kill -9 na visão (pid %d)\n" % pid, flush=True)
            show(draw(cv2, np, script, status, now))
            key = cv2.waitKey(200) & 0xFF
            if key == ord(" ") and not script.started:
                script.begin(time.monotonic())
                started_wall = time.time()
            elif key in (ord("n"), ord("N")) and script.started:
                script.skip(time.monotonic())
            elif key in (ord("q"), ord("Q")):
                interrupted = True
                break
    finally:
        ended_wall = time.time()
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        reader.join(timeout=5)
    _drain(lines, script, status)

    outside = ", ".join(event for event, _ in script.outside) or "nenhum"
    measured = 0.0 if not script.started else min(time.monotonic(), script.end) - script.start
    telemetry = telemetry_report(read_telemetry(args.data), started_wall or ended_wall, ended_wall, killed_wall)
    items = criteria(parse_summary(status["summary"]), telemetry, script, measured,
                     esp32=bool(args.esp32), kill=args.kill_vision)
    print("\nBancada da visão (%s): %s, %s (máximo %s; principal rodando por %s)" % (
        script.name, clock(measured), ending(script, crashed, interrupted), clock(args.seconds),
        clock(time.monotonic() - launched)))
    print("O principal saiu antes do fim: %s (código %s)" % ("sim" if crashed else "não", child.returncode))
    for name, passed, value in items:
        print("  %-6s %s: %s" % ("OK" if passed else "FALHOU", name, value))
    print("Eventos fora do pedido: %s" % outside)
    print("Resumo: %s" % (status["summary"] or "não chegou"))
    print("Log: %s" % log_path)

    show(draw_final(cv2, np, items, status, crashed, interrupted))
    while cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) >= 1:
        if (cv2.waitKey(200) & 0xFF) in (ord("q"), ord("Q")):
            break
    cv2.destroyAllWindows()
    passed = all(ok for _, ok, _ in items)
    return 0 if passed and not crashed and not interrupted else 1


if __name__ == "__main__":
    sys.exit(main())
