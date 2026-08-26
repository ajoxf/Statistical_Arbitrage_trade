"""A manual trade obeys the Manual Trade Card and nothing else.

Operator, 2026-08-25: "When I take a manual trade, ignore all the Algo
Logic. Only focus on the items in the Manual Trade Card. This is Manual
trading by a trader and will not conflict with the Algo Logic."

What closed POS_0003 was TIME_STOP at 15 minutes — which is
`HARD_TIME_STOP_MULT (3) x MIN_MAX_HOLD_SEC (300)`, a floor that
applied because the AR(1) half-life fitted to 0.6s quotes read 8
seconds. Not one part of that number came from the trader or from the
market.

So for a MANUAL entry the engine's dollar stop, sigma take-profit,
reversion gate, max-hold, hard time stop and z-stop are all OFF. What
remains is exactly the card: Take Profit, Stop Loss, Overnight, and the
Close button.

The consequence, asserted here so it can never be a surprise: a manual
trade with no Stop Loss has NO STOP.
"""

import pytest

from statarb.exits import ExitLadder
from statarb.models import OrderSide, Position, SignalType, Trade


def position(signal_type=SignalType.SELL_BASIS):
    return Position('POS_0003', 'GOLD', signal_type,
                    Trade('XAUUSD', OrderSide.BUY, 0.02),
                    Trade('GC1225', OrderSide.SELL, 0.02))


def manual(**over):
    """A hand-placed trade, as the coordinator stamps it."""
    base = {'source': 'MANUAL', 'tp_usd': 3.02, 'stop_usd': 10.07,
            'gate_floor_usd': 0.0, 'max_hold_sec': 300, 'rt_cost_usd': 1.0,
            'entry_z': None, 'entry_sigma': 0.376, 'entry_mu': 55.91,
            'entry_home': False, 'half_life_sec': 8.0}
    base.update(over)
    return base


def signal(**over):
    return manual(**dict({'source': 'SIGNAL', 'entry_z': 3.2}, **over))


# --- the algo's clocks and stops do not touch a manual trade ----------

@pytest.mark.parametrize('age_sec', [900, 5400, 86_400])
def test_no_time_stop(config, age_sec):
    """15 minutes was 3 x a 5-minute floor on a max-hold derived from
    an 8-second half-life. A trader's trade is not on that clock."""
    assert ExitLadder(config).evaluate(
        position(), manual(), z=-1.6, gross_pnl=-2.10,
        age_sec=age_sec, spread=55.47) is None


def test_no_engine_dollar_stop(config):
    """Down far past the engine's $10.07 stop, and it does not fire —
    the trader's own Stop Loss is the stop."""
    assert ExitLadder(config).evaluate(
        position(), manual(), z=-1.6, gross_pnl=-500.0,
        age_sec=60, spread=55.47) is None


def test_no_engine_take_profit(config):
    """Well past the engine's $3.02 target: a manual trade banks at the
    trader's level, not the strategy's."""
    assert ExitLadder(config).evaluate(
        position(), manual(), z=-1.6, gross_pnl=500.0,
        age_sec=60, spread=55.47) is None


def test_no_reversion_exit(config):
    """z home, in profit, past 2x max-hold — every condition the
    reversion rung wants, and it stays shut."""
    assert ExitLadder(config).evaluate(
        position(), manual(), z=0.0, gross_pnl=5.0,
        age_sec=700, spread=55.47, mid_spread=55.47) is None


def test_no_max_hold(config):
    assert ExitLadder(config).evaluate(
        position(), manual(), z=0.2, gross_pnl=2.0,
        age_sec=400, spread=55.47) is None


def test_no_z_stop(config):
    config.EXITS['Z_STOP_EXIT_ENABLED'] = True
    config.SIGNALS['STOP_Z'] = 1.0
    assert ExitLadder(config).evaluate(
        position(), manual(), z=9.9, gross_pnl=-1.0,
        age_sec=60, spread=55.47) is None


# --- what the card says still governs ---------------------------------

def test_the_traders_stop_fires(config):
    """A short loses as the spread RISES, so its stop sits above."""
    p = manual(manual_stop_spread=56.00)
    ladder = ExitLadder(config)
    assert ladder.evaluate(position(), p, z=0.0, gross_pnl=-1.0,
                           age_sec=60, spread=55.99) is None
    assert ladder.evaluate(position(), p, z=0.0, gross_pnl=-1.0,
                           age_sec=60, spread=56.00) == 'MANUAL_STOP'


def test_the_traders_target_fires(config):
    p = manual(manual_exit_spread=54.00)
    ladder = ExitLadder(config)
    assert ladder.evaluate(position(), p, z=0.0, gross_pnl=1.0,
                           age_sec=60, spread=54.01) is None
    assert ladder.evaluate(position(), p, z=0.0, gross_pnl=1.0,
                           age_sec=60, spread=54.00) == 'MANUAL_TARGET'


def test_both_mirrored_for_a_long(config):
    """A long spread profits as it rises: target above, stop below."""
    ladder = ExitLadder(config)
    p = manual(manual_exit_spread=57.0, manual_stop_spread=54.0)
    long_pos = position(SignalType.BUY_BASIS)
    assert ladder.evaluate(long_pos, p, z=0.0, gross_pnl=1.0,
                           age_sec=60, spread=57.1) == 'MANUAL_TARGET'
    assert ladder.evaluate(long_pos, p, z=0.0, gross_pnl=-1.0,
                           age_sec=60, spread=53.9) == 'MANUAL_STOP'


def test_the_stop_outranks_the_target_in_the_same_tick(config):
    """Both reachable at once: the trader's stop wins."""
    p = manual(manual_exit_spread=56.0, manual_stop_spread=56.0)
    assert ExitLadder(config).evaluate(
        position(), p, z=0.0, gross_pnl=0.0,
        age_sec=60, spread=56.5) == 'MANUAL_STOP'


def test_no_stop_set_means_no_stop(config):
    """Stated as a test because it is the price of the instruction: an
    empty Stop Loss box now means nothing will close the trade but the
    target, the overnight rule, or the operator."""
    p = manual(manual_exit_spread=54.0)      # target only
    assert p.get('manual_stop_spread') is None
    assert ExitLadder(config).evaluate(
        position(), p, z=0.0, gross_pnl=-10_000.0,
        age_sec=86_400, spread=99.0) is None


# --- a SIGNAL trade is completely unchanged ---------------------------

def test_a_signal_trade_still_gets_the_whole_ladder(config):
    ladder = ExitLadder(config)
    assert ladder.evaluate(position(), signal(), z=3.0, gross_pnl=-500.0,
                           age_sec=60, spread=55.47) == 'DOLLAR_STOP'
    assert ladder.evaluate(position(), signal(), z=3.0, gross_pnl=500.0,
                           age_sec=60, spread=55.47) == 'TAKE_PROFIT'
    # In profit but BELOW the $3.02 target, so the reversion rung is
    # the one that can fire.
    assert ladder.evaluate(position(), signal(), z=0.1, gross_pnl=2.0,
                           age_sec=60, spread=55.47,
                           mid_spread=55.47) == 'REVERSION_EXIT'
    config.EXITS['HARD_TIME_STOP_MULT'] = 3
    assert ladder.evaluate(position(), signal(), z=3.0, gross_pnl=-1.0,
                           age_sec=100_000, spread=55.47) == 'TIME_STOP'


def test_an_unstamped_plan_is_treated_as_a_signal(config):
    """Fail SAFE. A plan that reached the ladder without a source is
    managed by the engine, not left to run unmanaged."""
    p = signal()
    p.pop('source')
    assert ExitLadder(config).evaluate(
        position(), p, z=3.0, gross_pnl=-500.0,
        age_sec=60, spread=55.47) == 'DOLLAR_STOP'


def test_build_plan_stamps_the_source(config):
    """`evaluate` reads it, so it cannot be left to the caller."""
    ladder = ExitLadder(config)
    # The stop has to clear the $0.80 crossed on the way in, or the
    # SIGNAL plan is refused before it can be stamped (2026-08-26).
    config.EXITS['STOP_USD_PER_LOT'] = 100.0
    market = {'spot_price': 4636.0, 'futures_price': 4691.0,
              'spot_bid': 4635.9, 'spot_ask': 4636.1,
              'futures_bid': 4690.9, 'futures_ask': 4691.1,
              'spot_spread': 0.0, 'futures_spread': 0.0}
    assert ladder.build_plan(0.02, 100, 3.0, 0.376, 600,
                             market)['source'] == 'SIGNAL'
    assert ladder.build_plan(0.02, 100, 3.0, 0.376, 600, market,
                             manual=True)['source'] == 'MANUAL'


def test_a_manual_entry_is_never_vetoed(config):
    """The viability test asks whether a SIGNAL target can clear the
    round trip. Refusing a hand-placed trade on it is the engine
    overruling the trader — and it happens exactly when the operator
    sets no take-profit, which is when they least expect a refusal."""
    ladder = ExitLadder(config)
    market = {'spot_price': 4636.0, 'futures_price': 4691.0,
              'spot_bid': 4630.0, 'spot_ask': 4642.0,     # a huge book,
              'futures_bid': 4685.0, 'futures_ask': 4697.0,  # so cost is
              'spot_spread': 1200.0, 'futures_spread': 1200.0}  # enormous
    # A tiny sigma makes any plausible reversion worth far less than the
    # round trip — the refusal case.
    assert ladder.build_plan(0.02, 100, 3.0, 0.001, 600, market) is None
    assert ladder.build_plan(0.02, 100, 3.0, 0.001, 600, market,
                             manual=True) is not None


# --- the card shows only what can fire (2026-08-25, second sighting) ---

def test_reprice_does_not_put_the_rr_stop_back_on_a_manual_trade():
    """Live card: a hand-set target of $1.89 rendered "SL 58.53 /
    -$6.30 / stop from target $1.89 / RR 0.3 / needs 77% of trades to
    win" on a trade whose Stop Loss box was empty.

    `_restate_manual_risk` had run — and then `reprice_target` re-ran
    `_choose_stop`, which re-derived tp/RR and undid it. Order matters:
    the operator's risk is restated LAST.
    """
    from statarb.coordinator import Coordinator
    from statarb.exits import ExitLadder

    fill, k = 55.38, 2.0
    plan = {'source': 'MANUAL', 'lots': 0.02, 'spread_units': k,
            'manual_exit_spread': 54.435, 'rt_cost_usd': 1.24,
            'capital_at_risk': 1000.0, 'entry_z': 1.276,
            'entry_sigma': 0.2, 'tp_usd': 1.89, 'stop_usd': 6.30,
            'stop_source': 'target $1.89 / RR 0.3'}

    cfg = type('C', (), {'EXITS': {'RR': 0.3, 'STOP_USD_PER_LOT': 0.0,
                                   'STOP_CAPITAL_PCT': 0.0}})()
    ladder = ExitLadder(cfg)
    ladder.reprice_target(plan, abs(fill - 54.435) * k)
    # The engine's RR stop is what it derives...
    assert plan['stop_usd'] == pytest.approx(1.89 / 0.3, abs=0.01)

    # ...and the operator's empty Stop Loss box is what governs.
    Coordinator._restate_manual_risk(plan, fill)
    assert plan['stop_usd'] == 0.0
    assert 'RR' not in plan['stop_source']
    assert plan['breakeven_win_rate'] is None
    # The EV was priced off that stop too; with none armed it says so
    # rather than standing on a barrier that cannot be reached.
    assert plan['expectancy']['ev_usd'] is None
    assert 'no dollar stop' in plan['expectancy']['reason']

    levels = ExitLadder.spread_levels(plan, fill, k, SignalType.SELL_BASIS)
    assert levels['sl'] is None or levels['sl'] == pytest.approx(fill)
