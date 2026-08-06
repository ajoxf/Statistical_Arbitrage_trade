"""Exchange Order Log: both MT5 accounts' order activity, end to end.

Path under test:
    BrokerSession.order_log  (MT5 deals/orders -> normalised rows)
      -> LocalLeg / RemoteLeg (stamps the account)
      -> LegServer 'order_log' command (remote accounts)
      -> Coordinator._poll_order_logs (both legs, throttled)
      -> DataLogger.broker_orders (dedup on re-poll)
      -> /api/exchange-orders (+ CSV) -> dashboard table

The point of the feature is that ONE table shows BOTH accounts, so
most of these assertions are about the account tag surviving the trip.
"""

import json
import os
import threading
from types import SimpleNamespace

import pytest

from statarb import broker as broker_module
from statarb.leg_runner import LegServer
from statarb.legs import LocalLeg, RemoteLeg


# --- a stand-in for the MetaTrader5 module --------------------------------

class FakeMT5:
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TYPE_BUY_STOP = 4
    ORDER_TYPE_SELL_STOP = 5
    ORDER_STATE_STARTED = 0
    ORDER_STATE_PLACED = 1
    ORDER_STATE_CANCELED = 2
    ORDER_STATE_PARTIAL = 3
    ORDER_STATE_FILLED = 4
    ORDER_STATE_REJECTED = 5
    ORDER_STATE_EXPIRED = 6

    def __init__(self, deals=(), history_orders=(), pending=()):
        self._deals = deals
        self._history_orders = history_orders
        self._pending = pending

    def history_deals_get(self, since, until):
        return self._deals

    def history_orders_get(self, since, until):
        return self._history_orders

    def orders_get(self):
        return self._pending


def deal(**kwargs):
    base = dict(ticket=5001, order=4001, symbol='XAUUSD', type=0, entry=0,
                volume=10.0, price=3300.5, commission=-7.0, swap=0.0,
                profit=0.0, time=1_760_000_000, position_id=7001,
                magic=broker_module.MAGIC_NUMBER, comment='BASIS_ARB_spot')
    base.update(kwargs)
    return SimpleNamespace(**base)


def hist_order(**kwargs):
    base = dict(ticket=4002, symbol='XAUUSD',
                type=FakeMT5.ORDER_TYPE_BUY_LIMIT,
                state=FakeMT5.ORDER_STATE_CANCELED, volume_initial=10.0,
                price_open=3299.0, time_setup=1_760_000_100,
                time_done=1_760_000_160, position_id=0,
                magic=broker_module.MAGIC_NUMBER, comment='BASIS_ARB_spot')
    base.update(kwargs)
    return SimpleNamespace(**base)


def pending_order(**kwargs):
    base = dict(ticket=4003, symbol='GC1225',
                type=FakeMT5.ORDER_TYPE_SELL_LIMIT, volume_initial=10.0,
                volume_current=10.0, price_open=3320.0,
                time_setup=1_760_000_200, position_id=0,
                magic=broker_module.MAGIC_NUMBER, comment='BASIS_ARB_fut')
    base.update(kwargs)
    return SimpleNamespace(**base)


@pytest.fixture
def fake_mt5(monkeypatch):
    def install(**kwargs):
        fake = FakeMT5(**kwargs)
        monkeypatch.setattr(broker_module, 'mt5', fake)
        return fake
    return install


def make_session():
    account = SimpleNamespace(name='account_a', terminal_path=None, login=1,
                              server='FxPro', password_env=None)
    return broker_module.BrokerSession(account)


# --- BrokerSession.order_log ----------------------------------------------

def test_order_log_is_empty_without_mt5(monkeypatch):
    monkeypatch.setattr(broker_module, 'mt5', None)
    assert make_session().order_log() == []


def test_filled_deal_is_normalised_for_the_table(fake_mt5):
    fake_mt5(deals=[deal()])
    row = make_session().order_log()[0]
    assert row['order_id'] == '4001' and row['deal_id'] == '5001'
    assert row['symbol'] == 'XAUUSD'
    assert row['side'] == 'buy' and row['pos_side'] == 'open'
    assert row['quantity'] == 10.0 and row['fill_qty'] == 10.0
    assert row['fill_price'] == 3300.5
    assert row['fee'] == -7.0            # commission + swap
    assert row['state'] == 'filled'
    assert row['filled_at'] == 1_760_000_000 * 1000    # ms for the UI
    assert row['is_bot'] is True


def test_closing_deal_is_tagged_close_with_its_pnl(fake_mt5):
    fake_mt5(deals=[deal(entry=FakeMT5.DEAL_ENTRY_OUT, type=1, profit=250.0,
                         swap=-3.0, commission=-7.0)])
    row = make_session().order_log()[0]
    assert row['pos_side'] == 'close' and row['side'] == 'sell'
    assert row['pnl'] == 250.0 and row['fee'] == -10.0


def test_manual_terminal_trades_show_up_but_are_not_flagged_as_ours(fake_mt5):
    """The operator wants the account's whole story, with our orders
    distinguishable from ones placed by hand in the terminal."""
    fake_mt5(deals=[deal(magic=0, comment='manual')])
    row = make_session().order_log()[0]
    assert row['is_bot'] is False


def test_balance_entries_are_not_orders(fake_mt5):
    fake_mt5(deals=[deal(type=2, symbol='')])       # DEAL_TYPE_BALANCE
    assert make_session().order_log() == []


def test_cancelled_and_rejected_orders_survive(fake_mt5):
    """A cancel or a rejection is exactly what you go to the log for —
    those orders never produce a deal."""
    fake_mt5(history_orders=[
        hist_order(),
        hist_order(ticket=4009, state=FakeMT5.ORDER_STATE_REJECTED),
    ])
    rows = make_session().order_log()
    assert {r['state'] for r in rows} == {'cancelled', 'rejected'}
    assert rows[0]['order_type'] == 'buy limit' and rows[0]['side'] == 'buy'
    assert rows[0]['fill_qty'] == 0.0


def test_filled_history_orders_are_left_to_their_deals(fake_mt5):
    fake_mt5(deals=[deal()],
             history_orders=[hist_order(ticket=4001,
                                        state=FakeMT5.ORDER_STATE_FILLED)])
    rows = make_session().order_log()
    assert len(rows) == 1 and rows[0]['deal_id'] == '5001'


def test_resting_orders_are_reported_as_working(fake_mt5):
    fake_mt5(pending=[pending_order()])
    row = make_session().order_log()[0]
    assert row['state'] == 'working' and row['inst_type'] == 'PENDING'
    assert row['side'] == 'sell' and row['order_type'] == 'sell limit'


def test_a_broken_history_read_does_not_raise(fake_mt5):
    fake = fake_mt5()
    fake.history_deals_get = lambda *a: (_ for _ in ()).throw(
        RuntimeError('terminal busy'))
    assert make_session().order_log() == []


# --- legs stamp the account -----------------------------------------------

def test_local_leg_stamps_its_account(fake_broker):
    fake_broker.broker_order_log = [{'order_id': '1', 'symbol': 'XAUUSD'}]
    leg = LocalLeg(fake_broker)
    assert leg.order_log()[0]['account'] == 'fake'


def test_remote_leg_round_trip_stamps_its_account(fake_broker):
    fake_broker.broker_order_log = [{'order_id': '1', 'symbol': 'GC1225'}]
    server = LegServer(fake_broker, '127.0.0.1', 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    leg = RemoteLeg('account_b', f'127.0.0.1:{server.port}')
    try:
        assert leg.connect(retries=3, delay=0.1)
        rows = leg.order_log(hours=6)
        assert len(rows) == 1 and rows[0]['account'] == 'account_b'
    finally:
        leg.close()
        server.stop()


def test_remote_leg_reports_unknown_not_empty_on_ipc_failure():
    leg = RemoteLeg('account_b', '127.0.0.1:9')     # never connected
    assert leg.order_log() is None


# --- storage: re-polling the same window must not duplicate ---------------

def row(account='account_a', order_id='4001', deal_id='5001', **kwargs):
    base = {'account': account, 'order_id': order_id, 'deal_id': deal_id,
            'symbol': 'XAUUSD', 'inst_type': 'DEAL', 'side': 'buy',
            'pos_side': 'open', 'order_type': 'market/limit',
            'quantity': 10.0, 'fill_qty': 10.0, 'fill_price': 3300.5,
            'fee': -7.0, 'fee_ccy': 'USD', 'pnl': 0.0, 'state': 'filled',
            'filled_at': 1_760_000_000_000, 'position_id': 7001,
            'is_bot': True, 'comment': ''}
    base.update(kwargs)
    return base


def test_repolling_updates_rows_instead_of_duplicating(data_logger):
    data_logger.record_broker_orders([row(state='working', fill_qty=0.0)])
    data_logger.record_broker_orders([row()])          # same order, now filled
    rows = data_logger.recent_broker_orders()
    assert len(rows) == 1 and rows[0]['state'] == 'filled'


def test_both_accounts_land_in_one_table_newest_first(data_logger):
    data_logger.record_broker_orders([
        row(account='account_a', filled_at=1_760_000_000_000),
        row(account='account_b', order_id='4100', deal_id='5100',
            symbol='GC1225', filled_at=1_760_000_500_000),
    ])
    rows = data_logger.recent_broker_orders()
    assert [r['account'] for r in rows] == ['account_b', 'account_a']
    assert data_logger.recent_broker_orders(account='account_a')[0][
        'symbol'] == 'XAUUSD'


def test_a_resting_order_that_fills_leaves_no_stale_working_row(data_logger):
    data_logger.record_broker_orders([
        row(order_id='4001', deal_id='', state='working', inst_type='PENDING',
            fill_qty=0.0)])
    # Next poll: it filled, so MT5 reports it as a deal and it is no
    # longer in the resting set.
    data_logger.record_broker_orders([row(order_id='4001', deal_id='5001')])
    rows = data_logger.recent_broker_orders()
    assert len(rows) == 1 and rows[0]['state'] == 'filled'


def test_an_unreadable_account_keeps_its_previous_rows(data_logger):
    data_logger.record_broker_orders([
        row(account='account_a', state='working', deal_id=''),
        row(account='account_b', order_id='4100', deal_id='',
            state='working')])
    # Only account_a could be read this pass
    data_logger.record_broker_orders([], accounts={'account_a'})
    remaining = data_logger.recent_broker_orders()
    assert [r['account'] for r in remaining] == ['account_b']


def test_same_order_id_on_two_accounts_stays_two_rows(data_logger):
    """Ticket numbers are per-terminal — two brokers can hand out the
    same one. Keying on the account keeps them apart."""
    data_logger.record_broker_orders([row(account='account_a'),
                                      row(account='account_b')])
    assert len(data_logger.recent_broker_orders()) == 2


# --- coordinator polling ---------------------------------------------------

class LogLeg:
    def __init__(self, name, rows):
        self.name = name
        self.rows = rows
        self.calls = 0

    def order_log(self, hours=24):
        self.calls += 1
        if self.rows is None:
            return None
        return [dict(r, account=self.name) for r in self.rows]


@pytest.fixture
def coordinator(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(config, trading_mode='PAPER')
    return coord


def test_poll_collects_every_leg_and_throttles(coordinator):
    coordinator.spot_leg = LogLeg('account_a', [row(account='account_a')])
    coordinator.futures_leg = LogLeg(
        'account_b', [row(account='account_b', order_id='4100',
                          deal_id='5100', symbol='GC1225')])

    assert coordinator._poll_order_logs() == 2
    stored = coordinator.data_logger.recent_broker_orders()
    assert {r['account'] for r in stored} == {'account_a', 'account_b'}

    # Second call inside the interval must not hit MT5 again
    assert coordinator._poll_order_logs() == 0
    assert coordinator.spot_leg.calls == 1


def test_one_leg_being_unreadable_does_not_lose_the_other(coordinator):
    coordinator.spot_leg = LogLeg('account_a', None)      # IPC failure
    coordinator.futures_leg = LogLeg('account_b',
                                     [row(account='account_b')])
    assert coordinator._poll_order_logs() == 1
    assert coordinator.data_logger.recent_broker_orders()[0][
        'account'] == 'account_b'


def test_a_raising_leg_never_breaks_the_trading_loop(coordinator):
    class Boom:
        name = 'account_a'

        def order_log(self, hours=24):
            raise RuntimeError('terminal gone')

    coordinator.spot_leg = Boom()
    coordinator.futures_leg = LogLeg('account_b', [row(account='account_b')])
    assert coordinator._poll_order_logs() == 1


def test_one_account_setup_polls_that_account_once(coordinator):
    leg = LogLeg('account_a', [row()])
    coordinator.spot_leg = coordinator.futures_leg = leg
    assert coordinator._poll_order_logs() == 1
    assert leg.calls == 1


# --- the API the dashboard actually calls ---------------------------------

pytest.importorskip("flask")

from statarb.database import DataLogger            # noqa: E402
from statarb.webapp import create_app              # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, 'environ', dict(os.environ))
    db = DataLogger(db_path=str(tmp_path / "algo.db"))
    db.record_broker_orders([
        row(account='account_a', filled_at=1_760_000_000_000),
        row(account='account_b', order_id='4100', deal_id='5100',
            symbol='GC1225', side='sell', pnl=250.0,
            filled_at=1_760_000_500_000),
    ])
    (tmp_path / "runtime_status.json").write_text(json.dumps({
        'accounts': {
            'account_a': {'login': 111, 'leverage': 100, 'currency': 'USD'},
            'account_b': {'login': 222, 'leverage': 50, 'currency': 'USD'}},
    }))
    (tmp_path / "config.json").write_text(json.dumps({}))
    app = create_app(db_path=str(tmp_path / "algo.db"),
                     status_path=str(tmp_path / "runtime_status.json"),
                     config_path=str(tmp_path / "config.json"),
                     control_path=str(tmp_path / "control.json"),
                     env_path=str(tmp_path / ".env"))
    app.config['TESTING'] = True
    return app.test_client()


def test_api_serves_both_accounts_in_one_log(client):
    data = client.get('/api/exchange-orders').get_json()
    assert data['count'] == 2
    assert data['accounts'] == ['account_a', 'account_b']
    accounts = [o['account'] for o in data['orders']]
    assert accounts == ['account_b', 'account_a']       # newest first


def test_api_stitches_in_per_account_leverage_and_login(client):
    orders = {o['account']: o
              for o in client.get('/api/exchange-orders').get_json()['orders']}
    assert orders['account_a']['leverage'] == 100
    assert orders['account_b']['leverage'] == 50
    assert orders['account_b']['login'] == 222


def test_api_can_filter_to_one_account(client):
    data = client.get('/api/exchange-orders?account=account_b').get_json()
    assert data['count'] == 1 and data['orders'][0]['symbol'] == 'GC1225'


def test_api_survives_a_database_with_no_log_yet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(db_path=str(tmp_path / "empty.db"),
                     status_path=str(tmp_path / "runtime_status.json"),
                     config_path=str(tmp_path / "config.json"),
                     control_path=str(tmp_path / "control.json"),
                     env_path=str(tmp_path / ".env"))
    app.config['TESTING'] = True
    data = app.test_client().get('/api/exchange-orders').get_json()
    assert data == {'orders': [], 'accounts': [], 'sources': [], 'count': 0}


def test_csv_export_names_the_account_on_every_row(client):
    resp = client.get('/api/exchange-orders/csv')
    assert resp.status_code == 200
    assert 'text/csv' in resp.headers['Content-Type']
    body = resp.get_data(as_text=True)
    header, *lines = [line for line in body.splitlines() if line]
    assert header.split(',')[0] == 'account'
    assert {line.split(',')[0] for line in lines} == {'account_a',
                                                      'account_b'}
    assert '1970' not in body        # ms timestamps read as ms, not seconds


# --- the table the operator looks at --------------------------------------

def test_dashboard_table_has_an_account_column(client):
    page = client.get('/').get_data(as_text=True)
    log = page.split('Exchange Order Log', 1)[1]
    header = log.split('</thead>', 1)[0]
    assert '<th>Account</th>' in header and '<th>Source</th>' in header
    # 16 columns now — every placeholder row must span them all
    assert header.count('<th>') == 16
    assert 'colspan="14"' not in log and 'colspan="15"' not in log
