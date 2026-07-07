"""Exit ladder — levels in DOLLARS, frozen at entry from actual fills.

z is for ENTRIES; exits act on money. The rolling mean chases the
spread during a hold, so in-trade z can "revert" without the price
paying you — every exit level here is a fixed dollar amount computed
once at entry.

Priority each tick (risk first):
1. DOLLAR_STOP   — ungated, fires on gross P&L, goes to market.
2. TAKE_PROFIT   — ungated, P&L alone; sigma-fraction target with a
                   cost floor (never target less than costs).
3. REVERSION_EXIT— gated: z back inside the band AND net >= gate
                   floor. Never book a losing "profit-take".
                   Fail-open: if P&L can't be priced, allow the exit.
4. MAX_HOLD      — after N x half-life, exit only if net > 0.
5. Z_STOP        — backstop; the dollar stop usually wins first.
"""

import logging

from . import costs as costs_mod
from .models import SignalType


class ExitLadder:
    def __init__(self, config):
        self.config = config

    # ------------------------------------------------------------------

    def build_plan(self, lots, contract_size, entry_z, sigma, half_life_sec,
                   market_data):
        """Compute the frozen exit levels. Returns None when the trade
        can never win (cost floor above plausible full reversion) —
        the entry must then be blocked."""
        exits = self.config.EXITS
        oz = lots * contract_size

        # Take-profit: sigma-fraction > fixed-$ fallback
        tp = None
        if exits.get('USE_SIGMA_TARGET', True) and sigma and entry_z:
            tp = self.config.COSTS['TARGET_FRACTION'] * abs(entry_z) \
                * sigma * oz
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

        # Stop: the TIGHTER of RR-derived and per-lot catastrophe cap
        candidates = [exits.get('STOP_USD_PER_LOT', 0) * lots]
        rr = exits.get('RR', 0)
        if tp and rr > 0:
            candidates.append(tp / rr)
        stop = min(c for c in candidates if c > 0)

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
        }
        logging.info("Exit plan: TP=$%s STOP=$%.0f gate=$%.0f "
                     "max_hold=%.0fmin (cost $%.0f)",
                     f"{tp:.0f}" if tp else "off", stop,
                     plan['gate_floor_usd'], max_hold / 60, rt_cost)
        return plan

    # ------------------------------------------------------------------

    def evaluate(self, position, plan, z, net_pnl, age_sec):
        """Return an exit reason string, or None to keep holding."""
        exits = self.config.EXITS
        cfg = self.config.SIGNALS

        # 1. Dollar stop — ungated, gross move
        if net_pnl is not None and net_pnl <= -plan['stop_usd']:
            return 'DOLLAR_STOP'

        # 2. Take profit — ungated, money alone
        if plan['tp_usd'] and net_pnl is not None \
                and net_pnl >= plan['tp_usd']:
            return 'TAKE_PROFIT'

        # 3. Reversion exit — gated on the floor; fail-open on no P&L
        if z is not None and abs(z) <= cfg['EXIT_Z']:
            if net_pnl is None:
                return 'REVERSION_EXIT'          # fail-open
            if net_pnl >= plan['gate_floor_usd']:
                return 'REVERSION_EXIT'
            # else: hold — never book a losing "profit-take"

        # 4. Max hold — only walk away with a profit
        if age_sec >= plan['max_hold_sec'] and net_pnl is not None \
                and net_pnl > 0:
            return 'MAX_HOLD'

        # 5. z-stop backstop (adverse stretch beyond the ceiling)
        if z is not None:
            if position.signal_type == SignalType.SELL_BASIS \
                    and z >= cfg['STOP_Z']:
                return 'Z_STOP'
            if position.signal_type == SignalType.BUY_BASIS \
                    and z <= -cfg['STOP_Z']:
                return 'Z_STOP'

        return None
