"""The trader's book and the algo's book are kept apart.

Operator, 2026-08-25: "stop manual trades feeding the breakers and
streak reducer. We should be able to easily distinguish 'Manual' and
'Algo' based trades. Manual Trades PnL and all Analysis should be
recorded somewhere."

Three things, and they are one idea: a hand-placed trade is governed by
different rules, so it must not drive the algo's governor, and the two
must be separable everywhere they are reported.
"""

import sqlite3

import pytest

from statarb import webapi
from statarb.database import DataLogger
from statarb.models import OrderSide, Position, SignalType, Trade
from statarb.risk import RiskManager


# --- the breakers see only the algo's book ----------------------------

@pytest.fixture
def rm(config):
    config.RISK_LIMITS.update({'LOSS_STREAK_PAUSE': 3,
                               'LOSS_STREAK_REDUCE': 2,
                               'STREAK_SIZE_CUT': 0.5,
                               'DAILY_MAX_LOSS_USD': 100.0,
                               'MARGIN_BREAKER_ENABLED': False})
    return RiskManager(config)


def test_manual_losses_do_not_trip_the_loss_streak_breaker(rm):
    """Four hand-placed losses would have paused the ALGO — the exact
    conflict the manual/algo split exists to remove."""
    for _ in range(4):
        rm.on_position_closed(-2.10, manual=True)
    assert rm.consecutive_losses == 0
    assert rm.halted() == (False, None)


def test_manual_losses_do_not_trip_the_daily_loss_breaker(rm):
    rm.on_position_closed(-500.0, manual=True)
    assert rm.halted()[0] is False


def test_manual_losses_do_not_shrink_the_algos_clip(rm):
    for _ in range(3):
        rm.on_position_closed(-2.10, manual=True)
    assert rm.size_multiplier() == pytest.approx(1.0)


def test_algo_losses_still_do_all_three(rm):
    """The breakers are not disabled — they just stopped reading the
    wrong book."""
    for _ in range(3):
        rm.on_position_closed(-2.10)
    assert rm.consecutive_losses == 3
    assert rm.halted()[0] is True
    assert rm.size_multiplier() < 1.0


def test_a_manual_entry_does_not_start_the_algos_cooldown(rm):
    """`last_signal_time` drives the entry cooldown, and daily_trades
    feeds MAX_DAILY_TRADES. A hand-placed trade must put the algo on
    neither."""
    rm.record_trade('GOLD', lots=5.0, manual=True)
    assert 'GOLD' not in rm.last_signal_time
    assert len(rm.daily_trades) == 0
    rm.record_trade('GOLD', lots=1.0)
    assert 'GOLD' in rm.last_signal_time
    assert len(rm.daily_trades) == 1


# --- ...but the money is still counted --------------------------------

def test_the_manual_pnl_is_still_recorded(rm):
    rm.on_position_closed(-2.10, manual=True)
    rm.on_position_closed(+0.50, manual=True)
    assert rm.manual_realized_pnl == pytest.approx(-1.60)
    assert rm.manual_trades_today == 2
    # ...and stayed out of the algo's total.
    assert rm.daily_realized_pnl == 0.0


def test_the_two_books_add_up(rm):
    rm.on_position_closed(-2.10, manual=True)
    rm.on_position_closed(+5.00)
    assert rm.daily_realized_pnl == pytest.approx(5.00)
    assert rm.manual_realized_pnl == pytest.approx(-2.10)
    assert rm.total_realized_pnl == pytest.approx(2.90)


def test_both_books_reset_on_a_new_day(rm):
    import datetime as _dt
    rm.on_position_closed(-2.10, manual=True)
    rm.on_position_closed(-5.00)
    rm._breaker_date = _dt.date(2000, 1, 1)
    rm._roll_breaker_date()
    assert rm.manual_realized_pnl == 0.0
    assert rm.manual_trades_today == 0
    assert rm.daily_realized_pnl == 0.0


# --- who placed it, recorded and readable -----------------------------

def review_row(tmp_path, source):
    db = DataLogger(db_path=str(tmp_path / f'{source}.db'))
    p = Position('POS_0001', 'GOLD', SignalType.SELL_BASIS,
                 Trade('XAUUSD', OrderSide.BUY, 0.02),
                 Trade('GC1225', OrderSide.SELL, 0.02))
    p.realized_pnl = -2.10
    p.exit_plan = {'source': source}
    db.log_trade_review(p)
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute('SELECT * FROM trade_review').fetchone())
    finally:
        conn.close()


@pytest.mark.parametrize('source', ['MANUAL', 'SIGNAL'])
def test_the_source_is_written_at_close(tmp_path, source):
    assert review_row(tmp_path, source)['source'] == source


def test_the_source_reaches_both_ui_shapes(tmp_path):
    row = review_row(tmp_path, 'MANUAL')
    assert webapi.trade_to_ui(row)['source'] == 'MANUAL'
    assert webapi.excursion_row(row)['source'] == 'MANUAL'


def test_a_row_predating_the_column_reads_as_algo():
    """It IS what the engine did with them: they were managed by the
    full exit ladder and they fed the breakers. Calling them 'unknown'
    would be more precise and less true."""
    assert webapi.trade_source({'position_id': 'OLD'}) == 'SIGNAL'
    assert webapi.trade_source({'source': None}) == 'SIGNAL'


# --- the analysis is recorded, and split ------------------------------

def rows():
    return [
        {'realized_pnl': -2.10, 'source': 'MANUAL', 'peak_pnl': 0.34,
         'peak_min': 2.0, 'position_id': 'M1'},
        {'realized_pnl': -1.90, 'source': 'MANUAL', 'peak_pnl': 0.10,
         'peak_min': 1.0, 'position_id': 'M2'},
        {'realized_pnl': 5.00, 'source': 'SIGNAL', 'peak_pnl': 6.0,
         'peak_min': 4.0, 'position_id': 'A1'},
    ]


def test_the_two_books_are_scored_apart():
    block = webapi.statistics_by_source(rows())
    assert block['manual']['total_trades'] == 2
    assert block['manual']['total_pnl'] == pytest.approx(-4.00)
    assert block['manual']['win_rate'] == 0
    assert block['algo']['total_trades'] == 1
    assert block['algo']['total_pnl'] == pytest.approx(5.00)
    assert block['algo']['win_rate'] == 100


def test_the_combined_book_is_still_available():
    """Both, because the account only has one balance."""
    block = webapi.statistics_by_source(rows())
    assert block['all']['total_trades'] == 3
    assert block['all']['total_pnl'] == pytest.approx(
        block['algo']['total_pnl'] + block['manual']['total_pnl'])


def test_an_empty_book_still_returns_a_full_block():
    """The template renders every field unconditionally, so a source
    with no trades must not come back as None or a short dict."""
    block = webapi.statistics_by_source([])
    for key in ('all', 'algo', 'manual'):
        assert block[key]['total_trades'] == 0
        assert block[key]['total_pnl'] == 0


def test_the_manual_excursions_are_recorded_too(tmp_path):
    """"all Analysis" — the peak/trough, the outcome tag, the slippage
    and the levels are all in trade_review for a manual trade exactly
    as for a signal one. Only the SOURCE distinguishes them."""
    db = DataLogger(db_path=str(tmp_path / 'a.db'))
    p = Position('POS_0004', 'GOLD', SignalType.SELL_BASIS,
                 Trade('XAUUSD', OrderSide.BUY, 0.02),
                 Trade('GC1225', OrderSide.SELL, 0.02))
    p.realized_pnl = -2.10
    p.peak_pnl, p.peak_min = 0.34, 2.0
    p.trough_pnl, p.trough_min = -1.69, 6.0
    p.exit_plan = {'source': 'MANUAL', 'levels': {'be': 55.18, 'tp': 54.73}}
    p.close_reason = 'MANUAL_STOP'
    db.log_trade_review(p, outcome='STOPPED_IN_TREND')

    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute('SELECT * FROM trade_review').fetchone())
    conn.close()
    assert row['source'] == 'MANUAL'
    assert row['peak_pnl'] == pytest.approx(0.34)
    assert row['trough_pnl'] == pytest.approx(-1.69)
    assert row['outcome'] == 'STOPPED_IN_TREND'
    assert row['exit_reason'] == 'MANUAL_STOP'
    assert row['be_spread'] == pytest.approx(55.18)
