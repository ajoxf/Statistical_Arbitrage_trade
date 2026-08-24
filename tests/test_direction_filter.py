"""Which spread directions the algo may OPEN.

Operator, 2026-08-24: "While running the Algo, can the user select if he
only wants to execute Short Spread, Long Spread or both?"

A pair is not always symmetric — one leg can be hard to borrow, the swap
can be punitive on one side only, or the operator may simply have a
view. This is an ENTRY rule and nothing else: exits are never filtered,
because a position must always be able to close whatever the entry rule
says today, and a manual trade is never blocked by it.
"""

import pytest

from statarb.models import SignalType
from statarb.signals import ZSignalGenerator


class Stats:
    """Warm stats with a z the caller picks and a flat trend."""

    def __init__(self, z):
        self.z = z
        self.warm = True
        self.degenerate = False
        self.sigma = 0.5
        self.mu = 0.0
        self.samples = 500

    def trend_slope(self):
        return 0.0

    def half_life(self):
        return 600.0


@pytest.fixture
def gen(config):
    config.SIGNALS.update({'ENTRY_Z': 3.0, 'MAX_ENTRY_Z': 9.0,
                           'TREND_FILTER': False, 'ENTRY_COOLDOWN_SEC': 0,
                           'STOP_COOLDOWN_SEC': 0})
    config.COSTS.update({'MIN_EDGE_MULTIPLE': 0.0, 'TARGET_FRACTION': 0.5})
    return ZSignalGenerator(config, clock=lambda: 10_000.0)


MD = {'spot_price': 4292.61, 'futures_price': 4351.55,
      'spot_bid': 4292.55, 'spot_ask': 4292.68,
      'futures_bid': 4351.38, 'futures_ask': 4351.72}


def fire(gen, z):
    # Generous size so the edge filter is never the thing that blocks.
    return gen.entry_signal('GOLD', Stats(z), MD, {}, 50.0, 100)


def test_both_is_the_default(config, gen):
    assert config.SIGNALS['ALLOWED_DIRECTIONS'] == 'both'
    assert fire(gen, +4.0) == SignalType.SELL_BASIS
    assert fire(gen, -4.0) == SignalType.BUY_BASIS


def test_short_only_refuses_the_long(config, gen):
    config.SIGNALS['ALLOWED_DIRECTIONS'] = 'short'
    assert fire(gen, +4.0) == SignalType.SELL_BASIS
    assert fire(gen, -4.0) is None


def test_long_only_refuses_the_short(config, gen):
    config.SIGNALS['ALLOWED_DIRECTIONS'] = 'long'
    assert fire(gen, -4.0) == SignalType.BUY_BASIS
    assert fire(gen, +4.0) is None


def test_an_unset_or_odd_value_falls_back_to_both(config, gen):
    for value in (None, '', 'BOTH', 'anything'):
        config.SIGNALS['ALLOWED_DIRECTIONS'] = value
        assert fire(gen, +4.0) == SignalType.SELL_BASIS, value


def test_the_block_names_the_direction_rule_not_the_trend(config, gen, caplog):
    """A standing decision is not a market condition. Reporting it as a
    trend block would send the operator looking at the wrong thing."""
    import logging
    config.SIGNALS['ALLOWED_DIRECTIONS'] = 'short'
    with caplog.at_level(logging.INFO):
        assert fire(gen, -4.0) is None
    assert 'SHORT SPREAD only' in caplog.text
    assert 'trend' not in caplog.text.lower()


def test_it_applies_live(config):
    """SIGNALS is a hot section, so the operator does not restart to
    change their mind."""
    from statarb.config import AlgoTradingConfig
    assert 'SIGNALS' in AlgoTradingConfig.HOT_SECTIONS
    fresh = AlgoTradingConfig()
    fresh.SIGNALS['ALLOWED_DIRECTIONS'] = 'long'
    config.hot_apply(fresh)
    assert config.SIGNALS['ALLOWED_DIRECTIONS'] == 'long'


def test_the_setting_round_trips_through_the_ui():
    from statarb import webapi
    raw, _, _ = webapi.apply_ui_config({}, {'allowed_directions': 'SHORT'})
    assert raw['signals']['ALLOWED_DIRECTIONS'] == 'short'
    assert webapi.to_ui_config(raw)['allowed_directions'] == 'short'
