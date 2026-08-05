"""Shadow what-if-held tracker and SD-touch detection."""

import sqlite3

import pytest

from statarb.models import OrderSide, Position, SignalType, Trade
from statarb.shadow import ShadowTracker


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def closed_position(pid='POS_0001', signal=SignalType.SELL_BASIS,
                    tp=15000.0, fees=3000.0, max_hold=2400):
    spot = Trade('XAUUSD', OrderSide.BUY, 50.0)
    spot.executed_price = 3300.0
    fut = Trade('GC1225', OrderSide.SELL, 50.0)
    fut.executed_price = 3320.0
    position = Position(pid, 'GOLD', signal, spot, fut)
    position.realized_pnl = -1500.0
    position.close_reason = 'DOLLAR_STOP'
    position.exit_plan = {'tp_usd': tp, 'stop_usd': 1500,
                          'rt_cost_usd': fees, 'max_hold_sec': max_hold,
                          'gate_floor_usd': 0, 'entry_z': 3.0,
                          'entry_sigma': 2.0}
    return position


def test_shadow_reverted_to_target(data_logger):
    clock = FakeClock()
    tracker = ShadowTracker(data_logger, clock=clock)
    tracker.start(closed_position(), contract_size=100)
    assert len(tracker.active) == 1

    # Spot +$4/oz -> gross 20k, net 17k >= 15k target
    clock.t = 600
    tracker.update('GOLD', 3304.0, 3320.0)
    assert tracker.active == []
    row = sqlite3.connect(data_logger.db_path).execute(
        "SELECT verdict, hit_tp_min, what_if_net FROM shadow_trades"
    ).fetchone()
    assert row[0] == 'REVERTED_TO_TARGET'
    assert row[1] == pytest.approx(10.0)
    assert row[2] == pytest.approx(17000.0)


def test_shadow_break_even_then_horizon(data_logger):
    clock = FakeClock()
    tracker = ShadowTracker(data_logger, clock=clock)
    tracker.start(closed_position(), contract_size=100)

    clock.t = 1200
    tracker.update('GOLD', 3300.8, 3320.0)     # net +$1k: BE touched
    assert tracker.active[0]['hit_be_min'] == pytest.approx(20.0)
    clock.t = 2 * 2400 + 7200                  # past the horizon
    tracker.update('GOLD', 3300.7, 3320.0)
    row = sqlite3.connect(data_logger.db_path).execute(
        "SELECT verdict FROM shadow_trades").fetchone()
    assert row[0] == 'REVERTED_TO_BREAK_EVEN'


def test_shadow_kept_bleeding(data_logger):
    clock = FakeClock()
    tracker = ShadowTracker(data_logger, clock=clock)
    tracker.start(closed_position(), contract_size=100)

    clock.t = 10 ** 6                          # way past horizon, deep red
    tracker.update('GOLD', 3295.0, 3320.0)
    row = sqlite3.connect(data_logger.db_path).execute(
        "SELECT verdict, what_if_net FROM shadow_trades").fetchone()
    assert row[0] == 'KEPT_BLEEDING'
    assert row[1] < 0


def test_sd_touch_detection(config, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coordinator = Coordinator(config, trading_mode='PAPER')

    # 0.5 -> 1.2 crosses +1 UP; 1.2 -> 3.2 crosses +2 and +3 UP;
    # 3.2 -> 0.4 crosses +3, +2, +1 DOWN; warm-up (None) never records
    for z in (None, 0.5, 1.2, 3.2, 0.4):
        coordinator._detect_sd_touches('GOLD', z, spread=20.0)

    rows = sqlite3.connect(coordinator.data_logger.db_path).execute(
        "SELECT sd_level, direction FROM sd_touches ORDER BY rowid"
    ).fetchall()
    assert rows == [(1, 'UP'), (2, 'UP'), (3, 'UP'),
                    (1, 'DOWN'), (2, 'DOWN'), (3, 'DOWN')]
