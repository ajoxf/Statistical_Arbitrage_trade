"""SQLite logging of trades, positions, market data, crash-safe
position state, the untracked-close ledger, and per-trade reviews."""

import json
import sqlite3
from datetime import datetime


class DataLogger:
    def __init__(self, db_path="algo_trading.db"):
        self.db_path = db_path
        self.init_database()

    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                trade_id TEXT PRIMARY KEY,
                position_id TEXT,
                symbol TEXT,
                order_type TEXT,
                lot_size REAL,
                requested_price REAL,
                executed_price REAL,
                order_ticket INTEGER,
                status TEXT,
                timestamp TEXT,
                execution_time TEXT,
                error_message TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                position_id TEXT PRIMARY KEY,
                asset TEXT,
                signal_type TEXT,
                entry_time TEXT,
                entry_premium REAL,
                close_time TEXT,
                close_reason TEXT,
                unrealized_pnl REAL,
                realized_pnl REAL,
                status TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS market_data (
                timestamp TEXT,
                asset TEXT,
                spot_price REAL,
                futures_price REAL,
                actual_basis REAL,
                spread REAL,
                basis_pct REAL,
                signal TEXT
            )
        ''')
        # Columns added after the first release — upgrade in place.
        # `spread`/`basis_pct` replaced `swap_basis`/`swap_premium_pct`
        # when the spread stopped being carry-detrended (2026-08); older
        # databases keep the old columns and gain the new ones empty.
        for column, ddl in (('z', 'z REAL'), ('spread', 'spread REAL'),
                            ('basis_pct', 'basis_pct REAL'),
                            # Identifies WHICH spread series a row belongs
                            # to (symbols + hedge ratio). A warm start must
                            # never seed the window with spreads computed
                            # under a different definition.
                            ('series_key', 'series_key TEXT')):
            try:
                cursor.execute(
                    f'ALTER TABLE market_data ADD COLUMN {ddl}')
            except sqlite3.OperationalError:
                pass
        # Crash-safe live-position snapshots for restart recovery
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS position_state (
                position_id TEXT PRIMARY KEY,
                status TEXT,
                state_json TEXT,
                updated TEXT
            )
        ''')
        # Money that moved outside a recorded trade (orphan cleanups)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS untracked_closes (
                timestamp TEXT,
                leg TEXT,
                symbol TEXT,
                ticket INTEGER,
                volume REAL,
                price REAL,
                note TEXT
            )
        ''')
        # Per-trade review metrics
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_review (
                position_id TEXT PRIMARY KEY,
                asset TEXT,
                entry_z REAL,
                exit_z REAL,
                entry_sigma REAL,
                capture_target REAL,
                cost_est REAL,
                realized_pnl REAL,
                exit_reason TEXT,
                lots REAL,
                opened TEXT,
                closed TEXT
            )
        ''')
        # Lifecycle extremes + outcome tag + spread levels (added later —
        # upgrade in place)
        for column, col_type in [('peak_pnl', 'REAL'), ('peak_min', 'REAL'),
                                 ('trough_pnl', 'REAL'),
                                 ('trough_min', 'REAL'),
                                 ('outcome', 'TEXT'),
                                 ('entry_spread', 'REAL'),
                                 ('exit_spread', 'REAL'),
                                 ('be_spread', 'REAL'),
                                 ('ex_spread', 'REAL'),
                                 ('tp_spread', 'REAL'),
                                 ('sl_spread', 'REAL'),
                                 ('notional', 'REAL')]:
            try:
                cursor.execute(f'ALTER TABLE trade_review '
                               f'ADD COLUMN {column} {col_type}')
            except sqlite3.OperationalError:
                pass    # column already exists
        # SD-touch distribution (z crossing integer sigma levels)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sd_touches (
                timestamp TEXT,
                asset TEXT,
                sd_level INTEGER,
                direction TEXT,
                zscore REAL,
                spread REAL
            )
        ''')
        # Shadow what-if-held results
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS shadow_trades (
                position_id TEXT PRIMARY KEY,
                asset TEXT,
                exit_reason TEXT,
                exit_pnl REAL,
                what_if_net REAL,
                peak REAL,
                trough REAL,
                hit_be_min REAL,
                hit_tp_min REAL,
                horizon_min REAL,
                verdict TEXT,
                completed TEXT
            )
        ''')
        # Exchange Order Log: raw MT5 order/deal activity per account,
        # including trades placed by hand in the terminal. Keyed by
        # (account, order, deal) so re-polling the same window updates
        # rows in place instead of duplicating them.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS broker_orders (
                account TEXT,
                order_id TEXT,
                deal_id TEXT,
                symbol TEXT,
                inst_type TEXT,
                side TEXT,
                pos_side TEXT,
                order_type TEXT,
                quantity REAL,
                fill_qty REAL,
                fill_price REAL,
                fee REAL,
                fee_ccy TEXT,
                pnl REAL,
                state TEXT,
                filled_at INTEGER,
                position_id TEXT,
                is_bot INTEGER,
                comment TEXT,
                seen TEXT,
                PRIMARY KEY (account, order_id, deal_id)
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_broker_orders_time '
                       'ON broker_orders (filled_at DESC)')
        # filled_at is a SERVER-time stamp; this is how far the broker's
        # clock runs ahead of ours, so the log can be shown on the same
        # clock as MT5's own History tab (2026-08). NULL where unknown.
        try:
            cursor.execute('ALTER TABLE broker_orders '
                           'ADD COLUMN server_offset_sec INTEGER')
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()

    BROKER_ORDER_FIELDS = (
        'account', 'order_id', 'deal_id', 'symbol', 'inst_type', 'side',
        'pos_side', 'order_type', 'quantity', 'fill_qty', 'fill_price',
        'fee', 'fee_ccy', 'pnl', 'state', 'filled_at', 'position_id',
        'is_bot', 'comment', 'server_offset_sec')

    def record_broker_orders(self, rows, accounts=None):
        """Upsert a poll's worth of order-log rows.

        `accounts` is the set of accounts this poll read successfully;
        their resting orders are re-stated in full, so previously
        'working' rows are cleared first. Without that, an order that
        fills is stored again under its deal id and the stale resting
        row would sit in the log forever. Accounts that were unreadable
        this pass keep whatever they had."""
        rows = rows or []
        if accounts is None:
            accounts = {row.get('account') for row in rows}
        accounts = {a for a in accounts if a}
        if not rows and not accounts:
            return 0

        seen = datetime.now().isoformat()
        payload = [
            tuple(row.get(f) for f in self.BROKER_ORDER_FIELDS) + (seen,)
            for row in rows]
        # Columns are NAMED, not positional: the table gains columns by
        # ALTER on upgrade, which appends them AFTER `seen`, so a bare
        # VALUES(...) would start writing fields into the wrong slots.
        columns = ', '.join(self.BROKER_ORDER_FIELDS + ('seen',))
        placeholders = ', '.join('?' * (len(self.BROKER_ORDER_FIELDS) + 1))
        conn = sqlite3.connect(self.db_path)
        conn.executemany(
            "DELETE FROM broker_orders WHERE account = ? "
            "AND state = 'working'",
            [(account,) for account in sorted(accounts)])
        if payload:
            conn.executemany(
                f'INSERT OR REPLACE INTO broker_orders ({columns}) '
                f'VALUES ({placeholders})', payload)
        conn.commit()
        conn.close()
        return len(payload)

    def recent_broker_orders(self, limit=100, account=None):
        if account:
            return self._query(
                'SELECT * FROM broker_orders WHERE account = ? '
                'ORDER BY filled_at DESC LIMIT ?', (account, limit))
        return self._query('SELECT * FROM broker_orders '
                           'ORDER BY filled_at DESC LIMIT ?', (limit,))

    def log_sd_touch(self, asset, sd_level, direction, zscore, spread):
        conn = sqlite3.connect(self.db_path)
        conn.execute('INSERT INTO sd_touches VALUES (?, ?, ?, ?, ?, ?)',
                     (datetime.now().isoformat(), asset, sd_level,
                      direction, zscore, spread))
        conn.commit()
        conn.close()

    def log_shadow(self, shadow):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT OR REPLACE INTO shadow_trades VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (shadow['position_id'], shadow['asset'],
              shadow['exit_reason'], shadow['exit_pnl'], shadow['net'],
              shadow['peak'], shadow['trough'], shadow['hit_be_min'],
              shadow['hit_tp_min'], shadow['horizon_sec'] / 60,
              shadow['verdict'], datetime.now().isoformat()))
        conn.commit()
        conn.close()

    def _query(self, sql, args=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        conn.close()
        return rows

    def recent_reviews(self, limit=10):
        return self._query("SELECT * FROM trade_review "
                           "ORDER BY closed DESC LIMIT ?", (limit,))

    def recent_shadows(self, limit=10):
        return self._query("SELECT * FROM shadow_trades "
                           "ORDER BY completed DESC LIMIT ?", (limit,))

    # -- crash-safe position state ---------------------------------------

    def save_position_state(self, position):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT OR REPLACE INTO position_state VALUES (?, ?, ?, ?)
        ''', (position.position_id, position.status.value,
              json.dumps(position.to_dict()), datetime.now().isoformat()))
        conn.commit()
        conn.close()

    def clear_position_state(self, position_id):
        conn = sqlite3.connect(self.db_path)
        conn.execute('DELETE FROM position_state WHERE position_id = ?',
                     (position_id,))
        conn.commit()
        conn.close()

    def load_open_position_states(self):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT state_json FROM position_state "
            "WHERE status IN ('ACTIVE', 'CLOSING')").fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows]

    # -- ledgers -----------------------------------------------------------

    def log_untracked_close(self, leg, symbol, ticket, volume, price, note):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT INTO untracked_closes VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (datetime.now().isoformat(), leg, symbol, ticket, volume,
              price, note))
        conn.commit()
        conn.close()

    def log_trade_review(self, position, exit_z=None, capture_target=None,
                         cost_est=None, outcome=None, exit_spread=None,
                         notional=None):
        conn = sqlite3.connect(self.db_path)
        plan = position.exit_plan or {}
        levels = plan.get('levels') or {}
        conn.execute('''
            INSERT OR REPLACE INTO trade_review VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
             ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            position.position_id, position.asset,
            plan.get('entry_z'), exit_z, plan.get('entry_sigma'),
            capture_target if capture_target is not None
            else plan.get('tp_usd'),
            cost_est if cost_est is not None else plan.get('rt_cost_usd'),
            position.realized_pnl, position.close_reason,
            position.spot_trade.lot_size if position.spot_trade else None,
            position.entry_time.isoformat(),
            position.close_time.isoformat() if position.close_time else None,
            position.peak_pnl, position.peak_min,
            position.trough_pnl, position.trough_min,
            outcome,
            levels.get('entry_spread'), exit_spread,
            levels.get('be'), levels.get('ex'),
            levels.get('tp'), levels.get('sl'),
            notional,
        ))
        conn.commit()
        conn.close()

    def log_trade(self, trade, position_id=None):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT OR REPLACE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trade.trade_id, position_id, trade.symbol, trade.side.value,
            trade.lot_size, trade.requested_price, trade.executed_price,
            trade.order_ticket, trade.status, trade.timestamp.isoformat(),
            trade.execution_time.isoformat() if trade.execution_time else None,
            trade.error_message,
        ))
        conn.commit()
        conn.close()

    def log_position(self, position):
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT OR REPLACE INTO positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            position.position_id, position.asset, position.signal_type.value,
            position.entry_time.isoformat(),
            position.entry_premium,
            position.close_time.isoformat() if position.close_time else None,
            position.close_reason, position.unrealized_pnl,
            position.realized_pnl, position.status.value,
        ))
        conn.commit()
        conn.close()

    def log_market_data(self, asset, market_data, signal, z=None,
                        series_key=None):
        signal_str = signal.value if hasattr(signal, 'value') else str(signal)
        conn = sqlite3.connect(self.db_path)
        conn.execute('''
            INSERT INTO market_data
                (timestamp, asset, spot_price, futures_price, actual_basis,
                 spread, basis_pct, signal, z, series_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().isoformat(), asset, market_data['spot_price'],
            market_data['futures_price'], market_data['actual_basis'],
            market_data['spread'], market_data.get('basis_pct'),
            signal_str, z, series_key,
        ))
        conn.commit()
        conn.close()

    def recent_spreads(self, asset, series_key, since):
        """[(epoch_seconds, spread)] for a warm start, oldest first.

        Restarting used to reset the rolling window to zero, so the
        engine re-served its whole warm-up — two hours before it could
        trade again after a config change or a crash. The quotes were
        already in this table; nothing read them back.

        `series_key` is matched exactly: a row logged under different
        symbols or a different HEDGE_RATIO is a different series, and
        seeding the window with it would produce a mean the current
        spread has no relationship to. Rows predating the column are
        NULL and are therefore never reused.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                'SELECT timestamp, spread FROM market_data '
                'WHERE asset=? AND series_key=? AND spread IS NOT NULL '
                'AND timestamp >= ? ORDER BY timestamp',
                (asset, series_key, since.isoformat())).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            conn.close()
        out = []
        for stamp, spread in rows:
            try:
                out.append((datetime.fromisoformat(stamp).timestamp(),
                            float(spread)))
            except (TypeError, ValueError):
                continue
        return out
