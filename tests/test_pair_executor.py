"""Cross-account 50-lot clip execution: slicing, partial fills,
hedge matching, unwind. These are the money paths for the two-broker
setup — keep them green before any LIVE run."""

import pytest

from statarb.models import PositionStatus, SignalType
from statarb.pair_executor import PairExecutor
from statarb.positions import PositionManager


class FakeLeg:
    """Leg with finite liquidity per symbol."""

    def __init__(self, name, liquidity=None, fail_symbols=None, price=100.0,
                 volume_min=0.01):
        self.name = name
        self.liquidity = dict(liquidity or {})   # symbol -> lots available
        self.fail_symbols = set(fail_symbols or [])
        self.price = price
        self.volume_min = volume_min
        self.orders = []                         # (symbol, side, req, filled)

    def ensure_symbol(self, symbol):
        return {'ok': True, 'volume_min': self.volume_min,
                'volume_max': 100.0, 'volume_step': 0.01,
                'tick_size': 0.01}

    def pending_orders(self, symbol=None):
        return []

    def order(self, symbol, side, volume, slippage_points=1.0, comment=""):
        if symbol in self.fail_symbols:
            self.orders.append((symbol, side, volume, 0.0))
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'ticket': None, 'error': 'forced failure'}
        available = self.liquidity.get(symbol, float('inf'))
        filled = min(volume, available)
        self.liquidity[symbol] = available - filled
        self.orders.append((symbol, side, volume, filled))
        if filled <= 0:
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'ticket': None, 'error': 'no liquidity'}
        return {'ok': True, 'filled_volume': filled, 'price': self.price,
                'ticket': len(self.orders), 'error': None}


@pytest.fixture
def clip_config(config):
    config.TRADING.update({'CLIP_LOTS': 50.0, 'SLICE_LOTS': 10.0,
                           'DAILY_LOT_TARGET': 500.0, 'HEDGE_RATIO': 1.0})
    config.RISK_LIMITS['MAX_LOT_SIZE'] = 50.0
    return config


def test_full_clip_sliced_and_hedged(clip_config):
    spot = FakeLeg('account_a')
    fut = FakeLeg('account_b')
    px = PairExecutor(clip_config, spot, fut)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert ok
    # 50 lots as 5 x 10-lot child orders on each leg
    assert [o[2] for o in spot.orders] == [10.0] * 5
    assert all(o[1] == 'BUY' for o in spot.orders)
    assert [o[2] for o in fut.orders] == [10.0] * 5
    assert all(o[1] == 'SELL' for o in fut.orders)
    assert spot_trade.lot_size == pytest.approx(50.0)
    assert fut_trade.lot_size == pytest.approx(50.0)
    assert spot_trade.status == fut_trade.status == "EXECUTED"


def test_partial_spot_fill_hedges_only_filled(clip_config):
    spot = FakeLeg('account_a', liquidity={'XAUUSD': 32.0})
    fut = FakeLeg('account_b')
    px = PairExecutor(clip_config, spot, fut)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert ok
    assert spot_trade.lot_size == pytest.approx(32.0)
    # Hedge sized to the actual spot fill, not the requested clip
    assert fut_trade.lot_size == pytest.approx(32.0)
    assert sum(o[3] for o in fut.orders) == pytest.approx(32.0)


def test_futures_total_failure_unwinds_spot(clip_config):
    spot = FakeLeg('account_a')
    fut = FakeLeg('account_b', fail_symbols={'GC1225'})
    px = PairExecutor(clip_config, spot, fut)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert not ok
    buys = sum(o[3] for o in spot.orders if o[1] == 'BUY')
    sells = sum(o[3] for o in spot.orders if o[1] == 'SELL')
    assert buys == pytest.approx(50.0)
    assert sells == pytest.approx(50.0)  # fully unwound — flat


def test_partial_hedge_keeps_matched_unwinds_excess(clip_config):
    spot = FakeLeg('account_a')
    fut = FakeLeg('account_b', liquidity={'GC1225': 20.0})
    px = PairExecutor(clip_config, spot, fut)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert ok
    assert fut_trade.lot_size == pytest.approx(20.0)
    assert spot_trade.lot_size == pytest.approx(20.0)   # matched size
    sells = sum(o[3] for o in spot.orders if o[1] == 'SELL')
    assert sells == pytest.approx(30.0)                  # excess unwound


def test_no_spot_fill_aborts_without_hedge(clip_config):
    spot = FakeLeg('account_a', fail_symbols={'XAUUSD'})
    fut = FakeLeg('account_b')
    px = PairExecutor(clip_config, spot, fut)

    ok, _, _ = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert not ok
    assert fut.orders == []   # hedge never attempted


def test_close_pair_reverses_recorded_lots(clip_config, data_logger):
    spot = FakeLeg('account_a')
    fut = FakeLeg('account_b')
    px = PairExecutor(clip_config, spot, fut)
    pm = PositionManager(data_logger)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')
    assert ok
    position = pm.create_position('GOLD', SignalType.SELL_BASIS,
                                  spot_trade, fut_trade, 25.0)
    spot.orders.clear()
    fut.orders.clear()

    assert pm.close_position(position.position_id, "SIGNAL_EXIT", px)
    assert position.status == PositionStatus.CLOSED
    # Entry: buy spot / sell futures -> close: sell spot / buy futures
    assert sum(o[3] for o in spot.orders if o[1] == 'SELL') == pytest.approx(50.0)
    assert sum(o[3] for o in fut.orders if o[1] == 'BUY') == pytest.approx(50.0)


def test_an_incomplete_close_stays_under_management(clip_config, data_logger):
    spot = FakeLeg('account_a')
    fut = FakeLeg('account_b')
    px = PairExecutor(clip_config, spot, fut)
    pm = PositionManager(data_logger)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')
    assert ok
    position = pm.create_position('GOLD', SignalType.SELL_BASIS,
                                  spot_trade, fut_trade, 25.0)
    fut.liquidity['GC1225'] = 10.0   # can only close 10 of 50

    assert not pm.close_position(position.position_id, "SIGNAL_EXIT", px)
    # Still ACTIVE: a partially closed pair is REAL residual exposure
    # and has to keep being retried and displayed, not filed away.
    assert position.status == PositionStatus.ACTIVE
    assert position.close_failures == 1


def test_atomic_precheck_refuses_before_any_order(clip_config):
    """A leg that fails minimums after the other filled is an instant
    naked position — BOTH legs are validated before EITHER order."""
    spot = FakeLeg('account_a')
    fut = FakeLeg('account_b', volume_min=20.0)   # child = 10 < min 20
    px = PairExecutor(clip_config, spot, fut)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert not ok
    assert spot.orders == []                      # spot NEVER placed
    assert fut.orders == []
    assert 'minimum' in (spot_trade.error_message or '')


def test_no_slicing_when_disabled(clip_config):
    clip_config.TRADING['SLICE_LOTS'] = 0.0
    spot = FakeLeg('account_a')
    fut = FakeLeg('account_b')
    px = PairExecutor(clip_config, spot, fut)

    ok, _, _ = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')
    assert ok
    assert [o[2] for o in spot.orders] == [50.0]   # single parent order


# --- unwinding on a HEDGING account -------------------------------------
# Live 2026-08-24. A manual gold pair, futures refused with 10027 (algo
# trading off in that terminal), and the spot leg "unwound":
#
#   [Account_Spot] unwound 0.05 lots of XAUUSD
#   Reconcile: orphan ticket 862 BUY  0.05 XAUUSD (strike 1/3)
#   Reconcile: orphan ticket 863 SELL 0.05 XAUUSD (strike 1/3)
#
# 862 was the entry, 863 was the unwind. A plain opposite order on a
# hedging account does not close a position, it OPENS an offsetting one.
# CLAUDE.md names this exact failure; the exit path already closed by
# ticket and the unwind path did not.

class HedgingLeg(FakeLeg):
    """Records positions the way a hedging account does: one per order,
    and an opposite order OPENS a second one instead of closing."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.book = {}            # ticket -> {'side', 'volume'}
        self.closed = []          # tickets closed by ticket
        self._next = 900

    def order(self, symbol, side, volume, slippage_points=1.0, comment=""):
        result = super().order(symbol, side, volume,
                               slippage_points=slippage_points,
                               comment=comment)
        if result.get('ok'):
            self._next += 1
            self.book[self._next] = {'side': side, 'symbol': symbol,
                                     'volume': result['filled_volume']}
            result['position_tickets'] = [self._next]
        return result

    def positions(self, symbol=None):
        return [{'ticket': t, 'symbol': p['symbol'], 'side': p['side'],
                 'volume': p['volume'], 'price_open': self.price}
                for t, p in self.book.items()
                if symbol is None or p['symbol'] == symbol]

    def close_ticket(self, symbol, ticket, volume, entry_side,
                     slippage_points=1.0, comment=""):
        held = self.book.get(ticket)
        if not held or held['volume'] < volume - 1e-9:
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'error': 'no such position'}
        held['volume'] -= volume
        if held['volume'] <= 1e-9:
            del self.book[ticket]
        self.closed.append((ticket, volume))
        return {'ok': True, 'filled_volume': volume, 'price': self.price,
                'error': None}


def test_an_unwind_closes_by_ticket_and_leaves_no_second_position(clip_config):
    spot = HedgingLeg('Account_Spot')
    fut = HedgingLeg('Account_Future', fail_symbols={'GC1225'})
    px = PairExecutor(clip_config, spot, fut)

    ok, _, _ = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert not ok
    # The book is EMPTY — not "flat by offsetting", actually empty.
    assert spot.positions() == []
    assert sum(v for _, v in spot.closed) == pytest.approx(50.0)


def test_the_unwind_does_not_send_an_opposite_order(clip_config):
    """The whole bug in one assertion: no SELL was ever sent on the spot
    leg, because closing a BUY by ticket is not a sell order."""
    spot = HedgingLeg('Account_Spot')
    fut = HedgingLeg('Account_Future', fail_symbols={'GC1225'})
    PairExecutor(clip_config, spot, fut).execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert all(o[1] == 'BUY' for o in spot.orders), spot.orders


def test_a_partial_hedge_unwinds_only_the_excess_by_ticket(clip_config):
    spot = HedgingLeg('Account_Spot')
    fut = HedgingLeg('Account_Future', liquidity={'GC1225': 20.0})
    px = PairExecutor(clip_config, spot, fut)

    ok, spot_trade, fut_trade = px.execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    assert ok
    assert sum(v for _, v in spot.closed) == pytest.approx(30.0)
    # 20 matched lots still held, as real positions
    assert sum(p['volume'] for p in spot.positions()) == pytest.approx(20.0)


def test_an_unclosable_ticket_still_falls_back_to_flattening(clip_config):
    """Offsetting is worse than closing and far better than staying
    naked. A netting account lands here too, where it is simply right."""
    spot = HedgingLeg('Account_Spot')
    fut = HedgingLeg('Account_Future', fail_symbols={'GC1225'})
    spot.close_ticket = lambda *a, **k: {'ok': False, 'filled_volume': 0.0,
                                         'price': None, 'error': 'refused'}
    px = PairExecutor(clip_config, spot, fut)
    px.execute_trade_pair('GOLD', SignalType.SELL_BASIS, 50.0,
                          'XAUUSD', 'GC1225')

    sells = sum(o[3] for o in spot.orders if o[1] == 'SELL')
    assert sells == pytest.approx(50.0)


def test_a_leg_that_cannot_report_positions_still_unwinds(clip_config):
    def boom(symbol=None):
        raise RuntimeError('IPC down')

    spot = HedgingLeg('Account_Spot')
    spot.positions = boom
    fut = HedgingLeg('Account_Future', fail_symbols={'GC1225'})
    PairExecutor(clip_config, spot, fut).execute_trade_pair(
        'GOLD', SignalType.SELL_BASIS, 50.0, 'XAUUSD', 'GC1225')

    sells = sum(o[3] for o in spot.orders if o[1] == 'SELL')
    assert sells == pytest.approx(50.0)
