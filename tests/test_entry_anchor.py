"""What the entry card is anchored on (operator, 2026-08-25: "The
Wanted value looks incorrect. Check it again").

Two separate faults sat behind one reading:

1. `market_data['spread']` preferred `tick.last` — the most recent
   TRADE — while short_spread, long_spread, spread_cost, the cost model
   and slippage's decision mid are all built from bid/ask. So the
   number labelled "wanted" and the number the levels were anchored on
   were not the same quantity, and the documented invariant
   `short <= mid <= long` was false whenever a last print sat outside
   the book.

2. The BE/EX/TP/SL SPREAD levels were anchored on the MID at decision,
   while the dollar ladder they translate fires off gross P&L — which
   is measured from the FILL. Every displayed level was therefore out
   by the entry crossing plus slippage.
"""

from types import SimpleNamespace

import pytest

from statarb.exits import ExitLadder
from statarb.marketdata import compute_market_data
from statarb.models import SignalType


ASSET = {'name': 'GOLD', 'multiplier': 1.0}


def ticks(spot_last=0.0, fut_last=0.0):
    return (SimpleNamespace(bid=4633.79, ask=4633.99, last=spot_last, time=1),
            SimpleNamespace(bid=4689.16, ask=4689.56, last=fut_last, time=1))


# --- the spread is the MID of the book, always ----------------------------

@pytest.mark.parametrize('spot_last, fut_last', [
    (0.0, 0.0),                 # no last reported (spot metals)
    (0.0, 4689.86),             # a futures print ABOVE the ask
    (0.0, 4688.66),             # ...and one BELOW the bid
    (4634.50, 4689.30),         # both legs printing
])
def test_the_mid_always_sits_between_the_two_executable_spreads(
        spot_last, fut_last):
    """`short <= mid <= long` is asserted "by construction" in
    marketdata's own comment, and with `last` preferred it was simply
    not true: a futures print 0.30 above the ask put the mid ABOVE the
    long spread — above the best price anyone could buy the spread at.
    """
    md = compute_market_data(ASSET, *ticks(spot_last, fut_last), 1.0)
    assert md['short_spread'] <= md['spread'] <= md['long_spread'], md


def test_a_trade_print_does_not_move_the_spread():
    """Two snapshots with the SAME book and different last prints are
    one observation of the spread. mu/sigma/z need one continuous
    definition; a series that switches between a midpoint and a trade
    print carries the jump between them as noise."""
    quiet = compute_market_data(ASSET, *ticks(), 1.0)
    printed = compute_market_data(ASSET, *ticks(4634.50, 4689.90), 1.0)
    assert quiet['spread'] == printed['spread']


def test_the_spread_is_the_two_mids():
    md = compute_market_data(ASSET, *ticks(fut_last=4689.86), 1.0)
    spot_mid = (4633.79 + 4633.99) / 2
    fut_mid = (4689.16 + 4689.56) / 2
    assert md['spread'] == pytest.approx(fut_mid - spot_mid)
    # ...which is exactly what slippage.py calls the decision mid, so
    # "wanted" on the entry-cost row and the entry spread beside it are
    # the same number rather than two definitions of one.
    assert md['spread'] == pytest.approx(
        (md['short_spread'] + md['long_spread']) / 2)


def test_the_multiplier_still_applies_to_leg_b():
    md = compute_market_data({'name': 'X', 'multiplier': 10.0},
                             *ticks(), 1.0)
    assert md['futures_price'] == pytest.approx(
        (4689.16 + 4689.56) / 2 * 10.0)


# --- the levels are anchored on the FILL ----------------------------------

def plan(**over):
    base = {'tp_usd': 2.90, 'stop_usd': 9.67, 'rt_cost_usd': 1.26,
            'gate_floor_usd': 0.0}
    base.update(over)
    return base


def test_the_stop_level_names_where_the_dollar_stop_actually_fires():
    """Live 2026-08-25: a short filled at 54.76 with a $9.67 stop. Gross
    P&L is measured from the FILL, so the stop trips when the spread
    reaches 54.76 + 9.67/2 = 59.595. Anchored on the decision mid of
    55.215 the card said 60.05 — 0.46 late, and the operator watching
    for it would already have been stopped out.
    """
    levels = ExitLadder.spread_levels(plan(), entry_spread=54.76, oz=2.0,
                                      signal_type=SignalType.SELL_BASIS)
    assert levels['sl'] == pytest.approx(59.595)
    # The old, mid-anchored answer is the one that must not come back.
    assert levels['sl'] != pytest.approx(60.05, abs=1e-3)


def test_break_even_is_break_even_from_the_fill():
    """Shown at 54.59 the card claimed break-even while the trade was
    still down $0.91."""
    levels = ExitLadder.spread_levels(plan(), entry_spread=54.76, oz=2.0,
                                      signal_type=SignalType.SELL_BASIS)
    assert levels['be'] == pytest.approx(54.76 - 1.26 / 2)


def test_the_coordinator_anchors_the_levels_on_the_executed_spread():
    """The choice itself, from the function the entry path calls."""
    from statarb.coordinator import Coordinator

    class Measured:
        entry_slippage = {'executed_spread': 54.76}

    anchor, fill = Coordinator.levels_anchor(Measured(), {'spread': 55.215})
    assert (anchor, fill) == (54.76, 54.76)


@pytest.mark.parametrize('position', [
    type('NoReport', (), {'entry_slippage': None})(),
    type('NoFill', (), {'entry_slippage': {'executed_spread': None}})(),
    type('NoAttr', (), {})(),
])
def test_an_unmeasurable_entry_falls_back_to_the_mid(position):
    """No decision snapshot, or a leg that did not fill. The mid is the
    only anchor left, and `fill_spread` reports None rather than
    quietly restating the mid as a fill."""
    from statarb.coordinator import Coordinator
    anchor, fill = Coordinator.levels_anchor(position, {'spread': 55.215})
    assert anchor == 55.215
    assert fill is None


def test_no_slippage_report_falls_back_to_the_mid():
    """An unmeasurable entry (no decision snapshot, or a leg that did
    not fill) must still get levels — the mid is the only anchor left,
    and levels no operator can read are worse than slightly-off ones."""
    levels = ExitLadder.spread_levels(plan(), entry_spread=55.215, oz=2.0,
                                      signal_type=SignalType.SELL_BASIS)
    assert levels['sl'] == pytest.approx(60.05)
