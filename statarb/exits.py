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
from . import expectancy
from .models import SignalType


def overnight_exit(mode, net_pnl, now, close_hour, close_minute):
    """Overnight handling for manual trades: ALLOW keeps the position
    (and pays the swap), EXIT_IF_PROFIT flattens only in profit,
    EXIT_ALWAYS flattens regardless — all at the session cutoff."""
    if not mode or mode == 'ALLOW':
        return None
    cutoff = now.replace(hour=int(close_hour), minute=int(close_minute),
                         second=0, microsecond=0)
    if now < cutoff:
        return None
    if mode == 'EXIT_ALWAYS':
        return 'OVERNIGHT_CLOSE'
    if mode == 'EXIT_IF_PROFIT' and net_pnl is not None and net_pnl > 0:
        return 'OVERNIGHT_CLOSE'
    return None


def outcome_tag(close_reason, z_reverted):
    """Deterministic post-trade outcome label (numbers-first review)."""
    reason = (close_reason or '').upper()
    if reason == 'TAKE_PROFIT':
        return 'TARGET_HIT'
    if reason == 'MANUAL_TARGET':
        return 'TARGET_HIT'
    if reason in ('OVERNIGHT_CLOSE', 'MANUAL_CLOSE'):
        return 'TIME_EXIT'
    if reason == 'REVERSION_EXIT':
        return 'REVERSION_BANKED'
    if reason in ('MAX_HOLD', 'TIME_STOP'):
        return 'TIME_EXIT'
    if reason in ('DOLLAR_STOP', 'Z_STOP', 'STOP_LOSS', 'MANUAL_STOP'):
        return ('STOPPED_AFTER_FULL_REVERSION' if z_reverted
                else 'STOPPED_IN_TREND')
    return reason


class ExitLadder:
    def __init__(self, config):
        self.config = config
        self._z_stop_logged = set()   # position ids already logged
        # Why the last build_plan returned None. A refusal that only
        # reaches the log leaves the operator pressing a button and
        # watching nothing happen.
        self.last_refusal = None

    # ------------------------------------------------------------------

    def _capital_at_risk(self, lots, contract_size, market_data):
        """Margin the pair ties up: each leg's notional over THAT
        account's leverage, plus an m2m buffer. The two accounts can be
        levered differently (100x spot, 500x futures), so the legs are
        divided separately; SPOT/FUT_LEVERAGE fall back to the single
        LEVERAGE knob. None when no leverage is set at all — the
        %-capital forms are then disabled."""
        exits = self.config.EXITS
        shared = exits.get('LEVERAGE', 0) or 0
        spot_lev = exits.get('SPOT_LEVERAGE', 0) or shared
        fut_lev = exits.get('FUT_LEVERAGE', 0) or shared
        if not (spot_lev and fut_lev):
            return None
        oz = lots * contract_size
        margin = (market_data['spot_price'] * oz / spot_lev
                  + market_data['futures_price'] * oz / fut_lev)
        buffer = 1 + exits.get('M2M_BUFFER_PCT', 0) / 100
        return margin * buffer

    def _hedge_units(self, lots, contract_size):
        """(contract size, lots) for Leg B, from the ASSET config.

        The ladder is handed only Leg A's size, but the round trip pays
        Leg B's bid-ask on Leg B's units. Falls back to Leg A's when
        the asset has no separate futures contract size — which is the
        common case and exactly the old behaviour.
        """
        asset = next((a for a in self.config.ASSETS.values()
                      if a.get('enabled', True)), {})
        contract_b = asset.get('fut_lot_size') or contract_size
        beta = self.config.TRADING.get('HEDGE_RATIO', 1.0) or 1.0
        lots_b = (lots * contract_size / (beta * contract_b)
                  if contract_b else lots)
        return contract_b, lots_b

    def build_plan(self, lots, contract_size, entry_z, sigma, half_life_sec,
                   market_data, manual_target_usd=None):
        """Compute the frozen exit levels. Returns None when the trade
        can never win (cost floor above plausible full reversion) —
        the entry must then be blocked, and `last_refusal` says why.

        `manual_target_usd` is what the OPERATOR's own take-profit is
        worth. When they have named a level, that is the trade they are
        placing, and vetoing it against a sigma-derived target they
        never asked for is the engine substituting its opinion for
        theirs. The viability test then measures their target instead.
        """
        self.last_refusal = None
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

        # Leg B is priced in its own units. Derived here rather than
        # passed in so every caller of build_plan gets it right.
        contract_b, lots_b = self._hedge_units(lots, contract_size)
        rt_cost = costs_mod.round_trip_cost(
            market_data, lots, contract_size, self.config.COSTS,
            lots_b=lots_b, contract_b=contract_b)

        if manual_target_usd is not None:
            # The operator's level IS the plan. Their target still has
            # to clear the round trip to be worth placing, but it is
            # measured against what THEY chose, not against a full
            # reversion of the current z.
            tp = float(manual_target_usd)
            if tp <= rt_cost:
                logging.warning(
                    "Manual take-profit is worth $%.0f against $%.0f of "
                    "round-trip cost — this trade cannot make money at "
                    "that level, placing it anyway because it was asked "
                    "for by hand", tp, rt_cost)
        elif tp is not None:
            floor = exits.get('COST_FLOOR_MULT', 1.0) * rt_cost
            tp = max(tp, floor)
            plausible = abs(entry_z or 0) * (sigma or 0) * oz
            if plausible > 0 and tp > plausible:
                self.last_refusal = (
                    f"a full reversion of the current z is only worth "
                    f"${plausible:,.0f}, and the round trip costs "
                    f"${rt_cost:,.0f} (target floor ${tp:,.0f}) — the "
                    f"trade cannot pay for itself")
                logging.info("Exit plan not viable: %s", self.last_refusal)
                return None

        # Stop: the TIGHTER of every armed form. Which one BOUND is
        # carried out with it — three knobs in three different units
        # resolve to one dollar figure, and "why is the stop $4.77"
        # (operator, 2026-08-24) is not answerable from the number.
        candidates = [
            (exits.get('STOP_USD_PER_LOT', 0) * lots,
             f"STOP_USD_PER_LOT ${exits.get('STOP_USD_PER_LOT', 0):g} "
             f"x {lots:g} lots"),
        ]
        if exits.get('STOP_CAPITAL_PCT', 0) > 0 and capital:
            candidates.append(
                (exits['STOP_CAPITAL_PCT'] / 100 * capital,
                 f"STOP_CAPITAL_PCT {exits['STOP_CAPITAL_PCT']:g}% of "
                 f"${capital:,.0f}"))
        rr = exits.get('RR', 0)
        if tp and rr > 0:
            candidates.append(
                (tp / rr, f"target ${tp:,.2f} / RR {rr:g}"))
        armed = [c for c in candidates if c[0] > 0]
        stop, stop_source = min(armed) if armed else (0.0, 'no stop armed')

        if half_life_sec:
            max_hold = exits.get('MAX_HOLD_HALF_LIVES', 4) * half_life_sec
        else:
            max_hold = exits.get('MAX_HOLD_FALLBACK_MIN', 240) * 60
        floor_sec = exits.get('MIN_MAX_HOLD_SEC', 0) or 0
        if floor_sec and max_hold < floor_sec:
            logging.warning(
                "Measured half-life %.1fs implies a %.0fs max hold and a "
                "%.0fs hard time stop — too short to be a real reversion "
                "horizon, so the floor of %.0fs applies. A half-life this "
                "small usually means the AR(1) fit is measuring tick noise "
                "rather than the spread.",
                half_life_sec or 0, max_hold,
                max_hold * exits.get('HARD_TIME_STOP_MULT', 0), floor_sec)
            max_hold = floor_sec

        # What the trade is worth going in. The ladder has always frozen
        # a target and a stop; this states the arithmetic they imply
        # instead of leaving the operator to do it. Reference only — no
        # gate reads it (see EV_MIN_USD below for the opt-in one).
        expectancy_block = expectancy.trade_expectancy(
            tp, stop, rt_cost, entry_z, sigma, oz)

        plan = {
            'tp_usd': tp,
            'stop_usd': stop,
            'gate_floor_usd': exits.get('GATE_FLOOR_USD', 0.0),
            'max_hold_sec': max_hold,
            'entry_z': entry_z,
            'entry_sigma': sigma,
            'rt_cost_usd': rt_cost,
            'capital_at_risk': capital,
            'half_life_sec': half_life_sec,
            'expectancy': expectancy_block,
            'stop_source': stop_source,
            # stop / (target + stop): the win rate this geometry needs
            # just to break even, before any edge. CLAUDE.md has carried
            # the rule "verify measured win rate clears stop/(target +
            # stop)" since the cost measurements, and nothing computed
            # it. Unlike the EV block it needs no sigma, so it is
            # available on the very first trade of a cold start — which
            # is exactly when a hand-set target and an RR-derived stop
            # can quietly ask for a 77% hit rate.
            'breakeven_win_rate': (stop / (tp + stop)
                                   if tp and stop and (tp + stop) else None),
        }

        # An OPT-IN veto (0 = off, the default). A negative-EV trade is
        # one the geometry says loses money on average, so refusing it
        # is defensible — but it is a real change in what the engine
        # trades, and it must not switch itself on behind the operator.
        # Manual entries are never vetoed: manual means manual.
        floor_ev = exits.get('EV_MIN_USD', 0.0) or 0.0
        if floor_ev and manual_target_usd is None \
                and expectancy_block.get('ev_usd') is not None \
                and expectancy_block['ev_usd'] < floor_ev:
            self.last_refusal = (
                f"expected value is ${expectancy_block['ev_usd']:+,.0f} "
                f"against a floor of ${floor_ev:,.0f} — "
                f"{expectancy_block['p_win'] * 100:.0f}% chance of "
                f"${expectancy_block['win_usd']:,.0f} does not pay for a "
                f"{(1 - expectancy_block['p_win']) * 100:.0f}% chance of "
                f"-${expectancy_block['loss_usd']:,.0f}")
            logging.info("Exit plan not viable: %s", self.last_refusal)
            return None
        # Print the RESOLVED levels, not the configs — a cost floor can
        # silently pin a %-target higher at the operating size
        logging.info("Exit plan (RESOLVED): TP=$%s STOP=$%.0f gate=$%.0f "
                     "max_hold=%.0fmin time_stop=%.0fmin (cost $%.0f%s)",
                     f"{tp:.0f}" if tp else "off", stop,
                     plan['gate_floor_usd'], max_hold / 60,
                     max_hold * exits.get('HARD_TIME_STOP_MULT', 0) / 60,
                     rt_cost,
                     f", capital ${capital:,.0f}" if capital else "")
        # Say which knob set the stop and what the geometry demands.
        # A stop WIDER than the target is not wrong — it is a bet on
        # frequency — but it needs saying, because nobody chooses a 77%
        # win rate on purpose.
        if stop:
            be_wr = plan['breakeven_win_rate']
            logging.info(
                "Exit plan (RISK): stop $%.2f from %s%s", stop, stop_source,
                (f"; risking ${stop:,.2f} to make ${tp:,.2f} needs "
                 f"{be_wr * 100:.0f}% of trades to win just to break even"
                 if be_wr else ""))
        logging.info("Exit plan (VALUE): %s",
                     expectancy.summarise(expectancy_block))
        return plan

    # ------------------------------------------------------------------

    @staticmethod
    def spread_levels(plan, entry_spread, oz, signal_type):
        """Translate the dollar ladder into absolute SPREAD levels for
        display (the in-position card): BE = entry cost-adjusted, EX =
        gate release, TP, SL. d = -1 when profit needs the spread to
        FALL (SELL_BASIS), +1 when it needs it to rise."""
        if not oz:
            return None
        d = -1.0 if signal_type == SignalType.SELL_BASIS else 1.0
        fees = plan.get('rt_cost_usd', 0.0)
        sl = (entry_spread - d * plan['stop_usd'] / oz
              if plan.get('stop_usd') else None)
        tp = (entry_spread + d * (plan['tp_usd'] + fees) / oz
              if plan.get('tp_usd') else None)
        # A manual trade carries the operator's own spread levels. Both
        # ladders are live, so the card must show whichever the spread
        # reaches FIRST — the one nearest entry on that side.
        manual_tp = plan.get('manual_exit_spread')
        manual_sl = plan.get('manual_stop_spread')
        tp = ExitLadder._nearest(tp, manual_tp, entry_spread)
        sl = ExitLadder._nearest(sl, manual_sl, entry_spread)
        levels = {
            'entry_spread': entry_spread,
            'be': entry_spread + d * fees / oz,
            'sl': sl,
            'tp': tp,
            'ex': entry_spread + d * (plan.get('gate_floor_usd', 0)
                                      + fees) / oz,
            'favorable': 'down' if d < 0 else 'up',
            'manual_tp': manual_tp,
            'manual_sl': manual_sl,
        }
        return levels

    @staticmethod
    def _nearest(a, b, anchor):
        """Of two levels on the same side of `anchor`, the one the
        spread reaches first — i.e. the closer of the two."""
        if a is None:
            return b
        if b is None:
            return a
        return a if abs(a - anchor) <= abs(b - anchor) else b

    def _reversion_home(self, plan, z, spread, signal_type):
        """Has the spread 'come home'? Depends on SIGNALS.EXIT_MODE:
        zscore (z inside the band), spread (crossed the mean frozen at
        entry), or hybrid (either).

        `spread` here is the MID. This is a STATISTICAL test — has the
        series returned to the mean it was measured against — and
        `entry_mu` is a mean of mids. Feeding it the buy-back price
        would compare two different definitions and bias the gate by
        half a round turn in whichever direction the position faces.
        The operator's own stop and target are the opposite case and DO
        read the executable side: those are prices they named, not
        statistics."""
        cfg = self.config.SIGNALS
        z_home = z is not None and abs(z) <= cfg['EXIT_Z']
        spread_home = False
        entry_mu = plan.get('entry_mu')
        if spread is not None and entry_mu is not None:
            if signal_type == SignalType.SELL_BASIS:
                spread_home = spread <= entry_mu
            else:
                spread_home = spread >= entry_mu
        mode = cfg.get('EXIT_MODE', 'zscore')
        if mode == 'spread':
            return spread_home
        if mode == 'hybrid':
            return z_home or spread_home
        return z_home

    def evaluate(self, position, plan, z, gross_pnl, age_sec, spread=None,
                 mid_spread=None):
        """Return an exit reason string, or None to keep holding.

        `spread` is the EXECUTABLE spread this position would CLOSE at —
        a short spread is bought back on the long side — and it is what
        the operator's hand-set stop and target are compared against.
        `mid_spread` is the series the statistics are measured on and is
        what the reversion gate reads; it falls back to `spread`, so
        callers that pass one spread keep their old behaviour.

        gross_pnl is the mark-to-market price move. Profit decisions
        act on NET = gross - round-trip costs (break-even aware: the
        TP is 'profit on top of break-even'); the dollar stop acts on
        GROSS so 'stop' means spread distance, not fees."""
        exits = self.config.EXITS
        cfg = self.config.SIGNALS
        max_hold = plan['max_hold_sec']
        fees = plan.get('rt_cost_usd', 0.0)
        net_pnl = gross_pnl - fees if gross_pnl is not None else None

        # 1. Manual trade: the operator named a stop SPREAD when they
        # placed it. Checked FIRST — a stop the operator set by hand is
        # the one instruction nothing else may outrank, and it is the
        # reason a manual trade is allowed to skip the signal gates at
        # all. The engine's own dollar stop below still applies, so
        # whichever is reached first wins.
        stop_level = plan.get('manual_stop_spread')
        if stop_level is not None and spread is not None:
            hit = (spread >= stop_level
                   if position.signal_type == SignalType.SELL_BASIS
                   else spread <= stop_level)
            if hit:
                return 'MANUAL_STOP'

        # 1a. Dollar stop — ungated, gross move
        if plan['stop_usd'] and gross_pnl is not None \
                and gross_pnl <= -plan['stop_usd']:
            return 'DOLLAR_STOP'

        # 1b. Manual trade: the operator named a take-profit SPREAD
        # when arming it. That target outranks the signal machinery
        # (it is why they placed the trade) but never outranks a stop.
        target = plan.get('manual_exit_spread')
        if target is not None and spread is not None:
            reached = (spread <= target
                       if position.signal_type == SignalType.SELL_BASIS
                       else spread >= target)
            if reached:
                return 'MANUAL_TARGET'

        # 2. Take profit — ungated, NET money alone (BE + target)
        if plan['tp_usd'] and net_pnl is not None \
                and net_pnl >= plan['tp_usd']:
            return 'TAKE_PROFIT'

        # 3. Reversion exit — gate floor decays with age (deadlock fix)
        if self._reversion_home(
                plan, z, mid_spread if mid_spread is not None else spread,
                position.signal_type):
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

        # 4. Max hold — only walk away with a NET profit; suppressed
        # while still travelling toward an EXISTING take-profit
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

        # 5. Hard time stops — ANY P&L. Two independent clocks: a
        # multiple of max-hold, and a FIXED minutes cap (exit before
        # the spread starts drifting, e.g. 90 minutes)
        time_stop_mult = exits.get('HARD_TIME_STOP_MULT', 0)
        if time_stop_mult and age_sec >= time_stop_mult * max_hold:
            return 'TIME_STOP'
        hard_minutes = exits.get('HARD_MAX_HOLD_MIN', 0)
        if hard_minutes and age_sec >= hard_minutes * 60:
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
                        position.position_id, z, gross_pnl or 0,
                        plan['stop_usd'])

        return None
