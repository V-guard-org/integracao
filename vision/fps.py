"""
V-GUARD — Quadros por segundo da visão (proposta §6.8, ticket 1b).

Recebe o instante de cada quadro processado e, a cada PERIOD_S, devolve a
média do período. Não lê o relógio: quem chama passa os instantes, e assim
se testa sem câmera.

    meter = FpsMeter()
    fps = meter.frame(time.monotonic())   # None, ou a média ao fechar 10 s
"""

# Período da média, em segundos. O log e o vision_fps da saúde usam este valor.
PERIOD_S = 10.0


class FpsMeter:
    """Média de quadros por segundo a cada PERIOD_S."""

    def __init__(self, period=PERIOD_S):
        self.period = period
        self._start = None
        self._frames = 0
        # Para a média da execução inteira (o resumo ao encerrar).
        self._first = None
        self._last = None
        self._total = 0

    def frame(self, now):
        """
        Um quadro processado no instante now (em segundos). Devolve a média
        do período quando ele fecha, e None nos outros quadros.
        """
        if self._start is None:
            self._start = self._first = self._last = now
            return None
        self._frames += 1
        self._total += 1
        self._last = now
        elapsed = now - self._start
        if elapsed < self.period:
            return None
        fps = self._frames / elapsed
        self._start, self._frames = now, 0
        return fps

    @property
    def average(self):
        """Média desde o primeiro quadro, contando o período que não fechou; 0 sem 2 quadros."""
        if not self._total or self._last <= self._first:
            return 0.0
        return self._total / (self._last - self._first)
