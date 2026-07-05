"""Tests for the money-touching paths: entries, closes, hedge unwind.

test_close_* are regression tests for the legacy bug where closing a
position routed through the entry path and always raised ValueError.
"""

import pytest

from statarb.execution import OrderManager
from statarb.models import OrderSide, PositionStatus, SignalType
from statarb.positions import PositionManager
from tests.conftest import FakeBroker


def open_position(config, data_logger, broker, signal_type):
    om = OrderManager(config, broker)
    pm = PositionManager(data_logger)
    ok, spot, fut = om.execute_trade_pair(
        'GOLD', signal_type, 1.0, 'XAUUSD', 'GC1225')
    assert ok
    position = pm.create_position('GOLD', signal_type, spot, fut, 25.0)
    return om, pm, position


def test_entry_sell_basis_sides(config, data_logger, fake_broker):
    om = OrderManager(config, fake_broker)
    ok, spot, fut = om.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 1.0, 'XAUUSD', 'GC1225')
    assert ok
    assert fake_broker.orders == [('XAUUSD', 'BUY', 1.0),
                                  ('GC1225', 'SELL', 1.0)]
    assert spot.status == "EXECUTED" and fut.status == "EXECUTED"


def test_entry_buy_basis_sides(config, data_logger, fake_broker):
    om = OrderManager(config, fake_broker)
    ok, spot, fut = om.execute_trade_pair(
        'GOLD', SignalType.BUY_BASIS, 1.0, 'XAUUSD', 'GC1225')
    assert ok
    assert fake_broker.orders == [('XAUUSD', 'SELL', 1.0),
                                  ('GC1225', 'BUY', 1.0)]


def test_entry_rejects_invalid_signal(config, fake_broker):
    om = OrderManager(config, fake_broker)
    with pytest.raises(ValueError):
        om.execute_trade_pair('GOLD', SignalType.NO_SIGNAL, 1.0,
                              'XAUUSD', 'GC1225')


def test_hedge_failure_reverses_spot_leg(config, fake_broker):
    fake_broker.fail_symbols.add('GC1225')
    om = OrderManager(config, fake_broker)
    ok, spot, fut = om.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 1.0, 'XAUUSD', 'GC1225')
    assert not ok
    # Spot bought, futures failed, spot reversed with a SELL
    assert fake_broker.orders == [('XAUUSD', 'BUY', 1.0),
                                  ('XAUUSD', 'SELL', 1.0)]


def test_close_sell_basis_position(config, data_logger, fake_broker):
    om, pm, position = open_position(config, data_logger, fake_broker,
                                     SignalType.SELL_BASIS)
    fake_broker.orders.clear()

    assert pm.close_position(position.position_id, "SIGNAL_EXIT", om)
    assert position.status == PositionStatus.CLOSED
    assert position.close_reason == "SIGNAL_EXIT"
    # Entry was buy spot / sell futures -> close is sell spot / buy futures
    assert fake_broker.orders == [('XAUUSD', 'SELL', 1.0),
                                  ('GC1225', 'BUY', 1.0)]


def test_close_buy_basis_position(config, data_logger, fake_broker):
    om, pm, position = open_position(config, data_logger, fake_broker,
                                     SignalType.BUY_BASIS)
    fake_broker.orders.clear()

    assert pm.close_position(position.position_id, "STOP_LOSS", om)
    assert position.status == PositionStatus.CLOSED
    assert fake_broker.orders == [('XAUUSD', 'BUY', 1.0),
                                  ('GC1225', 'SELL', 1.0)]


def test_close_failure_marks_error(config, data_logger, fake_broker):
    om, pm, position = open_position(config, data_logger, fake_broker,
                                     SignalType.SELL_BASIS)
    fake_broker.fail_symbols.add('XAUUSD')

    assert not pm.close_position(position.position_id, "SIGNAL_EXIT", om)
    assert position.status == PositionStatus.ERROR


def test_double_close_is_noop(config, data_logger, fake_broker):
    om, pm, position = open_position(config, data_logger, fake_broker,
                                     SignalType.SELL_BASIS)
    assert pm.close_position(position.position_id, "SIGNAL_EXIT", om)
    assert not pm.close_position(position.position_id, "SIGNAL_EXIT", om)


def test_pnl_uses_contract_size(config, data_logger, fake_broker):
    om, pm, position = open_position(config, data_logger, fake_broker,
                                     SignalType.SELL_BASIS)
    # Entry both legs at 100. Spot +1, futures unchanged, 100 oz/lot
    pm.update_position_pnl(position.position_id, 101.0, 100.0, 20.0,
                           contract_size=100)
    assert position.unrealized_pnl == pytest.approx(100.0)
