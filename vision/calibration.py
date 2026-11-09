"""Referência do motorista/montagem; não é calibração óptica da lente.

Pose é obrigatória no assistente. Olhos são opcionais e só fornecem evidência
auxiliar quando as amostras abertas/fechadas se separam nos dois olhos.
"""
from dataclasses import asdict, dataclass, replace
import json
import math
import os
from pathlib import Path
import statistics
import tempfile


def _rotation(angles):
    """Convenção de RQDecomp3x3: Rz(roll) @ Ry(yaw) @ Rx(pitch)."""
    pitch, yaw, roll = map(math.radians, angles)
    sx, sy, sz = math.sin(pitch), math.sin(yaw), math.sin(roll)
    cx, cy, cz = math.cos(pitch), math.cos(yaw), math.cos(roll)
    return ((cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
            (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
            (-sy, cy * sx, cy * cx))


def relative_pose(reference, current):
    """Expressa o movimento no referencial da cabeça neutra: R_neutra.T @ R_atual."""
    base, actual = _rotation(reference), _rotation(current)
    rotation = [[sum(base[k][i] * actual[k][j] for k in range(3))
                 for j in range(3)] for i in range(3)]
    return tuple(map(math.degrees, (
        math.atan2(rotation[2][1], rotation[2][2]),
        math.atan2(-rotation[2][0], math.hypot(rotation[0][0], rotation[1][0])),
        math.atan2(rotation[1][0], rotation[0][0]))))


def _pose(metrics):
    return metrics.pitch, metrics.yaw, metrics.roll


def valid_metrics(metrics):
    return metrics is not None and all(math.isfinite(value) for value in (
        metrics.ear_left, metrics.ear_right, metrics.ear, metrics.mar, *_pose(metrics)))


def _quantile(values, q):
    values = sorted(values)
    index = (len(values) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def _angle_mean(values):
    radians = [math.radians(value) for value in values]
    return math.degrees(math.atan2(sum(map(math.sin, radians)), sum(map(math.cos, radians))))


@dataclass(frozen=True)
class EyeReference:
    closed_high: float
    open_low: float
    low: float
    high: float

    @classmethod
    def from_samples(cls, opened, closed):
        closed_high, open_low = _quantile(closed, .90), _quantile(opened, .20)
        if open_low - closed_high < max(.025, .20 * statistics.median(opened)):
            raise ValueError("Olhos abertos e fechados ficaram parecidos")
        return cls(closed_high, open_low, max(0, _quantile(closed, .10) - .03),
                   _quantile(opened, .90) + .08)

    def validate(self):
        values = (self.low, self.closed_high, self.open_low, self.high)
        if (not all(type(x) in (int, float) and math.isfinite(x) for x in values)
                or not 0 <= self.low <= self.closed_high < self.open_low <= self.high
                or self.open_low - self.closed_high < .025):
            raise ValueError("Referencia ocular invalida")

    def classify(self, value):
        if not self.low <= value <= self.high:
            return None
        if value <= self.closed_high:
            return True
        if value >= self.open_low:
            return False
        return None  # faixa ambígua: peso zero


@dataclass(frozen=True)
class CalibrationProfile:
    width: int
    height: int
    neutral_pose: tuple
    context: str = ""
    left_eye: EyeReference | None = None
    right_eye: EyeReference | None = None

    def validate(self):
        if (type(self.width) is not int or type(self.height) is not int
                or self.width <= 0 or self.height <= 0 or len(self.neutral_pose) != 3
                or not isinstance(self.context, str)):
            raise ValueError("Resolucao ou pose invalida no perfil")
        if not all(type(x) in (int, float) and math.isfinite(x) and abs(x) <= 180
                   for x in self.neutral_pose):
            raise ValueError("Perfil contem angulos invalidos")
        if abs(self.neutral_pose[1]) >= 75:
            raise ValueError("Rosto muito de perfil: reposicione a camera")
        if (self.left_eye is None) != (self.right_eye is None):
            raise ValueError("Perfil ocular incompleto")
        if self.left_eye:
            self.left_eye.validate()
            self.right_eye.validate()

    @classmethod
    def from_neutral(cls, samples, width, height, context=""):
        if not samples or not all(valid_metrics(s) for s in samples):
            raise ValueError("Medidas do rosto invalidas")
        neutral = tuple(_angle_mean([_pose(s)[axis] for s in samples]) for axis in range(3))
        deviations = [max(map(abs, relative_pose(neutral, _pose(s)))) for s in samples]
        if max(deviations) > 12 or _quantile(deviations, .90) > 6:
            raise ValueError("A cabeca mexeu durante a medida")
        profile = cls(width, height, neutral, context)
        profile.validate()
        return profile

    def with_eyes(self, opened, closed):
        if not closed or not all(valid_metrics(s) for s in closed):
            raise ValueError("Sem medidas dos olhos fechados")
        if any(max(map(abs, relative_pose(self.neutral_pose, _pose(s)))) > 8 for s in closed):
            raise ValueError("A cabeca mexeu com os olhos fechados")
        profile = replace(self,
            left_eye=EyeReference.from_samples([s.ear_left for s in opened], [s.ear_left for s in closed]),
            right_eye=EyeReference.from_samples([s.ear_right for s in opened], [s.ear_right for s in closed]))
        profile.validate()
        return profile

    def apply(self, metrics):
        pitch, yaw, roll = relative_pose(self.neutral_pose, _pose(metrics))
        closure = None
        # Limites experimentais de uso, não uma estimativa de probabilidade.
        # Além deles a perspectiva difere demais da coleta para confiar no EAR.
        if (self.left_eye and abs(yaw) <= 35 and abs(pitch) <= 30 and abs(roll) <= 15
                and abs(metrics.yaw) < 65):
            left = self.left_eye.classify(metrics.ear_left)
            right = self.right_eye.classify(metrics.ear_right)
            if left is not None and left == right:
                closure = left
        return replace(metrics, pitch=pitch, yaw=yaw, roll=roll, eye_closure=closure)

    def save(self, path):
        self.validate()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".calibration-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": 2, **asdict(self)}, stream, indent=2, allow_nan=False)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @classmethod
    def load(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if data.pop("version", None) != 2:
                raise ValueError("Versao de calibracao desconhecida; recalibre")
            data["neutral_pose"] = tuple(data["neutral_pose"])
            for field in ("left_eye", "right_eye"):
                if data.get(field) is not None:
                    data[field] = EyeReference(**data[field])
            profile = cls(**data)
            profile.validate()
            return profile
        except (TypeError, KeyError, AttributeError) as exc:
            raise ValueError("Formato de calibracao invalido; recalibre") from exc




# ==============================================================================
# Tela da calibração e da conferência (Issue #7)
# ==============================================================================
# Os textos aparecem num painel por cima da imagem, em letra grande: só ASCII,
# porque a fonte do OpenCV não tem acento, e no máximo LINE_LIMIT caracteres
# por linha, para caber nos 640 px.

LINE_LIMIT = 36
KEYS_LIMIT = 52


@dataclass(frozen=True)
class Prompt:
    """
    O que a tela mostra agora. Sem `compact`, um painel com o passo, as
    instruções, o retorno de uma prova, os resultados e as teclas, mais a
    contagem regressiva (preparação) ou a barra de progresso (coleta).
    Com `compact`, uma linha só no rodapé, para não cobrir o rosto.
    """
    step: str
    lines: tuple = ()
    keys: str = ""
    countdown: int | None = None
    progress: float | None = None
    feedback: str = ""
    results: str = ""
    tone: str = "info"          # "info", "ok" ou "error": a cor do painel
    compact: bool = False


def wrap(text, limit=LINE_LIMIT):
    """Quebra um texto em linhas de no máximo `limit` caracteres, pelas palavras."""
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= limit or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return tuple(lines)


def _countdown(deadline, now):
    return max(1, math.ceil(deadline - now))


POSE_STEP = "Passo 1 de 2: postura"
EYES_STEP = "Passo 2 de 2: olhos (opcional)"
FACE_LOST = ("Rosto fora da imagem.", "Volte a posicao: a medida recomeca.")


class CalibrationSession:
    """
    C: postura; F: olhos opcionais; S: concluir. Coleta nunca emite eventos.
    O `prompt()` diz o que a tela mostra em cada estado.
    """

    def __init__(self, path=None, recalibrate=False, duration=3.0, min_samples=20, context=""):
        self.path = Path(path) if path else None
        self.context = context
        self.profile = None
        self.state = "idle"
        self.error = None          # o último problema, mostrado no painel
        self.face_lost = False     # o rosto sumiu no meio da coleta
        self.duration, self.min_samples = duration, min_samples
        self.opened, self.closed = [], []
        self.started = self.deadline = self.last_sample = None
        self.size = None
        if self.path and self.path.exists() and not recalibrate:
            self.profile = CalibrationProfile.load(self.path)
            if self.profile.context != context:
                raise ValueError("Camera/IR diferente do perfil; use --calibrate")
            self.state = "ready"
        elif recalibrate or self.path:
            self.state = "wait_neutral"

    @property
    def active(self):
        return self.state not in ("idle", "ready")

    def start(self, now):
        self.profile = None
        self.state, self.deadline = "neutral_delay", now + 3
        self.opened, self.closed = [], []
        self.started = self.last_sample = None
        self.error, self.face_lost = None, False

    def key(self, key, now):
        """Trata uma tecla. Devolve True quando o S gravou o perfil."""
        if key in (ord("c"), ord("C")):
            self.start(now)
        elif key in (ord("f"), ord("F")) and self.state in ("wait_closed", "wait_ready"):
            self.state, self.deadline = "closed_delay", now + 3
            self.closed = []
            self.started = self.last_sample = None
            self.error, self.face_lost = None, False
        elif key in (ord("s"), ord("S")) and self.state in ("wait_closed", "wait_ready"):
            try:
                if self.path:
                    self.profile.save(self.path)
            except OSError:
                self.error = "Falha ao gravar o perfil"
                return False
            self.state, self.error = "ready", None
            return True
        return False

    def update(self, metrics, now, width, height):
        if self.size is None:
            self.size = (width, height)
        if self.size != (width, height):
            raise ValueError("Resolucao mudou; reinicie e recalibre")
        if self.profile and (self.profile.width, self.profile.height) != (width, height):
            raise ValueError("Resolucao diferente do perfil; use --calibrate")
        if not valid_metrics(metrics):
            metrics = None
        if not self.active:
            return self.profile.apply(metrics) if self.profile and metrics else metrics
        if self.state in ("wait_neutral", "wait_closed", "wait_ready", "error"):
            return None
        if self.state.endswith("_delay"):
            if now < self.deadline:
                return None
            self.state = "neutral" if self.state == "neutral_delay" else "closed"
        samples = self.opened if self.state == "neutral" else self.closed
        if metrics is None or (self.last_sample is not None and now - self.last_sample > .5):
            samples.clear()
            self.started = self.last_sample = None
            if metrics is None:
                self.face_lost = True
                return None
        self.face_lost = False
        if self.started is None:
            self.started = now
        self.last_sample = now
        samples.append(metrics)
        elapsed = now - self.started
        if elapsed < self.duration or len(samples) < self.min_samples:
            return None
        if self.state == "neutral":
            try:
                self.profile = CalibrationProfile.from_neutral(samples, width, height, self.context)
                self.state = "wait_closed"
            except ValueError as exc:
                self.state, self.error = "error", str(exc)
        else:
            try:
                self.profile = self.profile.with_eyes(self.opened, self.closed)
                self.state = "wait_ready"
            except ValueError as exc:
                self.state, self.error = "wait_closed", str(exc)
        return None

    def _progress(self, now):
        if self.started is None:
            return 0.0
        return min(1.0, (now - self.started) / self.duration)

    def prompt(self, now):
        """O que a tela mostra agora, em cada estado do assistente."""
        state = self.state
        if state == "idle":
            return Prompt("Sem calibracao", keys="C: calibrar   Q: sair", compact=True)
        if state == "ready":
            return Prompt("Perfil carregado", keys="V: conferir   C: recalibrar   Q: sair", compact=True)
        if state == "wait_neutral":
            return Prompt(POSE_STEP, ("Sente como se estivesse dirigindo.",
                                      "Olhe um ponto a frente, como a",
                                      "estrada. Nao olhe para a tela."),
                          keys="C: comecar   Q: sair")
        if state == "neutral_delay":
            return Prompt(POSE_STEP, ("Olhe para a estrada agora.", "Olhos abertos, boca fechada."),
                          countdown=_countdown(self.deadline, now))
        if state == "neutral":
            if self.face_lost:
                return Prompt(POSE_STEP, FACE_LOST, keys="C: recomecar   Q: sair", tone="error")
            return Prompt(POSE_STEP, ("Continue olhando a estrada,", "com a cabeca parada."),
                          progress=self._progress(now), keys="C: recomecar")
        if state == "error":
            return Prompt(POSE_STEP, wrap(self.error) + ("Aperte C e olhe para a estrada.",),
                          keys="C: repetir   Q: sair", tone="error")
        if state == "wait_closed":
            if self.error:
                return Prompt(EYES_STEP, wrap(self.error) + ("Meca de novo ou salve so a postura.",),
                              keys="F: medir de novo   S: salvar so a postura", tone="error")
            return Prompt(EYES_STEP, ("Postura medida.", "Agora pode medir os olhos fechados."),
                          keys="F: medir os olhos   S: pular e salvar", tone="ok")
        if state == "closed_delay":
            return Prompt(EYES_STEP, ("Cabeca parada. Feche os olhos", "agora e conte ate 6 para abrir."),
                          countdown=_countdown(self.deadline, now))
        if state == "closed":
            if self.face_lost:
                return Prompt(EYES_STEP, FACE_LOST, keys="C: recomecar   Q: sair", tone="error")
            return Prompt(EYES_STEP, ("Olhos fechados, cabeca parada.",),
                          progress=self._progress(now), keys="C: recomecar")
        # wait_ready
        return Prompt(EYES_STEP, ("Olhos medidos. Pode abrir os olhos.",),
                      keys="S: salvar   F: medir de novo   C: recomecar", tone="ok")


# ------------------------------------------------------------------------------
# Conferência guiada
# ------------------------------------------------------------------------------

# (nome, rótulo, estado do detector que aprova, o que fazer, o que vem depois)
CHECKS = (
    ("lado", "Lado", "DISTRAIDO",
     ("Vire o rosto para o lado (a janela)", "e segure ate aparecer OK."), "virar o rosto para o lado"),
    ("bocejo", "Bocejo", "FADIGA",
     ("Boceje: abra bem a boca e segure", "ate aparecer OK."), "bocejar"),
    ("estrada", "Estrada", "NORMAL",
     ("Olhe para a estrada e fique parado.",), "ficar parado olhando a estrada"),
)


class GuidedCheck:
    """
    Conferência guiada, depois de salvar o perfil (ou com a tecla V): três
    provas mostram se o rosto virado, o bocejo e a postura normal são
    reconhecidos com o perfil novo. Nenhuma prova grava evento.

    Parte pura: recebe o estado do detector, as métricas já relativas ao
    perfil e o horário (monotônico). Cada prova tem uma preparação de
    `prep_s` com o detector zerado e até `run_s` para passar.
    """

    def __init__(self, now, prep_s=3.0, run_s=10.0, hold_s=3.0, yaw_limit=25.0, mar_limit=0.60,
                 done_panel_s=8.0):
        self.prep_s, self.run_s, self.hold_s = prep_s, run_s, hold_s
        self.yaw_limit, self.mar_limit = yaw_limit, mar_limit
        self.done_panel_s = done_panel_s
        self.index = 0
        self.phase = "prep"
        self.phase_start = now
        self.results = {}
        self.normal_since = None
        self.metrics = None
        self.face_lost = False

    @property
    def blocking(self):
        """Nas provas, nenhum evento é gravado."""
        return self.phase in ("prep", "run")

    @property
    def passed(self):
        return self.phase == "done" and all(self.results.get(name) == "OK" for name, *_ in CHECKS)

    def begin_frame(self, now):
        """Avança pelo tempo. Devolve True na preparação, quando o detector deve ser zerado."""
        if self.phase == "prep" and now - self.phase_start >= self.prep_s:
            self.phase, self.phase_start, self.normal_since = "run", now, None
        elif self.phase == "run" and now - self.phase_start >= self.run_s:
            self._finish("FALHOU", now)
        return self.phase == "prep"

    def observe(self, driver_state, metrics, now):
        """O estado do detector e as métricas relativas deste quadro."""
        self.metrics, self.face_lost = metrics, metrics is None
        if self.phase != "run":
            return
        expected = CHECKS[self.index][2]
        if expected == "NORMAL":
            if driver_state == "NORMAL":
                if self.normal_since is None:
                    self.normal_since = now
                if now - self.normal_since >= self.hold_s:
                    self._finish("OK", now)
            else:
                self.normal_since = None
        elif driver_state == expected:
            self._finish("OK", now)

    def _finish(self, result, now):
        self.results[CHECKS[self.index][0]] = result
        self.phase_start = now
        if self.index + 1 < len(CHECKS):
            self.index += 1
            self.phase = "prep"
        else:
            self.phase = "done"

    def results_line(self):
        return " | ".join(f"{label} {self.results.get(name, '-')}" for name, label, *_ in CHECKS)

    def _feedback(self, now):
        name = CHECKS[self.index][0]
        if self.face_lost:
            return "Rosto fora da imagem: vire menos." if name == "lado" else "Rosto fora da imagem."
        if self.metrics is None:
            return ""
        if name == "lado":
            return f"Lado: {abs(self.metrics.yaw):.0f} de {self.yaw_limit:.0f} graus"
        if name == "bocejo":
            return f"Boca: {self.metrics.mar:.2f} de {self.mar_limit:.2f}"
        held = 0.0 if self.normal_since is None else now - self.normal_since
        return f"Parado: {min(held, self.hold_s):.1f} de {self.hold_s:.0f} s"

    def prompt(self, now):
        """O que a tela mostra agora."""
        keys = "P: pular a conferencia   C: recalibrar"
        if self.phase == "done":
            done_keys = "V: conferir de novo   C: recalibrar   Q: sair"
            if now - self.phase_start >= self.done_panel_s:
                return Prompt("Conferencia: " + self.results_line(), keys=done_keys, compact=True)
            if self.passed:
                return Prompt("Conferencia concluida", ("Tudo certo: lado, bocejo e estrada.",),
                              results=self.results_line(), keys=done_keys, tone="ok")
            return Prompt("Conferencia concluida", ("Alguma prova falhou.", "Recalibre (C) ou confira de novo (V)."),
                          results=self.results_line(), keys=done_keys, tone="error")
        name, label, _, todo, upcoming = CHECKS[self.index]
        step = f"Conferencia {self.index + 1} de {len(CHECKS)}: {label.lower()}"
        if self.phase == "prep":
            last = self.results.get(CHECKS[self.index - 1][0]) if self.index else None
            tone = {"OK": "ok", "FALHOU": "error"}.get(last, "info")
            return Prompt(step, ("Volte a olhar para a estrada.", *wrap(f"Depois: {upcoming}.")),
                          countdown=_countdown(self.phase_start + self.prep_s, now),
                          results=self.results_line(), keys=keys, tone=tone)
        return Prompt(step, todo, feedback=self._feedback(now), results=self.results_line(), keys=keys)
