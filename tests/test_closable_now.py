"""The P&L you can actually take, and the levels that can actually fire.

Operator, 2026-08-25, on a short filled at 54.98 with the long spread at
55.27 and break-even at 54.38: "How is the trade showing a profit if the
price (Long Spread) is more than the BE Price?"

It should not have been. Two separate faults behind one card:

1. `unrealized_pnl` is marked at the MID — the basis the whole dollar
   ladder uses — but a short is bought back on the LONG side. The mid
   mark read +$0.02 where closing books -$0.58; the gap is exactly the
   exit half of the bid-ask.
2. The card was still drawing the ENGINE's stop, gate release and
   "stop from target / RR" line on a MANUAL trade, where none of them
   can fire any more. That position had an empty Stop Loss box — no
   stop at all — beside a displayed SL of 58.50.
"""

import pytest

from statarb.coordinator import Coordinator
from statarb.models import OrderSide, Position, SignalType, Trade


# The operator's actual card.
FILL, SHORT, LONG, K = 54.98, 54.67, 55.27, 2.0
MD = {'spread': (SHORT + LONG) / 2,
      'short_spread': SHORT, 'long_spread': LONG}


def position(signal_type=SignalType.SELL_BASIS):
    return Position('POS_0005', 'GOLD', signal_type,
                    Trade('XAUUSD', OrderSide.BUY, 0.02),
                    Trade('GC1225', OrderSide.SELL, 0.02))


def plan(**over):
    base = {'source': 'MANUAL', 'fill_spread': FILL, 'spread_units': K,
            'tp_usd': 2.11, 'stop_usd': 7.05, 'gate_floor_usd': -1.0,
            'rt_cost_usd': 1.20, 'manual_exit_spread': 53.92,
            'stop_source': 'target $2.11 / RR 0.3'}
    base.update(over)
    return base


# --- what closing right now would book --------------------------------

def test_a_short_is_marked_out_at_the_long_spread():
    """The exact reading. Mid-marked it was +$0.02; the money you can
    take is -$0.58."""
    got = Coordinator.realisable_pnl(position(), plan(), MD)
    assert got == pytest.approx((FILL - LONG) * K)
    assert got == pytest.approx(-0.58)
    # ...and it is WORSE than the mid mark by half a round turn.
    mid_mark = (FILL - MD['spread']) * K
    assert mid_mark == pytest.approx(0.02)
    assert mid_mark - got == pytest.approx((LONG - SHORT) / 2 * K)


def test_a_long_is_marked_out_at_the_short_spread():
    """The mirror: a long spread is SOLD to close, at the lower side."""
    got = Coordinator.realisable_pnl(position(SignalType.BUY_BASIS),
                                     plan(), MD)
    assert got == pytest.approx((SHORT - FILL) * K)


def test_it_agrees_with_break_even():
    """The whole point of the question. At the break-even spread,
    closing books exactly the round trip back."""
    be = FILL - plan()['rt_cost_usd'] / K
    at_be = dict(MD, short_spread=be - 0.30, long_spread=be)
    got = Coordinator.realisable_pnl(position(), plan(), at_be)
    assert got == pytest.approx(plan()['rt_cost_usd'])


@pytest.mark.parametrize('bad', [
    {},                                             # no touches at all
    {'spread': 55.0},                               # only the mid
])
def test_it_falls_back_to_the_mid_when_there_are_no_touches(bad):
    """`executable_spread` falls back to the mid, so a replayed row
    still prices — it does not silently report zero."""
    got = Coordinator.realisable_pnl(position(), plan(), bad)
    assert got is None or got == pytest.approx((FILL - 55.0) * K)


@pytest.mark.parametrize('missing', ['fill_spread', 'spread_units'])
def test_an_unmeasurable_entry_reports_none_not_zero(missing):
    """A zero here would read as flat when it is simply unknown."""
    assert Coordinator.realisable_pnl(
        position(), plan(**{missing: None}), MD) is None


# --- a manual card shows only what can fire ---------------------------

def test_an_empty_stop_box_shows_no_stop_at_all():
    """It displayed the engine's SL at 58.50 on a position that has no
    stop. A stop that will never fire is worse than none shown."""
    p = Coordinator._restate_manual_risk(plan(), FILL)
    assert p['stop_usd'] == 0.0
    assert 'no Stop Loss set' in p['stop_source']
    assert p['breakeven_win_rate'] is None


def test_a_hand_set_stop_is_priced_from_the_fill():
    p = Coordinator._restate_manual_risk(
        plan(manual_stop_spread=57.0), FILL)
    assert p['stop_usd'] == pytest.approx((57.0 - FILL) * K)
    assert '57' in p['stop_source']
    assert p['breakeven_win_rate'] == pytest.approx(
        p['stop_usd'] / (p['tp_usd'] + p['stop_usd']))


def test_the_gate_release_column_disappears():
    """EX is where a REVERSION exit releases, and a manual trade has no
    reversion rung."""
    assert Coordinator._restate_manual_risk(plan(), FILL)[
        'gate_floor_usd'] == 0.0


def test_the_engines_rr_stop_no_longer_speaks_for_a_manual_trade():
    """"stop from target $2.11 / RR 0.3" described a stop that cannot
    fire — the RR line is the engine's, and the engine is not managing
    this trade."""
    before = plan()['stop_source']
    after = Coordinator._restate_manual_risk(plan(), FILL)['stop_source']
    assert 'RR' in before and 'RR' not in after


def test_the_levels_that_follow_are_the_operators_own():
    """End to end into `spread_levels`, which is what the card draws."""
    from statarb.exits import ExitLadder
    p = Coordinator._restate_manual_risk(
        plan(manual_stop_spread=57.0), FILL)
    levels = ExitLadder.spread_levels(p, FILL, K, SignalType.SELL_BASIS)
    assert levels['sl'] == pytest.approx(57.0)          # theirs, not 58.50
    assert levels['tp'] == pytest.approx(53.92)         # theirs
    assert levels['ex'] == pytest.approx(levels['be'])  # so EX is hidden


def test_a_signal_trade_keeps_the_engines_risk_figures():
    """`_restate_manual_risk` is only ever called for a MANUAL plan —
    pinned here so a future edit cannot quietly widen it."""
    from statarb.exits import ExitLadder
    p = plan(source='SIGNAL')
    levels = ExitLadder.spread_levels(p, FILL, K, SignalType.SELL_BASIS)
    assert levels['sl'] == pytest.approx(FILL + 7.05 / K)   # 58.505
