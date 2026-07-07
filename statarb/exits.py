"""Exit ladder — levels in DOLLARS, frozen at entry from actual fills.

z is for ENTRIES; exits act on money. Post-entry, the rolling z is a
drifting statistic — never let it be the thing that pays or stops you.

Priority each tick (risk first):
1. DOLLAR_STOP   — ungated, gross P&L, straight to market. Tighter of
                   TP/RR, %-of-capital (when LEVERAGE set) and per-lot.
2. TAKE_PROFIT   — ungated, P&L alone. Precedence: sigma-fraction >
                   %-of-capital > fixed $, with a cost floor.
3. REVERSION_EXIT— gated: z home AND net >= floor — but the gate MUST
                   defer to max-hold: past 1x the floor decays to
                   break-even, past 2x it releases entirely (the
                   reversion edge is spent — take what's there).
                   Deadlock fix: gate + max-hold + unreachable TP once
                   jointly held a fully reverted trade at +$1.19 for
                   being 2 cents under the floor, then bled 80 minutes
                   to a -$4.46 stop. Fail-open if P&L can't be priced.
4. MAX_HOLD      — after Nx half-life, exit only if net > 0; suppressed
                   while z-progress >= 50% toward home ONLY when a TP
                   exists (never wait for a TP that is configured off).
5. TIME_STOP     — hard clock at ~3x max-hold regardless of P&L: the
                   sideways loser (net < 0, z never reverting) has no
                   other exit (exit-path completeness rule).
6. Z_STOP        — demoted: in-trade risk is dollars. Fires only when
                   explicitly enabled OR when no dollar stop is armed
                   (a trade must always have a stop). When disabled,
                   every would-have-fired occasion is LOGGED so the
                   design change is scoreable with data.
"""

import logging

from . import costs as costs_mod
from .models import SignalType


def outcome_tag(close_reason, z_reverted):
    """Deterministic post-trade outcome label (numbers-first review)."""
    reason = (close_reason or '').upper()
    if reason == 'TAKE_PROFIT':
        return 'TARGET_HIT'
    if reason == 'REVERSION_EXIT':
        return 'REVERSION_BANKED'
    if reason in ('MAX_HOLD', 'TIME_STOP'):
        return 'TIME_EXIT'
    if reason in ('DOLLAR_STOP', 'Z_STOP', 'STOP_LOSS'):
        return ('STOPPED_AFTER_FULL_REVERSION' if z_reverted
                else 'STOPPED_IN_TREND')
    return reason


class ExitLadder:
    def __init__(self, config):
        self.config = config
        self._z_stop_logged = set()   # position ids already logged

    # ------------------------------------------------------------------

    def _capital_at_risk(self, lots, contract_size, market_data):
        """Sum of leg notionals / leverage, with an m2m buffer. None
        when LEVERAGE is unset — %-capital forms are then disabled."""
        leverage = self.config.EXITS.get('LEVERAGE', 0)
        if not leverage:
            return None
        oz = lots * contract_size
        notional = (market_data['spot_price']
                    + market_data['futures_price']) * oz
        buffer = 1 + self.config.EXITS.get('M2M_BUFFER_PCT', 0) / 100
        return notional / leverage * buffer

    def build_plan(self, lots, contract_size, entry_z, sigma, half_life_sec,
                   market_data):
        """Compute the frozen exit levels. Returns None when the trade
        can never win (cost floor above plausible full reversion) —
        the entry must then be blocked."""
        exits = self.config.EXITS
        oz = lots * contract_size
        capital = self._capital_at_risk(lots, contract_size, market_data)

        # Take-profit precedence: sigma-fraction > %-capital > fixed $
        tp = None
        if exits.get('USE_SIGMA_TARGET', True) and sigma and entry_z:
            tp = self.config.COSTS['TARGET_FRACTION'] * abs(entry_z) \
                * sigma * oz
        elif exits.get('TP_CAPITAL_PCT', 0) > 0 and capital:
            tp = exits['TP_CAPITAL_PCT'] / 100 * capital
        elif exits.get('TP_USD_PER_LOT', 0) > 0:
            tp = exits['TP_USD_PER_LOT'] * lots

        rt_cost = costs_mod.round_trip_cost(
            market_data, lots, contract_size, self.config.COSTS)

        if tp is not None:
            floor = exits.get('COST_FLOOR_MULT', 1.0) * rt_cost
            tp = max(tp, floor)
            plausible = abs(entry_z or 0) * (sigma or 0) * oz
            if plausible > 0 and tp > plausible:
                logging.info(
                    "Exit plan not viable: cost floor $%.0f exceeds "
                    "plausible full reversion $%.0f — blocking entry",
                    tp, plausible)
                return None

        # Stop: the TIGHTER of every armed form
        candidates = [exits.get('STOP_USD_PER_LOT', 0) * lots]
        if exits.get('STOP_CAPITAL_PCT', 0) > 0 and capital:
            candidates.append(exits['STOP_CAPITAL_PCT'] / 100 * capital)
        rr = exits.get('RR', 0)
        if tp and rr > 0:
            candidates.append(tp / rr)
        armed = [c for c in candidates if c > 0]
        stop = min(armed) if armed else 0.0

        if half_life_sec:
            max_hold = exits.get('MAX_HOLD_HALF_LIVES', 4) * half_life_sec
        else:
            max_hold = exits.get('MAX_HOLD_FALLBACK_MIN', 240) * 60

        plan = {
            'tp_usd': tp,
            'stop_usd': stop,
            'gate_floor_usd': exits.get('GATE_FLOOR_USD', 0.0),
            'max_hold_sec': max_hold,
            'entry_z': entry_z,
            'entry_sigma': sigma,
            'rt_cost_usd': rt_cost,
            'capital_at_risk': capital,
        }
        # Print the RESOLVED levels, not the configs — a cost floor can
        # silently pin a %-target higher at the operating size
        logging.info("Exit plan (RESOLVED): TP=$%s STOP=$%.0f gate=$%.0f "
                     "max_hold=%.0fmin time_stop=%.0fmin (cost $%.0f%s)",
                     f"{tp:.0f}" if tp else "off", stop,
                     plan['gate_floor_usd'], max_hold / 60,
                     max_hold * exits.get('HARD_TIME_STOP_MULT', 0) / 60,
                     rt_cost,
                     f", capital ${capital:,.0f}" if capital else "")
        return plan

    # ------------------------------------------------------------------

    def evaluate(self, position, plan, z, net_pnl, age_sec):
        """Return an exit reason string, or None to keep holding."""
        exits = self.config.EXITS
        cfg = self.config.SIGNALS
        max_hold = plan['max_hold_sec']

        # 1. Dollar stop — ungated, gross move
        if plan['stop_usd'] and net_pnl is not None \
                and net_pnl <= -plan['stop_usd']:
            return 'DOLLAR_STOP'

        # 2. Take profit — ungated, money alone
        if plan['tp_usd'] and net_pnl is not None \
                and net_pnl >= plan['tp_usd']:
            return 'TAKE_PROFIT'

        # 3. Reversion exit — gate floor decays with age (deadlock fix)
        if z is not None and abs(z) <= cfg['EXIT_Z']:
            if net_pnl is None:
                return 'REVERSION_EXIT'              # fail-open
            if age_sec >= 2 * max_hold:
                return 'REVERSION_EXIT'              # gate released
            floor = plan['gate_floor_usd']
            if age_sec >= max_hold:
                floor = 0.0                          # decayed to break-even
            if net_pnl >= floor:
                return 'REVERSION_EXIT'
            # else: hold — never book a losing "profit-take" early

        # 4. Max hold — only walk away with a profit; suppressed while
        # still travelling toward an EXISTING take-profit
        if age_sec >= max_hold and net_pnl is not None and net_pnl > 0:
            suppressed = False
            entry_z = plan.get('entry_z')
            if plan['tp_usd'] and entry_z and z is not None \
                    and abs(entry_z) > 0:
                progress = 1.0 - abs(z) / abs(entry_z)
                suppressed = progress >= exits.get(
                    'MAX_HOLD_PROGRESS_SUPPRESS', 0.5)
            if not suppressed:
                return 'MAX_HOLD'

        # 5. Hard time stop — ANY P&L; the sideways loser's only clock
        time_stop_mult = exits.get('HARD_TIME_STOP_MULT', 0)
        if time_stop_mult and age_sec >= time_stop_mult * max_hold:
            return 'TIME_STOP'

        # 6. z-stop — demoted to entry-ceiling duty
        if z is not None:
            adverse = (
                (position.signal_type == SignalType.SELL_BASIS
                 and z >= cfg['STOP_Z'])
                or (position.signal_type == SignalType.BUY_BASIS
                    and z <= -cfg['STOP_Z']))
            if adverse:
                dollar_stop_armed = plan.get('stop_usd', 0) > 0
                if exits.get('Z_STOP_EXIT_ENABLED', False) \
                        or not dollar_stop_armed:
                    return 'Z_STOP'   # fail-safe: always have SOME stop
                if position.position_id not in self._z_stop_logged:
                    self._z_stop_logged.add(position.position_id)
                    logging.warning(
                        "Z-STOP WOULD HAVE FIRED for %s at z=%.2f (gross "
                        "$%.2f, dollar line -$%.0f) — disabled while the "
                        "dollar stop is armed; logged for design scoring",
                        position.position_id, z, net_pnl or 0,
                        plan['stop_usd'])

        return None
