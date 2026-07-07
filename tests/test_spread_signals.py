"""SpreadStats (rolling z, frozen anchor, half-life, trend) and the
ZSignalGenerator gates."""

import pytest

from statarb.models import SignalType
from statarb.signals import ZSignalGenerator
from statarb.spread import SpreadStats


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def market_data(spread_dollars=0.30):
    half = spread_dollars / 2
    return {'spot_bid': 3300 - half, 'spot_ask': 3300 + half,
            'futures_bid': 3320 - half, 'futures_ask': 3320 + half}


@pytest.fixture
def sig_config(config):
    config.SIGNALS.update({
        'ENTRY_Z': 2.0, 'EXIT_Z': 0.5, 'STOP_Z': 4.0,
        'LOOKBACK_SEC': 10000, 'STATS_INTERVAL_SEC': 5,
        'MIN_SAMPLES': 50, 'TREND_WINDOW_SEC': 100,
        'TREND_FILTER': False,
        'ENTRY_COOLDOWN_SEC': 60, 'STOP_COOLDOWN_SEC': 300,
    })
    config.COSTS.update({'MIN_EDGE_MULTIPLE': 1.5, 'TARGET_FRACTION': 0.5,
                         'SPREAD_COST_FACTOR': 1.0})
    return config


def feed(stats, clock, values, dt=1.0):
    for value in values:
        clock.t += dt
        stats.update(value)


def make_stats(cfg, clock):
    return SpreadStats(cfg.SIGNALS, clock=clock)


def test_warmup_blocks_until_min_samples(sig_config):
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    feed(stats, clock, [0, 1] * 10)          # 20 < MIN_SAMPLES
    assert not stats.warm and stats.z is None
    feed(stats, clock, [0, 1] * 20)          # 60 total
    assert stats.warm and stats.z is not None


def test_anchor_frozen_between_refreshes(sig_config):
    sig_config.SIGNALS['STATS_INTERVAL_SEC'] = 10 ** 9   # never refresh again
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    feed(stats, clock, [0, 1] * 30)
    mu_before = stats.mu
    # Spread runs away — the frozen anchor must NOT chase it
    feed(stats, clock, [5.0] * 50)
    assert stats.mu == mu_before
    assert stats.z > 3        # stretch measured against the frozen anchor


def test_half_life_from_ar1_decay(sig_config):
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    # Pure AR(1) decay with phi = 0.9, dt = 1s -> HL ~ 6.6s
    values, x = [], 10.0
    for _ in range(200):
        values.append(x)
        x *= 0.9
    feed(stats, clock, values, dt=1.0)
    assert stats.half_life_sec == pytest.approx(6.58, rel=0.15)


def test_trend_slope_sign(sig_config):
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    feed(stats, clock, [i * 0.1 for i in range(100)])
    assert stats.trend_slope() > 0
    stats2 = make_stats(sig_config, FakeClock())
    clock2 = stats2.clock
    feed(stats2, clock2, [10 - i * 0.1 for i in range(100)])
    assert stats2.trend_slope() < 0


# ---------------------------------------------------------------------------


def stretched_stats(cfg, clock, z_target=3.0, sigma=2.0):
    """Warm stats whose last value sits z_target sigmas above the mean."""
    stats = make_stats(cfg, clock)
    feed(stats, clock, [-sigma, sigma] * 40)         # mu~0, sigma~2
    cfg_interval = stats.cfg['STATS_INTERVAL_SEC']
    stats.cfg['STATS_INTERVAL_SEC'] = 10 ** 9        # freeze the anchor
    clock.t += 1
    stats.update(z_target * stats.sigma + stats.mu)  # live stretch
    stats.cfg['STATS_INTERVAL_SEC'] = cfg_interval
    return stats


def test_entry_fires_and_direction(sig_config):
    clock = FakeClock()
    stats = stretched_stats(sig_config, clock, z_target=3.0)
    gen = ZSignalGenerator(sig_config, clock=clock)
    signal = gen.entry_signal('GOLD', stats, market_data(), {}, 50.0, 100)
    assert signal == SignalType.SELL_BASIS          # rich basis -> sell it

    stats_low = stretched_stats(sig_config, FakeClock(), z_target=-3.0)
    signal = gen.entry_signal('GOLD', stats_low, market_data(), {}, 50.0, 100)
    assert signal == SignalType.BUY_BASIS


def test_entry_ceiling_refuses_on_top_of_stop(sig_config):
    clock = FakeClock()
    stats = stretched_stats(sig_config, clock, z_target=4.2)   # > STOP_Z=4.0
    gen = ZSignalGenerator(sig_config, clock=clock)
    assert gen.entry_signal('GOLD', stats, market_data(), {},
                            50.0, 100) is None


def test_trend_filter_blocks_fighting_the_tape(sig_config):
    sig_config.SIGNALS['TREND_FILTER'] = True
    clock = FakeClock()
    # Rising spread that is ALSO stretched high: z>0 wants SELL_BASIS,
    # but the tape is rising -> blocked
    stats = make_stats(sig_config, clock)
    feed(stats, clock, [i * 0.05 for i in range(80)])
    stats.cfg['STATS_INTERVAL_SEC'] = 10 ** 9
    clock.t += 1
    stats.update(stats.mu + 3.0 * stats.sigma)
    gen = ZSignalGenerator(sig_config, clock=clock)
    assert stats.z > 2 and stats.trend_slope() > 0
    assert gen.entry_signal('GOLD', stats, market_data(), {},
                            50.0, 100) is None


def test_cooldowns_and_z_reset_gate(sig_config):
    clock = FakeClock()
    stats = stretched_stats(sig_config, clock, z_target=3.0)
    gen = ZSignalGenerator(sig_config, clock=clock)

    # A stop just happened in the same direction
    gen.notify_close('GOLD', 'DOLLAR_STOP', SignalType.SELL_BASIS)
    assert gen.entry_signal('GOLD', stats, market_data(), {},
                            50.0, 100) is None      # stop cooldown

    clock.t += 301                                   # cooldowns expire
    # ... but z never came home: same-direction re-entry still blocked
    assert gen.entry_signal('GOLD', stats, market_data(), {},
                            50.0, 100) is None

    gen.update('GOLD', 0.2)                          # z re-enters the band
    assert gen.entry_signal('GOLD', stats, market_data(), {},
                            50.0, 100) == SignalType.SELL_BASIS


def test_edge_filter_blocks_dead_day(sig_config):
    # Tiny sigma -> capture can't clear costs -> no trade
    clock = FakeClock()
    stats = stretched_stats(sig_config, clock, z_target=3.0, sigma=2.0)
    gen = ZSignalGenerator(sig_config, clock=clock)

    # capture = 0.5 * 3 * sigma * oz; make sigma minuscule via a fresh
    # stats object with tight values
    tight = make_stats(sig_config, FakeClock())
    tclock = tight.clock
    feed(tight, tclock, [-0.0005, 0.0005] * 40)      # sigma ~ $0.0005
    tight.cfg['STATS_INTERVAL_SEC'] = 10 ** 9
    tclock.t += 1
    tight.update(tight.mu + 3.0 * tight.sigma)
    assert gen.entry_signal('GOLD', tight, market_data(0.30), {},
                            50.0, 100) is None

    # Healthy sigma passes (capture $15k vs cost ~$3k)
    assert gen.entry_signal('GOLD', stats, market_data(0.30), {},
                            50.0, 100) is not None


def test_no_entry_while_position_open(sig_config):
    clock = FakeClock()
    stats = stretched_stats(sig_config, clock, z_target=3.0)
    gen = ZSignalGenerator(sig_config, clock=clock)
    assert gen.entry_signal('GOLD', stats, market_data(),
                            {'POS_0001': object()}, 50.0, 100) is None
