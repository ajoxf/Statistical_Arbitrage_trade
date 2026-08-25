"""Which way the trade went, and the exits that read it.

Operator, 2026-08-25, on four short-spread trades all badged LONG:
"You have shorted the spread. High to Low. Why is it showing as 'long'
... The Exit should be a 'Short' - not a long."

The direction was never RECORDED. `trade_review` had no column for it,
so the UI inferred it from the sign of `entry_z` — and a manual trade
has no z, so `(None or 0) > 0` is False and every one of them rendered
LONG. Three of the four rows showed "Z 0.0" beside the wrong badge.

A direction is a fact about the order. It is read, not inferred.
"""

import pytest

from statarb import webapi
from statarb.exits import ExitLadder
from statarb.marketdata import executable_spread
from statarb.models import OrderSide, Position, SignalType, Trade


# --- the label ------------------------------------------------------------

def test_a_recorded_short_reads_short():
    assert webapi.position_type({'signal_type': 'SELL_BASIS'}) == 'SHORT'


def test_a_recorded_long_reads_long():
    assert webapi.position_type({'signal_type': 'BUY_BASIS'}) == 'LONG'


def test_the_recorded_direction_beats_the_z_sign():
    """A manual short can be entered at a NEGATIVE z — the operator
    picks the direction, not the statistic. The recorded fact wins."""
    assert webapi.position_type(
        {'signal_type': 'SELL_BASIS', 'entry_z': -1.7}) == 'SHORT'


@pytest.mark.parametrize('entry_z', [0.0, None])
def test_no_z_no_longer_means_long(entry_z):
    """The exact bug: `(None or 0) > 0` is False, so every trade
    without a z rendered LONG whatever it actually was.

    Asserted through `trade_to_ui` and `excursion_row`, which both
    exist on either side of the fix, so this fails on the ANSWER
    against the old code rather than on a helper that did not
    exist yet."""
    row = {'signal_type': 'SELL_BASIS', 'entry_z': entry_z,
           'position_id': 'P1'}
    assert webapi.position_type(row) == 'SHORT'
    assert webapi.trade_to_ui(row)['position_type'] == 'SHORT'
    assert webapi.excursion_row(row)['position_type'] == 'SHORT'


def test_old_rows_still_infer_from_the_z_sign():
    """Rows written before the column existed. For a SIGNAL entry the
    z sign IS the right inference — |z| >= ENTRY_Z and the direction
    follows its sign."""
    assert webapi.position_type({'entry_z': 3.2}) == 'SHORT'
    assert webapi.position_type({'entry_z': -3.2}) == 'LONG'


@pytest.mark.parametrize('row', [{}, {'entry_z': 0.0}, {'entry_z': None},
                                 {'signal_type': ''}])
def test_an_unknown_direction_is_none_not_a_guess(row):
    """Neither a recorded direction nor a z to infer one from. None, so
    the UI can show a dash — an unknown direction must not render as a
    confident one, which is the whole fault."""
    assert webapi.position_type(row) is None
    # ...and through the mapper the UI actually calls, which used
    # to answer 'LONG' here with total confidence.
    assert webapi.trade_to_ui(dict(row, position_id='P1'))[
        'position_type'] is None


def test_both_journal_shapes_use_it():
    row = {'signal_type': 'SELL_BASIS', 'entry_z': 0.0, 'position_id': 'P1'}
    assert webapi.trade_to_ui(row)['position_type'] == 'SHORT'
    assert webapi.excursion_row(row)['position_type'] == 'SHORT'


def test_the_direction_is_written_to_the_review_table(tmp_path):
    """Recorded at close, so it never has to be inferred again."""
    from statarb.database import DataLogger
    db = DataLogger(db_path=str(tmp_path / 'a.db'))
    position = Position('POS_0001', 'GOLD', SignalType.SELL_BASIS,
                        Trade('XAUUSD', OrderSide.BUY, 0.02),
                        Trade('GC1225', OrderSide.SELL, 0.02))
    position.realized_pnl = -2.32
    db.log_trade_review(position)

    import sqlite3
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM trade_review').fetchall()
    conn.close()
    assert rows and rows[0]['signal_type'] == 'SELL_BASIS'
    # ...and it survives the trip back out to the UI, with no z at all.
    assert webapi.position_type(dict(rows[0])) == 'SHORT'


# --- the exit reads the OTHER side of the book ----------------------------

def test_a_short_is_closed_on_the_long_spread():
    """Operator: "while taking the exit it should take the Bid and Ask
    for the 'Long' position." A short-spread position is bought back,
    which is a LONG-spread trade, so it pays the long touches. Reading
    the favourable side at both ends would make every trade look like
    it cleared its costs."""
    md = {'spread': 55.00, 'short_spread': 54.80, 'long_spread': 55.20}
    assert executable_spread(md, SignalType.SELL_BASIS) == 54.80
    assert executable_spread(md, SignalType.SELL_BASIS,
                             closing=True) == 55.20


def test_a_long_is_closed_on_the_short_spread():
    md = {'spread': 55.00, 'short_spread': 54.80, 'long_spread': 55.20}
    assert executable_spread(md, SignalType.BUY_BASIS) == 55.20
    assert executable_spread(md, SignalType.BUY_BASIS,
                             closing=True) == 54.80


def test_the_round_trip_pays_the_full_bid_ask_once():
    """Entry on one side, exit on the other: nothing moving, and the
    pair is down exactly one round turn."""
    md = {'spread': 55.00, 'short_spread': 54.80, 'long_spread': 55.20}
    entry = executable_spread(md, SignalType.SELL_BASIS)
    exit_ = executable_spread(md, SignalType.SELL_BASIS, closing=True)
    assert entry - exit_ == pytest.approx(
        -(md['long_spread'] - md['short_spread']))


# --- the reversion gate must have somewhere to come home FROM -------------

def make_position():
    return Position('POS_0001', 'GOLD', SignalType.SELL_BASIS,
                    Trade('XAUUSD', OrderSide.BUY, 0.02),
                    Trade('GC1225', OrderSide.SELL, 0.02))


def plan(**over):
    base = {'tp_usd': 0.0, 'stop_usd': 50.0, 'gate_floor_usd': 0.0,
            'max_hold_sec': 300, 'rt_cost_usd': 1.0, 'entry_z': 0.0,
            'entry_mu': None, 'entry_home': False}
    base.update(over)
    return base


def test_a_gate_that_was_home_at_entry_does_not_close_the_trade(config):
    """The operator's four trades. A MANUAL entry skips the signal
    gates, so it is routinely placed at z ~ 0 — already inside EXIT_Z.
    The reversion gate is then satisfied from the first tick, and its
    max-hold release closes the trade at ANY P&L: no profit, no stop
    hit, just a timed exit at a loss.
    """
    ladder = ExitLadder(config)
    position = make_position()
    p = plan(entry_home=True)
    # Well past 2x max-hold, sitting on a loss, gate "home" the whole
    # time. Nothing here is a reason to close.
    assert ladder.evaluate(position, p, z=0.0, gross_pnl=-1.3,
                           age_sec=10_000, spread=55.0) != 'REVERSION_EXIT'


def test_a_real_reversion_still_closes_the_trade(config):
    """The gate is not disabled — only withheld where it never left.
    A signal entry at z 3.2 that comes home still banks."""
    ladder = ExitLadder(config)
    position = make_position()
    p = plan(entry_z=3.2, entry_home=False)
    assert ladder.evaluate(position, p, z=0.2, gross_pnl=5.0,
                           age_sec=60, spread=55.0) == 'REVERSION_EXIT'


def test_the_trade_still_always_has_an_exit(config):
    """The completeness rule. A gate withheld must not strand a
    position: the dollar stop and the hard time stop both still fire.
    """
    ladder = ExitLadder(config)
    position = make_position()
    config.EXITS['HARD_TIME_STOP_MULT'] = 3
    p = plan(entry_home=True)
    assert ladder.evaluate(position, p, z=0.0, gross_pnl=-60.0,
                           age_sec=60, spread=55.0) == 'DOLLAR_STOP'
    assert ladder.evaluate(position, p, z=0.0, gross_pnl=-1.0,
                           age_sec=100_000, spread=55.0) == 'TIME_STOP'


def test_the_operators_own_levels_still_fire(config):
    """A manual trade's thesis is the operator's target and stop, and
    withholding the reversion opinion must not touch them."""
    ladder = ExitLadder(config)
    position = make_position()
    p = plan(entry_home=True, manual_exit_spread=53.0,
             manual_stop_spread=60.0)
    assert ladder.evaluate(position, p, z=0.0, gross_pnl=4.0,
                           age_sec=60, spread=52.9) == 'MANUAL_TARGET'
    assert ladder.evaluate(position, p, z=0.0, gross_pnl=-4.0,
                           age_sec=60, spread=60.1) == 'MANUAL_STOP'


def test_the_coordinator_freezes_whether_the_gate_started_home():
    """Computed once, at entry, against the mu it is measured on."""
    from statarb.config import AlgoTradingConfig
    cfg = AlgoTradingConfig()
    ladder = ExitLadder(cfg)
    inside = ladder._reversion_home({'entry_mu': None}, 0.0, 55.0,
                                    SignalType.SELL_BASIS)
    outside = ladder._reversion_home({'entry_mu': None}, 3.2, 55.0,
                                     SignalType.SELL_BASIS)
    assert inside is True and outside is False


# --- the z fallback is not sound for a MANUAL row ---------------------

def test_a_manual_row_with_no_recorded_direction_shows_a_dash():
    """The z-sign fallback assumes the entry gates chose the direction,
    which is true for a SIGNAL entry (|z| >= ENTRY_Z, sign decides) and
    false for a hand-placed one — the operator picks. Live 2026-08-25:
    a manual row badged SHORT at "Z 2.0" whose spread fell 0.90 (the
    profitable direction for a short) booked -$6.10, which reconciles
    exactly as a LONG."""
    assert webapi.position_type(
        {'source': 'MANUAL', 'entry_z': 2.0}) is None
    assert webapi.position_type(
        {'source': 'MANUAL', 'entry_z': -2.0}) is None


def test_a_manual_row_that_recorded_its_direction_is_believed():
    assert webapi.position_type(
        {'source': 'MANUAL', 'entry_z': 2.0,
         'signal_type': 'BUY_BASIS'}) == 'LONG'
    assert webapi.position_type(
        {'source': 'MANUAL', 'entry_z': -2.0,
         'signal_type': 'SELL_BASIS'}) == 'SHORT'


def test_a_signal_row_keeps_the_fallback():
    """It IS the right inference there, and old rows depend on it."""
    assert webapi.position_type({'entry_z': 2.0}) == 'SHORT'
    assert webapi.position_type(
        {'source': 'SIGNAL', 'entry_z': -2.0}) == 'LONG'
