"""Exit ladder: frozen dollar levels, priority order, gating rules."""

import pytest

from statarb.exits import ExitLadder
from statarb.models import Position, SignalType, Trade, OrderSide


def market_data(spread_dollars=0.30):
    half = spread_dollars / 2
    return {'spot_price': 3300.0, 'futures_price': 3320.0,
            'spot_bid': 3300 - half, 'spot_ask': 3300 + half,
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
    # z=2.6 -> progress 0.13, no suppression
    assert ladder.evaluate(position, plan, z=2.6, net_pnl=-100,
                           age_sec=age) is None
    assert ladder.evaluate(position, plan, z=2.6, net_pnl=100,
                           age_sec=age) == 'MAX_HOLD'


def test_max_hold_suppressed_while_travelling_toward_tp(exit_config):
    ladder, plan = build(exit_config, entry_z=3.0)
    position = make_position()
    age = plan['max_hold_sec'] + 1
    # z=1.2 -> progress 60% toward home AND a TP exists -> keep riding
    assert ladder.evaluate(position, plan, z=1.2, net_pnl=100,
                           age_sec=age) is None
    # No TP -> never wait for a target that is configured off
    plan_no_tp = dict(plan, tp_usd=None)
    assert ladder.evaluate(position, plan_no_tp, z=1.2, net_pnl=100,
                           age_sec=age) == 'MAX_HOLD'


def test_gate_floor_decays_with_age_deadlock_fix(exit_config):
    # Regression for the +$1.19-held-over-2c -> -$4.46 deadlock
    exit_config.EXITS['GATE_FLOOR_USD'] = 50.0
    ladder, plan = build(exit_config)
    position = make_position()
    max_hold = plan['max_hold_sec']

    # Young trade, z home, net below floor -> gate HOLDS
    assert ladder.evaluate(position, plan, z=0.2, net_pnl=20.0,
                           age_sec=60) is None
    # Past 1x max-hold the floor decays to break-even: +$20 releases...
    assert ladder.evaluate(position, plan, z=0.2, net_pnl=20.0,
                           age_sec=max_hold + 1) == 'REVERSION_EXIT'
    # ...but a loser still holds between 1x and 2x
    assert ladder.evaluate(position, plan, z=0.2, net_pnl=-20.0,
                           age_sec=max_hold + 1) is None
    # Past 2x the gate releases entirely — take what's there, even a loss
    assert ladder.evaluate(position, plan, z=0.2, net_pnl=-20.0,
                           age_sec=2 * max_hold + 1) == 'REVERSION_EXIT'


def test_hard_time_stop_is_the_sideways_losers_clock(exit_config):
    ladder, plan = build(exit_config)
    position = make_position()
    max_hold = plan['max_hold_sec']
    # Sideways loser: net < 0, z never reverts (z=2.6: no suppression,
    # no reversion, no z-stop). Before 3x: no exit fires...
    assert ladder.evaluate(position, plan, z=2.6, net_pnl=-100,
                           age_sec=2.5 * max_hold) is None
    # ...at 3x the hard clock closes it regardless of P&L
    assert ladder.evaluate(position, plan, z=2.6, net_pnl=-100,
                           age_sec=3 * max_hold + 1) == 'TIME_STOP'
    # Disabled -> corner accepted, still no exit
    exit_config.EXITS['HARD_TIME_STOP_MULT'] = 0
    assert ladder.evaluate(position, plan, z=2.6, net_pnl=-100,
                           age_sec=10 * max_hold) is None


def test_z_stop_suppression_matrix(exit_config, caplog):
    import logging as logging_mod
    sell = make_position(SignalType.SELL_BASIS)

    # DEFAULT (disabled) + dollar stop armed -> z-stop suppressed,
    # would-have-fired is LOGGED for design scoring
    ladder, plan = build(exit_config)
    with caplog.at_level(logging_mod.WARNING):
        assert ladder.evaluate(sell, plan, z=4.5, net_pnl=-100,
                               age_sec=10) is None
    assert any('WOULD HAVE FIRED' in r.message for r in caplog.records)
    # Dollar stop itself is NEVER suppressed
    assert ladder.evaluate(sell, plan, z=4.5, net_pnl=-2000,
                           age_sec=10) == 'DOLLAR_STOP'

    # Explicitly enabled -> fires, directionally
    exit_config.EXITS['Z_STOP_EXIT_ENABLED'] = True
    ladder2, plan2 = build(exit_config)
    assert ladder2.evaluate(sell, plan2, z=4.5, net_pnl=-100,
                            age_sec=10) == 'Z_STOP'
    assert ladder2.evaluate(sell, plan2, z=-4.5, net_pnl=-100,
                            age_sec=10) is None
    buy = make_position(SignalType.BUY_BASIS)
    assert ladder2.evaluate(buy, plan2, z=-4.5, net_pnl=-100,
                            age_sec=10) == 'Z_STOP'

    # FAIL-SAFE: disabled but NO dollar stop armed -> a trade must
    # always have a stop, so the z-stop re-enables itself
    exit_config.EXITS['Z_STOP_EXIT_ENABLED'] = False
    ladder3, plan3 = build(exit_config)
    plan_unarmed = dict(plan3, stop_usd=0.0)
    assert ladder3.evaluate(sell, plan_unarmed, z=4.5, net_pnl=-100,
                            age_sec=10) == 'Z_STOP'


def test_capital_pct_forms_bind_when_leverage_set(exit_config):
    # LEVERAGE on: capital = (3300+3320)*5000/100 = $331,000
    exit_config.EXITS.update({'LEVERAGE': 100.0, 'USE_SIGMA_TARGET': False,
                              'TP_CAPITAL_PCT': 0.5,
                              'STOP_CAPITAL_PCT': 0.3,
                              'COST_FLOOR_MULT': 0.0, 'RR': 0.0})
    ladder, plan = build(exit_config)
    assert plan['capital_at_risk'] == pytest.approx(331000, rel=0.001)
    assert plan['tp_usd'] == pytest.approx(1655, rel=0.001)     # 0.5%
    # Stop = min(per-lot 1500, 0.3% cap 993) -> the %-cap binds
    assert plan['stop_usd'] == pytest.approx(993, rel=0.001)


def test_outcome_tags_are_deterministic():
    from statarb.exits import outcome_tag
    assert outcome_tag('TAKE_PROFIT', False) == 'TARGET_HIT'
    assert outcome_tag('REVERSION_EXIT', True) == 'REVERSION_BANKED'
    assert outcome_tag('MAX_HOLD', True) == 'TIME_EXIT'
    assert outcome_tag('TIME_STOP', False) == 'TIME_EXIT'
    # The stop split that matters: did z come home but price never pay?
    assert outcome_tag('DOLLAR_STOP', False) == 'STOPPED_IN_TREND'
    assert outcome_tag('DOLLAR_STOP', True) == 'STOPPED_AFTER_FULL_REVERSION'
    assert outcome_tag('Z_STOP', True) == 'STOPPED_AFTER_FULL_REVERSION'
