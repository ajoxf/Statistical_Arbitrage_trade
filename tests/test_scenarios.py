"""Round-trip order scenarios (the Exchanges page's test suite).

These are the tests the operator's REAL-money test button leans on:
every scenario must complete a full round trip and, above all, must
never leave a leg naked when something fails halfway.
"""

import json
import threading
import time

import pytest

from statarb import scenarios
from tests.test_limit_execution import FakeClock, LimitFakeLeg


class ScenarioFakeLeg(LimitFakeLeg):
    """LimitFakeLeg plus a way to make closes fail."""

    def __init__(self, *args, fail_close=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_close = set(fail_close or [])
        self.live_positions = []       # what the BROKER shows as open

    def positions(self, symbol=None):
        return [p for p in self.live_positions
                if symbol is None or p['symbol'] == symbol]

    def account_info(self):
        return {'account': self.name, 'login': 1, 'equity': 1e6}

    def close_ticket(self, symbol, ticket, volume, entry_side,
                     slippage_points=1.0, comment=""):
        if symbol in self.fail_close:
            return {'ok': False, 'filled_volume': 0.0, 'price': None,
                    'error': 'close rejected'}
        return super().close_ticket(symbol, ticket, volume, entry_side,
                                    slippage_points, comment)


def make_runner(spot_leg=None, futures_leg=None, stats=(1.5, 1.0, 0.4, 1.25)):
    spot_leg = spot_leg or ScenarioFakeLeg('account_a', price=3300.0)
    futures_leg = futures_leg or ScenarioFakeLeg('account_b', price=3320.0)
    spot = scenarios.Leg(spot_leg, 'XAUUSD', 'SPOT', contract_size=100.0,
                         commission_per_lot=6.0)
    futures = scenarios.Leg(futures_leg, 'GC1225', 'FUTURES',
                            contract_size=100.0, commission_per_lot=4.0)
    clock = FakeClock()
    runner = scenarios.ScenarioRunner(
        spot, futures, spread_stats=lambda: stats,
        clock=clock, sleep=clock.sleep)
    return runner, spot_leg, futures_leg


# --- the catalogue --------------------------------------------------------

def test_catalogue_is_the_forty_scenarios():
    cat = scenarios.CATALOGUE
    assert len(cat) == 40
    assert sum(1 for s in cat if s['mode'] == 'LIMIT') == 18
    assert sum(1 for s in cat if s['mode'] == 'MARKET') == 22
    assert sum(1 for s in cat if s['variant'] == 'cancel') == 6
    assert sum(1 for s in cat if s['variant'] == 'quick_close') == 6
    assert sum(1 for s in cat
               if s['variant'].startswith('partial_')) == 4
    assert [s['id'] for s in cat] == list(range(40))


def test_catalogue_covers_both_legs_and_both_spread_directions():
    types = {s['type'] for s in scenarios.CATALOGUE}
    assert types == {'BUY_SPOT', 'SELL_SPOT', 'BUY_FUT', 'SELL_FUT',
                     'LONG_SPR', 'SHORT_SPR'}


# --- single-leg round trips -----------------------------------------------

def test_market_round_trip_opens_and_closes_on_the_right_account():
    runner, spot_leg, fut_leg = make_runner()
    out = runner.run('BUY_SPOT', 'MARKET')
    assert out['success']
    assert spot_leg.market_orders == [('XAUUSD', 'BUY', 0.01)]
    assert spot_leg.closed_tickets and not fut_leg.market_orders
    assert 'open @' in out['detail'] and 'close @' in out['detail']


def test_futures_scenarios_route_to_the_futures_account():
    runner, spot_leg, fut_leg = make_runner()
    out = runner.run('SELL_FUT', 'MARKET')
    assert out['success']
    assert fut_leg.market_orders == [('GC1225', 'SELL', 0.01)]
    assert not spot_leg.market_orders
    assert 'account_b' in out['detail']


def test_a_failed_open_fails_the_scenario_without_a_close():
    runner, spot_leg, _ = make_runner(
        spot_leg=ScenarioFakeLeg('account_a', fail_market=['XAUUSD']))
    out = runner.run('BUY_SPOT', 'MARKET')
    assert not out['success']
    assert not spot_leg.closed_tickets
    assert 'FAILED' in out['detail']


def test_a_failed_close_is_reported_loudly():
    runner, _, _ = make_runner(
        spot_leg=ScenarioFakeLeg('account_a', fail_close=['XAUUSD']))
    out = runner.run('BUY_SPOT', 'MARKET')
    assert not out['success']
    assert 'close FAILED' in out['detail']


def test_limit_round_trip_rests_then_fills_then_closes():
    spot_leg = ScenarioFakeLeg('account_a', price=3300.0,
                               limit_fill_polls={'XAUUSD': 2})
    runner, spot_leg, _ = make_runner(spot_leg=spot_leg)
    out = runner.run('BUY_SPOT', 'LIMIT')
    assert out['success']
    symbol, side, volume, price = spot_leg.placed[0]
    assert (symbol, side, volume) == ('XAUUSD', 'BUY', 0.01)
    assert price == pytest.approx(3299.95)      # rests at the bid
    assert spot_leg.closed_tickets


def test_a_limit_that_never_fills_is_cancelled_not_left_resting():
    spot_leg = ScenarioFakeLeg('account_a', limit_fill_polls={'XAUUSD': None})
    runner, spot_leg, _ = make_runner(spot_leg=spot_leg)
    out = runner.run('BUY_SPOT', 'LIMIT')
    assert out['success']              # cancelling cleanly IS the pass
    assert len(spot_leg.cancels) == 1
    assert 'no fill in 15s' in out['detail']
    assert not spot_leg.closed_tickets


def test_cancel_variant_parks_away_from_the_market():
    spot_leg = ScenarioFakeLeg('account_a', price=3300.0,
                               limit_fill_polls={'XAUUSD': None})
    runner, spot_leg, _ = make_runner(spot_leg=spot_leg)
    out = runner.run('BUY_SPOT', 'LIMIT', variant='cancel')
    assert out['success']
    _, _, _, price = spot_leg.placed[0]
    assert price == pytest.approx(3299.95 * 0.99)   # ~1% below the bid
    assert len(spot_leg.cancels) == 1


def test_a_fill_leaking_through_a_cancel_fails_the_scenario():
    """Deal history lags a cancel. If volume slipped through, the
    operator must see it — a silent pass would hide a live position."""
    spot_leg = ScenarioFakeLeg('account_a', limit_fill_polls={'XAUUSD': None},
                               leak_on_cancel={'XAUUSD': 0.01})
    runner, _, _ = make_runner(spot_leg=spot_leg)
    out = runner.run('BUY_SPOT', 'LIMIT', variant='cancel')
    assert not out['success']
    assert 'before the cancel landed' in out['detail']


# --- spread (two-account) round trips -------------------------------------

def test_long_spread_buys_spot_on_a_and_sells_futures_on_b():
    runner, spot_leg, fut_leg = make_runner()
    out = runner.run('LONG_SPR', 'MARKET')
    assert out['success']
    assert spot_leg.market_orders == [('XAUUSD', 'BUY', 0.01)]
    assert fut_leg.market_orders == [('GC1225', 'SELL', 0.01)]
    assert len(spot_leg.closed_tickets) == 1
    assert len(fut_leg.closed_tickets) == 1
    assert 'account_a' in out['detail'] and 'account_b' in out['detail']


def test_short_spread_is_the_mirror():
    runner, spot_leg, fut_leg = make_runner()
    assert runner.run('SHORT_SPR', 'MARKET')['success']
    assert spot_leg.market_orders == [('XAUUSD', 'SELL', 0.01)]
    assert fut_leg.market_orders == [('GC1225', 'BUY', 0.01)]


def test_a_failed_hedge_rolls_the_first_leg_back():
    """The one outcome that must never happen is a naked leg left on
    the book because the other account rejected."""
    fut_leg = ScenarioFakeLeg('account_b', fail_market=['GC1225'])
    runner, spot_leg, fut_leg = make_runner(futures_leg=fut_leg)
    out = runner.run('LONG_SPR', 'MARKET')
    assert not out['success']
    assert len(spot_leg.closed_tickets) == 1       # rolled back
    assert 'rollback close' in out['detail']


def test_spread_limit_cancels_both_resting_orders():
    spot_leg = ScenarioFakeLeg('account_a', limit_fill_polls={'XAUUSD': None})
    fut_leg = ScenarioFakeLeg('account_b', limit_fill_polls={'GC1225': None})
    runner, spot_leg, fut_leg = make_runner(spot_leg, fut_leg)
    out = runner.run('LONG_SPR', 'LIMIT', variant='cancel')
    assert out['success']
    assert len(spot_leg.cancels) == 1 and len(fut_leg.cancels) == 1


def test_spread_limit_where_only_one_leg_fills_closes_it_and_cancels_the_other():
    spot_leg = ScenarioFakeLeg('account_a', limit_fill_polls={'XAUUSD': 2})
    fut_leg = ScenarioFakeLeg('account_b', limit_fill_polls={'GC1225': None})
    runner, spot_leg, fut_leg = make_runner(spot_leg, fut_leg)
    out = runner.run('LONG_SPR', 'LIMIT')
    assert len(spot_leg.closed_tickets) == 1       # filled leg flattened
    assert len(fut_leg.cancels) == 1               # resting leg pulled
    assert 'no fill in 15s' in out['detail']


def test_spread_limit_rolls_back_when_the_second_order_is_rejected():
    fut_leg = ScenarioFakeLeg('account_b')

    def reject(*args, **kwargs):
        return {'ok': False, 'ticket': None, 'error': 'rejected'}

    fut_leg.place_limit = reject
    runner, spot_leg, _ = make_runner(futures_leg=fut_leg)
    out = runner.run('LONG_SPR', 'LIMIT')
    assert not out['success']
    assert len(spot_leg.cancels) == 1
    assert 'rollback' in out['detail']


# --- partial-fill recovery ------------------------------------------------

@pytest.mark.parametrize('variant,traded,idle', [
    ('partial_spot', 'spot', 'fut'), ('partial_futures', 'fut', 'spot')])
def test_partial_recovery_market_closes_only_the_filled_leg(variant, traded,
                                                            idle):
    runner, spot_leg, fut_leg = make_runner()
    out = runner.run('LONG_SPR', 'MARKET', variant=variant)
    legs = {'spot': spot_leg, 'fut': fut_leg}
    assert out['success']
    assert len(legs[traded].market_orders) == 1
    assert len(legs[traded].closed_tickets) == 1
    assert not legs[idle].market_orders
    assert 'recovery close' in out['detail']


# --- the report -----------------------------------------------------------

def test_report_carries_prices_slippage_spread_z_fees_and_net():
    runner, _, _ = make_runner(stats=(1.5, 1.0, 0.4, 1.25))
    detail = runner.run('LONG_SPR', 'MARKET')['detail']
    assert 'bid=' in detail and 'ask=' in detail and 'fill=' in detail
    assert 'Δtgt=' in detail and 'Δmid=' in detail
    assert 'spread@open: +1.5000 μ=+1.0000 σ=0.4000 z=+1.25' in detail
    assert 'spread@close' in detail
    # Commission is round-turn per lot: 6.0 spot + 4.0 futures at 0.01 lots
    assert 'fees: $0.060+$0.040=$0.100' in detail
    assert 'gross=$' in detail and 'net=$' in detail


def test_report_survives_a_coordinator_with_no_spread_stats_yet():
    runner, _, _ = make_runner(stats=None)
    detail = runner.run('BUY_SPOT', 'MARKET')['detail']
    assert 'spread@open' not in detail
    assert 'gross=$' in detail


def test_unknown_scenario_is_rejected_not_executed():
    runner, spot_leg, fut_leg = make_runner()
    out = runner.run('NONSENSE', 'MARKET')
    assert not out['success'] and 'Unknown scenario' in out['detail']
    assert not spot_leg.market_orders and not fut_leg.market_orders


def test_an_unavailable_symbol_stops_before_ordering():
    spot_leg = ScenarioFakeLeg('account_a')
    spot_leg.ensure_symbol = lambda symbol: {'ok': False,
                                             'error': 'XAUUSD not found'}
    runner, spot_leg, _ = make_runner(spot_leg=spot_leg)
    out = runner.run('BUY_SPOT', 'MARKET')
    assert not out['success'] and 'not found' in out['detail']
    assert not spot_leg.market_orders


# --- coordinator wiring ---------------------------------------------------

@pytest.fixture
def coordinator(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(config, trading_mode='PAPER')
    coord.spot_leg = ScenarioFakeLeg('account_a', price=3300.0)
    coord.futures_leg = ScenarioFakeLeg('account_b', price=3320.0)
    coord.spot_leg.ping = lambda: True
    coord.futures_leg.ping = lambda: True
    coord.active_assets['GOLD'] = {
        'config': config.ASSETS['GOLD'], 'spot_symbol': 'XAUUSD',
        'futures_symbol': 'GC1225', 'last_data': None}
    coord.algo_enabled = False          # scenarios need the algo stopped
    coord.control_path = str(tmp_path / "control.json")
    return coord


def request_scenario(coordinator, **spec):
    payload = {'algo_enabled': coordinator.algo_enabled,
               'scenario': dict({'type': 'BUY_SPOT', 'mode': 'MARKET',
                                 'variant': 'normal', 'ts': 1.0}, **spec)}
    with open(coordinator.control_path, 'w') as f:
        json.dump(payload, f)
    coordinator._control_mtime = 0
    coordinator._read_control()
    return coordinator._scenario_result


def test_control_file_request_runs_the_scenario(coordinator):
    result = request_scenario(coordinator)
    assert result['success'] and result['ts'] == 1.0
    assert coordinator.spot_leg.market_orders == [('XAUUSD', 'BUY', 0.01)]


def test_the_result_reaches_runtime_status_immediately(coordinator, tmp_path):
    request_scenario(coordinator)
    with open(tmp_path / "runtime_status.json") as f:
        status = json.load(f)
    assert status['scenario_result']['success'] is True


def test_scenarios_are_refused_while_the_algo_is_running(coordinator):
    coordinator.algo_enabled = True
    result = request_scenario(coordinator)
    assert not result['success'] and 'Stop the algo' in result['detail']
    assert not coordinator.spot_leg.market_orders


def test_scenarios_are_refused_with_a_position_open(coordinator):
    from statarb.models import OrderSide, Position, SignalType, Trade
    position = Position('POS_0001', 'GOLD', SignalType.SELL_BASIS,
                        Trade('XAUUSD', OrderSide.BUY, 50.0),
                        Trade('GC1225', OrderSide.SELL, 50.0))
    coordinator.position_manager.positions['POS_0001'] = position
    result = request_scenario(coordinator)
    assert not result['success'] and 'flat book' in result['detail']
    assert not coordinator.spot_leg.market_orders


def test_a_disconnected_leg_is_reported_before_any_order(coordinator):
    coordinator.futures_leg.ping = lambda: False
    result = request_scenario(coordinator, type='LONG_SPR')
    assert not result['success'] and 'not connected' in result['detail']
    assert not coordinator.spot_leg.market_orders


def test_the_same_request_does_not_run_twice(coordinator):
    request_scenario(coordinator)
    coordinator._control_mtime = 0
    coordinator._read_control()          # same ts — already handled
    assert len(coordinator.spot_leg.market_orders) == 1


def test_commission_and_contract_size_come_from_config(coordinator):
    coordinator.config.COSTS['COMMISSION_PER_LOT_SPOT'] = 6.0
    spot, futures, error = coordinator._scenario_legs('GOLD')
    assert error is None
    assert spot.symbol == 'XAUUSD' and futures.symbol == 'GC1225'
    assert spot.commission_per_lot == 6.0
    assert spot.contract_size == coordinator.config.ASSETS['GOLD']['lot_size']


# --- the API the Exchanges page calls -------------------------------------

pytest.importorskip("flask")

from statarb.webapp import create_app                    # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        'accounts': {'account_a': {'login': 111},
                     'account_b': {'login': 222}},
        'leg_accounts': {'spot': 'account_a', 'futures': 'account_b'}}))
    (tmp_path / "runtime_status.json").write_text(json.dumps({}))
    app = create_app(db_path=str(tmp_path / "algo.db"),
                     status_path=str(tmp_path / "runtime_status.json"),
                     config_path=str(tmp_path / "config.json"),
                     control_path=str(tmp_path / "control.json"),
                     env_path=str(tmp_path / ".env"),
                     scenario_timeout=1.0)
    app.config['TESTING'] = True
    client = app.test_client()
    client.tmp_path = tmp_path
    return client


def test_catalogue_endpoint_feeds_the_table(client):
    data = client.get('/api/scenario-catalogue').get_json()
    assert len(data['scenarios']) == 40
    assert data['spacing_sec']['LIMIT'] > data['spacing_sec']['MARKET']


def test_posting_a_scenario_queues_it_for_the_coordinator(client):
    """The web app never touches MT5 — it writes the request and waits
    for the coordinator to publish the outcome."""
    tmp_path = client.tmp_path

    def fake_coordinator():
        for _ in range(60):
            try:
                with open(tmp_path / "control.json") as f:
                    spec = (json.load(f).get('scenario') or {})
            except (OSError, ValueError):
                spec = {}
            if spec.get('ts'):
                with open(tmp_path / "runtime_status.json", 'w') as f:
                    json.dump({'scenario_result': {
                        'ts': spec['ts'], 'success': True,
                        'detail': 'round trip ok'}}, f)
                return
            time.sleep(0.05)

    worker = threading.Thread(target=fake_coordinator, daemon=True)
    worker.start()
    data = client.post('/api/scenario-test',
                       json={'type': 'LONG_SPR', 'mode': 'MARKET',
                             'variant': 'normal'}).get_json()
    worker.join(timeout=5)
    assert data['success'] and data['detail'] == 'round trip ok'
    with open(tmp_path / "control.json") as f:
        assert json.load(f)['scenario']['type'] == 'LONG_SPR'


def test_a_silent_coordinator_times_out_instead_of_hanging(client):
    data = client.post('/api/scenario-test', json={'type': 'BUY_SPOT'}
                       ).get_json()
    assert not data['success']
    assert 'No answer from the coordinator' in data['detail']


def test_a_scenario_with_no_type_is_rejected(client):
    resp = client.post('/api/scenario-test', json={})
    assert resp.status_code == 400


def test_stale_results_are_not_mistaken_for_this_run(client):
    """A result from an earlier scenario must not be handed back as
    the answer to this one."""
    with open(client.tmp_path / "runtime_status.json", 'w') as f:
        json.dump({'scenario_result': {'ts': 1.0, 'success': True,
                                       'detail': 'old news'}}, f)
    data = client.post('/api/scenario-test', json={'type': 'BUY_SPOT'}
                       ).get_json()
    assert not data['success'] and 'old news' not in data['detail']


# --- the page the operator opens ------------------------------------------

def test_exchanges_page_renders_the_suite(client):
    page = client.get('/setup').get_data(as_text=True)
    assert 'Full Order Test Suite' in page
    assert 'Run Full Test Suite' in page
    assert '/api/scenario-catalogue' in page
    assert '/api/scenario-test' in page
    assert 'REAL orders' in page


# --- the broker's book, not just the engine's ----------------------------
# Live 2026-08-06: leaked fills left positions the ENGINE did not know
# about, so the flat-book precondition passed and every further run
# added to the pile — eleven orphans, all reporting PASS.

def test_scenarios_refuse_to_run_on_top_of_stranded_positions(coordinator):
    coordinator.spot_leg.live_positions = [
        {'ticket': 102269437, 'symbol': 'XAUUSD_', 'side': 'BUY',
         'volume': 0.01, 'price_open': 4259.9},
        {'ticket': 102269457, 'symbol': 'GC1226', 'side': 'SELL',
         'volume': 0.10, 'price_open': 4318.0}]
    result = request_scenario(coordinator)
    assert not result['success']
    assert 'still open on the broker' in result['detail']
    assert '102269437' in result['detail'] and 'GC1226' in result['detail']
    assert not coordinator.spot_leg.market_orders


def test_a_flat_broker_book_lets_scenarios_run(coordinator):
    assert request_scenario(coordinator)['success']


def test_an_unreadable_book_is_not_assumed_flat(coordinator):
    """None means 'could not read', not 'nothing there' — placing test
    orders blind is exactly how the pile started."""
    coordinator.spot_leg.positions = lambda symbol=None: None
    result = request_scenario(coordinator)
    assert not result['success']
    assert 'refusing to place test orders blind' in result['detail']
    assert not coordinator.spot_leg.market_orders


# --- a leaked fill must be closed on the side it was OPENED --------------
# Live 2026-08-06:
#   [SPOT @ ...] cancel FAILED: order filled 0.01 before the cancel landed
#   [SPOT SPOT @ ...] leak cleanup FAILED: 'SPOT' is not a valid OrderSide
#   Reconcile: orphan position ticket 102279299 BUY 0.01 XAUUSD_
# The cleanup passed the leg's ROLE where a side belonged, so the close
# raised inside the leg runner and the leaked position stayed open.

def test_a_leaked_fill_is_closed_with_the_orders_own_side():
    runner, spot_leg, _ = make_runner()
    spot_leg.leak_on_cancel['XAUUSD'] = 0.01
    runner.run('BUY_SPOT', 'LIMIT', 'cancel')

    assert spot_leg.closes, 'the leaked position was never closed'
    symbol, ticket, volume, entry_side = spot_leg.closes[-1][:4]
    assert entry_side == 'BUY'          # not 'SPOT'
    assert volume == 0.01


def test_a_leaked_sell_is_closed_as_a_sell():
    runner, spot_leg, _ = make_runner()
    spot_leg.leak_on_cancel['XAUUSD'] = 0.01
    runner.run('SELL_SPOT', 'LIMIT', 'cancel')
    assert spot_leg.closes[-1][3] == 'SELL'


def test_the_side_passed_to_the_leg_is_always_a_real_order_side():
    """OrderSide('SPOT') is what raised inside the leg runner."""
    from statarb.models import OrderSide
    runner, spot_leg, fut_leg = make_runner()
    spot_leg.leak_on_cancel['XAUUSD'] = 0.01
    fut_leg.leak_on_cancel['GC1225'] = 0.01
    for s_type in ('BUY_SPOT', 'SELL_SPOT', 'BUY_FUT', 'SELL_FUT'):
        runner.run(s_type, 'LIMIT', 'cancel')
    for leg in (spot_leg, fut_leg):
        for call in leg.closes:
            OrderSide(call[3])          # raises if a role leaked through


def test_a_leaked_fill_still_fails_the_scenario():
    """Cleaning up is not passing — a fill that beat the cancel means
    the scenario did not do what it set out to do."""
    runner, spot_leg, _ = make_runner()
    spot_leg.leak_on_cancel['XAUUSD'] = 0.01
    assert not runner.run('BUY_SPOT', 'LIMIT', 'cancel')['success']


# --- a close is logged as the order that was actually sent ---------------
# Live 2026-08-06: "[FUTURES SELL ...] leak cleanup ... fill=$4302.02"
# where 4302.02 was the ASK. Only a BUY pays the ask; the line was
# labelled with the side the position had been OPENED on.

def test_closing_a_long_is_logged_as_a_sell():
    runner, spot_leg, _ = make_runner()
    runner.run('BUY_SPOT', 'MARKET', 'normal')
    closes = [a for a in runner.actions if a['kind'] == 'close']
    assert closes and closes[-1]['side'] == 'SELL'
    assert closes[-1]['entry_side'] == 'BUY'
    assert 'SELL' in closes[-1]['leg_label']


def test_closing_a_short_is_logged_as_a_buy():
    runner, spot_leg, _ = make_runner()
    runner.run('SELL_SPOT', 'MARKET', 'normal')
    closes = [a for a in runner.actions if a['kind'] == 'close']
    assert closes and closes[-1]['side'] == 'BUY'
    assert closes[-1]['entry_side'] == 'SELL'


def test_the_leg_still_receives_the_entry_side():
    """MT5 needs the side the POSITION is on to reverse it — only the
    label changed."""
    runner, spot_leg, _ = make_runner()
    runner.run('BUY_SPOT', 'MARKET', 'normal')
    assert spot_leg.closes[-1][3] == 'BUY'


def test_a_leaked_fill_cleanup_is_labelled_by_its_own_direction():
    runner, spot_leg, _ = make_runner()
    spot_leg.leak_on_cancel['XAUUSD'] = 0.01
    runner.run('BUY_SPOT', 'LIMIT', 'cancel')
    cleanup = [a for a in runner.actions if a['kind'] == 'leak cleanup'][-1]
    assert cleanup['side'] == 'SELL' and cleanup['entry_side'] == 'BUY'


# --- a spread scenario must actually be hedged ---------------------------
# Live 2026-08-06: CFI's spot minimum is 0.01 (1 oz) and its futures
# minimum is 0.1 (10 oz). Taking each leg's own minimum made "LONG_SPR"
# 1 oz long against 10 oz short — 9 oz net directional, and a reported
# cost that was ~94% one leg.

def test_a_spread_scenario_carries_matched_notional():
    runner, spot_leg, fut_leg = make_runner()
    spot_leg.volume_min, fut_leg.volume_min = 0.01, 0.1
    runner.spot.meta = runner.futures.meta = None      # re-read specs
    spot_lots, fut_lots = runner.pair_volumes()
    assert spot_lots == pytest.approx(0.1)
    assert fut_lots == pytest.approx(0.1)


def test_the_hedge_ratio_scales_leg_b():
    runner, spot_leg, fut_leg = make_runner()
    runner.hedge_ratio = 2.0
    spot_lots, fut_lots = runner.pair_volumes()
    assert fut_lots == pytest.approx(spot_lots * 2.0)


def test_neither_leg_is_sized_below_its_minimum():
    runner, spot_leg, fut_leg = make_runner()
    spot_leg.volume_min, fut_leg.volume_min = 0.05, 0.1
    runner.spot.meta = runner.futures.meta = None
    spot_lots, fut_lots = runner.pair_volumes()
    assert spot_lots >= 0.05 and fut_lots >= 0.1


def test_the_spread_scenario_sends_the_matched_volumes():
    runner, spot_leg, fut_leg = make_runner()
    spot_leg.volume_min, fut_leg.volume_min = 0.01, 0.1
    runner.spot.meta = runner.futures.meta = None
    runner.run('LONG_SPR', 'MARKET', 'normal')
    opens = [a for a in runner.actions if a['kind'] == 'open']
    assert len(opens) == 2
    assert opens[0]['volume'] == pytest.approx(opens[1]['volume'])


def test_single_leg_scenarios_still_use_that_legs_minimum():
    """A one-leg test should stay as small as the broker allows."""
    runner, spot_leg, _ = make_runner()
    spot_leg.volume_min = 0.01
    runner.spot.meta = None
    runner.run('BUY_SPOT', 'MARKET', 'normal')
    opens = [a for a in runner.actions if a['kind'] == 'open']
    assert opens[0]['volume'] == pytest.approx(0.01)
