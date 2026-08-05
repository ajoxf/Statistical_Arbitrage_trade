"""Telegram notifier: disabled no-op, formatting, worker delivery,
command handling. No network — transport is injected."""

import time

import pytest

from statarb.models import OrderSide, Position, SignalType, Trade
from statarb.notify import TelegramNotifier


class FakeTransport:
    def __init__(self):
        self.calls = []          # (method, payload)

    def __call__(self, url, payload, timeout=10):
        method = url.rsplit('/', 1)[-1]
        self.calls.append((method, payload))
        return {'ok': True, 'result': []}

    def sent_texts(self):
        return [p['text'] for m, p in self.calls if m == 'sendMessage']


def closed_position(pnl=1234.56, reason="TAKE_PROFIT"):
    spot = Trade('XAUUSD', OrderSide.BUY, 50.0)
    spot.executed_price = 3300.0
    fut = Trade('GC1225', OrderSide.SELL, 50.0)
    fut.executed_price = 3320.0
    position = Position('POS_0007', 'GOLD', SignalType.SELL_BASIS, spot, fut)
    position.realized_pnl = pnl
    position.close_reason = reason
    position.exit_plan = {'entry_z': 3.1, 'entry_sigma': 2.0,
                          'tp_usd': 15000.0, 'stop_usd': 1500.0,
                          'max_hold_sec': 2400, 'gate_floor_usd': 0.0,
                          'rt_cost_usd': 3000.0, 'entry_spread': 20.0,
                          'half_life_sec': 600,
                          'levels': {'entry_spread': 20.0, 'be': 19.4,
                                     'ex': 19.4, 'tp': 16.4, 'sl': 20.3,
                                     'favorable': 'down'}}
    from datetime import timedelta
    position.close_time = position.entry_time + timedelta(hours=2)
    return position


def wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def make_notifier(config, transport, commands=False):
    config.TELEGRAM['COMMANDS'] = commands
    return TelegramNotifier(config, token='TEST:TOKEN', chat_id='42',
                            transport=transport)


def test_disabled_without_token_is_noop(config):
    notifier = TelegramNotifier(config, token='', chat_id='',
                                transport=FakeTransport())
    assert not notifier.enabled
    # Every notify_* must be safe to call while disabled
    notifier.notify_trade_closed(closed_position())
    notifier.notify_breaker("test")
    notifier.notify_error("test")


def test_trade_closed_message_delivered_and_formatted(config):
    transport = FakeTransport()
    notifier = make_notifier(config, transport)
    notifier.notify_trade_closed(closed_position(), exit_z=0.4)

    assert wait_for(lambda: transport.sent_texts())
    text = transport.sent_texts()[0]
    assert 'TRADE EXIT' in text
    assert '$+1,234.56' in text                  # gross P&L row
    assert 'TAKE_PROFIT' in text
    assert '+3.10 → +0.40' in text               # z path
    assert 'TARGET HIT' not in text              # no outcome passed here
    notifier.stop()


def test_trade_opened_message_includes_exit_plan(config):
    transport = FakeTransport()
    notifier = make_notifier(config, transport)
    position = closed_position()
    market_data = {'actual_basis': 20.0}
    notifier.notify_trade_opened(position, market_data, z=3.1)

    assert wait_for(lambda: transport.sent_texts())
    text = transport.sent_texts()[0]
    assert 'TRADE ENTRY' in text
    assert 'EXIT GEOMETRY' in text
    assert '+$15,000' in text and '-$1,500' in text  # TP and stop dollars
    assert 'Breakeven' in text                       # fees -> BE move
    notifier.stop()


def test_notify_gates_respect_config(config):
    config.TELEGRAM['NOTIFY_TRADES'] = False
    transport = FakeTransport()
    notifier = make_notifier(config, transport)
    notifier.notify_trade_closed(closed_position())
    notifier.notify_error("boom")                   # errors still enabled

    assert wait_for(lambda: transport.sent_texts())
    texts = transport.sent_texts()
    assert all('TRADE EXIT' not in t for t in texts)
    assert any('boom' in t for t in texts)
    notifier.stop()


def test_command_handler_wired_through(config):
    transport = FakeTransport()
    notifier = make_notifier(config, transport)
    notifier.command_handler = lambda cmd: f"echo {cmd}"
    notifier._handle_command('/status')

    assert wait_for(lambda: transport.sent_texts())
    assert transport.sent_texts()[0] == 'echo /status'
    notifier.stop()
