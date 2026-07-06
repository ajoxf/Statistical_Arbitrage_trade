"""Round-trip tests: coordinator <-> leg runner over localhost TCP."""

import threading

from statarb.leg_runner import LegServer
from statarb.legs import RemoteLeg


def start_server(fake_broker):
    server = LegServer(fake_broker, '127.0.0.1', 0)  # ephemeral port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_ping_tick_order_roundtrip(fake_broker):
    server = start_server(fake_broker)
    leg = RemoteLeg('account_a', f'127.0.0.1:{server.port}')
    try:
        assert leg.connect(retries=3, delay=0.1)
        assert leg.ping()

        account = leg.account_info()
        assert account['server'] == 'FakeServer'

        meta = leg.ensure_symbol('XAUUSD')
        assert meta['ok'] and meta['volume_step'] == 0.01

        tick = leg.tick('XAUUSD')
        assert tick['bid'] < tick['ask']

        result = leg.order('XAUUSD', 'BUY', 2.0, comment='BASIS_ARB_test')
        assert result['ok'] and result['filled_volume'] == 2.0
        assert fake_broker.orders == [('XAUUSD', 'BUY', 2.0)]
    finally:
        leg.close()
        server.stop()


def test_unknown_symbol_and_command(fake_broker):
    fake_broker.fail_symbols.add('NOPE')
    server = start_server(fake_broker)
    leg = RemoteLeg('account_a', f'127.0.0.1:{server.port}')
    try:
        assert leg.connect(retries=3, delay=0.1)
        assert leg.tick('NOPE') is None
        assert not leg.ensure_symbol('NOPE')['ok']
        reply = leg._request({'cmd': 'bogus'})
        assert reply and not reply['ok']
    finally:
        leg.close()
        server.stop()


def test_order_failure_propagates(fake_broker):
    fake_broker.fail_symbols.add('XAUUSD')
    server = start_server(fake_broker)
    leg = RemoteLeg('account_a', f'127.0.0.1:{server.port}')
    try:
        assert leg.connect(retries=3, delay=0.1)
        result = leg.order('XAUUSD', 'SELL', 1.0)
        assert not result['ok'] and result['filled_volume'] == 0.0
    finally:
        leg.close()
        server.stop()
