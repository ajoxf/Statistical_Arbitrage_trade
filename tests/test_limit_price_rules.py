"""A pending limit price MT5 will actually accept.

Two constraints, one opaque error code. A price off the tick grid and a
price too close to the market both come back as `10015 - Invalid price`,
so the rules have to be enforced before the order is sent.

Live 2026-08-07: every BUY_SPOT LIMIT scenario failed 10015 on USOIL_U6
while the same code passed on XAUUSD_ — the oil symbol carries a
trade_stops_level and gold does not.
"""

from types import SimpleNamespace

import pytest

from statarb import broker as broker_module
from statarb.broker import BrokerSession
from statarb.models import OrderSide


class PriceFakeMT5:
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3

    def __init__(self, bid, ask, tick_size, point, stops_level):
        self.info = SimpleNamespace(
            trade_tick_size=tick_size, point=point,
            trade_stops_level=stops_level, visible=True)
        self.tick = SimpleNamespace(bid=bid, ask=ask)

    def symbol_info(self, symbol):
        return self.info

    def symbol_info_tick(self, symbol):
        return self.tick


@pytest.fixture
def session(monkeypatch):
    def build(bid=76.90, ask=76.92, tick_size=0.01, point=0.01,
              stops_level=0):
        fake = PriceFakeMT5(bid, ask, tick_size, point, stops_level)
        monkeypatch.setattr(broker_module, 'mt5', fake)
        broker = BrokerSession.__new__(BrokerSession)
        broker.symbol_info = fake.symbol_info
        return broker
    return build


def test_a_price_off_the_tick_grid_is_snapped(session):
    broker = session(tick_size=0.01)
    price, note = broker.legal_limit_price('USOIL_U6', OrderSide.BUY,
                                           76.8037)
    assert price == pytest.approx(76.80)
    assert note is None          # snapping alone is not worth reporting


def test_a_resting_price_inside_the_book_is_left_alone(session):
    """The normal case: a passive peg well away from the touch."""
    broker = session(bid=76.90, ask=76.92, stops_level=0)
    price, note = broker.legal_limit_price('USOIL_U6', OrderSide.BUY, 76.85)
    assert price == pytest.approx(76.85) and note is None

    price, note = broker.legal_limit_price('USOIL_U6', OrderSide.SELL, 76.99)
    assert price == pytest.approx(76.99) and note is None


def test_a_buy_limit_is_pushed_below_the_brokers_minimum_distance(session):
    """USOIL_U6: 30 points at 0.01 = 0.30 clear of the ask."""
    broker = session(bid=76.90, ask=76.92, point=0.01, stops_level=30)
    price, note = broker.legal_limit_price('USOIL_U6', OrderSide.BUY, 76.91)
    assert price == pytest.approx(76.62)     # 76.92 - 0.30
    assert price <= 76.92 - 0.30 + 1e-9
    assert '76.62' in note and 'ask' in note


def test_a_sell_limit_is_pushed_above_the_brokers_minimum_distance(session):
    broker = session(bid=76.90, ask=76.92, point=0.01, stops_level=30)
    price, note = broker.legal_limit_price('USOIL_U6', OrderSide.SELL, 76.91)
    assert price == pytest.approx(77.20)     # 76.90 + 0.30
    assert price >= 76.90 + 0.30 - 1e-9
    assert 'bid' in note


def test_the_clamp_lands_on_the_tick_grid_on_the_legal_side(session):
    """Rounding must never push the price back over the line it was
    just moved behind — buy floors, sell ceils."""
    broker = session(bid=100.0, ask=100.07, tick_size=0.05, point=0.01,
                     stops_level=3)                 # gap 0.05 -> one tick
    price, _ = broker.legal_limit_price('X', OrderSide.BUY, 100.06)
    assert price <= 100.07 - 0.05 + 1e-9
    assert abs(round(price / 0.05) * 0.05 - price) < 1e-9

    price, _ = broker.legal_limit_price('X', OrderSide.SELL, 100.01)
    assert price >= 100.0 + 0.05 - 1e-9
    assert abs(round(price / 0.05) * 0.05 - price) < 1e-9


def test_gold_with_no_stops_level_still_rests_one_tick_inside(session):
    """The gold case that always worked must not regress: with no stops
    level the only requirement is to stay off the touch."""
    broker = session(bid=4337.44, ask=4337.69, tick_size=0.01, point=0.01,
                     stops_level=0)
    price, _ = broker.legal_limit_price('XAUUSD_', OrderSide.BUY, 4337.69)
    assert price == pytest.approx(4337.68)   # one tick below the ask
    price, _ = broker.legal_limit_price('XAUUSD_', OrderSide.SELL, 4337.44)
    assert price == pytest.approx(4337.45)   # one tick above the bid


def test_no_book_means_no_guess(session):
    """Without a tick we cannot measure distance — snap and send rather
    than invent a price."""
    broker = session(bid=0, ask=0, stops_level=30)
    price, note = broker.legal_limit_price('USOIL_U6', OrderSide.BUY, 76.91)
    assert price == pytest.approx(76.91) and note is None
