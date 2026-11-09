"""Referência do motorista/montagem; não é calibração óptica da lente.

Pose é obrigatória no assistente. Olhos são opcionais e só fornecem evidência
auxiliar quando as amostras abertas/fechadas se separam nos dois olhos.
"""
from dataclasses import asdict, dataclass, replace
import json
import logging
import math
import os
from pathlib import Path
import statistics
import tempfile

from options import ConfigError


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
# A janela mostra a câmera e, ao lado, uma coluna com o pedido da vez em letra
# grande, no estilo da bancada guiada (tools/vision_bench.py), que funcionou na
# bancada de 09/11: quem está na tela lê o pedido em voz alta e aperta as
# teclas; quem está na câmera olha para a frente e só faz o que for pedido.
# ESPAÇO segue o pedido da vez, N pula, C recomeça e Q sai. Só ASCII, porque
# a fonte do OpenCV não tem acento.

log = logging.getLogger("vision")

SPACE = 32
DETAIL_LIMIT = 40      # caracteres por linha das frases curtas
KEYS_LIMIT = 60


@dataclass(frozen=True)
class Prompt:
    """
    O que a coluna da tela mostra agora: o passo (linha pequena de cima), o
    pedido em letra grande, até três frases curtas, as teclas e, quando houver,
    a contagem antes da medida, a barra da medida, o retorno ao vivo de uma
    prova e os resultados da conferência.
    """
    step: str
    title: str                  # "\n" marca onde quebrar a linha
    detail: tuple = ()
    keys: str = ""
    tone: str = "info"          # "info" (amarelo), "ok" (verde), "error" (vermelho), "neutral" (branco)
    countdown: str = ""
    progress: float | None = None
    feedback: str = ""
    results: str = ""


def wrap(text, limit=DETAIL_LIMIT):
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


POSE_STEP = "Calibracao 1 de 3: postura"
EYES_STEP = "Calibracao 2 de 3: olhos"
CHECK_STEP = "Calibracao 3 de 3: conferencia"
FACE_LOST = ("Volte a posicao: a medida", "recomeca sozinha.")
MEASURE_KEYS = "C: recomecar   Q: sair"
EYES_KEYS = "N: pular os olhos   C: recomecar   Q: sair"


class CalibrationSession:
    """
    O assistente da calibração, guiado pela tela. ESPAÇO começa: a postura é
    medida olhando para a frente e, logo depois, os olhos fechados (N pula os
    olhos). O perfil é gravado sozinho no fim, e o pipeline começa então a
    conferência guiada (GuidedCheck). A coleta nunca emite eventos.

    Estados: wait_neutral (PRONTOS?), neutral_delay, neutral, neutral_ok,
    closed_delay, closed, closed_ok, error (postura não medida), wait_closed
    (olhos não medidos), save_error, ready (perfil pronto) e idle (sem perfil).
    """

    def __init__(self, path=None, recalibrate=False, duration=3.0, min_samples=20, context="",
                 delay_s=3.0, ok_s=2.0):
        self.path = Path(path) if path else None
        self.context = context
        self.profile = None
        self.state = "idle"
        self.since = None          # quando o estado atual começou
        self.error = None          # o último problema, mostrado na tela
        self.face_lost = False     # o rosto sumiu no meio da medida
        self.saved = False         # o perfil acabou de ser gravado (ver pop_saved)
        self.duration, self.min_samples = duration, min_samples
        self.delay_s, self.ok_s = delay_s, ok_s
        self.opened, self.closed = [], []
        self.started = self.last_sample = None
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

    def _enter(self, state, now):
        self.state, self.since = state, now
        self.started = self.last_sample = None
        self.face_lost = False

    def start(self, now):
        """Começa (ou recomeça) pela postura."""
        self.profile = None
        self.opened, self.closed = [], []
        self.error = None
        self._enter("neutral_delay", now)

    def _start_eyes(self, now):
        self.closed = []
        self.error = None
        self._enter("closed_delay", now)

    def _save(self, now):
        try:
            if self.path:
                self.profile.save(self.path)
        except OSError as exc:
            self.error = f"O perfil nao foi gravado: {exc.strerror or exc}"
            self._enter("save_error", now)
            log.error("calibração: o perfil não foi gravado em %s: %s", self.path, exc)
            return
        self.error = None
        self._enter("ready", now)
        self.saved = True
        eyes = "com os olhos" if self.profile.left_eye else "só com a postura"
        log.info("calibração: perfil gravado %s%s", eyes, f" em {self.path}" if self.path else "")

    def pop_saved(self):
        """True uma vez, logo depois de gravar o perfil: hora de começar a conferência."""
        saved, self.saved = self.saved, False
        return saved

    def key(self, key, now):
        """
        Uma tecla da tela: C recomeça; ESPAÇO começa, mede de novo ou tenta
        gravar de novo; N pula os olhos e grava só a postura.
        """
        if key in (ord("c"), ord("C")):
            self.start(now)
        elif key == SPACE:
            if self.state in ("idle", "wait_neutral", "error"):
                self.start(now)
            elif self.state == "wait_closed":
                self._start_eyes(now)
            elif self.state == "save_error":
                self._save(now)
        elif key in (ord("n"), ord("N")) and self.state in ("closed_delay", "closed", "wait_closed"):
            log.info("calibração: olhos pulados (N)")
            self._save(now)

    def update(self, metrics, now, width, height):
        if self.size is None:
            self.size = (width, height)
        if self.size != (width, height):
            raise ValueError("Resolucao mudou; reinicie e recalibre")
        if self.profile and (self.profile.width, self.profile.height) != (width, height):
            raise ConfigError("Resolucao diferente do perfil; use --calibrate")
        if not valid_metrics(metrics):
            metrics = None
        if not self.active:
            return self.profile.apply(metrics) if self.profile and metrics else metrics

        # Passos com tempo: o OK fica na tela por ok_s; a contagem dura delay_s.
        if self.state == "neutral_ok" and now - self.since >= self.ok_s:
            self._start_eyes(now)
        elif self.state == "closed_ok" and now - self.since >= self.ok_s:
            self._save(now)
        elif self.state in ("neutral_delay", "closed_delay") and now - self.since >= self.delay_s:
            self._enter("neutral" if self.state == "neutral_delay" else "closed", now)
        if self.state not in ("neutral", "closed"):
            return None

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
        if now - self.started < self.duration or len(samples) < self.min_samples:
            return None
        if self.state == "neutral":
            try:
                self.profile = CalibrationProfile.from_neutral(samples, width, height, self.context)
                self._enter("neutral_ok", now)
                log.info("calibração: postura medida")
            except ValueError as exc:
                self.error = str(exc)
                self._enter("error", now)
                log.info("calibração: postura não medida: %s", exc)
        else:
            try:
                self.profile = self.profile.with_eyes(self.opened, self.closed)
                self._enter("closed_ok", now)
                log.info("calibração: olhos medidos")
            except ValueError as exc:
                self.error = str(exc)
                self._enter("wait_closed", now)
                log.info("calibração: olhos não medidos: %s", exc)
        return None

    def _progress(self, now):
        if self.started is None:
            return 0.0
        return min(1.0, (now - self.started) / self.duration)

    def prompt(self, now):
        """O que a coluna da tela mostra agora, em cada estado do assistente."""
        state = self.state
        if state == "idle":
            return Prompt("Sem perfil de calibracao", "SEM CALIBRACAO",
                          ("A pose nao e corrigida pela montagem.",),
                          keys="C: calibrar   Q: sair", tone="neutral")
        if state == "ready":
            return Prompt("Perfil carregado", "MONITORANDO", ("Os eventos sao gravados normalmente.",),
                          keys="V: conferir   C: calibrar de novo   Q: sair", tone="neutral")
        if state == "wait_neutral":
            return Prompt("Calibracao do motorista", "PRONTOS?",
                          ("Na camera: sentar como se dirigisse", "e olhar sempre para a frente.",
                           "Na tela: ler em voz alta o pedido."),
                          keys="ESPACO: comecar   Q: sair", tone="neutral")
        if state == "neutral_delay":
            return Prompt(POSE_STEP, "PECA PARA OLHAR\nPARA A FRENTE",
                          ("Olhos abertos, boca fechada,", "cabeca parada."),
                          countdown=f"A medida comeca em {_countdown(self.since + self.delay_s, now)}",
                          keys=MEASURE_KEYS)
        if state == "neutral":
            if self.face_lost:
                return Prompt(POSE_STEP, "ROSTO FORA\nDA IMAGEM", FACE_LOST, keys=MEASURE_KEYS, tone="error")
            return Prompt(POSE_STEP, "MEDINDO A POSTURA", ("Continue olhando para a frente,", "parado."),
                          progress=self._progress(now), keys=MEASURE_KEYS)
        if state == "neutral_ok":
            return Prompt(POSE_STEP, "OK! POSTURA MEDIDA", ("Agora os olhos.",), tone="ok")
        if state == "error":
            return Prompt(POSE_STEP, "MEDIR DE NOVO",
                          wrap(self.error + ".") + ("Peca para ficar parado olhando", "para a frente."),
                          keys="ESPACO: medir de novo   Q: sair", tone="error")
        if state == "closed_delay":
            return Prompt(EYES_STEP, "PECA PARA FECHAR\nOS OLHOS",
                          ("Cabeca parada, olhos fechados", "ate aparecer OK aqui."),
                          countdown=f"A medida comeca em {_countdown(self.since + self.delay_s, now)}",
                          keys=EYES_KEYS)
        if state == "closed":
            if self.face_lost:
                return Prompt(EYES_STEP, "ROSTO FORA\nDA IMAGEM", FACE_LOST, keys=EYES_KEYS, tone="error")
            return Prompt(EYES_STEP, "MEDINDO OS OLHOS", ("Continue de olhos fechados,", "cabeca parada."),
                          progress=self._progress(now), keys=EYES_KEYS)
        if state == "closed_ok":
            return Prompt(EYES_STEP, "OK! PODE ABRIR\nOS OLHOS", ("Olhos medidos. Gravando o perfil...",),
                          tone="ok")
        if state == "wait_closed":
            return Prompt(EYES_STEP, "OLHOS NAO MEDIDOS",
                          wrap(self.error + ".") + ("Pode medir de novo ou pular.",),
                          keys="ESPACO: medir de novo   N: pular os olhos", tone="error")
        # save_error
        return Prompt(EYES_STEP, "FALHA AO GRAVAR", wrap(self.error + "."),
                      keys="ESPACO: tentar de novo   Q: sair", tone="error")


# ------------------------------------------------------------------------------
# Conferência guiada
# ------------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckStep:
    """Uma prova da conferência e os textos dela."""
    name: str          # a chave nos resultados
    label: str         # o rótulo no resumo
    state: str         # o estado do detector que aprova a prova
    verb: str          # "Daqui a 3 s: pedir para <verb>."
    ask: str           # o pedido, em letra grande
    how: tuple         # como fazer
    insist: tuple      # depois de hint_after_s sem passar
    ok: str            # o OK, em letra grande
    done: str          # a frase do OK


CHECKS = (
    CheckStep("lado", "Lado", "DISTRAIDO", "virar o rosto", "PECA PARA VIRAR\nO ROSTO",
              ("Para o lado (a janela), segurando,", "ate aparecer OK aqui."),
              ("Ainda nao: virar MAIS o rosto", "e segurar."),
              "OK! PODE VOLTAR", "Rosto virado detectado."),
    CheckStep("bocejo", "Bocejo", "FADIGA", "bocejar", "PECA PARA BOCEJAR",
              ("Boca bem aberta, segurando,", "ate aparecer OK aqui."),
              ("Ainda nao: abrir MAIS a boca", "e segurar."),
              "OK! PODE FECHAR\nA BOCA", "Bocejo detectado."),
    CheckStep("estrada", "Estrada", "NORMAL", "ficar parado", "PECA PARA FICAR\nPARADO",
              ("Olhando para a frente,", "ate aparecer OK aqui."),
              ("Ainda nao: ficar parado olhando", "para a frente."),
              "OK! POSTURA NORMAL", "Postura normal reconhecida."),
)
CHECK_KEYS = "N: pular esta prova   C: calibrar de novo   Q: sair"


class GuidedCheck:
    """
    Conferência guiada, logo depois de gravar o perfil (ou com V/ESPAÇO com um
    perfil carregado): três provas mostram se o rosto virado, o bocejo e a
    postura normal são reconhecidos com o perfil. Nenhuma prova grava evento.

    Parte pura: recebe o estado do detector, as métricas já relativas ao
    perfil e o horário (monotônico). Cada prova tem um aviso de `prep_s`
    (PREPARE-SE), com o detector zerado; o pedido, até `run_s`, sem contagem
    na tela; e o resultado (OK ou NAO DETECTOU) por `result_s`.
    """

    def __init__(self, now, prep_s=3.0, run_s=20.0, hint_after_s=8.0, result_s=2.5, hold_s=3.0,
                 yaw_limit=25.0, mar_limit=0.60):
        self.prep_s, self.run_s, self.hint_after_s = prep_s, run_s, hint_after_s
        self.result_s, self.hold_s = result_s, hold_s
        self.yaw_limit, self.mar_limit = yaw_limit, mar_limit
        self.index = 0
        self.phase = "prep"
        self.phase_start = now
        self.results = {}
        self.normal_since = None
        self.metrics = None
        self.face_lost = False

    @property
    def step(self):
        return CHECKS[self.index]

    @property
    def blocking(self):
        """Durante as provas, nenhum evento é gravado."""
        return self.phase != "done"

    @property
    def passed(self):
        return self.phase == "done" and all(self.results.get(step.name) == "OK" for step in CHECKS)

    def _enter(self, phase, now):
        self.phase, self.phase_start, self.normal_since = phase, now, None

    def _result(self, result, now):
        self.results[self.step.name] = result
        self._enter("result", now)

    def begin_frame(self, now):
        """Avança pelo tempo. Devolve True no aviso, quando o detector deve ser zerado."""
        elapsed = now - self.phase_start
        if self.phase == "prep" and elapsed >= self.prep_s:
            self._enter("run", now)
        elif self.phase == "run" and elapsed >= self.run_s:
            self._result("FALHOU", now)
        elif self.phase == "result" and elapsed >= self.result_s:
            if self.index + 1 < len(CHECKS):
                self.index += 1
                self._enter("prep", now)
            else:
                self._enter("done", now)
        return self.phase == "prep"

    def observe(self, driver_state, metrics, now):
        """O estado do detector e as métricas relativas deste quadro."""
        self.metrics, self.face_lost = metrics, metrics is None
        if self.phase != "run":
            return
        if self.step.state == "NORMAL":
            if driver_state == "NORMAL":
                if self.normal_since is None:
                    self.normal_since = now
                if now - self.normal_since >= self.hold_s:
                    self._result("OK", now)
            else:
                self.normal_since = None
        elif driver_state == self.step.state:
            self._result("OK", now)

    def skip(self, now):
        """Tecla N: pula a prova da vez (ou o resultado que está na tela)."""
        if self.phase in ("prep", "run"):
            self._result("PULADA", now)
        elif self.phase == "result":
            self.phase_start = now - self.result_s     # o próximo begin_frame segue

    def results_line(self):
        return " | ".join(f"{step.label} {self.results.get(step.name, '-')}" for step in CHECKS)

    def _feedback(self, now):
        name = self.step.name
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
        """O que a coluna da tela mostra agora."""
        results = self.results_line()
        if self.phase == "done":
            keys = "ESPACO: conferir de novo   C: calibrar de novo   Q: sair"
            if self.passed:
                return Prompt(CHECK_STEP, "CALIBRACAO PRONTA",
                              ("As 3 provas passaram.", "A partir de agora os eventos", "sao gravados normalmente."),
                              results=results, keys=keys, tone="ok")
            return Prompt(CHECK_STEP, "CONFERIR",
                          ("Alguma prova nao passou.", "Confira de novo (ESPACO)", "ou calibre de novo (C)."),
                          results=results, keys=keys, tone="error")
        step = self.step
        label = f"{CHECK_STEP} ({self.index + 1} de {len(CHECKS)})"
        elapsed = now - self.phase_start
        if self.phase == "prep":
            left = _countdown(self.phase_start + self.prep_s, now)
            return Prompt(label, "PREPARE-SE", (f"Daqui a {left} s: pedir para {step.verb}.",),
                          results=results, keys=CHECK_KEYS)
        if self.phase == "run":
            how = step.insist if elapsed >= self.hint_after_s else step.how
            return Prompt(label, step.ask, how, feedback=self._feedback(now), results=results,
                          keys=CHECK_KEYS)
        result = self.results.get(step.name)
        last = self.index + 1 >= len(CHECKS)
        if result == "OK":
            return Prompt(label, step.ok, (step.done,), results=results, tone="ok")
        title = "PROVA PULADA" if result == "PULADA" else "NAO DETECTOU"
        follow = "Fim da conferencia." if last else "Seguindo para a proxima prova."
        return Prompt(label, title, (follow,), results=results, tone="error")
