"""Reconciliation: orphan auto-close (3 strikes, ledger, breaker
charge), ghost force-clear, flaky-snapshot tolerance."""

import logging
import sqlite3

import pytest

from statarb.models import (OrderSide, Position, PositionStatus,
                            SignalType, Trade)
from statarb.positions import PositionManager
from statarb.reconcile import Reconciler
from statarb.risk import RiskManager


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class ReconFakeLeg:
    def __init__(self, name):
        self.name = name
        self.live = []            # broker-side positions
        self.snapshot_fails = 0   # return None for N calls
        self.closed = []          # (symbol, ticket, volume)
        self.close_should_fail = False

    def positions(self, symbol=None):
        if self.snapshot_fails > 0:
            self.snapshot_fails -= 1
            return None
        return list(self.live)

    def close_ticket(self, symbol, ticket, volume, entry_side,
                     slippage_points=1.0, comment=""):
        if self.close_should_fail:
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'error': 'forced failure'}
        self.closed.append((symbol, ticket, volume))
        self.live = [p for p in self.live if p['ticket'] != ticket]
        return {'ok': True, 'filled_volume': volume, 'price': 3301.0,
                'error': None}


def make_recon(config, data_logger, spot_leg, fut_leg):
    pm = PositionManager(data_logger)
    rm = RiskManager(config)
    clock = FakeClock()
    recon = Reconciler(config, pm, data_logger, rm,
                       {'spot': spot_leg, 'futures': fut_leg}, clock=clock)
    return recon, pm, rm


def tracked_position(pm, spot_tickets, fut_tickets):
    spot = Trade('XAUUSD', OrderSide.BUY, 50.0)
    spot.executed_price = 3300.0
    spot.position_tickets = spot_tickets
    fut = Trade('GC1225', OrderSide.SELL, 50.0)
    fut.executed_price = 3320.0
    fut.position_tickets = fut_tickets
    return pm.create_position('GOLD', SignalType.SELL_BASIS, spot, fut, 25.0)


def orphan(ticket=999):
    return {'ticket': ticket, 'symbol': 'XAUUSD', 'side': 'BUY',
            'volume': 10.0, 'price_open': 3300.0}


def test_orphan_closed_after_three_strikes(config, data_logger):
    spot_leg = ReconFakeLeg('a')
    fut_leg = ReconFakeLeg('b')
    recon, pm, rm = make_recon(config, data_logger, spot_leg, fut_leg)
    spot_leg.live = [orphan()]

    assert recon.check() == []          # strike 1
    assert recon.check() == []          # strike 2
    assert spot_leg.closed == []
    actions = recon.check()             # strike 3 -> act
    assert ('orphan_closed', 'a', 999) in actions
    assert spot_leg.closed == [('XAUUSD', 999, 10.0)]

    # Ledger entry written and daily-loss breaker charged
    conn = sqlite3.connect(data_logger.db_path)
    rows = conn.execute("SELECT ticket, volume FROM untracked_closes"
                        ).fetchall()
    conn.close()
    assert rows == [(999, 10.0)]
    # (3301 - 3300) x 10 lots x 100 oz/lot. The contract size was MISSING
    # here and this test asserted $10 — live 2026-08-26 four orphans were
    # booked at a hundredth of what they cost, understating both the
    # ledger the operator reads and the daily-loss breaker.
    assert rm.daily_realized_pnl == pytest.approx(1000.0)


def test_tracked_positions_are_never_touched(config, data_logger):
    spot_leg = ReconFakeLeg('a')
    fut_leg = ReconFakeLeg('b')
    recon, pm, rm = make_recon(config, data_logger, spot_leg, fut_leg)
    tracked_position(pm, [101, 102], [201])
    spot_leg.live = [{'ticket': 101, 'symbol': 'XAUUSD', 'side': 'BUY',
                      'volume': 25.0, 'price_open': 3300.0},
                     {'ticket': 102, 'symbol': 'XAUUSD', 'side': 'BUY',
                      'volume': 25.0, 'price_open': 3300.0}]
    fut_leg.live = [{'ticket': 201, 'symbol': 'GC1225', 'side': 'SELL',
                     'volume': 50.0, 'price_open': 3320.0}]

    for _ in range(5):
        assert recon.check() == []
    assert spot_leg.closed == [] and fut_leg.closed == []


def test_ghost_position_force_cleared(config, data_logger):
    spot_leg = ReconFakeLeg('a')
    fut_leg = ReconFakeLeg('b')
    recon, pm, rm = make_recon(config, data_logger, spot_leg, fut_leg)
    position = tracked_position(pm, [101], [201])
    # Broker is flat on both legs (crashed close, manual intervention...)

    recon.check()
    recon.check()
    assert position.status == PositionStatus.ACTIVE
    actions = recon.check()
    assert any(a[0] == 'ghost_cleared' for a in actions)
    assert position.status == PositionStatus.CLOSED
    assert position.close_reason == "RECONCILE_FORCE_CLEAR"


def test_flaky_snapshot_does_not_strike(config, data_logger):
    spot_leg = ReconFakeLeg('a')
    fut_leg = ReconFakeLeg('b')
    recon, pm, rm = make_recon(config, data_logger, spot_leg, fut_leg)
    spot_leg.live = [orphan()]
    spot_leg.snapshot_fails = 2         # two unreadable snapshots

    recon.check()                       # skipped — no strike
    recon.check()                       # skipped — no strike
    recon.check()                       # strike 1
    recon.check()                       # strike 2
    assert spot_leg.closed == []
    recon.check()                       # strike 3 -> close
    assert spot_leg.closed != []


def test_failed_orphan_close_books_nothing(config, data_logger):
    spot_leg = ReconFakeLeg('a')
    fut_leg = ReconFakeLeg('b')
    recon, pm, rm = make_recon(config, data_logger, spot_leg, fut_leg)
    spot_leg.live = [orphan()]
    spot_leg.close_should_fail = True

    for _ in range(4):
        recon.check()
    conn = sqlite3.connect(data_logger.db_path)
    rows = conn.execute("SELECT COUNT(*) FROM untracked_closes").fetchone()
    conn.close()
    assert rows[0] == 0
    assert rm.daily_realized_pnl == 0.0


def test_an_orphan_that_cannot_be_closed_escalates_once(config,
                                                        data_logger, caplog):
    """Live on CFI: every close came back 'Unsupported filling mode' and
    the reconciler retried the same four positions every 20 seconds
    forever. A position the broker will not close needs a human."""
    from statarb.reconcile import Reconciler
    from statarb.positions import PositionManager
    from statarb.risk import RiskManager

    class StuckLeg:
        name = 'Uts'

        def __init__(self):
            self.attempts = 0

        def positions(self, symbol=None):
            return [{'ticket': 102269341, 'symbol': 'XAUUSD_', 'side': 'BUY',
                     'volume': 0.01, 'price_open': 4260.0}]

        def close_ticket(self, *args, **kwargs):
            self.attempts += 1
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'error': 'Close failed: 10030 - Unsupported filling mode'}

    leg = StuckLeg()
    reconciler = Reconciler(config, PositionManager(data_logger),
                            data_logger, RiskManager(config),
                            {'spot': leg, 'futures': leg},
                            clock=lambda: 0.0)
    strikes = config.RECONCILE['STRIKES']
    attempts_allowed = config.RECONCILE['CLOSE_ATTEMPTS']

    with caplog.at_level('CRITICAL'):
        for _ in range(strikes * (attempts_allowed + 4)):
            reconciler.last_sync = -1e9        # always due
            reconciler.check()

    assert leg.attempts == attempts_allowed    # stopped hammering it
    assert 'CLOSE IT BY HAND' in caplog.text
    assert caplog.text.count('GIVING UP') == 1  # escalated exactly once


def test_a_close_that_starts_working_clears_the_escalation(config,
                                                           data_logger):
    from statarb.reconcile import Reconciler
    from statarb.positions import PositionManager
    from statarb.risk import RiskManager

    class Leg:
        name = 'Uts'
        open_position = True

        def positions(self, symbol=None):
            return ([{'ticket': 7001, 'symbol': 'XAUUSD_', 'side': 'BUY',
                      'volume': 0.01, 'price_open': 4260.0}]
                    if self.open_position else [])

        def close_ticket(self, *args, **kwargs):
            self.open_position = False
            return {'ok': True, 'filled_volume': 0.01, 'price': 4261.0,
                    'error': None}

    leg = Leg()
    reconciler = Reconciler(config, PositionManager(data_logger),
                            data_logger, RiskManager(config),
                            {'spot': leg, 'futures': leg},
                            clock=lambda: 0.0)
    for _ in range(config.RECONCILE['STRIKES']):
        reconciler.last_sync = -1e9
        actions = reconciler.check()
    assert ('orphan_closed', 'Uts', 7001) in actions
    assert not reconciler.unclosable


# ----------------------------------------------------------------------
# Orphan P&L is MONEY, so it carries the contract size
# ----------------------------------------------------------------------

def test_orphan_pnl_uses_the_futures_leg_contract_size(config, data_logger):
    """Leg B can have its OWN contract size, and the orphan booked
    against it must use that one rather than leg A's."""
    config.ASSETS['GOLD']['fut_lot_size'] = 50
    spot_leg = ReconFakeLeg('a')
    fut_leg = ReconFakeLeg('b')
    recon, pm, rm = make_recon(config, data_logger, spot_leg, fut_leg)
    fut_leg.live = [{'ticket': 777, 'symbol': 'GC1225', 'side': 'SELL',
                     'volume': 2.0, 'price_open': 3311.0}]

    for _ in range(3):
        recon.check()

    assert fut_leg.closed == [('GC1225', 777, 2.0)]
    # SELL closed at 3301: (3311 - 3301) x 2 lots x 50 = +$1,000
    assert rm.daily_realized_pnl == pytest.approx(1000.0)


def test_an_unknown_symbol_books_per_unit_and_says_so(config, data_logger,
                                                      caplog):
    """A symbol that belongs to no configured pair gets 1.0 rather than
    a guess — and a warning, because the figure is then understated by
    exactly the multiplier nobody could supply."""
    spot_leg = ReconFakeLeg('a')
    fut_leg = ReconFakeLeg('b')
    recon, pm, rm = make_recon(config, data_logger, spot_leg, fut_leg)
    spot_leg.live = [{'ticket': 555, 'symbol': 'WHO_KNOWS', 'side': 'BUY',
                      'volume': 10.0, 'price_open': 3300.0}]

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            recon.check()

    assert rm.daily_realized_pnl == pytest.approx(10.0)
    assert any('not a configured leg symbol' in r.message
               for r in caplog.records)
