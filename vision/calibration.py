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
            raise ValueError("Olhos sobrepostos. F: repetir ou S: usar so postura")
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
            raise ValueError("Amostras invalidas. C: repetir postura")
        neutral = tuple(_angle_mean([_pose(s)[axis] for s in samples]) for axis in range(3))
        deviations = [max(map(abs, relative_pose(neutral, _pose(s)))) for s in samples]
        if max(deviations) > 12 or _quantile(deviations, .90) > 6:
            raise ValueError("Cabeca instavel. C: repetir olhando a estrada")
        profile = cls(width, height, neutral, context)
        profile.validate()
        return profile

    def with_eyes(self, opened, closed):
        if not closed or not all(valid_metrics(s) for s in closed):
            raise ValueError("Sem amostras dos olhos. F: repetir ou S: pular")
        if any(max(map(abs, relative_pose(self.neutral_pose, _pose(s)))) > 8 for s in closed):
            raise ValueError("Cabeca mudou. F: repetir ou S: usar so postura")
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


class CalibrationSession:
    """C: postura; F: olhos opcionais; S: concluir. Coleta nunca emite eventos."""

    def __init__(self, path=None, recalibrate=False, duration=3.0, min_samples=20, context=""):
        self.path = Path(path) if path else None
        self.context = context
        self.profile = None
        self.state = "idle"
        self.message = "SEM CALIBRACAO. C: calibrar postura | Q: sair"
        self.duration, self.min_samples = duration, min_samples
        self.opened, self.closed = [], []
        self.started = self.deadline = self.last_sample = None
        self.size = None
        if self.path and self.path.exists() and not recalibrate:
            self.profile = CalibrationProfile.load(self.path)
            if self.profile.context != context:
                raise ValueError("Camera/IR diferente do perfil; use --calibrate")
            self.state, self.message = "ready", "Perfil carregado. C: recalibrar | Q: sair"
        elif recalibrate or self.path:
            self.state = "wait_neutral"
            self.message = "Veiculo parado. C: calibrar olhando a estrada"

    @property
    def active(self):
        return self.state not in ("idle", "ready")

    def start(self, now):
        self.profile = None
        self.state, self.deadline = "neutral_delay", now + 3
        self.opened, self.closed = [], []
        self.started = self.last_sample = None
        self.message = "Prepare: olhos abertos, olhando a estrada simulada"

    def key(self, key, now):
        if key in (ord("c"), ord("C")):
            self.start(now)
        elif key in (ord("f"), ord("F")) and self.state == "wait_closed":
            self.state, self.deadline = "closed_delay", now + 3
            self.closed = []
            self.started = self.last_sample = None
            self.message = "Prepare: feche os olhos, mantenha a cabeca parada"
        elif key in (ord("s"), ord("S")) and self.state in ("wait_closed", "wait_ready"):
            try:
                if self.path:
                    self.profile.save(self.path)
                self.state = "ready"
                eyes = "auxiliar ocular ativo" if self.profile.left_eye else "so postura"
                self.message = f"Pronto: {eyes}. C: repetir | Q: sair"
            except OSError:
                self.message = "Falha ao salvar. S: tentar novamente | Q: sair"

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
            stage = "ABERTOS, olhando estrada" if self.state == "neutral_delay" else "FECHADOS, cabeca parada"
            self.message = f"Olhos {stage}: coleta em {max(0, self.deadline - now):.0f}s"
            if now < self.deadline:
                return None
            self.state = "neutral" if self.state == "neutral_delay" else "closed"
        samples = self.opened if self.state == "neutral" else self.closed
        if metrics is None or (self.last_sample is not None and now - self.last_sample > .5):
            samples.clear()
            self.started = self.last_sample = None
            if metrics is None:
                self.message = "Rosto perdido. Mantenha a postura; C: recomecar"
                return None
        if self.started is None:
            self.started = now
        self.last_sample = now
        samples.append(metrics)
        elapsed = now - self.started
        stage = "ABERTOS, olhando estrada" if self.state == "neutral" else "FECHADOS, cabeca parada"
        self.message = f"Olhos {stage}: {min(elapsed, self.duration):.1f}/{self.duration:.0f}s"
        if elapsed < self.duration or len(samples) < self.min_samples:
            return None
        if self.state == "neutral":
            try:
                self.profile = CalibrationProfile.from_neutral(samples, width, height, self.context)
                self.state = "wait_closed"
                self.message = "Postura OK. F: medir olhos (6s) | S: usar so postura"
            except ValueError as exc:
                self.state, self.message = "error", str(exc)
        else:
            try:
                self.profile = self.profile.with_eyes(self.opened, self.closed)
                self.state = "wait_ready"
                self.message = "Abra os olhos. S: salvar e iniciar | C: repetir"
            except ValueError as exc:
                self.state, self.message = "wait_closed", str(exc)
        return None
