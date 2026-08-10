"""Managing the PAIR itself: rename, disable, delete, and noticing when
the pair_type cannot be describing the two instruments.

Until 2026-08-07 an asset could only be created, never removed. A
phantom pair born of the old label-into-the-key bug survived every
attempt to clear it, and a SILVER pair whose futures symbol did not
exist on the account warned at every startup with no way to switch it
off from the UI.
"""

import json

import pytest

from statarb.fairvalue import mislabelled_pair, fair_value_block


# --- pair_type sanity -----------------------------------------------

def test_a_basis_label_on_two_different_instruments_is_flagged():
    """WTI vs Brent under SPOT_FUTURE: carry says a few cents, the live
    spread is $5.03. The card used to render that as an enormous edge."""
    warning = mislabelled_pair('SPOT_FUTURE', spread=5.03, value=0.55)
    assert warning
    assert 'RELATED' in warning and 'not SPOT_FUTURE' in warning
    assert 'changes no trading decision' in warning


def test_a_real_basis_pair_is_not_flagged():
    """Gold: a 59.4 spread against a 58.9 carry value is the pair
    working exactly as described."""
    assert mislabelled_pair('SPOT_FUTURE', spread=59.42, value=58.90) is None


def test_no_fair_value_means_no_verdict():
    """RELATED pairs compute no fair value, and a missing input must
    not manufacture a warning."""
    assert mislabelled_pair('RELATED', spread=5.03, value=None) is None
    assert mislabelled_pair('SPOT_FUTURE', spread=None, value=0.5) is None


def test_the_block_carries_the_warning_to_the_ui():
    asset = {'pair_type': 'SPOT_FUTURE', 'risk_free_rate': 0.0425,
             'futures_expiry': None}
    block = fair_value_block(asset, 76.88, 81.91, 5.03)
    # No expiry -> no fair value -> nothing to compare, and no guess
    assert block['fair_value'] is None
    assert block['fair_warning'] is None
    assert 'fair_warning' in block          # key always present


def test_the_health_block_warns_without_claiming_to_block(config, tmp_path,
                                                          monkeypatch):
    """A mislabelled pair is worth saying and is not a halt — it must
    not appear in 'held up by'."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(config, trading_mode='PAPER')
    md = {'spot_price': 76.88, 'futures_price': 81.91, 'spread': 5.03,
          'tick_age_ms': 100,
          'fair_warning': 'the live spread is nowhere near carry'}
    rows = coord._health('GOLD', md)
    pair = [r for r in rows if r[0] == 'pair']
    assert len(pair) == 1
    assert pair[0][1] == coord.WARN
    assert pair[0][1] not in (coord.BLOCKED, coord.FAILED)


# --- pair administration --------------------------------------------

@pytest.fixture
def pairs_client(tmp_path, monkeypatch):
    """A config carrying the real-world mess: one live pair, one that
    cannot resolve, and one phantom from the label-into-the-key bug."""
    from statarb.webapp import create_app
    monkeypatch.chdir(tmp_path)

    (tmp_path / 'config.json').write_text(json.dumps({
        'trading_mode': 'paper',
        'assets': {
            'GOLD': {'name': 'Gold', 'enabled': True, 'lot_size': 100.0,
                     'spot_symbols': ['XAUUSD_'],
                     'futures_symbols': ['GC1226']},
            'SILVER': {'name': 'Silver', 'enabled': True,
                       'lot_size': 5000.0, 'spot_symbols': ['XAGUSD_'],
                       'futures_symbols': ['SI1225']},
            'XAUUSD_/GC1225': {'enabled': True,
                               'spot_symbols': ['XAUUSD_'],
                               'futures_symbols': ['GC1225']},
        },
    }))
    (tmp_path / 'runtime_status.json').write_text(json.dumps(
        {'positions': []}))

    app = create_app(db_path=str(tmp_path / 'algo.db'),
                     status_path=str(tmp_path / 'runtime_status.json'),
                     config_path=str(tmp_path / 'config.json'),
                     control_path=str(tmp_path / 'control.json'),
                     env_path=str(tmp_path / '.env'))
    app.config['TESTING'] = True
    client = app.test_client()
    client.tmp_path = tmp_path
    client.db_path = str(tmp_path / 'algo.db')
    return client


def read_assets(client):
    with open(client.tmp_path / 'config.json', encoding='utf-8') as f:
        return json.load(f)['assets']


def test_the_pairs_are_listed_with_their_state(pairs_client):
    rows = {r['key']: r for r in pairs_client.get('/api/assets').get_json()}
    assert set(rows) == {'GOLD', 'SILVER', 'XAUUSD_/GC1225'}
    assert rows['GOLD']['spot_symbol'] == 'XAUUSD_'
    assert rows['GOLD']['futures_symbol'] == 'GC1226'
    assert all(r['enabled'] for r in rows.values())
    assert not any(r['has_open_position'] for r in rows.values())


@pytest.mark.parametrize('url', [
    '/api/assets/XAUUSD_/GC1225',          # raw slash
    '/api/assets/XAUUSD_%2FGC1225',        # encoded slash
])
def test_a_phantom_pair_with_a_slash_in_its_key_can_be_deleted(
        pairs_client, url):
    """The key that most needs removing is the one the old
    label-into-the-key bug created, and it has a slash in it. A
    default <string> route 404s on precisely that row."""
    response = pairs_client.delete(url)
    assert response.status_code == 200
    assert response.get_json()['success']
    assets = read_assets(pairs_client)
    assert 'XAUUSD_/GC1225' not in assets and 'GOLD' in assets


def test_a_pair_the_account_cannot_trade_can_be_disabled(pairs_client):
    response = pairs_client.post('/api/assets/SILVER',
                                 json={'enabled': False})
    assert response.get_json()['success']
    silver = read_assets(pairs_client)['SILVER']
    assert silver['enabled'] is False
    # Disabling keeps the row — the symbols are still there to fix
    assert silver['spot_symbols']


def test_renaming_carries_the_recorded_history_across(pairs_client):
    """A rename that stranded every past trade and the whole warm-start
    window would be a worse outcome than the confusing name."""
    import sqlite3

    db = pairs_client.db_path
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE IF NOT EXISTS market_data '
                 '(asset TEXT, spread REAL)')
    conn.executemany('INSERT INTO market_data VALUES (?, ?)',
                     [('GOLD', 5.01), ('GOLD', 5.02), ('SILVER', 1.0)])
    conn.commit()
    conn.close()

    response = pairs_client.post('/api/assets/GOLD',
                                 json={'rename': 'WTI_BRENT'})
    body = response.get_json()
    assert body['success'] and body['key'] == 'WTI_BRENT'

    assets = read_assets(pairs_client)
    assert 'GOLD' not in assets and 'WTI_BRENT' in assets
    assert assets['WTI_BRENT']['spot_symbols'] == ['XAUUSD_']

    conn = sqlite3.connect(db)
    rows = dict(conn.execute('SELECT asset, COUNT(*) FROM market_data '
                             'GROUP BY asset').fetchall())
    conn.close()
    assert rows.get('WTI_BRENT') == 2 and 'GOLD' not in rows
    assert rows.get('SILVER') == 1          # untouched


def test_renaming_onto_an_existing_pair_is_refused(pairs_client):
    response = pairs_client.post('/api/assets/SILVER',
                                 json={'rename': 'GOLD'})
    assert response.status_code == 400
    assert 'already exists' in response.get_json()['error']


def test_an_unknown_pair_is_a_404(pairs_client):
    assert pairs_client.delete('/api/assets/NOPE').status_code == 404


def test_a_pair_with_money_on_it_is_not_a_config_row(pairs_client, tmp_path,
                                                     monkeypatch):
    """Renaming or removing a pair mid-trade takes the open position out
    of the exit loop."""
    (tmp_path / 'runtime_status.json').write_text(json.dumps({
        'positions': [{'position_id': 'POS_0001', 'asset': 'GOLD'}]}))

    response = pairs_client.post('/api/assets/GOLD',
                                 json={'rename': 'WTI_BRENT'})
    assert response.status_code == 409
    assert 'close it first' in response.get_json()['error']

    assert pairs_client.delete('/api/assets/GOLD').status_code == 409
    assert pairs_client.post('/api/assets/GOLD',
                             json={'enabled': False}).status_code == 409


# --- beta sanity ----------------------------------------------------

def _health_rows(config, tmp_path, monkeypatch, beta, spot, fut):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    config.TRADING['HEDGE_RATIO'] = beta
    coord = Coordinator(config, trading_mode='PAPER')
    md = {'spot_price': spot, 'futures_price': fut,
          'spread': fut - beta * spot, 'tick_age_ms': 100}
    return {name: (state, detail)
            for name, state, detail in coord._health('GOLD', md)}


def test_a_beta_that_wrecks_the_spread_is_called_out(config, tmp_path,
                                                     monkeypatch):
    """Live 2026-08-10: HEDGE_RATIO 10 on USOIL/UKOIL at 81.76/85.07
    gave a spread of -732.53. Every downstream number — mu, sigma, z,
    the exit levels — then describes a series that does not exist."""
    rows = _health_rows(config, tmp_path, monkeypatch, 10.0, 81.76, 85.07)
    assert 'beta' in rows
    state, detail = rows['beta']
    from statarb.coordinator import Coordinator
    assert state == Coordinator.BLOCKED
    assert '-732' in detail and 'HEDGE_RATIO 10' in detail
    assert 'NOT a contract-size or lot ratio' in detail


def test_an_impossible_spread_blocks_entries(config, tmp_path, monkeypatch):
    """It happened three times in one day, twice on advice this repo
    gave, and the third time simply by leaving XAGUSD/XAUUSD's 66.94
    behind when the pair was switched back to oil. Reporting it was not
    enough: the engine must not OPEN a position on a series it can show
    is not the difference between the two prices it is quoting."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    config.TRADING['HEDGE_RATIO'] = 66.94
    coord = Coordinator(config, trading_mode='PAPER')
    md = {'spot_price': 82.61, 'futures_price': 86.05,
          'spread': 86.05 - 66.94 * 82.61, 'tick_age_ms': 100}

    called = []
    coord.z_gen.entry_signal = lambda *a, **k: called.append(a) or 'SIGNAL'
    coord._clip_lots = lambda *a, **k: 1.0
    assert coord._entry_signal('GOLD', object(), md, {}, 1000.0) is None
    assert not called                    # never even asked for a signal


def test_a_sane_spread_still_reaches_the_signal_generator(config, tmp_path,
                                                          monkeypatch):
    """The block must not stand in front of a working pair."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    config.TRADING['HEDGE_RATIO'] = 1.0
    coord = Coordinator(config, trading_mode='PAPER')
    md = {'spot_price': 82.61, 'futures_price': 86.05,
          'spread': 3.44, 'tick_age_ms': 100}
    coord.z_gen.entry_signal = lambda *a, **k: 'SIGNAL'
    coord._clip_lots = lambda *a, **k: 1.0
    assert coord._entry_signal('GOLD', object(), md, {}, 1000.0) == 'SIGNAL'


def test_a_sane_beta_is_silent(config, tmp_path, monkeypatch):
    """Both the real pairs this engine has run must stay quiet."""
    # WTI vs Brent at beta 1: a $3 differential on $80 legs
    assert 'beta' not in _health_rows(config, tmp_path, monkeypatch,
                                      1.0, 81.76, 85.07)
    # Gold spot vs future at beta 1: a $59 basis on $4,300 legs
    assert 'beta' not in _health_rows(config, tmp_path, monkeypatch,
                                      1.0, 4292.61, 4351.55)


# --- saved vs running symbols ---------------------------------------

def _ui(running_a, running_b, configured_a, configured_b):
    from statarb import webapi
    status = {'assets': [{'asset': 'GOLD',
                          'rt_spot_symbol': running_a,
                          'rt_fut_symbol': running_b}]}
    raw = {'assets': {'GOLD': {'enabled': True,
                               'spot_symbols': [configured_a],
                               'futures_symbols': [configured_b]}}}
    return webapi.status_to_ui(status, raw)['signal']


def test_the_cards_are_labelled_with_what_the_engine_streams():
    """Live 2026-08-10: the cards read "XAGUSD 82.0050" and "XAUUSD
    85.3500" — the newly saved names over the old pair's oil prices,
    because the labels came from config and the prices from the
    engine. A picture that cannot be true."""
    signal = _ui('USOIL', 'UKOIL', 'XAGUSD', 'XAUUSD')
    assert signal['leg_a_symbol'] == 'USOIL'
    assert signal['leg_b_symbol'] == 'UKOIL'


def test_a_saved_symbol_the_engine_has_not_adopted_is_flagged():
    pending = _ui('USOIL', 'UKOIL', 'XAGUSD', 'XAUUSD')[
        'symbols_pending_restart']
    assert set(pending) == {'spot', 'futures'}
    assert pending['spot'] == {'running': 'USOIL', 'configured': 'XAGUSD'}
    assert pending['futures'] == {'running': 'UKOIL', 'configured': 'XAUUSD'}

    # One leg changed, one not
    half = _ui('USOIL', 'UKOIL', 'USOIL', 'XAUUSD')['symbols_pending_restart']
    assert set(half) == {'futures'}


def test_agreement_is_silent():
    assert _ui('USOIL', 'UKOIL', 'USOIL', 'UKOIL')[
        'symbols_pending_restart'] == {}


def test_nothing_is_claimed_before_the_engine_has_published():
    """No running symbols yet is not a mismatch — it is a cold start."""
    assert _ui(None, None, 'USOIL', 'UKOIL')[
        'symbols_pending_restart'] == {}


# --- the hedge ratio suggestion -------------------------------------

def _beta_client(tmp_path, monkeypatch, pair_type, a_price, b_price):
    from statarb.webapp import create_app
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config.json').write_text(json.dumps({
        'trading_mode': 'paper',
        'assets': {'PAIR': {'enabled': True, 'pair_type': pair_type,
                            'spot_symbols': ['A'],
                            'futures_symbols': ['B']}}}))
    (tmp_path / 'runtime_status.json').write_text(json.dumps({
        'assets': [{'asset': 'PAIR', 'rt_spot_symbol': 'A',
                    'rt_fut_symbol': 'B', 'spot_price': a_price,
                    'futures_price': b_price}]}))
    app = create_app(db_path=str(tmp_path / 'a.db'),
                     status_path=str(tmp_path / 'runtime_status.json'),
                     config_path=str(tmp_path / 'config.json'),
                     control_path=str(tmp_path / 'control.json'),
                     env_path=str(tmp_path / '.env'))
    app.config['TESTING'] = True
    return app.test_client().get(
        '/api/leg-prices?leg_a=A&leg_b=B').get_json()


def test_the_suggestion_speaks_the_shape_the_page_reads(tmp_path,
                                                        monkeypatch):
    """The page reads success / suggested_beta / leg_a_price /
    leg_b_price; the endpoint returned only {leg_a, leg_b}, so the
    Hedge Ratio suggestion read "unavailable" from the day it shipped.
    """
    body = _beta_client(tmp_path, monkeypatch, 'RELATED', 64.686, 4352.26)
    assert body['success'] is True
    assert set(body) >= {'suggested_beta', 'leg_a_price', 'leg_b_price',
                         'reason'}


def test_different_instruments_are_suggested_the_price_ratio(tmp_path,
                                                             monkeypatch):
    """Gold vs silver: beta near the price ratio is what makes the two
    legs comparable at all."""
    body = _beta_client(tmp_path, monkeypatch, 'RELATED', 64.686, 4352.26)
    assert body['suggested_beta'] == pytest.approx(4352.26 / 64.686)
    assert 'money' in body['reason']


@pytest.mark.parametrize('pair_type', ['SPOT_FUTURE', 'FUTURE_FUTURE'])
def test_the_same_underlying_is_suggested_one(tmp_path, monkeypatch,
                                              pair_type):
    """Gold spot vs its own future: the price ratio is ~1.014, and
    using it as beta collapses the spread to zero — deleting the basis
    the strategy exists to trade."""
    body = _beta_client(tmp_path, monkeypatch, pair_type, 4292.61, 4351.55)
    assert body['suggested_beta'] == 1.0
    assert 'same underlying' in body['reason']


def test_no_prices_means_no_suggestion(tmp_path, monkeypatch):
    body = _beta_client(tmp_path, monkeypatch, 'RELATED', None, None)
    assert body['success'] is False
