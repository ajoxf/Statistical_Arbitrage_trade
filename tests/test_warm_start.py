"""Recovering the rolling window across a restart.

Operator, 2026-08-06: "Everytime we start the program the Data
Collection goes to 0". Every quote was already being written to the
market_data table; nothing read it back. With MIN_HISTORY_SEC at 120
minutes that made a restart cost two hours before the engine could
trade — after a crash, a config change, or simply closing the launcher.

app.py solved the same problem the same way, and its comment names this
exact symptom: "This fixes the issue where progress shows 0% on restart
despite having DB data".
"""

from datetime import datetime, timedelta

import pytest

from statarb.database import DataLogger
from statarb.spread import SpreadStats


KEY = 'XAUUSD_|GC1226|1.000000'


@pytest.fixture
def logger(tmp_path):
    return DataLogger(db_path=str(tmp_path / 'warm.db'))


def snapshot(spread):
    return {'spot_price': 4243.0, 'futures_price': 4243.0 + spread,
            'actual_basis': spread, 'spread': spread, 'basis_pct': 1.0}


def write(logger, spreads, key=KEY, asset='GOLD'):
    for spread in spreads:
        logger.log_market_data(asset, snapshot(spread), 'NO_SIGNAL',
                               z=None, series_key=key)


# --- the query -----------------------------------------------------------

def test_stored_quotes_come_back(logger):
    write(logger, [58.7, 58.8, 58.9])
    rows = logger.recent_spreads('GOLD', KEY,
                                 datetime.now() - timedelta(hours=1))
    assert [round(v, 2) for _, v in rows] == [58.7, 58.8, 58.9]
    assert all(isinstance(t, float) for t, _ in rows)


def test_quotes_older_than_the_window_are_left_behind(logger):
    write(logger, [58.7])
    rows = logger.recent_spreads('GOLD', KEY,
                                 datetime.now() + timedelta(seconds=5))
    assert rows == []


def test_another_asset_is_not_mixed_in(logger):
    write(logger, [58.7], asset='GOLD')
    write(logger, [12.3], asset='SILVER')
    rows = logger.recent_spreads('GOLD', KEY,
                                 datetime.now() - timedelta(hours=1))
    assert [round(v, 2) for _, v in rows] == [58.7]


# --- the series key is the safety property -------------------------------

def test_a_different_hedge_ratio_is_a_different_series(logger):
    """HEDGE_RATIO defines the spread. Seeding a beta=2 window with
    beta=1 history would give a mean the live spread never visits —
    and the engine would read that as an enormous z."""
    write(logger, [58.7, 58.8], key='XAUUSD_|GC1226|1.000000')
    rows = logger.recent_spreads('GOLD', 'XAUUSD_|GC1226|2.000000',
                                 datetime.now() - timedelta(hours=1))
    assert rows == []


def test_a_different_symbol_is_a_different_series(logger):
    write(logger, [58.7], key='XAUUSD_|GC1226|1.000000')
    rows = logger.recent_spreads('GOLD', 'XAUUSD_|GC1227|1.000000',
                                 datetime.now() - timedelta(hours=1))
    assert rows == []


def test_rows_from_before_the_column_existed_are_never_reused(logger):
    logger.log_market_data('GOLD', snapshot(58.7), 'NO_SIGNAL')  # no key
    rows = logger.recent_spreads('GOLD', KEY,
                                 datetime.now() - timedelta(hours=1))
    assert rows == []


# --- seeding the window --------------------------------------------------

class FakeClock:
    def __init__(self, t=100000.0):
        self.t = t

    def __call__(self):
        return self.t


def stats_for(config, **signals):
    config.SIGNALS.update({'LOOKBACK_SEC': 7200, 'MIN_SAMPLES': 50,
                           'MIN_HISTORY_SEC': 7200, 'STATS_INTERVAL_SEC': 5})
    config.SIGNALS.update(signals)
    return SpreadStats(config.SIGNALS, clock=FakeClock())


def series(clock, count, start_ago, step=1.0):
    base = clock() - start_ago
    return [(base + i * step, 58.7 + (i % 5) * 0.05) for i in range(count)]


def test_seeding_fills_the_window_and_the_stats(config):
    stats = stats_for(config)
    assert stats.seed(series(stats.clock, 300, 7000)) == 300
    assert len(stats.samples) == 300
    assert stats.mu is not None and stats.sigma > 0


def test_a_restart_no_longer_replays_the_whole_warm_up(config):
    """The operator's complaint: two hours of collection thrown away.

    A warm start cannot credit MORE history than the window holds —
    samples older than LOOKBACK_SEC are gone — so with MIN_HISTORY_SEC
    equal to LOOKBACK_SEC there is a short top-up before trading. The
    point is that it is MINUTES, not the full two hours.
    """
    stats = stats_for(config)
    stats.seed(series(stats.clock, 300, 7000))
    assert stats.history_sec >= 6900               # credited from the DATA
    remaining = stats.min_history_sec - stats.history_sec
    assert 0 < remaining < 400                     # ~3 min, not 120

    stats.clock.t += remaining + 1                 # the top-up elapses
    stats.update(58.9, quote_id='live-1')
    assert stats.warm and stats.z is not None


def test_a_shorter_history_gate_is_satisfied_immediately(config):
    """The usual setup: MIN_HISTORY_SEC below the window width."""
    stats = stats_for(config, MIN_HISTORY_SEC=1800)
    stats.seed(series(stats.clock, 300, 7000))
    assert stats.warm and stats.z is not None


def test_history_is_credited_from_the_oldest_sample_not_from_now(config):
    stats = stats_for(config)
    stats.seed(series(stats.clock, 100, 3600, step=36))
    assert stats.history_sec == pytest.approx(3600, abs=60)
    assert not stats.warm                      # only 60 of 120 minutes


def test_samples_older_than_the_window_are_discarded(config):
    stats = stats_for(config)
    old = [(stats.clock() - 20000, 58.0)]
    inside = series(stats.clock, 100, 3000)
    assert stats.seed(old + inside) == 100
    assert len(stats.samples) == 100


def test_seeding_nothing_is_harmless(config):
    stats = stats_for(config)
    assert stats.seed([]) == 0
    assert stats.seed([(stats.clock() - 99999, 58.0)]) == 0
    assert not stats.warm


def test_live_quotes_continue_on_top_of_the_seed(config):
    stats = stats_for(config)
    stats.seed(series(stats.clock, 300, 7000))
    before = len(stats.samples)
    stats.clock.t += 1
    stats.update(58.95, quote_id='live-1')
    assert len(stats.samples) == before + 1
    assert stats.last_value == 58.95


def test_the_first_live_quote_is_not_swallowed_as_a_duplicate(config):
    """seed() must not leave a quote_id behind that dedups a real one."""
    stats = stats_for(config)
    stats.seed(series(stats.clock, 60, 600))
    stats.clock.t += 1
    stats.update(58.9, quote_id=None)
    assert stats.last_value == 58.9


# --- the coordinator wires it together -----------------------------------

def test_the_coordinator_recovers_the_window_on_start(tmp_path, monkeypatch,
                                                      config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(config, trading_mode='PAPER')
    coord.active_assets = {'GOLD': {'spot_symbol': 'XAUUSD_',
                                    'futures_symbol': 'GC1226',
                                    'config': config.ASSETS['GOLD'],
                                    'last_data': None}}
    config.TRADING['HEDGE_RATIO'] = 1.0
    write(coord.data_logger, [58.7, 58.75, 58.8],
          key=coord._series_key('GOLD'))

    coord.stats['GOLD'] = SpreadStats(config.SIGNALS)
    assert coord._warm_start('GOLD') == 3
    assert len(coord.stats['GOLD'].samples) == 3


def test_a_changed_hedge_ratio_refuses_the_stored_window(tmp_path,
                                                         monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(config, trading_mode='PAPER')
    coord.active_assets = {'GOLD': {'spot_symbol': 'XAUUSD_',
                                    'futures_symbol': 'GC1226',
                                    'config': config.ASSETS['GOLD'],
                                    'last_data': None}}
    config.TRADING['HEDGE_RATIO'] = 1.0
    write(coord.data_logger, [58.7, 58.75], key=coord._series_key('GOLD'))

    config.TRADING['HEDGE_RATIO'] = 2.0        # structural change
    coord.stats['GOLD'] = SpreadStats(config.SIGNALS)
    assert coord._warm_start('GOLD') == 0
    assert not coord.stats['GOLD'].samples
