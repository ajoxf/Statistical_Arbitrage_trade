from datetime import datetime, timedelta

from statarb.models import Position, SignalType
from statarb.positions import PositionManager
from statarb.risk import RiskManager


def make_position(signal_type, entry_premium):
    pos = Position("POS_0001", 'GOLD', signal_type, None, None)
    pos.entry_premium = entry_premium
    return pos


def test_lot_size_limit(config, data_logger):
    rm = RiskManager(config)
    pm = PositionManager(data_logger)
    ok, reason = rm.validate_new_position('GOLD', SignalType.SELL_BASIS,
                                          5.0, pm)
    assert not ok and "Lot size" in reason


def test_min_time_between_signals(config, data_logger):
    rm = RiskManager(config)
    pm = PositionManager(data_logger)
    rm.record_trade('GOLD')
    ok, reason = rm.validate_new_position('GOLD', SignalType.SELL_BASIS,
                                          1.0, pm)
    assert not ok and "Too soon" in reason
    # A different asset is unaffected
    ok, _ = rm.validate_new_position('SILVER', SignalType.SELL_BASIS, 1.0, pm)
    assert ok


def test_daily_trade_limit(config, data_logger):
    rm = RiskManager(config)
    pm = PositionManager(data_logger)
    for _ in range(config.RISK_LIMITS['MAX_DAILY_TRADES']):
        rm.daily_trades.append((datetime.now(), 'GOLD', 1.0))
    ok, reason = rm.validate_new_position('GOLD', SignalType.SELL_BASIS,
                                          1.0, pm)
    assert not ok and "Daily trade limit" in reason


def test_stop_loss_sell_basis_on_widening_premium(config):
    rm = RiskManager(config)
    pos = make_position(SignalType.SELL_BASIS, entry_premium=25.0)
    # Premium widened beyond STOP_LOSS_PCT (5.0)
    hit, action = rm.check_position_risk(pos, 31.0)
    assert hit and action == "STOP_LOSS"
    hit, _ = rm.check_position_risk(pos, 27.0)
    assert not hit


def test_stop_loss_buy_basis_on_deepening_discount(config):
    rm = RiskManager(config)
    pos = make_position(SignalType.BUY_BASIS, entry_premium=-20.0)
    hit, action = rm.check_position_risk(pos, -26.0)
    assert hit and action == "STOP_LOSS"
    hit, _ = rm.check_position_risk(pos, -22.0)
    assert not hit


def test_lot_target_tracks_but_never_rejects(config, data_logger):
    """DAILY_LOT_TARGET is a throughput target, NOT a cap."""
    config.TRADING['DAILY_LOT_TARGET'] = 500.0
    config.TRADING['CLIP_LOTS'] = 50.0
    config.RISK_LIMITS['MAX_LOT_SIZE'] = 50.0
    config.EXECUTION['MIN_TIME_BETWEEN_SIGNALS'] = 0

    rm = RiskManager(config)
    pm = PositionManager(data_logger)

    for _ in range(12):  # 600 lots — beyond the 500 target
        ok, reason = rm.validate_new_position('GOLD', SignalType.SELL_BASIS,
                                              50.0, pm)
        assert ok, f"target must not reject: {reason}"
        rm.record_trade('GOLD', lots=50.0)

    assert rm.lots_traded_today('GOLD') == 600.0
    assert rm.lots_traded_today('SILVER') == 0.0
