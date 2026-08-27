"""Proof that an order reached MT5.

`order_send` returning success is only the broker acknowledging the
request. These tests cover the read-back: after placing, the engine
asks MT5 what IT holds for that ticket, says so in the log, and pulls
the terminal's own rows into the Exchange Order Log immediately.
"""

import json
import time
from types import SimpleNamespace

import pytest

from statarb import broker as broker_module
from statarb import scenarios
from tests.test_limit_execution import FakeClock
from tests.test_scenarios import ScenarioFakeLeg


# --- BrokerSession.verify_ticket -----------------------------------------

class VerifyMT5:
    def __init__(self, positions=(), deals=(), orders=(), deals_after=0):
        self._positions = list(positions)
        self._deals = list(deals)
        self._orders = list(orders)
        self.deals_after = deals_after      # reads before history shows up
        self.deal_reads = 0

    def positions_get(self, ticket=None):
        return [p for p in self._positions if p.ticket == ticket]

    def history_deals_get(self, position=None, ticket=None):
        self.deal_reads += 1
        if self.deal_reads <= self.deals_after:
            return ()
        key = position if position is not None else ticket
        return [d for d in self._deals if d.position_id == key]

    def history_orders_get(self, ticket=None):
        return [o for o in self._orders if o.ticket == ticket]


def a_deal(**kwargs):
    base = dict(ticket=5001, order=4001, symbol='XAUUSD', volume=0.01,
                price=3300.5, commission=-0.07, profit=0.0,
                time=1_760_000_000, position_id=7001, comment='SCENARIO MKT')
    base.update(kwargs)
    return SimpleNamespace(**base)


def a_position(**kwargs):
    base = dict(ticket=7001, symbol='XAUUSD', volume=0.01, price_open=3300.5,
                time=1_760_000_000, magic=broker_module.MAGIC_NUMBER,
                comment='SCENARIO MKT')
    base.update(kwargs)
    return SimpleNamespace(**base)


def session():
    return broker_module.BrokerSession(
        SimpleNamespace(name='account_a', terminal_path=None, login=1,
                        server='FxPro', password_env=None))


@pytest.fixture
def mt5(monkeypatch):
    def install(fake):
        monkeypatch.setattr(broker_module, 'mt5', fake)
        return fake
    return install


def test_a_filled_order_is_confirmed_from_deal_history(mt5):
    mt5(VerifyMT5(deals=[a_deal()]))
    found = session().verify_ticket(7001)
    assert found['confirmed'] and found['source'] == 'deal history'
    assert found['deals'][0]['deal_id'] == 5001
    assert found['deals'][0]['order_id'] == 4001
    assert found['price'] == 3300.5


def test_an_open_position_is_confirmation_too(mt5):
    mt5(VerifyMT5(positions=[a_position()]))
    found = session().verify_ticket(7001)
    assert found['confirmed'] and found['position_open'] is True
    assert found['comment'] == 'SCENARIO MKT'


def test_history_lag_is_retried_before_being_believed(mt5):
    """Deal history appears a moment after the fill — a single empty
    read must not be reported as 'the order never reached MT5'."""
    fake = mt5(VerifyMT5(deals=[a_deal()], deals_after=2))
    found = session().verify_ticket(7001, attempts=4, delay=0.01)
    assert found['confirmed'] and fake.deal_reads > 2


def test_a_ticket_mt5_has_never_heard_of_is_not_confirmed(mt5):
    mt5(VerifyMT5())
    found = session().verify_ticket(9999, attempts=2, delay=0.01)
    assert not found['confirmed'] and 'not found' in found['error']


def test_verification_without_the_mt5_package_says_so(monkeypatch):
    monkeypatch.setattr(broker_module, 'mt5', None)
    found = session().verify_ticket(7001)
    assert not found['confirmed'] and 'not installed' in found['error']


# --- scenarios confirm every ticket ---------------------------------------

class VerifyingLeg(ScenarioFakeLeg):
    """Fake leg that can answer 'what does MT5 hold for this ticket'."""

    def __init__(self, *args, confirm=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.confirm = confirm
        self.verified = []

    def verify_order(self, ticket):
        self.verified.append(ticket)
        if not self.confirm:
            return {'ticket': ticket, 'confirmed': False,
                    'error': 'not found in MT5 positions, deals or orders'}
        return {'ticket': ticket, 'confirmed': True, 'source': 'deal history',
                'deals': [{'deal_id': 5000 + ticket, 'order_id': 4000 + ticket,
                           'symbol': 'XAUUSD', 'volume': 0.01,
                           'price': self.price, 'commission': -0.07,
                           'profit': 0.0, 'time': 1_760_000_000,
                           'comment': 'SCENARIO'}],
                'position_open': False}


def runner(spot_leg=None, futures_leg=None):
    spot_leg = spot_leg or VerifyingLeg('account_a', price=3300.0)
    futures_leg = futures_leg or VerifyingLeg('account_b', price=3320.0)
    spot = scenarios.Leg(spot_leg, 'XAUUSD', 'SPOT', 100.0, 6.0)
    futures = scenarios.Leg(futures_leg, 'GC1225', 'FUTURES', 100.0, 4.0)
    clock = FakeClock()
    return (scenarios.ScenarioRunner(spot, futures, clock=clock,
                                     sleep=clock.sleep),
            spot_leg, futures_leg)


def test_every_scenario_ticket_is_read_back_out_of_mt5():
    run, spot_leg, fut_leg = runner()
    out = run.run('LONG_SPR', 'MARKET')
    assert out['success']
    # Two opens and two closes, each confirmed against the terminal
    assert len(spot_leg.verified) == 2 and len(fut_leg.verified) == 2
    assert out['detail'].count('✓ MT5 confirms') == 4
    assert 'deal 5' in out['detail'] and 'commission -0.07' in out['detail']


def test_an_order_mt5_cannot_confirm_fails_the_scenario():
    """The dangerous case: our side thinks it placed an order, the
    terminal has no record. That must never read as a pass."""
    run, spot_leg, _ = runner(spot_leg=VerifyingLeg('account_a',
                                                    confirm=False))
    out = run.run('BUY_SPOT', 'MARKET')
    assert not out['success']
    assert '✗ NOT FOUND IN MT5' in out['detail']


def test_limit_fills_are_confirmed_too():
    spot_leg = VerifyingLeg('account_a', limit_fill_polls={'XAUUSD': 2})
    run, spot_leg, _ = runner(spot_leg=spot_leg)
    out = run.run('BUY_SPOT', 'LIMIT')
    assert out['success'] and out['detail'].count('✓ MT5 confirms') == 2


def test_a_leg_that_cannot_verify_still_runs_the_scenario():
    """Paper fakes and older leg runners have no verify_order — the
    scenario runs, it just does not claim confirmation."""
    run, _, _ = runner(spot_leg=ScenarioFakeLeg('account_a'),
                       futures_leg=ScenarioFakeLeg('account_b'))
    out = run.run('BUY_SPOT', 'MARKET')
    assert out['success'] and 'MT5 confirms' not in out['detail']


def test_a_verifier_that_raises_does_not_break_the_scenario():
    class Boom(ScenarioFakeLeg):
        def verify_order(self, ticket):
            raise RuntimeError('terminal gone')

    run, _, _ = runner(spot_leg=Boom('account_a'))
    out = run.run('BUY_SPOT', 'MARKET')
    assert not out['success'] and 'terminal gone' in out['detail']


# --- the coordinator confirms live entries and exits ----------------------

@pytest.fixture
def coordinator(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    config.TRADING.update({'CLIP_LOTS': 1.0, 'SLICE_LOTS': 1.0})
    coord = Coordinator(config, trading_mode='PAPER')
    coord.spot_leg = VerifyingLeg('account_a', price=3300.0)
    coord.futures_leg = VerifyingLeg('account_b', price=3320.0)
    coord.spot_leg.broker_order_log = [
        {'account': 'account_a', 'order_id': '4001', 'deal_id': '5001',
         'symbol': 'XAUUSD', 'state': 'filled', 'is_bot': True,
         'comment': 'MANUAL_ab12', 'filled_at': 1_760_000_000_000}]
    coord.spot_leg.order_log = lambda hours=24: [
        dict(r) for r in coord.spot_leg.broker_order_log]
    coord.futures_leg.order_log = lambda hours=24: []
    coord.active_assets['GOLD'] = {
        'config': config.ASSETS['GOLD'], 'spot_symbol': 'XAUUSD',
        'futures_symbol': 'GC1225', 'last_data': None}
    return coord


def make_position(asset='GOLD'):
    from statarb.models import OrderSide, Position, SignalType, Trade
    spot = Trade('XAUUSD', OrderSide.BUY, 1.0)
    spot.position_tickets = [7001]
    fut = Trade('GC1225', OrderSide.SELL, 1.0)
    fut.position_tickets = [7002]
    return Position('POS_0001', asset, SignalType.SELL_BASIS, spot, fut)


def test_entry_tickets_are_confirmed_against_both_terminals(coordinator,
                                                            caplog):
    with caplog.at_level('INFO'):
        found = coordinator._confirm_with_mt5(make_position(), 'entry')
    assert [f['confirmed'] for f in found] == [True, True]
    assert 'MT5 CONFIRMED' in caplog.text
    assert coordinator._last_confirmation['confirmed'] == 2
    assert coordinator._last_confirmation['total'] == 2


def test_an_unconfirmed_ticket_is_logged_as_an_error(coordinator, caplog):
    coordinator.futures_leg.confirm = False
    with caplog.at_level('ERROR'):
        coordinator._confirm_with_mt5(make_position(), 'entry')
    assert 'MT5 NOT CONFIRMED' in caplog.text
    assert 'terminal has no record' in caplog.text
    assert coordinator._last_confirmation['confirmed'] == 1


def test_confirming_pulls_the_terminals_rows_into_the_log_at_once(
        coordinator):
    """No waiting for the 30s poll: after an order, the Exchange Order
    Log has the broker's own rows."""
    coordinator._confirm_with_mt5(make_position(), 'entry')
    rows = coordinator.data_logger.recent_broker_orders()
    assert [r['order_id'] for r in rows] == ['4001']
    assert rows[0]['comment'] == 'MANUAL_ab12'


def test_the_confirmation_reaches_runtime_status(coordinator, tmp_path):
    coordinator._confirm_with_mt5(make_position(), 'entry')
    coordinator._write_runtime_status({})
    with open(tmp_path / "runtime_status.json") as f:
        published = json.load(f)['order_confirmation']
    assert published['confirmed'] == 2 and published['what'] == 'entry'


def test_paper_mode_does_not_claim_mt5_confirmations(coordinator):
    """Paper fills never reach a terminal — confirming them would be a
    lie. _open_position only verifies in LIVE."""
    from statarb.coordinator import PaperExecutor
    coordinator.executor = PaperExecutor(coordinator.spot_leg,
                                         coordinator.futures_leg)
    coordinator.stats['GOLD'] = None
    market = {'spread': 1.5, 'spot_price': 3300.0,
              'futures_price': 3320.0, 'actual_basis': 20.0,
              'basis_pct': 5.0, 'spot_bid': 3299.9,
              'spot_ask': 3300.1, 'futures_bid': 3319.9,
              'futures_ask': 3320.1}
    from statarb.models import SignalType
    position = coordinator._open_position(
        'GOLD', SignalType.SELL_BASIS, 1.0, market, None, 100.0, manual=True)
    assert position is not None            # the trade really did open
    assert coordinator._last_confirmation is None


# --- the log says where each order came from ------------------------------

pytest.importorskip("flask")

from statarb.database import DataLogger                  # noqa: E402
from statarb.webapp import create_app                    # noqa: E402


def row(**kwargs):
    base = {'account': 'account_a', 'order_id': '4001', 'deal_id': '5001',
            'symbol': 'XAUUSD', 'inst_type': 'DEAL', 'side': 'buy',
            'pos_side': 'open', 'order_type': 'market/limit',
            'quantity': 0.01, 'fill_qty': 0.01, 'fill_price': 3300.5,
            'fee': -0.07, 'fee_ccy': 'USD', 'pnl': 0.0, 'state': 'filled',
            'filled_at': 1_760_000_000_000, 'position_id': 7001,
            'is_bot': True, 'comment': 'BASIS_ARB_ab12'}
    base.update(kwargs)
    return base


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = DataLogger(db_path=str(tmp_path / "algo.db"))
    db.record_broker_orders([
        row(order_id='1', deal_id='1', comment='BASIS_ARB_ab12'),
        row(order_id='2', deal_id='2', comment='MANUAL_cd34'),
        row(order_id='3', deal_id='3', comment='SCENARIO MKT spr/spot'),
        row(order_id='4', deal_id='4', comment='ORDER_TEST'),
        row(order_id='5', deal_id='5', comment='my own click', is_bot=False),
        row(order_id='6', deal_id='6', comment='BASIS_ARB_ORPHAN'),
        row(order_id='7', deal_id='7', comment='MANUAL_CX_ef56'),
        row(order_id='8', deal_id='8', comment='something else'),
    ])
    (tmp_path / "runtime_status.json").write_text(json.dumps({}))
    (tmp_path / "config.json").write_text(json.dumps({}))
    app = create_app(db_path=str(tmp_path / "algo.db"),
                     status_path=str(tmp_path / "runtime_status.json"),
                     config_path=str(tmp_path / "config.json"),
                     control_path=str(tmp_path / "control.json"),
                     env_path=str(tmp_path / ".env"))
    app.config['TESTING'] = True
    client = app.test_client()
    client.tmp_path = tmp_path
    return client


def test_every_row_says_where_the_order_came_from(client):
    data = client.get('/api/exchange-orders').get_json()
    by_id = {o['order_id']: o['source'] for o in data['orders']}
    assert by_id == {'1': 'STRATEGY', '2': 'MANUAL TRADE',
                     '3': 'TEST SUITE', '4': 'ORDER TEST',
                     '5': 'MANUAL (terminal)',
                     # Cleanup after a failed close is not a decision the
                     # strategy made — it fires precisely when something
                     # went wrong. Four such rows read ALGO while the
                     # algo had been stopped for an hour (2026-08-27).
                     '6': 'RECONCILE',
                     # A hand-placed trade's EXIT used to be tagged
                     # BASIS_ARB_CX like every other close, so one trade
                     # showed MANUAL going in and ALGO coming out.
                     '7': 'MANUAL TRADE',
                     # Our magic, a comment we do not recognise. Saying
                     # ALGO there is a claim the column cannot support.
                     '8': 'BOT (unknown)'}
    assert 'TEST SUITE' in data['sources']


def test_the_log_can_be_filtered_to_one_source(client):
    data = client.get('/api/exchange-orders?source=TEST SUITE').get_json()
    assert data['count'] == 1 and data['orders'][0]['order_id'] == '3'


def test_the_csv_carries_the_source_column(client):
    body = client.get('/api/exchange-orders/csv').get_data(as_text=True)
    assert 'source' in body.splitlines()[0]
    assert 'TEST SUITE' in body and 'MANUAL TRADE' in body


def test_the_dashboard_table_shows_the_source(client):
    page = client.get('/').get_data(as_text=True)
    log = page.split('Exchange Order Log', 1)[1]
    assert '<th>Source</th>' in log.split('</thead>', 1)[0]
    assert 'SOURCE_STYLE' in page


# --- filling mode: the broker decides, not us ----------------------------
# Live on CFI7-Demo: every close came back "10030 - Unsupported filling
# mode" because the market paths hardcoded IOC. The engine could open
# positions and never exit them.

class FillingMT5:
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = 10009
    POSITION_TYPE_BUY = 0

    def __init__(self, allowed_mask, accepts):
        self.allowed_mask = allowed_mask
        self.accepts = accepts          # the ONE mode the broker takes
        self.attempts = []

    def symbol_info(self, symbol):
        return SimpleNamespace(filling_mode=self.allowed_mask, point=0.01,
                               digits=2, visible=True)

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=3300.0, ask=3300.2, last=3300.1,
                               time=1_760_000_000)

    def positions_get(self, **kwargs):
        return ()

    def history_deals_get(self, *args, **kwargs):
        return ()

    def last_error(self):
        return (0, 'ok')

    def order_send(self, request):
        mode = request['type_filling']
        self.attempts.append(mode)
        if mode != self.accepts:
            return SimpleNamespace(retcode=10030,
                                   comment='Unsupported filling mode',
                                   order=0, price=0.0, volume=0.0)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE,
                               comment='Done', order=555,
                               price=request['price'],
                               volume=request['volume'])


def test_a_close_retries_until_the_broker_accepts_a_filling_mode(mt5):
    """FOK-only broker: the close must still go through."""
    from statarb.models import OrderSide
    fake = mt5(FillingMT5(allowed_mask=1,
                          accepts=FillingMT5.ORDER_FILLING_FOK))
    result = session().close_position_ticket('XAUUSD_', 102269341, 0.01,
                                             OrderSide.BUY)
    assert result.success
    assert FillingMT5.ORDER_FILLING_FOK in fake.attempts


def test_an_ioc_broker_is_served_first_since_it_allows_partials(mt5):
    from statarb.models import OrderSide
    fake = mt5(FillingMT5(allowed_mask=2,
                          accepts=FillingMT5.ORDER_FILLING_IOC))
    assert session().close_position_ticket('XAUUSD_', 1, 0.01,
                                           OrderSide.BUY).success
    assert fake.attempts[0] == FillingMT5.ORDER_FILLING_IOC


def test_market_orders_use_the_brokers_mode_too(mt5):
    from statarb.models import OrderSide
    fake = mt5(FillingMT5(allowed_mask=1,
                          accepts=FillingMT5.ORDER_FILLING_FOK))
    result = session().send_market_order('XAUUSD_', OrderSide.BUY, 0.01)
    assert result.success and result.ticket == 555
    assert FillingMT5.ORDER_FILLING_FOK in fake.attempts


def test_a_symbol_declaring_nothing_still_gets_all_three_tried(mt5):
    from statarb.models import OrderSide
    fake = mt5(FillingMT5(allowed_mask=0,
                          accepts=FillingMT5.ORDER_FILLING_RETURN))
    assert session().send_market_order('XAUUSD_', OrderSide.BUY, 0.01).success
    assert len(fake.attempts) == 3


def test_a_genuine_rejection_is_not_retried_forever(mt5):
    """Only 10030 means 'wrong filling mode'. Anything else — no money,
    market closed — must surface immediately."""
    from statarb.models import OrderSide

    class Rejects(FillingMT5):
        def order_send(self, request):
            self.attempts.append(request['type_filling'])
            return SimpleNamespace(retcode=10019, comment='No money',
                                   order=0, price=0.0, volume=0.0)

    fake = mt5(Rejects(allowed_mask=3, accepts=None))
    result = session().send_market_order('XAUUSD_', OrderSide.BUY, 0.01)
    assert not result.success and 'No money' in result.error
    assert len(fake.attempts) == 1


def test_exhausting_every_mode_says_what_the_broker_allows(mt5):
    from statarb.models import OrderSide
    mt5(FillingMT5(allowed_mask=1, accepts=None))
    result = session().close_position_ticket('XAUUSD_', 1, 0.01,
                                             OrderSide.BUY)
    assert not result.success
    assert 'tried every filling mode' in result.error
    assert 'broker allows FOK' in result.error


# --- a cancel that did not really cancel ---------------------------------
# Live on CFI: a scenario reported "cancelled (no fill in 15s)" and
# PASS; eleven seconds later the reconciler found a live position with
# that same ticket. MT5 turns a filled pending order into a POSITION
# carrying the order's ticket, and positions_get shows it before the
# deal history does.

class LeakyCancelMT5:
    TRADE_ACTION_REMOVE = 8
    TRADE_RETCODE_DONE = 10009

    def __init__(self, position=None):
        self.position = position

    def order_send(self, request):
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE,
                               comment='Done', order=request.get('order', 0),
                               price=0.0, volume=0.0)

    def history_deals_get(self, *args, **kwargs):
        return ()                       # history has not caught up

    def orders_get(self, ticket=None):
        return ()                       # no longer resting

    def positions_get(self, ticket=None):
        if self.position and self.position.ticket == ticket:
            return (self.position,)
        return ()

    def last_error(self):
        return (0, 'ok')


def test_a_cancel_that_actually_filled_is_reported_as_filled(mt5):
    mt5(LeakyCancelMT5(position=SimpleNamespace(
        ticket=102269437, volume=0.01, price_open=4259.915,
        symbol='XAUUSD_')))
    state = session().cancel_pending(102269437)
    assert state['filled_volume'] == 0.01
    assert state['position_tickets'] == [102269437]
    assert state['leaked_fill'] is True
    assert state['price'] == 4259.915


def test_a_genuinely_cancelled_order_reports_no_fill(mt5):
    mt5(LeakyCancelMT5(position=None))
    state = session().cancel_pending(102269440)
    assert state['filled_volume'] == 0
    assert state['cancelled'] is True
    assert not state.get('leaked_fill')


class LeakyLeg(VerifyingLeg):
    """Cancel says 'clean', but the terminal has an open position."""

    def cancel_order(self, ticket):
        self.cancels.append(ticket)
        return {'ok': True, 'cancelled': True, 'filled_volume': 0.0,
                'price': None, 'position_tickets': [], 'still_open': False,
                'error': None}

    def verify_order(self, ticket):
        return {'ticket': ticket, 'confirmed': True, 'position_open': True,
                'volume': 0.01, 'price': self.price, 'deals': []}


def test_a_leaked_fill_is_flattened_and_never_passes_quietly():
    """This is the one that must never pass QUIETLY — it left a real
    naked position on the account. It may pass once the position is
    flattened and MT5 confirms it, but the leak stays on the report."""
    leg = LeakyLeg('account_a', limit_fill_polls={'XAUUSD': None})
    run, leg, _ = runner(spot_leg=leg)
    out = run.run('BUY_SPOT', 'LIMIT', variant='cancel')
    assert 'before the cancel landed' in out['detail']
    assert leg.closed_tickets, 'the leaked position must be closed'
    assert 'leak cleanup' in out['detail']


def test_the_placement_is_logged_once_not_twice():
    """The report printed the same 'place @' line twice on a no-fill."""
    leg = VerifyingLeg('account_a', limit_fill_polls={'XAUUSD': None})
    run, _, _ = runner(spot_leg=leg)
    out = run.run('BUY_SPOT', 'LIMIT')
    assert out['detail'].count('place @') == 1
    assert 'no fill in 15s' in out['detail']


def test_a_leak_stays_flagged_now_that_the_fill_state_finds_it_first(mt5):
    """order_fill_state now reports a fill it can only see as a
    POSITION, so cancel_pending no longer reaches its own positions
    check. The leak must still be flagged: a cancel that failed to
    prevent a fill is a distinct event and has to stay visible in the
    scenario report rather than read as a normal fill."""
    mt5(LeakyCancelMT5(position=SimpleNamespace(
        ticket=102269437, volume=0.01, price_open=4259.915,
        symbol='XAUUSD_')))
    state = session().cancel_pending(102269437)
    assert state['filled_volume'] == 0.01
    assert state['leaked_fill'] is True
    assert state['from_position'] is True
