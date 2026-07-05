from statarb.models import Position, SignalType
from statarb.signals import SignalGenerator


def make_market_data(premium):
    return {'swap_premium_pct': premium}


def make_position(signal_type):
    pos = Position("POS_0001", 'GOLD', signal_type, None, None)
    pos.entry_premium = 25.0
    return pos


def test_entry_sell_basis_above_premium_threshold(config):
    gen = SignalGenerator(config)
    assert gen.generate_signal('GOLD', make_market_data(25.0), {}) \
        == SignalType.SELL_BASIS


def test_entry_buy_basis_below_discount_threshold(config):
    gen = SignalGenerator(config)
    assert gen.generate_signal('GOLD', make_market_data(-20.0), {}) \
        == SignalType.BUY_BASIS


def test_no_signal_in_neutral_zone(config):
    gen = SignalGenerator(config)
    assert gen.generate_signal('GOLD', make_market_data(10.0), {}) \
        == SignalType.NO_SIGNAL


def test_no_new_entry_while_position_open(config):
    gen = SignalGenerator(config)
    active = {'POS_0001': make_position(SignalType.SELL_BASIS)}
    # Premium above entry threshold but a position exists -> no new entry
    assert gen.generate_signal('GOLD', make_market_data(30.0), active) \
        == SignalType.NO_SIGNAL


def test_exit_sell_basis_when_premium_normalizes(config):
    gen = SignalGenerator(config)
    active = {'POS_0001': make_position(SignalType.SELL_BASIS)}
    signal = gen.generate_signal('GOLD', make_market_data(4.0), active)
    assert signal == ('POS_0001', SignalType.CLOSE_LONG)


def test_exit_buy_basis_when_discount_normalizes(config):
    gen = SignalGenerator(config)
    active = {'POS_0001': make_position(SignalType.BUY_BASIS)}
    signal = gen.generate_signal('GOLD', make_market_data(-4.0), active)
    assert signal == ('POS_0001', SignalType.CLOSE_SHORT)
