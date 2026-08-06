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
        'ENTRY_Z': 2.0, 'EXIT_Z': 0.5, 'STOP_Z': 4.0, 'MAX_ENTRY_Z': 4.0,
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
    stats = stretched_stats(sig_config, clock, z_target=4.2)  # > ceiling 4.0
    gen = ZSignalGenerator(sig_config, clock=clock)
    assert gen.entry_signal('GOLD', stats, market_data(), {},
                            50.0, 100) is None


def test_entry_ceiling_independent_of_z_stop(sig_config):
    # MAX_ENTRY_Z is its own knob: ceiling 3.5 refuses z=3.8 even
    # though the z-stop threshold sits far away at 4.5
    sig_config.SIGNALS['MAX_ENTRY_Z'] = 3.5
    sig_config.SIGNALS['STOP_Z'] = 4.5
    clock = FakeClock()
    stats = stretched_stats(sig_config, clock, z_target=3.8)
    gen = ZSignalGenerator(sig_config, clock=clock)
    assert gen.entry_signal('GOLD', stats, market_data(), {},
                            50.0, 100) is None
    # ... while z=3.0 inside the band still enters
    stats_ok = stretched_stats(sig_config, FakeClock(), z_target=3.0)
    assert gen.entry_signal('GOLD', stats_ok, market_data(), {},
                            50.0, 100) is not None


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


# --- degenerate windows: a sigma near zero is not a signal ---------------
# Live 2026-08-06: "swap_diff +9.13 | z +53026.30". Sigma had collapsed
# because the spread barely moved in the window, and dividing by it
# produced a number with no meaning.

def test_a_flat_window_alone_is_not_yet_dangerous(sig_config):
    """While the spread sits still, z stays around 1 however small
    sigma is — no entry fires on that. The danger starts when the
    value LEAVES the collapsed anchor."""
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    feed(stats, clock, [9.13, 9.1300001] * 30)
    assert stats.sigma < 1e-4
    assert abs(stats.z) < 2


def test_the_absurd_z_the_operator_saw_is_refused(sig_config):
    """swap_diff +9.13 with z +53026: sigma had collapsed, then the
    spread moved. That number reached the log and the dashboard."""
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    feed(stats, clock, [9.13, 9.1300001] * 30)
    stats.last_value = 9.13 + 5.0        # spread leaves the anchor
    assert abs((stats.last_value - stats.mu) / stats.sigma) > 25
    assert stats.degenerate and stats.z is None


def test_a_healthy_window_still_gives_a_z(sig_config):
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    feed(stats, clock, [8.5, 8.7, 9.0, 9.3, 8.9, 9.1] * 10)
    assert not stats.degenerate
    assert stats.warm and stats.z is not None
    assert abs(stats.z) < 25


def test_an_absolute_sigma_floor_can_be_set(sig_config):
    """The only guard against a SMALL-but-not-absurd sigma putting z
    inside the entry band on noise — the operator sets it once the
    spread's real sigma is known."""
    clock = FakeClock()
    sig_config.SIGNALS['MIN_SIGMA'] = 0.05
    stats = make_stats(sig_config, clock)
    feed(stats, clock, [9.10, 9.11, 9.12, 9.13] * 20)   # sigma ~0.011
    assert stats.sigma < 0.05
    assert stats.degenerate and stats.z is None

    sig_config.SIGNALS['MIN_SIGMA'] = 0.001
    assert not stats.degenerate and stats.z is not None


def test_the_floor_is_off_by_default(sig_config):
    assert sig_config.SIGNALS['MIN_SIGMA'] == 0.0


# --- the window is a series of QUOTES, not of poll iterations ------------
# Root cause of that collapsed sigma: the coordinator polls every 0.5s,
# faster than either broker ticks, so the same quote was sampled over and
# over until the window held almost no variation.

def test_a_repeated_quote_adds_no_sample(sig_config):
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    for _ in range(200):
        clock.t += 0.5
        stats.update(9.13, quote_id='t1|9.13')
    assert len(stats.samples) == 1
    assert not stats.warm


def test_each_new_quote_adds_one_sample(sig_config):
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    for i in range(60):
        for _ in range(8):                    # polled 8x per quote
            clock.t += 0.5
            stats.update(9.0 + 0.1 * (i % 5), quote_id=f'q{i}')
    assert len(stats.samples) == 60
    assert stats.warm and stats.z is not None


def test_oversampling_no_longer_collapses_sigma(sig_config):
    """The same six quotes, polled once each vs polled twenty times
    each, must give the SAME sigma."""
    clock_a, clock_b = FakeClock(), FakeClock()
    once, many = make_stats(sig_config, clock_a), make_stats(sig_config, clock_b)
    values = [8.5, 8.7, 9.0, 9.3, 8.9, 9.1] * 10
    for i, value in enumerate(values):
        clock_a.t += 10
        once.update(value, quote_id=f'q{i}')
        for _ in range(20):
            clock_b.t += 0.5
            many.update(value, quote_id=f'q{i}')
    assert many.sigma == pytest.approx(once.sigma)
    assert many.warm and not many.degenerate


def test_a_feed_that_stops_ticking_goes_cold(sig_config):
    """Ageing runs on every poll, not only on new quotes — a dead feed
    must drain out of the window instead of freezing a stale z."""
    sig_config.SIGNALS['LOOKBACK_SEC'] = 300
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    for i in range(60):
        clock.t += 1
        stats.update(9.0 + 0.1 * (i % 5), quote_id=f'q{i}')
    assert stats.warm
    for _ in range(800):                      # feed frozen, poll continues
        clock.t += 0.5
        stats.update(9.2, quote_id='q59')
    assert not stats.samples and not stats.warm and stats.z is None


def test_without_a_quote_id_every_call_is_a_sample(sig_config):
    """Callers that have no quote identity (tests, the legacy path)
    keep the old behaviour."""
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    feed(stats, clock, [9.0, 9.2] * 30)
    assert len(stats.samples) == 60


# --- the rolling-window settings reach the LIVE stats --------------------
# Operator, 2026-08-06: "the lookback period setting ... not working".
# SpreadStats holds a reference to config.SIGNALS, and hot_apply updates
# that dict in place — so a saved setting must take effect without a
# restart. If either side ever swaps the dict for a new one instead of
# updating it, the live window silently keeps the old value.

def test_a_hot_reloaded_lookback_applies_without_a_restart(sig_config):
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    feed(stats, clock, [9.0, 9.2] * 30, dt=10.0)     # 600s of quotes
    assert len(stats.samples) == 60

    sig_config.SIGNALS['LOOKBACK_SEC'] = 100         # as the UI would save
    clock.t += 1
    stats.update(9.1)
    assert len(stats.samples) <= 11                  # older quotes dropped


def test_a_hot_reloaded_min_samples_applies_without_a_restart(sig_config):
    clock = FakeClock()
    stats = make_stats(sig_config, clock)
    feed(stats, clock, [8.5, 8.7, 9.0, 9.3, 8.9, 9.1] * 10)
    assert stats.warm

    sig_config.SIGNALS['MIN_SAMPLES'] = 10 ** 6
    assert not stats.warm and stats.z is None


def test_hot_apply_updates_the_signals_dict_in_place(config):
    """SpreadStats was handed this exact dict at start-up."""
    from statarb.config import AlgoTradingConfig
    live = config.SIGNALS
    fresh = AlgoTradingConfig()
    fresh.SIGNALS['LOOKBACK_SEC'] = 1234
    config.hot_apply(fresh)
    assert live is config.SIGNALS            # same object, not replaced
    assert live['LOOKBACK_SEC'] == 1234
