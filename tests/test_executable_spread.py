"""The two spreads you can actually trade.

Operator, 2026-08-24: "Short Spread - Sell Future and Buy Spot - The
spread should be calculated using (Bid - Future) and (Ask - Spot). Long
Spread - Buy Future and Sell Spot ... The relevant spread prices should
also be used in the Algo."

The headline spread is a midpoint of two midpoints and nobody fills
there. Each direction crosses a different pair of touches, and a
position reads a DIFFERENT one at each end of its life because it comes
out the opposite way it went in.
"""

from types import SimpleNamespace

import pytest

from statarb import marketdata
from statarb.marketdata import compute_market_data, executable_spread
from statarb.models import SignalType


def ticks(spot_bid=4292.55, spot_ask=4292.68,
          fut_bid=4351.38, fut_ask=4351.72):
    return (SimpleNamespace(bid=spot_bid, ask=spot_ask, last=0, time=1000),
            SimpleNamespace(bid=fut_bid, ask=fut_ask, last=0, time=1000))


def gold(config, **over):
    asset = dict(config.ASSETS['GOLD'])
    asset.update(over)
    return asset


def test_short_spread_sells_the_future_and_buys_the_spot(config):
    md = compute_market_data(gold(config), *ticks(), 1.0)
    assert md['short_spread'] == pytest.approx(4351.38 - 4292.68)


def test_long_spread_buys_the_future_and_sells_the_spot(config):
    md = compute_market_data(gold(config), *ticks(), 1.0)
    assert md['long_spread'] == pytest.approx(4351.72 - 4292.55)


def test_the_mid_sits_between_the_two_executable_spreads(config):
    md = compute_market_data(gold(config), *ticks(), 1.0)
    assert md['short_spread'] < md['spread'] < md['long_spread']


def test_the_gap_between_them_is_one_round_turn_of_bid_ask(config):
    """long - short = fut spread + beta x spot spread. The same
    quantity costs.round_trip_cost charges in dollars — two views of ONE
    cost, which is why the executable spreads do not also get charged."""
    beta = 1.0
    md = compute_market_data(gold(config), *ticks(), beta)
    assert md['spread_cost'] == pytest.approx(
        (4351.72 - 4351.38) + beta * (4292.68 - 4292.55))


def test_the_hedge_ratio_scales_leg_a_in_both(config):
    md = compute_market_data(gold(config), *ticks(), 2.0)
    assert md['short_spread'] == pytest.approx(4351.38 - 2.0 * 4292.68)
    assert md['long_spread'] == pytest.approx(4351.72 - 2.0 * 4292.55)


def test_the_multiplier_applies_to_both_futures_touches(config):
    md = compute_market_data(gold(config, multiplier=2.0), *ticks(), 1.0)
    assert md['short_spread'] == pytest.approx(2 * 4351.38 - 4292.68)
    assert md['long_spread'] == pytest.approx(2 * 4351.72 - 4292.55)


# --- which one a given action reads -------------------------------------

def test_entering_short_reads_the_short_spread(config):
    md = compute_market_data(gold(config), *ticks(), 1.0)
    assert executable_spread(md, SignalType.SELL_BASIS) == md['short_spread']


def test_entering_long_reads_the_long_spread(config):
    md = compute_market_data(gold(config), *ticks(), 1.0)
    assert executable_spread(md, SignalType.BUY_BASIS) == md['long_spread']


def test_a_position_exits_on_the_other_side(config):
    """A short spread is BOUGHT back. Reading the favourable touch at
    both ends would make every trade look like it cleared its costs."""
    md = compute_market_data(gold(config), *ticks(), 1.0)
    assert executable_spread(md, SignalType.SELL_BASIS,
                             closing=True) == md['long_spread']
    assert executable_spread(md, SignalType.BUY_BASIS,
                             closing=True) == md['short_spread']


def test_a_round_trip_pays_the_spread_cost_exactly_once(config):
    """In and out at the executable prices, with nothing moving: the
    loss is one round turn, no more and no less."""
    md = compute_market_data(gold(config), *ticks(), 1.0)
    entry = executable_spread(md, SignalType.SELL_BASIS)
    exit_ = executable_spread(md, SignalType.SELL_BASIS, closing=True)
    assert entry - exit_ == pytest.approx(-md['spread_cost'])


def test_a_plain_string_direction_works_too(config):
    """The armed manual order carries the direction as a string."""
    md = compute_market_data(gold(config), *ticks(), 1.0)
    assert executable_spread(md, 'SELL_BASIS') == md['short_spread']


def test_a_snapshot_without_touches_falls_back_to_the_mid():
    """Replayed rows and older callers have no touches. A missing touch
    is not a reason to refuse to price a level."""
    assert executable_spread({'spread': 58.94}, 'SELL_BASIS') == 58.94
    assert executable_spread(None, 'SELL_BASIS') is None


# --- the algo actually uses them ----------------------------------------

@pytest.fixture
def coord(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator, PaperExecutor

    class Leg:
        name = 'broker'

        def ensure_symbol(self, sym):
            return {'ok': True, 'volume_step': 0.01, 'volume_min': 0.01,
                    'volume_max': 100.0, 'point': 0.01}

        def tick(self, s):
            return None

        def account_info(self):
            return {}

        def order_log(self, hours=24):
            return []

        def ping(self):
            return True

    c = Coordinator(config, trading_mode='PAPER')
    c.spot_leg = c.futures_leg = Leg()
    c.executor = PaperExecutor(c.spot_leg, c.futures_leg, config)
    c.active_assets['GOLD'] = {'config': config.ASSETS['GOLD'],
                               'spot_symbol': 'XAUUSD_',
                               'futures_symbol': 'GC1226',
                               'last_data': None}
    return c


def test_an_armed_short_fires_on_the_short_spread_not_the_mid(coord, config):
    """Arming at 59.00 and firing when the MID touches it fills lower by
    both legs' bid-ask — a level the market never offered."""
    fired = []
    coord._manual_open = lambda *a, **k: fired.append(a)
    coord.manual_order = {'asset': 'GOLD', 'direction': 'SELL_BASIS',
                          'entry_spread': 58.90}

    # Mid is above 58.90, but the SHORT spread is not — no fill yet.
    md = compute_market_data(gold(config), *ticks(), 1.0)
    assert md['spread'] > 58.90 > md['short_spread']
    coord._check_manual_arm('GOLD', md)
    assert not fired

    # Push the whole book up until the short spread clears it.
    md = compute_market_data(gold(config),
                             *ticks(fut_bid=4351.70, fut_ask=4352.04), 1.0)
    assert md['short_spread'] >= 58.90
    coord._check_manual_arm('GOLD', md)
    assert fired


def test_an_armed_long_fires_on_the_long_spread(coord, config):
    fired = []
    coord._manual_open = lambda *a, **k: fired.append(a)
    coord.manual_order = {'asset': 'GOLD', 'direction': 'BUY_BASIS',
                          'entry_spread': 59.20}
    md = compute_market_data(gold(config), *ticks(), 1.0)
    # A BUY_BASIS arms BELOW its level, and the long spread is the
    # HIGHER of the two — so a level the mid has already reached has
    # not necessarily been reached at a price you can buy at.
    assert md['long_spread'] > md['spread']
    coord.manual_order['entry_spread'] = md['spread']
    coord._check_manual_arm('GOLD', md)
    assert not fired

    coord.manual_order['entry_spread'] = md['long_spread']
    coord._check_manual_arm('GOLD', md)
    assert fired


def test_a_short_positions_manual_stop_reads_the_buy_back_price(config):
    """The stop is where the position gets CLOSED, and a short spread is
    closed by buying it back at the long spread."""
    from statarb.exits import ExitLadder
    from statarb.models import OrderSide, Position, Trade

    md = compute_market_data(gold(config), *ticks(), 1.0)
    plan = {'stop_usd': 0, 'tp_usd': 0, 'gate_floor_usd': 0.0,
            'max_hold_sec': 9e9, 'rt_cost_usd': 0.0}
    position = Position('P1', 'GOLD', SignalType.SELL_BASIS,
                        Trade('XAUUSD_', OrderSide.BUY, 1.0),
                        Trade('GC1226', OrderSide.SELL, 1.0))
    ladder = ExitLadder(config)

    mid = md['spread']
    closing = marketdata.executable_spread(md, SignalType.SELL_BASIS,
                                           closing=True)
    assert closing > mid            # buying back costs more than the mid

    # A stop between the two: the mid has not reached it, the price the
    # position would actually close at has.
    plan['manual_stop_spread'] = (mid + closing) / 2
    # z well outside the exit band, so the reversion gate is not what
    # decides this — the stop level is.
    assert ladder.evaluate(position, plan, z=3.0, gross_pnl=0.0,
                           age_sec=1, spread=mid) is None
    assert ladder.evaluate(position, plan, z=3.0, gross_pnl=0.0,
                           age_sec=1, spread=closing) == 'MANUAL_STOP'
