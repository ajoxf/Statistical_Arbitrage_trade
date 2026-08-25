"""The reset buttons: what they delete, and what they report.

Operator, 2026-08-25, on pressing Reset SD / Trades:
"Error: TypeError: Cannot read properties of undefined (reading
'trades')".

Two faults behind one dialog:

1. The endpoints returned `{'success': True}` with no `deleted` key,
   and the page reads `data.deleted.trades` to say what went. So every
   reset ended in a bare TypeError and the operator could not tell
   whether anything had been deleted.
2. `sd_touches` was never in the delete list — so "Reset trades and SD
   analysis" left the whole SD distribution in place while the toast
   claimed it had gone. The button did not do what it said.
"""

import sqlite3

import pytest

from statarb.webapp import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / 'a.db'
    # DataLogger owns the schema; create_app only reads it.
    from statarb.database import DataLogger
    DataLogger(db_path=str(db))
    (tmp_path / 'config.json').write_text('{"trading_mode": "paper"}')
    application = create_app(
        db_path=str(db), status_path=str(tmp_path / 'runtime_status.json'),
        config_path=str(tmp_path / 'config.json'),
        control_path=str(tmp_path / 'control.json'),
        env_path=str(tmp_path / '.env'))
    return application, str(db)


def seed(db_path):
    """Two rows in every table a reset is supposed to clear."""
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO trade_review (position_id, asset) "
                 "VALUES ('P1', 'GOLD'), ('P2', 'GOLD')")
    conn.execute("INSERT INTO sd_touches (asset, sd_level) "
                 "VALUES ('GOLD', 1), ('GOLD', 2)")
    conn.execute("INSERT INTO market_data (asset, spread) "
                 "VALUES ('GOLD', 55.0), ('GOLD', 55.1)")
    conn.commit()
    conn.close()


def count(db_path, table):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    finally:
        conn.close()


def test_reset_trades_reports_what_it_deleted(app):
    """The page renders `data.deleted.trades`; without the key it threw
    before it could say anything."""
    application, db_path = app
    seed(db_path)
    body = application.test_client().post('/api/reset-trades').get_json()
    assert body['success'] is True
    assert 'deleted' in body, 'the page reads data.deleted and it was absent'
    assert body['deleted']['sd_touches'] == 2


def test_reset_trades_actually_clears_the_sd_analysis(app):
    """The button says "Reset trades and SD analysis" and the toast
    counts SD touches. They were never deleted."""
    application, db_path = app
    seed(db_path)
    assert count(db_path, 'sd_touches') == 2
    application.test_client().post('/api/reset-trades')
    assert count(db_path, 'sd_touches') == 0
    assert count(db_path, 'trade_review') == 0


def test_reset_trades_preserves_the_spread_history(app):
    """"(spread preserved)" — the whole reason this button exists
    beside Reset Everything, and the reason a warm start survives it."""
    application, db_path = app
    seed(db_path)
    application.test_client().post('/api/reset-trades')
    assert count(db_path, 'market_data') == 2


def test_reset_all_reports_every_count_the_toast_names(app):
    application, db_path = app
    seed(db_path)
    body = application.test_client().post('/api/reset-all').get_json()
    deleted = body['deleted']
    for key in ('trades', 'sd_touches', 'spread_history'):
        assert key in deleted, key
    assert deleted['sd_touches'] == 2
    assert deleted['spread_history'] == 2
    assert count(db_path, 'market_data') == 0


def test_a_missing_table_counts_zero_rather_than_failing(app):
    """A database that predates a table must not turn a reset into a
    500 — the operator would read that as "nothing was deleted" while
    the other tables had already gone."""
    application, db_path = app
    conn = sqlite3.connect(db_path)
    conn.execute('DROP TABLE shadow_trades')
    conn.commit()
    conn.close()
    body = application.test_client().post('/api/reset-trades').get_json()
    assert body['success'] is True
    assert body['deleted']['shadow_trades'] == 0


def test_resetting_an_empty_book_says_zero_not_nothing(app):
    """0 is a real answer. The page renders it with `?? 0` so a count
    of none reads as none rather than as "undefined"."""
    application, _ = app
    body = application.test_client().post('/api/reset-trades').get_json()
    assert body['deleted']['trades'] == 0


def test_the_page_never_dereferences_deleted_unguarded():
    """The server is fixed, but the browser must not be one missing key
    away from a TypeError again — that is what the operator saw."""
    page = (pytest.importorskip('pathlib').Path('templates/dashboard.html')
            .read_text())
    assert 'data.deleted;' not in page, 'unguarded data.deleted'
    assert page.count('data.deleted || {}') == 2
