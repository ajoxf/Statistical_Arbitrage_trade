"""Signal generation.

SignalGenerator  — legacy fixed premium thresholds (single-account loop).
ZSignalGenerator — z-score entries on the carry-detrended spread with
entry ceiling, trend filter, cooldowns and z-reset gate. z earns
ENTRIES; exits act on money (see exits.py).
"""

import logging

from . import costs as costs_mod
from .models import SignalType


class SignalGenerator:
    def __init__(self, config):
        self.config = config

    def generate_signal(self, asset, market_data, active_positions):
        """Return a SignalType, or (position_id, SignalType) for exits."""
        basis_pct = market_data['basis_pct']

        if not active_positions:
            if basis_pct > self.config.SIGNAL_THRESHOLDS['PREMIUM_ENTRY']:
                return SignalType.SELL_BASIS
            if basis_pct < self.config.SIGNAL_THRESHOLDS['DISCOUNT_ENTRY']:
                return SignalType.BUY_BASIS

        for position_id, position in active_positions.items():
            if position.signal_type == SignalType.SELL_BASIS:
                if basis_pct <= self.config.SIGNAL_THRESHOLDS['PREMIUM_EXIT']:
                    return (position_id, SignalType.CLOSE_LONG)
            elif position.signal_type == SignalType.BUY_BASIS:
                if basis_pct >= self.config.SIGNAL_THRESHOLDS['DISCOUNT_EXIT']:
                    return (position_id, SignalType.CLOSE_SHORT)

        return SignalType.NO_SIGNAL


class ZSignalGenerator:
    """Entry decisions from the z-score of the detrended spread.

    All gates must pass: warm stats, |z| >= ENTRY_Z, |z| < STOP_Z
    (entry ceiling — never enter on top of the stop), trend filter,
    entry/stop cooldowns, z-reset after a stop, and the edge filter
    (expected capture vs round-trip cost).
    """

    def __init__(self, config, clock):
        self.config = config
        self.clock = clock
        self.last_close_time = {}       # asset -> t
        self.last_stop_time = {}        # asset -> t
        self.blocked_direction = {}     # asset -> SignalType blocked until z-reset

    # -- state fed by the coordinator ---------------------------------

    def notify_close(self, asset, reason, signal_type):
        now = self.clock()
        self.last_close_time[asset] = now
        if (reason or '').upper() in ('STOP_LOSS', 'DOLLAR_STOP', 'Z_STOP'):
            self.last_stop_time[asset] = now
            self.blocked_direction[asset] = signal_type
            logging.info("%s: %s stop — same-direction re-entry blocked "
                         "until z re-enters the exit band", asset, reason)

    def update(self, asset, z):
        """Clear the z-reset block once z returns inside the exit band."""
        if asset in self.blocked_direction and z is not None:
            if abs(z) <= self.config.SIGNALS['EXIT_Z']:
                logging.info("%s: z-reset — re-entry unblocked", asset)
                del self.blocked_direction[asset]

    # -- entry evaluation ----------------------------------------------

    def entry_signal(self, asset, stats, market_data, active_positions,
                     lots, contract_size):
        cfg = self.config.SIGNALS
        if active_positions or not stats.warm:
            return None

        z = stats.z
        if z is None or abs(z) < cfg['ENTRY_Z']:
            return None

        ceiling = cfg.get('MAX_ENTRY_Z', cfg['STOP_Z'])
        if abs(z) >= ceiling:
            logging.info("%s: |z|=%.2f beyond entry ceiling %.2f — a z "
                         "this stretched is a momentum spike mid-flight, "
                         "not a better entry", asset, abs(z), ceiling)
            return None

        # Basis rich (z>0): sell it. Basis cheap (z<0): buy it.
        direction = SignalType.SELL_BASIS if z > 0 else SignalType.BUY_BASIS

        if cfg.get('TREND_FILTER', True):
            slope = stats.trend_slope()
            # Never fight the tape: SELL_BASIS profits when the spread
            # falls — blocked while it is rising, and vice versa.
            if direction == SignalType.SELL_BASIS and slope > 0:
                logging.info("%s: trend filter — spread rising, "
                             "SELL_BASIS blocked", asset)
                return None
            if direction == SignalType.BUY_BASIS and slope < 0:
                logging.info("%s: trend filter — spread falling, "
                             "BUY_BASIS blocked", asset)
                return None

        now = self.clock()
        if now - self.last_close_time.get(asset, -1e18) \
                < cfg['ENTRY_COOLDOWN_SEC']:
            return None
        if now - self.last_stop_time.get(asset, -1e18) \
                < cfg['STOP_COOLDOWN_SEC']:
            return None
        if self.blocked_direction.get(asset) == direction:
            return None

        passes, capture, cost = costs_mod.edge_ok(
            z, stats.sigma, lots, contract_size, market_data,
            self.config.COSTS)
        if not passes:
            logging.info("%s: edge filter — capture $%.0f < %.1fx cost "
                         "$%.0f, no trade", asset, capture,
                         self.config.COSTS['MIN_EDGE_MULTIPLE'], cost)
            return None

        logging.info("%s: ENTRY %s — z=%.2f, capture $%.0f vs cost $%.0f",
                     asset, direction.value, z, capture, cost)
        return direction
