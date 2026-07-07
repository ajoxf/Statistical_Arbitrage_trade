"""Exit ladder: frozen dollar levels, priority order, gating rules."""

import pytest

from statarb.exits import ExitLadder
from statarb.models import Position, SignalType, Trade, OrderSide


def market_data(spread_dollars=0.30):
    half = spread_dollars / 2
    return {'spot_bid': 3300 - half, 'spot_ask': 3300 + half,
            'futures_bid': 3320 - half, 'futures_ask': 3320 + half}


def make_position(signal_type=SignalType.SELL_BASIS):
    spot = Trade('XAUUSD', OrderSide.BUY, 50.0)
    fut = Trade('GC1225', OrderSide.SELL, 50.0)
    return Position('POS_0001', 'GOLD', signal_type, spot, fut)


@pytest.fixture
def exit_config(config):
    config.SIGNALS.update({'EXIT_Z': 0.5, 'STOP_Z': 4.0})
    config.COSTS.update({'TARGET_FRACTION': 0.5, 'SPREAD_COST_FACTOR': 1.0})
    config.EXITS.update({
        'USE_SIGMA_TARGET': True, 'COST_FLOOR_MULT': 1.2,
        'STOP_USD_PER_LOT': 30.0, 'RR': 0.3, 'GATE_FLOOR_USD': 0.0,
        'MAX_HOLD_HALF_LIVES': 4.0, 'MAX_HOLD_FALLBACK_MIN': 240,
    })
    return config


def build(exit_config, entry_z=3.0, sigma=2.0, half_life=600):
    ladder = ExitLadder(exit_config)
    plan = ladder.build_plan(50.0, 100, entry_z, sigma, half_life,
                             market_data())
    return ladder, plan


def test_plan_sigma_target_and_stop(exit_config):
    _, plan = build(exit_config)
    # TP = 0.5 * 3 * 2 * 5000 = $15,000 (above the cost floor)
    assert plan['tp_usd'] == pytest.approx(15000)
    # Stop candidates: per-lot 30*50 = 1500; TP/RR = 50k -> tighter wins
    assert plan['stop_usd'] == pytest.approx(1500)
    assert plan['max_hold_sec'] == pytest.approx(2400)


def test_stop_rr_side_can_bind(exit_config):
    exit_config.EXITS['STOP_USD_PER_LOT'] = 1000.0   # cap now loose
    _, plan = build(exit_config)
    # TP/RR = 15000/0.3 = 50k vs per-lot 50k -> equal; tighten RR
    exit_config.EXITS['RR'] = 0.9
    _, plan = build(exit_config)
    assert plan['stop_usd'] == pytest.approx(15000 / 0.9)


def test_cost_floor_raises_tiny_target(exit_config):
    # sigma small -> raw TP $375 < floor 1.2 x cost ($3,600)
    _, plan = build(exit_config, entry_z=3.0, sigma=0.05)
    assert plan is None or plan['tp_usd'] >= 3600
    # With this sigma, plausible reversion = 3*0.05*5000 = $750 < floor
    # -> the entry must be BLOCKED (plan None)
    assert plan is None


def test_dollar_stop_fires_first_and_ungated(exit_config):
    ladder, plan = build(exit_config)
    position = make_position()
    # z back inside the exit band AND huge loss: stop wins, not reversion
    reason = ladder.evaluate(position, plan, z=0.1, net_pnl=-2000,
                             age_sec=10)
    assert reason == 'DOLLAR_STOP'


def test_take_profit_on_money_alone(exit_config):
    ladder, plan = build(exit_config)
    position = make_position()
    # z still far from home — TP fires on P&L alone
    reason = ladder.evaluate(position, plan, z=2.5, net_pnl=15500,
                             age_sec=10)
    assert reason == 'TAKE_PROFIT'


def test_reversion_exit_gated_never_books_a_loss(exit_config):
    ladder, plan = build(exit_config)
    position = make_position()
    # z home but net below the gate floor -> HOLD
    assert ladder.evaluate(position, plan, z=0.2, net_pnl=-50,
                           age_sec=10) is None
    # net at/above floor -> exit
    assert ladder.evaluate(position, plan, z=0.2, net_pnl=25,
                           age_sec=10) == 'REVERSION_EXIT'


def test_reversion_fails_open_without_pnl(exit_config):
    ladder, plan = build(exit_config)
    position = make_position()
    assert ladder.evaluate(position, plan, z=0.2, net_pnl=None,
                           age_sec=10) == 'REVERSION_EXIT'


def test_max_hold_only_walks_away_with_profit(exit_config):
    ladder, plan = build(exit_config)
    position = make_position()
    age = plan['max_hold_sec'] + 1
    assert ladder.evaluate(position, plan, z=1.5, net_pnl=-100,
                           age_sec=age) is None
    assert ladder.evaluate(position, plan, z=1.5, net_pnl=100,
                           age_sec=age) == 'MAX_HOLD'


def test_z_stop_backstop_is_directional(exit_config):
    ladder, plan = build(exit_config)
    sell = make_position(SignalType.SELL_BASIS)
    # SELL_BASIS suffers when z stretches FURTHER positive
    assert ladder.evaluate(sell, plan, z=4.5, net_pnl=-100,
                           age_sec=10) == 'Z_STOP'
    assert ladder.evaluate(sell, plan, z=-4.5, net_pnl=-100,
                           age_sec=10) is None
    buy = make_position(SignalType.BUY_BASIS)
    assert ladder.evaluate(buy, plan, z=-4.5, net_pnl=-100,
                           age_sec=10) == 'Z_STOP'
