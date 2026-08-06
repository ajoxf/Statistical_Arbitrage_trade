"""Rolling statistics of the spread (futures - hedge_ratio * spot).

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
        self.last_quote_id = None
        self.collecting_since = None    # see history_sec

    # ------------------------------------------------------------------

    def update(self, value, quote_id=None):
        """Feed one spread observation; refresh frozen stats when due.

        quote_id identifies the market quote the value came from. The
        coordinator polls on a fixed clock, faster than either broker
        actually ticks, so without it the window fills with the SAME
        quote sampled over and over: sigma collapses toward zero and z
        explodes (live 2026-08-06: z +53026 on a 9.13 spread). The
        statistics belong to quote EVENTS, not to poll iterations, so a
        repeated quote adds no sample. Ageing still runs on every call
        — a feed that stops ticking must drain out of the window and
        go cold, not freeze a stale mu/sigma in place.
        """
        now = self.clock()
        fresh = quote_id is None or quote_id != self.last_quote_id
        if fresh:
            self.last_quote_id = quote_id
            if self.collecting_since is None:
                self.collecting_since = now
            self.samples.append((now, value))
            self.last_value = value

        horizon = now - self.cfg['LOOKBACK_SEC']
        while self.samples and self.samples[0][0] < horizon:
            self.samples.popleft()
        if not self.samples:
            # The feed died and everything aged out. Whatever history we
            # had is gone, so the clock starts again rather than
            # crediting time nobody was collecting through.
            self.collecting_since = None

        if (now - self.last_refresh >= self.cfg['STATS_INTERVAL_SEC']
                or self.mu is None):
            self._refresh(now)

    def seed(self, samples):
        """Prime the window from persisted quotes, oldest first.

        `samples` is [(epoch_seconds, spread)]. Restarting is otherwise
        a full re-warm — with MIN_HISTORY_SEC at 120 minutes, every
        config change or crash costs two hours before the engine can
        trade again, even though the quotes were on disk the whole
        time.

        Collection is credited from the OLDEST seeded sample, so
        history_sec reflects when data collection really began rather
        than when this process started. Anything already outside the
        window is dropped on the first update, exactly as if it had
        arrived live.
        """
        if not samples:
            return 0
        now = self.clock()
        horizon = now - self.cfg['LOOKBACK_SEC']
        fresh = [(t, v) for t, v in samples if t >= horizon]
        if not fresh:
            return 0
        fresh.sort(key=lambda row: row[0])
        self.samples.extend(fresh)
        self.last_value = fresh[-1][1]
        self.collecting_since = (fresh[0][0] if self.collecting_since is None
                                 else min(self.collecting_since, fresh[0][0]))
        self._refresh(now)
        return len(fresh)

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
    def suggested_lookback_sec(self):
        """A window long enough to measure the reversion we can see.

        mu/sigma are only meaningful over a window that contains
        several round trips of the series, so the measured AR(1)
        half-life is the honest basis for a suggestion — not a tick
        count. None until a half-life exists; the operator's configured
        LOOKBACK_SEC is never changed automatically.
        """
        if not self.half_life_sec:
            return None
        multiple = self.cfg.get('LOOKBACK_HALF_LIVES', 6.0) or 6.0
        return self.half_life_sec * multiple

    @property
    def history_sec(self):
        """How long we have been collecting, in seconds.

        Deliberately measured from when collection STARTED rather than
        as the span of the window: samples older than LOOKBACK_SEC are
        dropped, so the span can approach but never reach the window
        width, and a "wait for a full window" gate written against the
        span would never be satisfied. Resets if the feed dies and the
        window empties.
        """
        if self.collecting_since is None:
            return 0.0
        return max(0.0, self.clock() - self.collecting_since)

    @property
    def min_history_sec(self):
        """Seconds of data required before trading. Capped at the
        window width — asking for more history than the window keeps
        would be asking for data that has already been discarded."""
        required = self.cfg.get('MIN_HISTORY_SEC', 0.0) or 0.0
        return min(float(required), float(self.cfg['LOOKBACK_SEC']))

    @property
    def warm(self):
        return (len(self.samples) >= self.cfg['MIN_SAMPLES']
                and self.history_sec >= self.min_history_sec
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
