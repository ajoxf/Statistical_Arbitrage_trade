"""How fast the price on the dashboard moves.

The operator reported the screen not refreshing at its 300ms cadence.
Nothing in the browser was at fault: the chain is coordinator poll ->
runtime_status.json -> the webapp's socket bridge -> the page, and the
slowest link sets the rate. The status file used to be written only
alongside the 10-second log line, so the prices on screen were up to
ten seconds old however often the page asked for them.
"""

import json
import re
import time

import pytest

from statarb.coordinator import Coordinator


class StatusLeg:
    def __init__(self, name):
        self.name = name
        self.account_info_calls = 0

    def account_info(self):
        self.account_info_calls += 1
        return {'login': 1, 'balance': 50000.0, 'equity': 50000.0,
                'margin': 0.0, 'margin_free': 50000.0, 'currency': 'USD'}

    def order_log(self, hours=24):
        return []

    def ping(self):
        return True

    def close(self):
        pass


def market_data(spot=3300.0, futures=3320.0):
    from datetime import datetime
    return {'GOLD': {
        'asset_name': 'GOLD', 'timestamp': datetime.now(),
        'quote_id': f'{spot}/{futures}',
        'spot_price': spot, 'spot_bid': spot - 0.1, 'spot_ask': spot + 0.1,
        'futures_price': futures, 'futures_bid': futures - 0.1,
        'futures_ask': futures + 0.1,
        'actual_basis': futures - spot, 'spread': futures - spot,
        'basis_pct': 0.0, 'hedge_ratio': 1.0,
        'spread_formula': 'spread = futures - 1 x spot',
    }}


@pytest.fixture
def coord(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    coordinator = Coordinator(config, trading_mode='PAPER')
    coordinator.spot_leg = StatusLeg('account_a')
    coordinator.futures_leg = StatusLeg('account_b')
    return coordinator


def status_file(tmp_path):
    with open(tmp_path / 'runtime_status.json') as f:
        return json.load(f)


# --- the write happens per poll, not per log line -------------------------

def test_the_status_file_is_written_on_every_poll(coord, tmp_path,
                                                  monkeypatch):
    """This is the operator's bug: prices were as stale as the 10s log."""
    coord.get_all_market_data = lambda: market_data()
    coord.process_asset = lambda *a, **k: None
    writes = []
    original = coord._write_runtime_status
    coord._write_runtime_status = lambda md: (writes.append(md), original(md))

    laps = []

    def stop_after_five(_seconds):
        laps.append(1)
        if len(laps) >= 5:
            coord.is_running = False

    monkeypatch.setattr(time, 'sleep', stop_after_five)
    coord.run()

    assert len(writes) == 5           # one per poll, not one per 10s
    assert status_file(tmp_path)['assets'][0]['spot_price'] == 3300.0


def test_the_log_line_no_longer_writes_the_status_file(coord, tmp_path):
    """Untangling the two is the fix — the log stays on its slow clock."""
    coord.log_status(market_data())
    assert not (tmp_path / 'runtime_status.json').exists()


def test_polling_faster_does_not_make_the_log_noisier(coord, monkeypatch):
    """Housekeeping is on the clock, not on a loop count — tripling the
    poll rate used to triple the status lines in the log."""
    coord.get_all_market_data = lambda: market_data()
    coord.process_asset = lambda *a, **k: None
    logged = []
    coord.log_status = lambda md, heartbeat=False: logged.append(heartbeat)

    laps = []

    def stop_after_fifty(_seconds):
        laps.append(1)
        if len(laps) >= 50:
            coord.is_running = False

    monkeypatch.setattr(time, 'sleep', stop_after_fifty)
    coord.run()

    assert len(laps) == 50           # 50 polls, all inside 10s of wall clock
    # log_status is called every poll now — it decides for itself
    # whether anything changed — but only ONE of those is a heartbeat.
    assert len(logged) == 50
    assert logged.count(True) == 1


def test_the_poll_interval_is_reread_so_a_hot_reload_applies(coord,
                                                             monkeypatch):
    """POLL_INTERVAL_SEC is in HOT_TRADING_KEYS, but run() captured it
    once before the loop, so changing it live did nothing."""
    coord.get_all_market_data = lambda: market_data()
    coord.process_asset = lambda *a, **k: None
    slept = []

    def sleeper(seconds):
        slept.append(seconds)
        if len(slept) == 1:
            coord.config.TRADING['POLL_INTERVAL_SEC'] = 2.0
        if len(slept) >= 3:
            coord.is_running = False

    monkeypatch.setattr(time, 'sleep', sleeper)
    coord.run()
    assert slept[-1] > slept[0]


# --- the sub-second stamp the socket bridge compares ----------------------

def test_the_stamp_carries_milliseconds(coord, tmp_path):
    """webapp's broadcast_loop skips when 'updated' is unchanged, so at
    whole-second resolution that alone capped the screen at 1 Hz."""
    coord._write_runtime_status(market_data())
    assert re.fullmatch(r'\d{2}:\d{2}:\d{2}\.\d{3}',
                        status_file(tmp_path)['updated'])


def test_the_socket_bridge_looks_faster_than_the_page_refreshes():
    from statarb import webapp
    assert webapp.BROADCAST_INTERVAL_SEC <= 0.3


def test_the_default_poll_matches_the_dashboard_cadence(config):
    assert config.TRADING['POLL_INTERVAL_SEC'] <= 0.3


# --- account figures are NOT re-fetched at the price rate -----------------

def test_account_info_is_cached_across_polls(coord):
    """Each call is an IPC round-trip into MT5. Prices move at 3Hz;
    balances do not, and must not drag two terminals along."""
    for _ in range(20):
        coord._write_runtime_status(market_data())
    assert coord.spot_leg.account_info_calls == 1
    assert coord.futures_leg.account_info_calls == 1


def test_the_account_cache_expires(coord, monkeypatch):
    coord._write_runtime_status(market_data())
    coord._accounts_at -= coord.ACCOUNT_REFRESH_SEC + 1
    coord._write_runtime_status(market_data())
    assert coord.spot_leg.account_info_calls == 2


def test_the_margin_breaker_still_sees_each_refresh(coord):
    seen = []
    coord.risk_manager.update_accounts = lambda a: seen.append(a)
    coord._write_runtime_status(market_data())
    coord._accounts_at -= coord.ACCOUNT_REFRESH_SEC + 1
    coord._write_runtime_status(market_data())
    assert len(seen) == 2
    assert set(seen[-1]) == {'account_a', 'account_b'}


def test_the_cached_equity_still_reaches_the_dashboard(coord, tmp_path):
    for _ in range(3):
        coord._write_runtime_status(market_data())
    published = status_file(tmp_path)
    assert published['equity'] == pytest.approx(100000.0)
    assert set(published['accounts']) == {'account_a', 'account_b'}
