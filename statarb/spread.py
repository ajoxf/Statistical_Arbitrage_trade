"""Rolling statistics of the spread (swap_diff = actual basis minus
swap-implied basis).

Why swap_diff and not raw basis: raw basis drifts deterministically
toward zero as the futures approach expiry, so a rolling mean on it
carries a systematic bias. swap_diff is the carry-detrended spread —
the natural series to z-score.

mu/sigma are FROZEN between refreshes (STATS_INTERVAL_SEC) so the
anchor doesn't chase the spread intra-hold; z uses the live spread
against the frozen stats.
"""

import math
import time as time_mod
from collections import deque


class SpreadStats:
    def __init__(self, signals_cfg, clock=time_mod.time):
        self.cfg = signals_cfg
        self.clock = clock
        self.samples = deque()          # (t, value)
        self.mu = None
        self.sigma = None
        self.half_life_sec = None
        self.last_refresh = 0.0
        self.last_value = None

    # ------------------------------------------------------------------

    def update(self, value):
        """Feed one spread observation; refresh frozen stats when due."""
        now = self.clock()
        self.samples.append((now, value))
        self.last_value = value

        horizon = now - self.cfg['LOOKBACK_SEC']
        while self.samples and self.samples[0][0] < horizon:
            self.samples.popleft()

        if (now - self.last_refresh >= self.cfg['STATS_INTERVAL_SEC']
                or self.mu is None):
            self._refresh(now)

    def _refresh(self, now):
        if len(self.samples) < 2:
            return
        values = [v for _, v in self.samples]
        n = len(values)
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        self.mu = mean
        self.sigma = math.sqrt(variance)
        self.half_life_sec = self._estimate_half_life()
        self.last_refresh = now

    def _estimate_half_life(self):
        """AR(1) fit S_{t+1} = c + phi*S_t; HL = -ln2/ln(phi) steps."""
        values = [v for _, v in self.samples]
        if len(values) < 30:
            return None
        x = values[:-1]
        y = values[1:]
        n = len(x)
        mx = sum(x) / n
        my = sum(y) / n
        cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
        var = sum((a - mx) ** 2 for a in x)
        if var <= 0:
            return None
        phi = cov / var
        if phi <= 0 or phi >= 1:
            return None                     # not mean-reverting in window
        avg_dt = (self.samples[-1][0] - self.samples[0][0]) / max(n - 1, 1)
        return (-math.log(2) / math.log(phi)) * avg_dt

    # ------------------------------------------------------------------

    @property
    def degenerate(self):
        """True when the window cannot support a z-score.

        A spread that barely moved — or a feed that repeated the same
        number while we polled faster than it ticked — gives a sigma
        near zero, and dividing by it produces a meaningless z in the
        tens of thousands (seen live 2026-08-06: z +53026 on a spread
        of 9.13). Anything computed from that is noise: the entry
        ceiling would block the absurd values, but a merely SMALL sigma
        lands z inside the entry band on nothing at all."""
        if self.sigma is None or self.sigma <= 0:
            return True
        floor = self.cfg.get('MIN_SIGMA', 0.0) or 0.0
        if floor and self.sigma < floor:
            return True
        raw = ((self.last_value - self.mu) / self.sigma
               if self.last_value is not None and self.mu is not None
               else 0.0)
        return abs(raw) > self.cfg.get('MAX_ABS_Z', 25.0)

    @property
    def warm(self):
        return (len(self.samples) >= self.cfg['MIN_SAMPLES']
                and self.sigma is not None and not self.degenerate)

    @property
    def z(self):
        if not self.warm or self.last_value is None:
            return None
        return (self.last_value - self.mu) / self.sigma

    def trend_slope(self):
        """Spread change per second over the trend window (sign is what
        the direction filter uses)."""
        window = self.cfg.get('TREND_WINDOW_SEC', 900)
        now = self.clock()
        recent = [(t, v) for t, v in self.samples if t >= now - window]
        if len(recent) < 10:
            return 0.0
        t0 = recent[0][0]
        xs = [t - t0 for t, _ in recent]
        ys = [v for _, v in recent]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        var = sum((a - mx) ** 2 for a in xs)
        if var <= 0:
            return 0.0
        return sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / var
