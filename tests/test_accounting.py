"""True-position accounting: per-leg P&L to the cent, circuit
breakers, crash-safe persistence and restart recovery."""

import pytest

from statarb.database import DataLogger
from statarb.models import (OrderSide, Position, PositionStatus,
                            SignalType, Trade)
from statarb.positions import PositionManager
from statarb.risk import RiskManager


def filled_trade(symbol, side, lots, price, tickets=()):
    trade = Trade(symbol, side, lots)
    trade.executed_price = price
    trade.status = "EXECUTED"
    trade.position_tickets = list(tickets)
    return trade


def test_per_leg_pnl_matches_broker_to_the_cent():
    # SELL_BASIS: long spot @3300.25, short futures @3321.75, 50 lots,
    # 100 oz/lot. Exit spot @3305.10, futures @3324.30.
    position = Position(
        'POS_0001', 'GOLD', SignalType.SELL_BASIS,
        filled_trade('XAUUSD', OrderSide.BUY, 50.0, 3300.25),
        filled_trade('GC1225', OrderSide.SELL, 50.0, 3321.75))
    close_spot = filled_trade('XAUUSD', OrderSide.SELL, 50.0, 3305.10)
    close_fut = filled_trade('GC1225', OrderSide.BUY, 50.0, 3324.30)

    pnl = PositionManager.realized_pnl_from_fills(
        position, close_spot, close_fut, contract_size=100)
    # Spot: (3305.10-3300.25)*5000 = 24,250.00
    # Fut:  (3321.75-3324.30)*5000 = -12,750.00
    assert pnl == pytest.approx(11500.00, abs=0.01)


def test_per_leg_pnl_buy_basis_mirrored():
    position = Position(
        'POS_0002', 'GOLD', SignalType.BUY_BASIS,
        filled_trade('XAUUSD', OrderSide.SELL, 10.0, 3300.00),
        filled_trade('GC1225', OrderSide.BUY, 10.0, 3320.00))
    close_spot = filled_trade('XAUUSD', OrderSide.BUY, 10.0, 3298.50)
    close_fut = filled_trade('GC1225', OrderSide.SELL, 10.0, 3319.00)

    pnl = PositionManager.realized_pnl_from_fills(
        position, close_spot, close_fut, contract_size=100)
    # Spot short: (3300.00-3298.50)*1000 = +1500
    # Fut long:   (3319.00-3320.00)*1000 = -1000
    assert pnl == pytest.approx(500.00, abs=0.01)


def test_missing_close_price_falls_back_to_mark():
    position = Position(
        'POS_0003', 'GOLD', SignalType.SELL_BASIS,
        filled_trade('XAUUSD', OrderSide.BUY, 50.0, 3300.0),
        filled_trade('GC1225', OrderSide.SELL, 50.0, 3320.0))
    position.unrealized_pnl = 777.0
    close_spot = filled_trade('XAUUSD', OrderSide.SELL, 50.0, None)
    pnl = PositionManager.realized_pnl_from_fills(
        position, close_spot, None, contract_size=100)
    assert pnl == 777.0


# ---------------------------------------------------------------------------


def test_circuit_breaker_streak_reducer_and_pause(config):
    config.RISK_LIMITS.update({'LOSS_STREAK_REDUCE': 3,
                               'STREAK_SIZE_CUT': 0.2,
                               'LOSS_STREAK_PAUSE': 6,
                               'DAILY_MAX_LOSS_USD': 0})
    rm = RiskManager(config)
    assert rm.size_multiplier() == 1.0

    for _ in range(3):
        rm.on_position_closed(-100)
    assert rm.size_multiplier() == pytest.approx(0.8)   # -20% at 3
    assert rm.halted() == (False, None)

    for _ in range(3):
        rm.on_position_closed(-100)
    halted, why = rm.halted()
    assert halted and 'consecutive losses' in why       # pause at 6

    rm.on_position_closed(+500)                          # a win resets
    assert rm.consecutive_losses == 0
    assert rm.size_multiplier() == 1.0


def test_circuit_breaker_daily_loss_halt(config, data_logger):
    config.RISK_LIMITS['DAILY_MAX_LOSS_USD'] = 1000.0
    rm = RiskManager(config)
    pm = PositionManager(data_logger)

    rm.on_position_closed(-600)
    ok, _ = rm.validate_new_position('GOLD', SignalType.SELL_BASIS, 1.0, pm)
    assert ok
    rm.on_position_closed(-600)
    ok, reason = rm.validate_new_position('GOLD', SignalType.SELL_BASIS,
                                          1.0, pm)
    assert not ok and 'Circuit breaker' in reason


# ---------------------------------------------------------------------------


def test_paper_mode_full_close_path_end_to_end(tmp_path):
    """Open -> mark -> close entirely through the PaperExecutor: the
    same lifecycle code as LIVE, fills at the simulated touch."""
    from statarb.coordinator import PaperExecutor
    from tests.test_limit_execution import LimitFakeLeg

    spot_leg = LimitFakeLeg('a', price=3300.0)
    fut_leg = LimitFakeLeg('b', price=3320.0)
    executor = PaperExecutor(spot_leg, fut_leg)
    db = DataLogger(db_path=str(tmp_path / "paper.db"))
    pm = PositionManager(db)

    ok, spot_trade, fut_trade = executor.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')
    assert ok
    assert spot_trade.executed_price == pytest.approx(3300.05)  # ask
    assert fut_trade.executed_price == pytest.approx(3319.95)   # bid
    position = pm.create_position('GOLD', SignalType.SELL_BASIS,
                                  spot_trade, fut_trade, 25.0)

    # Market moves, then a full close through the same executor
    spot_leg.price = 3310.0
    fut_leg.price = 3325.0
    assert pm.close_position(position.position_id, "TAKE_PROFIT",
                             executor, contract_size=100)
    assert position.status == PositionStatus.CLOSED
    # Spot: sell at bid 3309.95 -> (3309.95-3300.05)*5000 = 49,500
    # Fut: buy at ask 3325.05 -> (3319.95-3325.05)*5000 = -25,500
    assert position.realized_pnl == pytest.approx(24000.0, abs=0.01)


def test_crash_safe_state_and_restart_recovery(tmp_path):
    db = DataLogger(db_path=str(tmp_path / "recovery.db"))
    pm = PositionManager(db)

    spot = filled_trade('XAUUSD', OrderSide.BUY, 50.0, 3300.0,
                        tickets=[101, 102])
    fut = filled_trade('GC1225', OrderSide.SELL, 50.0, 3320.0,
                       tickets=[201])
    position = pm.create_position('GOLD', SignalType.SELL_BASIS,
                                  spot, fut, 25.0)
    position.exit_plan = {'tp_usd': 15000.0, 'stop_usd': 1500.0,
                          'gate_floor_usd': 0.0, 'max_hold_sec': 2400,
                          'entry_z': 3.0, 'entry_sigma': 2.0,
                          'rt_cost_usd': 3000.0}
    db.save_position_state(position)

    # --- simulate a crash: brand-new manager reading the same DB ---
    pm2 = PositionManager(db)
    states = db.load_open_position_states()
    assert len(states) == 1
    recovered = Position.from_dict(states[0])
    pm2.restore_position(recovered)

    assert recovered.position_id == position.position_id
    assert recovered.status == PositionStatus.ACTIVE
    assert recovered.spot_trade.position_tickets == [101, 102]
    assert recovered.futures_trade.position_tickets == [201]
    assert recovered.exit_plan['stop_usd'] == 1500.0
    assert recovered.spot_trade.side is OrderSide.BUY
    # Counter advanced: the next position id will not collide
    assert pm2.position_counter >= 1

    # A successful close clears the crash-state row
    class InstantCloser:
        def execute_close_pair(self, p, reason=None):
            cs = filled_trade(p.spot_trade.symbol, OrderSide.SELL,
                              p.spot_trade.lot_size, 3301.0)
            cf = filled_trade(p.futures_trade.symbol, OrderSide.BUY,
                              p.futures_trade.lot_size, 3319.0)
            return True, cs, cf

    assert pm2.close_position(recovered.position_id, "TAKE_PROFIT",
                              InstantCloser(), contract_size=100)
    assert db.load_open_position_states() == []
    # And realized P&L came from the fills: (1)*5000 + (1)*5000
    assert recovered.realized_pnl == pytest.approx(10000.0, abs=0.01)
