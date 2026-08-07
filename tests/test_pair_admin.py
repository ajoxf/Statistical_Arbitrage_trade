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
