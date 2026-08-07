"""Decision-to-fill measurement.

Owner (2026-08-07): "what your signal wanted to enter at and what the
orders got placed at on MT5".

The trap this module exists to avoid: the signal fires on a MID spread
and fills happen at the TOUCH, so the naive difference reports the
quoted bid-ask as if it were slippage. Every test here is about keeping
those two costs apart and getting the signs right.
"""

import pytest

from statarb import slippage
from statarb.models import OrderSide, SignalType, Trade


# A book with a 0.2 spot spread and a 0.4 futures spread.
BOOK = {
    'spot_bid': 4264.90, 'spot_ask': 4265.10,
    'futures_bid': 4323.80, 'futures_ask': 4324.20,
}
BETA = 1.0
OZ = 100.0        # 1 lot of gold


def entry(spot_fill, fut_fill, signal=SignalType.SELL_BASIS, closing=False):
    spot_side = OrderSide.BUY if signal == SignalType.SELL_BASIS \
        else OrderSide.SELL
    fut_side = spot_side.opposite
    if closing:
        spot_side, fut_side = spot_side.opposite, fut_side.opposite
    return slippage.build(signal, closing, BETA, OZ, spot_side, fut_side,
                          BOOK, spot_fill, fut_fill, 'XAUUSD', 'GC1226')


# --- the three prices -----------------------------------------------------

def test_a_perfect_fill_at_the_touch_has_zero_slippage_but_pays_crossing():
    """Filling exactly at the quote is NOT free — you still crossed the
    book. Reporting that as slippage would condemn perfect execution."""
    r = entry(spot_fill=4265.10, fut_fill=4323.80)   # ask / bid: the touch
    assert r['slippage_spread'] == pytest.approx(0.0)
    assert r['crossing_spread'] == pytest.approx(0.3)   # 0.1 + 0.2 halves
    assert r['total_spread'] == pytest.approx(0.3)


def test_a_worse_fill_than_the_quote_is_slippage():
    # Paid 0.05 more for spot, received 0.05 less for futures.
    r = entry(spot_fill=4265.15, fut_fill=4323.75)
    assert r['slippage_spread'] == pytest.approx(0.10)
    assert r['slippage_usd'] == pytest.approx(10.0)     # x 100 oz
    assert r['crossing_spread'] == pytest.approx(0.3)   # unchanged


def test_price_improvement_keeps_its_sign():
    """Limit fills DO come back better than the touch. Taking an
    absolute value here would hide the one thing that makes limit-first
    execution worth its complexity."""
    r = entry(spot_fill=4265.00, fut_fill=4323.90)
    assert r['slippage_spread'] < 0
    assert r['slippage_usd'] == pytest.approx(-20.0)


def test_the_decision_spread_is_the_mid_the_signal_saw():
    r = entry(spot_fill=4265.10, fut_fill=4323.80)
    assert r['decision_spread'] == pytest.approx(4324.00 - 4265.00)
    assert r['quoted_spread'] == pytest.approx(4323.80 - 4265.10)


# --- signs: the same position flips direction between entry and exit ------

def test_short_spread_sells_on_entry_and_buys_on_exit():
    assert slippage.selling_the_spread(SignalType.SELL_BASIS, False) is True
    assert slippage.selling_the_spread(SignalType.SELL_BASIS, True) is False
    assert slippage.selling_the_spread(SignalType.BUY_BASIS, False) is False
    assert slippage.selling_the_spread(SignalType.BUY_BASIS, True) is True


def test_crossing_is_a_cost_in_all_four_directions():
    """If any of these came out negative, the report would be claiming
    the book pays you to cross it."""
    for signal in (SignalType.SELL_BASIS, SignalType.BUY_BASIS):
        for closing in (False, True):
            r = entry(4265.0, 4324.0, signal=signal, closing=closing)
            assert r['crossing_spread'] > 0, (signal, closing)


def test_the_exit_of_a_short_spread_is_costly_when_it_fills_high():
    """Closing a short spread BUYS the spread back, so a HIGHER
    executed level is the expensive one. The naive sign would have
    reported this as a gain."""
    r = entry(spot_fill=4264.85, fut_fill=4324.25,
              signal=SignalType.SELL_BASIS, closing=True)
    # quote for the close: sell spot at bid 4264.90, buy futures at ask
    # 4324.20 -> quoted 59.30; executed 59.40, i.e. 0.10 worse.
    assert r['slippage_spread'] == pytest.approx(0.10)


def test_the_exit_of_a_long_spread_is_costly_when_it_fills_low():
    r = entry(spot_fill=4265.15, fut_fill=4323.75,
              signal=SignalType.BUY_BASIS, closing=True)
    assert r['slippage_spread'] == pytest.approx(0.10)


# --- the invariant that proves both halves are measured the same way ------

@pytest.mark.parametrize('signal', [SignalType.SELL_BASIS,
                                    SignalType.BUY_BASIS])
@pytest.mark.parametrize('closing', [False, True])
@pytest.mark.parametrize('beta', [1.0, 0.5, 2.0])
def test_leg_costs_reconcile_to_the_spread_cost(signal, closing, beta):
    """spread = futures - beta * spot, so each leg's cost must enter the
    spread cost with exactly its weight in the spread. If this ever
    fails, one of the two numbers on the operator's screen is wrong and
    they will not be able to tell which."""
    spot_side = OrderSide.BUY if signal == SignalType.SELL_BASIS \
        else OrderSide.SELL
    fut_side = spot_side.opposite
    if closing:
        spot_side, fut_side = spot_side.opposite, fut_side.opposite
    r = slippage.build(signal, closing, beta, OZ, spot_side, fut_side,
                       BOOK, 4265.07, 4323.91)
    legs = r['legs']
    assert r['slippage_spread'] == pytest.approx(
        legs['futures']['slippage'] + beta * legs['spot']['slippage'])
    assert r['crossing_spread'] == pytest.approx(
        legs['futures']['crossing'] + beta * legs['spot']['crossing'])


# --- unmeasurable is not zero ---------------------------------------------

def test_no_snapshot_reports_nothing_rather_than_zero():
    """A zero here would read as flawless execution."""
    assert slippage.build(SignalType.SELL_BASIS, False, 1.0, OZ,
                          OrderSide.BUY, OrderSide.SELL,
                          None, 4265.0, 4324.0) is None


def test_an_unfilled_leg_leaves_the_spread_numbers_blank():
    r = entry(spot_fill=4265.10, fut_fill=None)
    assert r['executed_spread'] is None
    assert r['slippage_spread'] is None
    assert r['slippage_usd'] is None
    # ...but what IS known is still reported.
    assert r['crossing_spread'] == pytest.approx(0.3)
    assert 'not measured' in slippage.summarise(r)


def test_a_missing_quote_does_not_raise():
    r = slippage.build(SignalType.SELL_BASIS, False, 1.0, OZ,
                       OrderSide.BUY, OrderSide.SELL,
                       {'spot_bid': None, 'spot_ask': None,
                        'futures_bid': 4323.8, 'futures_ask': 4324.2},
                       4265.0, 4324.0)
    assert r['slippage_spread'] is None


def test_zero_ounces_leaves_dollars_blank_not_zero():
    r = slippage.build(SignalType.SELL_BASIS, False, 1.0, 0.0,
                       OrderSide.BUY, OrderSide.SELL, BOOK, 4265.1, 4323.8)
    assert r['slippage_spread'] is not None
    assert r['slippage_usd'] is None


# --- the summary line the operator reads in the log -----------------------

def test_the_log_line_names_all_three_prices():
    line = slippage.summarise(entry(4265.15, 4323.75))
    assert 'decision' in line and 'quoted' in line and 'filled' in line
    assert 'crossing' in line and 'slippage' in line
    assert 'USD' in line


def test_an_unmeasured_trade_says_so():
    assert 'not measured' in slippage.summarise(None)


# --- through the real execution paths -------------------------------------

def make_book(spot_bid, spot_ask, fut_bid, fut_ask):
    return {'spot_bid': spot_bid, 'spot_ask': spot_ask,
            'futures_bid': fut_bid, 'futures_ask': fut_ask,
            'spread': (fut_bid + fut_ask) / 2 - (spot_bid + spot_ask) / 2}


def test_the_live_pair_executor_measures_and_records_it(config, fake_broker):
    """The pair executor wrote requested_price as NULL from the day it
    was built, so there was nothing to compare a fill against."""
    from statarb.legs import LocalLeg
    from statarb.pair_executor import PairExecutor
    spot_leg = LocalLeg(fake_broker)
    executor = PairExecutor(config, spot_leg, spot_leg)
    # The decision was made a moment ago at these prices...
    reference = make_book(3299.90, 3300.10, 3319.80, 3320.20)
    ok, spot_trade, fut_trade = executor.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 1.0, 'XAUUSD', 'GC1225',
        reference=reference)
    assert ok
    assert spot_trade.requested_price == pytest.approx(3300.10)  # the ask
    assert fut_trade.requested_price == pytest.approx(3319.80)   # the bid
    report = spot_trade.slippage
    assert report is not None
    assert report['decision_spread'] == pytest.approx(20.0)
    assert report['closing'] is False


def test_a_live_entry_without_a_snapshot_reports_nothing(config, fake_broker):
    from statarb.legs import LocalLeg
    from statarb.pair_executor import PairExecutor
    spot_leg = LocalLeg(fake_broker)
    executor = PairExecutor(config, spot_leg, spot_leg)
    ok, spot_trade, _ = executor.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 1.0, 'XAUUSD', 'GC1225')
    assert ok and spot_trade.slippage is None


def test_paper_measures_it_the_same_way_as_live(tmp_path, config):
    """Paper must produce the number too — the operator needs to read
    it BEFORE risking money, not after."""
    from statarb.coordinator import PaperExecutor
    from tests.test_limit_execution import LimitFakeLeg
    spot_leg = LimitFakeLeg('a', price=3300.0)
    fut_leg = LimitFakeLeg('b', price=3320.0)
    executor = PaperExecutor(spot_leg, fut_leg, config)
    reference = make_book(3299.95, 3300.05, 3319.95, 3320.05)
    ok, spot_trade, fut_trade = executor.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 1.0, 'XAUUSD', 'GC1225',
        reference=reference)
    assert ok
    # Paper fills exactly at the touch, so slippage is zero by
    # construction — but the CROSSING cost is real and must show.
    assert spot_trade.slippage['slippage_spread'] == pytest.approx(0.0)
    assert spot_trade.slippage['crossing_spread'] == pytest.approx(0.1)


def test_the_position_carries_both_halves_of_the_round_trip(tmp_path, config):
    from statarb.coordinator import PaperExecutor
    from statarb.database import DataLogger
    from statarb.positions import PositionManager
    from tests.test_limit_execution import LimitFakeLeg
    spot_leg = LimitFakeLeg('a', price=3300.0)
    fut_leg = LimitFakeLeg('b', price=3320.0)
    executor = PaperExecutor(spot_leg, fut_leg, config)
    db = DataLogger(db_path=str(tmp_path / 'slip.db'))
    pm = PositionManager(db)

    reference = make_book(3299.95, 3300.05, 3319.95, 3320.05)
    ok, spot_trade, fut_trade = executor.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 1.0, 'XAUUSD', 'GC1225',
        reference=reference)
    position = pm.create_position('GOLD', SignalType.SELL_BASIS,
                                  spot_trade, fut_trade, 25.0)
    position.entry_slippage = spot_trade.slippage

    spot_leg.price, fut_leg.price = 3310.0, 3325.0
    exit_ref = make_book(3309.95, 3310.05, 3324.95, 3325.05)
    assert pm.close_position(position.position_id, 'TAKE_PROFIT', executor,
                             contract_size=100, reference=exit_ref)
    assert position.exit_slippage is not None
    assert position.exit_slippage['closing'] is True
    # Entry sold the spread, exit bought it back.
    assert position.entry_slippage['selling_spread'] is True
    assert position.exit_slippage['selling_spread'] is False


def test_the_round_trip_lands_in_trade_review(tmp_path, config):
    import sqlite3
    from datetime import datetime
    from statarb.database import DataLogger
    from statarb.models import Position

    db = DataLogger(db_path=str(tmp_path / 'review.db'))
    spot = Trade('XAUUSD', OrderSide.BUY, 1.0)
    fut = Trade('GC1225', OrderSide.SELL, 1.0)
    position = Position('POS_0001', 'GOLD', SignalType.SELL_BASIS, spot, fut)
    position.close_time = datetime.now()
    position.close_reason = 'TAKE_PROFIT'
    position.entry_slippage = {'crossing_spread': 0.30,
                               'slippage_spread': 0.05, 'slippage_usd': 5.0}
    position.exit_slippage = {'crossing_spread': 0.30,
                              'slippage_spread': 0.02, 'slippage_usd': 2.0}
    db.log_trade_review(position, outcome='TARGET_HIT')

    conn = sqlite3.connect(db.db_path)
    row = conn.execute(
        'SELECT entry_slip_usd, exit_slip_usd, slip_usd, entry_cross_spread, '
        'asset, exit_reason FROM trade_review WHERE position_id=?',
        ('POS_0001',)).fetchone()
    conn.close()
    assert row[0] == pytest.approx(5.0)
    assert row[1] == pytest.approx(2.0)
    assert row[2] == pytest.approx(7.0)          # round trip
    assert row[3] == pytest.approx(0.30)
    # Named columns, so nothing shifted a slot when the table grew.
    assert row[4] == 'GOLD' and row[5] == 'TAKE_PROFIT'


def test_an_unmeasured_round_trip_stores_null_not_zero(tmp_path):
    import sqlite3
    from datetime import datetime
    from statarb.database import DataLogger
    from statarb.models import Position

    db = DataLogger(db_path=str(tmp_path / 'review2.db'))
    position = Position('POS_0002', 'GOLD', SignalType.SELL_BASIS,
                        Trade('XAUUSD', OrderSide.BUY, 1.0),
                        Trade('GC1225', OrderSide.SELL, 1.0))
    position.close_time = datetime.now()
    db.log_trade_review(position, outcome='TIME_EXIT')
    conn = sqlite3.connect(db.db_path)
    row = conn.execute('SELECT slip_usd FROM trade_review '
                       'WHERE position_id=?', ('POS_0002',)).fetchone()
    conn.close()
    assert row[0] is None       # a zero would read as perfect execution


def test_the_entry_report_survives_a_restart():
    from statarb.models import Position
    position = Position('POS_0003', 'GOLD', SignalType.SELL_BASIS,
                        Trade('XAUUSD', OrderSide.BUY, 1.0),
                        Trade('GC1225', OrderSide.SELL, 1.0))
    position.spot_trade.requested_price = 3300.10
    position.entry_slippage = {'slippage_spread': 0.05, 'slippage_usd': 5.0}
    recovered = Position.from_dict(position.to_dict())
    assert recovered.entry_slippage['slippage_usd'] == 5.0
    assert recovered.spot_trade.requested_price == 3300.10


# --- reaching the operator ------------------------------------------------

def review_row(**over):
    base = {'position_id': 'P1', 'asset': 'GOLD', 'realized_pnl': 10.0,
            'cost_est': 59.0, 'lots': 1.0, 'notional': 430000.0,
            'entry_cross_spread': 0.30, 'entry_cross_usd': 30.0,
            'entry_slip_usd': 5.0, 'exit_cross_spread': 0.30,
            'exit_cross_usd': 30.0, 'exit_slip_usd': 2.0, 'slip_usd': 7.0}
    base.update(over)
    return base


def test_the_aggregate_counts_fills_not_trades():
    from statarb.webapi import slippage_block
    block = slippage_block([review_row(), review_row(position_id='P2')])
    assert block['measured_sides'] == 4          # 2 trades x 2 sides
    assert block['measured_round_trips'] == 2
    assert block['avg_entry_slip_usd'] == pytest.approx(5.0)
    assert block['avg_exit_slip_usd'] == pytest.approx(2.0)
    assert block['avg_round_trip_slip_usd'] == pytest.approx(7.0)


def test_unmeasured_trades_are_excluded_not_counted_as_zero():
    """Averaging in a zero would drag the figure toward 'flawless'."""
    from statarb.webapi import slippage_block
    blank = {'position_id': 'P0', 'realized_pnl': 1.0}
    block = slippage_block([review_row(), blank])
    assert block['measured_sides'] == 2
    assert block['avg_slip_usd'] == pytest.approx(3.5)   # (5+2)/2, not /4


def test_price_improvement_is_counted_and_reported():
    from statarb.webapi import slippage_block
    block = slippage_block([review_row(entry_slip_usd=-4.0, slip_usd=-2.0)])
    assert block['improved_sides'] == 1
    assert block['avg_slip_usd'] == pytest.approx(-1.0)


def test_the_model_audit_compares_like_with_like():
    """CLAUDE.md: alarm when modelled >= 2x realised. Realised must be
    crossing PLUS slippage — the model's cost_est covers the whole
    round trip, so comparing it against slippage alone would flatter
    it enormously."""
    from statarb.webapi import slippage_block
    block = slippage_block([review_row()])       # 30 + 30 + 7 = 67 real
    assert block['avg_realised_cost_usd'] == pytest.approx(67.0)
    assert block['avg_modelled_cost_usd'] == pytest.approx(59.0)
    assert block['model_ratio'] == pytest.approx(59.0 / 67.0)
    assert block['compared_trades'] == 1


def test_the_audit_stays_blank_without_both_numbers():
    from statarb.webapi import slippage_block
    block = slippage_block([review_row(cost_est=None)])
    assert block['model_ratio'] is None
    assert block['compared_trades'] == 0


def test_an_empty_book_does_not_divide_by_zero():
    from statarb.webapi import slippage_block
    block = slippage_block([])
    assert block['measured_sides'] == 0
    assert block['avg_slip_usd'] is None
    assert block['model_ratio'] is None


def test_the_open_position_card_receives_it():
    from statarb import webapi
    ui = webapi.status_to_ui({
        'assets': [{'asset': 'GOLD', 'z': 1.0}],
        'positions': [{'position_id': 'P1', 'asset': 'GOLD',
                       'signal_type': 'SELL_BASIS', 'lots': 1.0,
                       'entry_slippage': {'slippage_usd': 5.0,
                                          'executed_spread': 58.7}}],
    }, {})
    assert ui['open_trade']['entry_slippage']['slippage_usd'] == 5.0


def test_the_journal_row_carries_both_halves():
    from statarb import webapi
    row = webapi.trade_to_ui(review_row())
    assert row['slip_usd'] == pytest.approx(7.0)
    assert row['entry_slip_usd'] == pytest.approx(5.0)
    assert row['exit_slip_usd'] == pytest.approx(2.0)


def test_the_journal_leaves_an_unmeasured_trade_blank():
    from statarb import webapi
    row = webapi.trade_to_ui({'position_id': 'P0', 'realized_pnl': 1.0})
    assert row['slip_usd'] is None       # not 0.0


def test_telegram_reports_the_execution_block(config):
    from statarb.notify import TelegramNotifier
    notifier = TelegramNotifier(config)
    rows = notifier._slippage_rows({
        'decision_spread': 59.00, 'quoted_spread': 58.70,
        'executed_spread': 58.65, 'crossing_spread': 0.30,
        'slippage_spread': 0.05, 'slippage_usd': 5.0})
    text = '\n'.join(rows)
    assert 'EXECUTION' in text
    assert 'Wanted' in text and 'Quoted' in text and 'Filled' in text
    assert 'Crossing' in text and 'Slippage' in text


def test_telegram_stays_silent_when_nothing_was_measured(config):
    from statarb.notify import TelegramNotifier
    notifier = TelegramNotifier(config)
    assert notifier._slippage_rows(None) == []
    assert notifier._slippage_rows({'executed_spread': None}) == []
