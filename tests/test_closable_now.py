"""The P&L is what closing would book, and the levels that can fire.

Operator, 2026-08-25, on a short filled at 54.98 with the long spread at
55.27 and break-even at 54.38: "How is the trade showing a profit if the
price (Long Spread) is more than the BE Price?" — then, on being shown
the two numbers side by side: "Do not use Mid. I would like the exact -
Bid and Ask Price and the right Bid and Ask values should be taken."

It was not in profit. Three faults behind one card:

1. `unrealized_pnl` was marked at the two MIDS, while a short is bought
   back on the LONG side. The mid mark read +$0.02 where closing books
   -$0.58; the gap is exactly the exit half of the bid-ask. It is now
   marked leg by leg at the price each leg would actually close at.
2. NET then double-charged the crossing: `gross - rt_cost_usd` takes a
   whole round turn off a mark that already contains one. Only the
   commissions are still outstanding — `exits.mark_fees`.
3. The card was still drawing the ENGINE's stop, gate release and
   "stop from target / RR" line on a MANUAL trade, where none of them
   can fire. That position had an empty Stop Loss box — no stop at all
   — beside a displayed SL of 58.50.
"""

import pytest

from statarb import marketdata
from statarb.coordinator import Coordinator
from statarb.costs import cost_parts, round_trip_cost
from statarb.exits import ExitLadder, mark_fees
from statarb.models import OrderSide, Position, SignalType, Trade
from statarb.positions import PositionManager


# The operator's actual card: a short filled at 54.98 with the book at
# 54.67 / 55.27, 0.02 lots of a 100-unit contract on both legs (k = 2).
FILL, SHORT, LONG, K = 54.98, 54.67, 55.27, 2.0
SPOT_BID, SPOT_ASK = 4292.50, 4292.66
# short = fut BID - spot ASK, long = fut ASK - spot BID.
FUT_BID, FUT_ASK = SPOT_ASK + SHORT, SPOT_BID + LONG

MD = {
    'spot_bid': SPOT_BID, 'spot_ask': SPOT_ASK,
    'futures_bid': FUT_BID, 'futures_ask': FUT_ASK,
    'spot_price': (SPOT_BID + SPOT_ASK) / 2,
    'futures_price': (FUT_BID + FUT_ASK) / 2,
    'short_spread': FUT_BID - SPOT_ASK,
    'long_spread': FUT_ASK - SPOT_BID,
    'spread': ((FUT_BID - SPOT_ASK) + (FUT_ASK - SPOT_BID)) / 2,
    'basis_pct': 0.0,
}


class _Logger:
    def log_position(self, *a, **k):
        pass

    def save_position_state(self, *a, **k):
        pass


def filled(symbol, side, lots, price):
    trade = Trade(symbol, side, lots)
    trade.executed_price = price
    trade.status = 'EXECUTED'
    return trade


def position(signal_type=SignalType.SELL_BASIS, spot=4292.66,
             fut=None, lots=0.02):
    """A pair whose fill spread is FILL by construction."""
    if fut is None:
        fut = spot + FILL
    long_spot = signal_type == SignalType.SELL_BASIS
    return Position(
        'POS_0005', 'GOLD', signal_type,
        filled('XAUUSD', OrderSide.BUY if long_spot else OrderSide.SELL,
               lots, spot),
        filled('GC1225', OrderSide.SELL if long_spot else OrderSide.BUY,
               lots, fut))


def mark(pos, market_data=MD, contract_size=100.0, contract_b=None):
    """Mark a position the way the coordinator does."""
    pm = PositionManager(_Logger())
    pm.positions[pos.position_id] = pos
    spot, fut = marketdata.closing_prices(market_data, pos.signal_type)
    pm.update_position_pnl(pos.position_id, spot, fut,
                           market_data.get('basis_pct', 0.0),
                           contract_size=contract_size,
                           contract_b=contract_b)
    return pos.unrealized_pnl


def plan(**over):
    base = {'source': 'MANUAL', 'fill_spread': FILL, 'spread_units': K,
            'tp_usd': 2.11, 'stop_usd': 7.05, 'gate_floor_usd': -1.0,
            'rt_cost_usd': 1.20, 'mark_fees_usd': 0.08,
            'manual_exit_spread': 53.92,
            'stop_source': 'target $2.11 / RR 0.3'}
    base.update(over)
    return base


# --- the touches each leg is marked at --------------------------------

def test_a_short_closes_on_the_spot_bid_and_the_futures_ask():
    """Long spot is SOLD to close; short futures is BOUGHT back."""
    assert marketdata.closing_prices(MD, SignalType.SELL_BASIS) == \
        (SPOT_BID, FUT_ASK)


def test_a_long_is_the_mirror():
    assert marketdata.closing_prices(MD, SignalType.BUY_BASIS) == \
        (SPOT_ASK, FUT_BID)


@pytest.mark.parametrize('signal', [SignalType.SELL_BASIS,
                                    SignalType.BUY_BASIS])
def test_the_two_legs_make_the_closing_executable_spread(signal):
    """The per-leg marks and `executable_spread(closing=True)` are the
    same statement, so a level and the P&L it is compared against can
    never be quoted on different bases."""
    spot, fut = marketdata.closing_prices(MD, signal)
    assert fut - spot == pytest.approx(
        marketdata.executable_spread(MD, signal, closing=True))


def test_a_missing_touch_falls_back_to_that_legs_mid():
    """Replayed rows and older snapshots still price — they do not
    silently mark at zero."""
    assert marketdata.closing_prices(
        {'spot_price': 10.0, 'futures_price': 12.0},
        SignalType.SELL_BASIS) == (10.0, 12.0)


# --- what that mark books ---------------------------------------------

def test_the_operators_reading():
    """The exact card. Mid-marked it showed +$0.02; the money you can
    take is -$0.58, and the difference is the exit half of the bid-ask."""
    got = mark(position())
    assert got == pytest.approx((FILL - LONG) * K)
    assert got == pytest.approx(-0.58)

    mid_mark = (FILL - MD['spread']) * K
    assert mid_mark == pytest.approx(0.02)
    assert mid_mark - got == pytest.approx((LONG - SHORT) / 2 * K)


def test_a_long_is_marked_out_at_the_short_spread():
    got = mark(position(SignalType.BUY_BASIS))
    assert got == pytest.approx((SHORT - FILL) * K)


@pytest.mark.parametrize('signal', [SignalType.SELL_BASIS,
                                    SignalType.BUY_BASIS])
def test_the_leg_mark_equals_the_spread_derivation(signal):
    """Two independent routes to the same figure: leg by leg from the
    fills (what MT5 books) and spread x k (what the levels are drawn
    in). Checked at beta 2 with different contract sizes, where leg A's
    multiplier and leg B's are not interchangeable."""
    beta, c_a, c_b = 2.0, 100.0, 50.0
    lots_a, lots_b = 0.02, 0.02 * c_a / (beta * c_b)      # L_A C_A = b L_B C_B
    k = lots_b * c_b

    spot_fill, fut_fill = 4292.66, 4292.66 * beta + FILL
    long_spot = signal == SignalType.SELL_BASIS
    pos = Position(
        'POS_0006', 'GOLD', signal,
        filled('XAUUSD', OrderSide.BUY if long_spot else OrderSide.SELL,
               lots_a, spot_fill),
        filled('GC1225', OrderSide.SELL if long_spot else OrderSide.BUY,
               lots_b, fut_fill))

    md = dict(MD, futures_bid=SPOT_ASK * beta + SHORT,
              futures_ask=SPOT_BID * beta + LONG)
    md['short_spread'] = md['futures_bid'] - beta * md['spot_ask']
    md['long_spread'] = md['futures_ask'] - beta * md['spot_bid']

    got = mark(pos, md, contract_size=c_a, contract_b=c_b)
    d = -1.0 if long_spot else 1.0
    exit_spread = marketdata.executable_spread(md, signal, closing=True)
    assert got == pytest.approx(d * (exit_spread - FILL) * k)


def test_the_bid_ask_is_still_charged_exactly_once():
    """Enter at the executable spread, nothing moves, close: the pair is
    down one round turn of crossing and one of commission — the modelled
    round trip, no more. Charging `rt_cost_usd` against this mark would
    take the crossing twice."""
    costs_cfg = {'SPREAD_COST_FACTOR': 1.0,
                 'COMMISSION_PER_LOT_SPOT': 3.0,
                 'COMMISSION_PER_LOT_FUT': 5.0}
    lots, contract = 0.02, 100.0
    # Filled AT the short spread: spot lifted on the ask, futures hit
    # on the bid. That is what shorting the spread costs.
    pos = position(spot=SPOT_ASK, fut=FUT_BID, lots=lots)
    gross = mark(pos, contract_size=contract)

    crossing, commission = cost_parts(MD, lots, contract, costs_cfg)
    assert gross == pytest.approx(-crossing)
    assert gross - commission == pytest.approx(
        -round_trip_cost(MD, lots, contract, costs_cfg))


def test_mark_fees_are_the_commissions_only():
    crossing, commission = cost_parts(
        MD, 0.02, 100.0, {'SPREAD_COST_FACTOR': 1.0,
                          'COMMISSION_PER_LOT_SPOT': 3.0,
                          'COMMISSION_PER_LOT_FUT': 5.0})
    assert commission == pytest.approx(0.16)
    assert crossing > 0
    assert mark_fees({'rt_cost_usd': crossing + commission,
                      'mark_fees_usd': commission}) == commission


def test_a_plan_from_before_the_split_still_prices():
    """The conservative direction, and the old behaviour exactly."""
    assert mark_fees({'rt_cost_usd': 1.20}) == 1.20
    assert mark_fees({}) == 0.0
    assert mark_fees(None) == 0.0


def test_break_even_is_where_the_net_is_zero():
    """The whole of the operator's question. BE is now a level on the
    CLOSING side of the book, so it can be read straight off the long
    spread for a short — and reaching it books zero."""
    p = plan(source='SIGNAL')
    levels = ExitLadder.spread_levels(p, FILL, K, SignalType.SELL_BASIS)
    be = levels['be']
    assert be == pytest.approx(FILL - p['mark_fees_usd'] / K)

    # A book whose LONG spread sits exactly at break-even.
    at_be = dict(MD, futures_ask=SPOT_BID + be)
    at_be['long_spread'] = be
    gross = mark(position(), at_be)
    assert gross - mark_fees(p) == pytest.approx(0.0)

    # And the reading that started it: a long spread ABOVE break-even
    # is a LOSS, which is what the operator said it should be.
    assert mark(position(), MD) - mark_fees(p) < 0
    assert MD['long_spread'] > be


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
    p = Coordinator._restate_manual_risk(
        plan(manual_stop_spread=57.0), FILL)
    levels = ExitLadder.spread_levels(p, FILL, K, SignalType.SELL_BASIS)
    assert levels['sl'] == pytest.approx(57.0)          # theirs, not 58.50
    assert levels['tp'] == pytest.approx(53.92)         # theirs
    assert levels['ex'] == pytest.approx(levels['be'])  # so EX is hidden


def test_a_signal_trade_keeps_the_engines_risk_figures():
    """`_restate_manual_risk` is only ever called for a MANUAL plan —
    pinned here so a future edit cannot quietly widen it."""
    p = plan(source='SIGNAL')
    levels = ExitLadder.spread_levels(p, FILL, K, SignalType.SELL_BASIS)
    assert levels['sl'] == pytest.approx(FILL + 7.05 / K)   # 58.505
