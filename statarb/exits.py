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
from . import sizing
from .models import SignalType


def mark_fees(plan):
    """What still has to come off a FILL-marked P&L to make it NET.

    Commissions, and nothing else. The bid-ask is already in the two
    prices: the position went on at a real fill and is marked at the
    price it would come off at, so both crossings have been paid in the
    mark. `rt_cost_usd` charges the whole round turn, which is the right
    figure for the edge filter and the expected value — they compare a
    trade that has not happened yet — and charging it again against a
    mark that contains it is the bid-ask twice.

    That was already half-wrong before the mark moved off the mid
    (2026-08-25): the entry fill carried the entry crossing, so
    `gross - rt_cost` over-charged by the exit half. It just never
    showed as an error, because everything on the card was consistent
    with everything else on the card.

    Falls back to `rt_cost_usd` for a plan built before the split — the
    conservative direction, and the old behaviour exactly.
    """
    fees = (plan or {}).get('mark_fees_usd')
    return (plan or {}).get('rt_cost_usd', 0.0) if fees is None else fees


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

    def _capital_at_risk(self, lots, contract_size, market_data,
                         units_b=None):
        """Margin the pair ties up: each leg's notional over THAT
        account's leverage, plus an m2m buffer. The two accounts can be
        levered differently (100x spot, 500x futures), so the legs are
        divided separately; SPOT/FUT_LEVERAGE fall back to the single
        LEVERAGE knob. None when no leverage is set at all — the
        %-capital forms are then disabled.

        `units_b` is leg B's own quantity (`L_B x C_B`). It defaults to
        leg A's, which is right only at beta 1 with equal contract
        sizes — the one configuration this has ever run in.
        """
        exits = self.config.EXITS
        shared = exits.get('LEVERAGE', 0) or 0
        spot_lev = exits.get('SPOT_LEVERAGE', 0) or shared
        fut_lev = exits.get('FUT_LEVERAGE', 0) or shared
        if not (spot_lev and fut_lev):
            return None
        units_a = lots * contract_size
        if not units_b:
            units_b = units_a
        margin = (market_data['spot_price'] * units_a / spot_lev
                  + market_data['futures_price'] * units_b / fut_lev)
        buffer = 1 + exits.get('M2M_BUFFER_PCT', 0) / 100
        return margin * buffer

    def _hedge_units(self, lots, contract_size, asset_cfg=None):
        """(contract size, lots) for Leg B.

        The ladder is handed only Leg A's size, but the round trip pays
        Leg B's bid-ask on Leg B's units, and a spread move is worth
        `L_B x C_B` per point. Falls back to Leg A's contract size when
        the asset has no separate futures one — the common case, and
        exactly the old behaviour.

        `asset_cfg` is the asset actually being traded. Without it this
        scans for the first ENABLED asset, which is the right answer
        only while one pair is configured.
        """
        asset = asset_cfg
        if asset is None:
            asset = next((a for a in self.config.ASSETS.values()
                          if a.get('enabled', True)), {})
        contract_b = asset.get('fut_lot_size') or contract_size
        beta = self.config.TRADING.get('HEDGE_RATIO', 1.0) or 1.0
        lots_b = (lots * contract_size / (beta * contract_b)
                  if contract_b else lots)
        return contract_b, lots_b

    def _choose_stop(self, tp, lots, capital):
        """The TIGHTER of every armed stop form, and WHICH one bound.

        Three knobs in three different units resolve to one dollar
        figure, and "why is the stop $4.77" (operator, 2026-08-24) is
        not answerable from the number alone.

        Note RR is a REWARD ratio: below 1 the stop is WIDER than the
        target, so the target is what sets the risk.
        """
        exits = self.config.EXITS
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
        return min(armed) if armed else (0.0, 'no stop armed')

    def reprice_target(self, plan, target_usd):
        """Restate the dollar target once the FILL is known, and
        everything derived from it.

        `build_plan` runs BEFORE the order exists, so a manual target
        can only be measured from the mid — while `tp_usd` is compared
        against P&L, which is measured from the executed prices. The
        gap is the entry crossing plus slippage, and it does not stop
        at the target: with TP/RR armed the STOP is `tp / RR`, so at
        RR 0.3 an overstated target widens the risk by more than three
        times the overstatement.
        """
        if target_usd is None or not plan.get('lots'):
            return plan
        plan['tp_usd'] = target_usd
        plan['stop_usd'], plan['stop_source'] = self._choose_stop(
            target_usd, plan['lots'], plan.get('capital_at_risk'))
        tp, stop = plan['tp_usd'], plan['stop_usd']
        plan['breakeven_win_rate'] = (stop / (tp + stop)
                                      if tp and stop and (tp + stop) else None)
        plan['expectancy'] = expectancy.trade_expectancy(
            tp, stop, plan.get('rt_cost_usd', 0.0), plan.get('entry_z'),
            plan.get('entry_sigma'), plan.get('spread_units'))
        return plan

    def build_plan(self, lots, contract_size, entry_z, sigma, half_life_sec,
                   market_data, manual_target_usd=None, asset_cfg=None,
                   manual=False):
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

        # Leg B is priced in its own units. Derived here rather than
        # passed in so every caller of build_plan gets it right.
        contract_b, lots_b = self._hedge_units(lots, contract_size, asset_cfg)
        # `k` — dollars per 1.00 of spread. From the hedge derivation
        # (L_A x C_A = beta x L_B x C_B) the pair's P&L for a spread
        # move is leg B's quantity, NOT leg A's. They coincide only at
        # beta 1 with equal contract sizes, which is the one setup this
        # has been run in; away from there every figure below was out
        # by exactly 1/beta. `costs.expected_capture` was corrected to
        # leg B on 2026-08-11 and this was the other half.
        k = sizing.spread_units(lots_b, contract_b) or (lots * contract_size)
        capital = self._capital_at_risk(lots, contract_size, market_data,
                                        units_b=k)

        # Take-profit precedence: sigma-fraction > %-capital > fixed $
        tp = None
        if exits.get('USE_SIGMA_TARGET', True) and sigma and entry_z:
            tp = self.config.COSTS['TARGET_FRACTION'] * abs(entry_z) \
                * sigma * k
        elif exits.get('TP_CAPITAL_PCT', 0) > 0 and capital:
            tp = exits['TP_CAPITAL_PCT'] / 100 * capital
        elif exits.get('TP_USD_PER_LOT', 0) > 0:
            tp = exits['TP_USD_PER_LOT'] * lots

        # Split, because only the commissions are still outstanding once
        # the position is marked at the price it would close at — the
        # crossing is in that mark already. See `mark_fees`.
        crossing, commission = costs_mod.cost_parts(
            market_data, lots, contract_size, self.config.COSTS,
            lots_b=lots_b, contract_b=contract_b)
        rt_cost = crossing + commission

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
            plausible = abs(entry_z or 0) * (sigma or 0) * k
            # A MANUAL entry is never vetoed. The viability test asks
            # whether a SIGNAL-derived target can clear the round trip,
            # which is the edge filter's last line and right for a trade
            # the strategy chose. Refusing a hand-placed one is the
            # engine overruling the trader.
            if manual:
                pass
            elif plausible > 0 and tp > plausible:
                self.last_refusal = (
                    f"a full reversion of the current z is only worth "
                    f"${plausible:,.0f}, and the round trip costs "
                    f"${rt_cost:,.0f} (target floor ${tp:,.0f}) — the "
                    f"trade cannot pay for itself")
                logging.info("Exit plan not viable: %s", self.last_refusal)
                return None

        stop, stop_source = self._choose_stop(tp, lots, capital)

        # A stop INSIDE the entry crossing fires on the tick it opens.
        #
        # Since 2026-08-25 a position is marked at the touches it would
        # CLOSE at, so its gross P&L at t=0 is exactly minus one round
        # turn of both legs' bid-ask — that is what closing immediately
        # would book, and it is the right mark. DOLLAR_STOP compares
        # gross against `stop_usd`, so a stop at or under that crossing
        # is tripped before the spread has moved at all.
        #
        # It does NOT wash out with size: both sides scale with lots.
        # The shipped STOP_USD_PER_LOT of $30 against gold's measured
        # $58/lot round turn is stopped on the first tick at 0.1 lots,
        # at 1 lot and at 10 lots alike.
        #
        # Refused for a SIGNAL entry, on the same grounds as the
        # viability veto directly above: a trade that cannot survive its
        # own entry is not a trade. A MANUAL one is warned about and
        # placed — the trader's stop is the trader's.
        by_hand = manual or manual_target_usd is not None
        if stop and crossing and stop <= crossing:
            detail = (f"the stop is ${stop:,.2f} ({stop_source}) but "
                      f"opening the pair crosses ${crossing:,.2f} of "
                      f"bid-ask, so the trade is stopped out on the tick "
                      f"it opens — widen the stop past the round turn")
            if by_hand:
                logging.warning("MANUAL entry: %s", detail)
            else:
                self.last_refusal = detail
                logging.error("Exit plan not viable: %s", detail)
                return None

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
            tp, stop, rt_cost, entry_z, sigma, k)

        plan = {
            'tp_usd': tp,
            'stop_usd': stop,
            'gate_floor_usd': exits.get('GATE_FLOOR_USD', 0.0),
            'max_hold_sec': max_hold,
            'entry_z': entry_z,
            'entry_sigma': sigma,
            'rt_cost_usd': rt_cost,
            # The part of it that is NOT already in an exit-side mark.
            'mark_fees_usd': commission,
            'lots': lots,
            # Stamped HERE, not by the caller afterwards: `evaluate`
            # reads it to decide whether the card or the engine governs
            # this trade, and a plan that reached the ladder unstamped
            # would be managed by the algo.
            'source': 'MANUAL' if manual else 'SIGNAL',
            # Dollars per 1.00 of spread, leg B's units. Published so
            # the levels, the manual target and the slippage report all
            # translate spread<->dollars with the SAME multiplier the
            # plan itself was built on, rather than each deriving one.
            'spread_units': k,
            'leg_b_lots': lots_b,
            'contract_b': contract_b,
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
        # silently pin a %-target higher at the operating size.
        #
        # A MANUAL trade is governed by the card alone, so printing the
        # engine's ladder beside it would name clocks and stops that
        # will never fire — which is how POS_0003's "time_stop=15min"
        # read as a considered decision.
        if manual:
            # The card's own levels are stamped by the caller, AFTER
            # this returns, so the summary is logged there —
            # `describe_manual_plan` below. What matters here is that
            # the RESOLVED / RISK / VALUE lines are NOT printed: they
            # describe machinery that will not run on this trade, and
            # printing "time_stop=15min" beside a hand-placed order is
            # how POS_0003's clock read as a considered decision.
            return plan
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

    @staticmethod
    def describe_manual_plan(plan):
        """One line for a hand-placed trade: what will close it.

        Logged once the caller has stamped the card's own levels onto
        the plan. It names the ONLY things that can now close the
        position, because the engine's stop, take-profit, reversion
        gate, max-hold and time stop are all off for a manual trade.
        """
        target = plan.get('manual_exit_spread')
        stop = plan.get('manual_stop_spread')
        logging.info(
            "Exit plan (MANUAL): the Manual Trade Card governs this trade "
            "— target %s, stop %s, overnight %s. The engine's own stop, "
            "take-profit, reversion gate, max-hold and time stop do NOT "
            "apply.",
            f"{target:g}" if target is not None else "none",
            f"{stop:g}" if stop is not None else "NONE",
            plan.get('overnight_mode') or 'ALLOW')
        if stop is None:
            logging.warning(
                "MANUAL trade has NO STOP LOSS. Nothing will close this "
                "position except your take-profit, the overnight rule, or "
                "you closing it by hand.")

    # ------------------------------------------------------------------

    @staticmethod
    def spread_levels(plan, entry_spread, oz, signal_type):
        """Translate the dollar ladder into absolute SPREAD levels for
        display (the in-position card): BE = entry cost-adjusted, EX =
        gate release, TP, SL. d = -1 when profit needs the spread to
        FALL (SELL_BASIS), +1 when it needs it to rise.

        These are levels for the CLOSING side of the book — the long
        spread for a short, the short spread for a long — because that
        is what the P&L they translate is marked against. Read against
        the mid they would each be half a round turn out, which is the
        gap the operator saw when a short's break-even at 54.38 sat
        below a long spread of 55.27 on a card reporting a profit.
        """
        if not oz:
            return None
        d = -1.0 if signal_type == SignalType.SELL_BASIS else 1.0
        fees = mark_fees(plan)
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

    @staticmethod
    def _manual_level_hit(signal_type, spread, level, is_stop):
        """Has the spread reached one of the operator's own levels?

        A short-spread position loses as the spread RISES, so its stop
        sits above entry and its target below; a long is the mirror.
        `spread` is the executable CLOSING side — the price the position
        can actually be bought back or sold at — because these are
        prices the operator named, not statistics.
        """
        if level is None or spread is None:
            return False
        short = signal_type == SignalType.SELL_BASIS
        rising = short if is_stop else not short
        return spread >= level if rising else spread <= level

    def _manual_exit(self, position, plan, spread):
        """The Manual Trade Card, and nothing else.

        Stop first: a stop the operator set by hand is the one
        instruction nothing may outrank, and if both levels are
        reachable in the same tick the stop wins.
        """
        if self._manual_level_hit(position.signal_type, spread,
                                  plan.get('manual_stop_spread'), True):
            return 'MANUAL_STOP'
        if self._manual_level_hit(position.signal_type, spread,
                                  plan.get('manual_exit_spread'), False):
            return 'MANUAL_TARGET'
        return None

    def evaluate(self, position, plan, z, gross_pnl, age_sec, spread=None,
                 mid_spread=None):
        """Return an exit reason string, or None to keep holding.

        `spread` is the EXECUTABLE spread this position would CLOSE at —
        a short spread is bought back on the long side — and it is what
        the operator's hand-set stop and target are compared against.
        `mid_spread` is the series the statistics are measured on and is
        what the reversion gate reads; it falls back to `spread`, so
        callers that pass one spread keep their old behaviour.

        gross_pnl is the position marked at the prices it would CLOSE
        at, so it already carries both legs' bid-ask. Profit decisions
        act on NET = gross - the fees that are NOT in that mark, i.e.
        commissions (break-even aware: the TP is 'profit on top of
        break-even'); the dollar stop acts on GROSS so 'stop' means
        spread distance, not fees."""
        exits = self.config.EXITS
        cfg = self.config.SIGNALS
        max_hold = plan['max_hold_sec']
        fees = mark_fees(plan)
        net_pnl = gross_pnl - fees if gross_pnl is not None else None

        # A MANUAL trade is governed by the Manual Trade Card and by
        # NOTHING ELSE (operator, 2026-08-25: "When I take a manual
        # trade, ignore all the Algo Logic. Only focus on the items in
        # the Manual Trade Card. This is Manual trading by a trader and
        # will not conflict with the Algo Logic").
        #
        # So: the operator's Stop Loss, their Take Profit, their
        # Overnight rule (applied by the caller, before this), and the
        # Close button. The engine's dollar stop, sigma take-profit,
        # reversion gate, max-hold, hard time stop and z-stop are all
        # SIGNAL machinery — they exist to manage a trade the strategy
        # chose, on a thesis the strategy formed, and a hand-placed
        # trade has neither.
        #
        # This is what closed POS_0003: TIME_STOP at 15 minutes, which
        # is 3 x a 5-minute floor on a max-hold derived from an 8-second
        # half-life fitted to tick noise. Nothing about that number came
        # from the trader or from the market.
        #
        # THE CONSEQUENCE, stated plainly because it is real: a manual
        # trade with the Stop Loss box left empty now has NO STOP. It
        # runs until the target, the overnight rule, or the operator
        # closes it. That is the instruction — a trader's stop is the
        # trader's — and the panel says so.
        if (plan.get('source') or '').upper() == 'MANUAL':
            return self._manual_exit(position, plan, spread)

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

        # 3. Reversion exit — gate floor decays with age (deadlock fix).
        #
        # Skipped entirely when the gate was ALREADY satisfied at entry.
        # It asks "has the spread come home", and a spread that never
        # left cannot come home — the test is vacuous, and its
        # max-hold release below then closes the trade at ANY P&L, so
        # a vacuous gate becomes an unconditional timed exit at a loss.
        #
        # A SIGNAL entry cannot reach this: the entry gates guarantee
        # |z| >= ENTRY_Z and ENTRY_Z > EXIT_Z. A MANUAL entry skips
        # those gates by design, so it is routinely placed at z ~ 0 —
        # live 2026-08-25, four hand-placed trades all closed at about
        # -$2 with no profit and no stop hit, which is this.
        #
        # The trade still always has an exit (the completeness rule):
        # the dollar stop, the operator's own stop and target, MAX_HOLD
        # on a profit, the hard TIME_STOP and the overnight rule are
        # all untouched. Only the reversion opinion is withheld, and it
        # is withheld precisely where it has no information.
        if not plan.get('entry_home') and self._reversion_home(
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
