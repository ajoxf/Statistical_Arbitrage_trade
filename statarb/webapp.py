"""Nexus web control panel.

Serves the Nexus UI (templates/ + static/, vendored verbatim) backed
by this MT5 engine. Runs in its OWN process and talks to the
coordinator only through files, so the UI can never block or crash
the trading loop:

- reads  : SQLite DB + runtime_status.json (coordinator refreshes)
- writes : config.json  (coordinator hot-reloads safe sections ~10s;
           structural changes need a launcher restart)
           control.json (algo toggle, manual open/close, self-tests)
           .env         (secrets typed in the UI — never in config)

    python start.py            (launcher runs this automatically)
    python run_dashboard.py    (standalone)
"""

import json
import os
import sqlite3
import time
from datetime import datetime

try:
    from flask import Flask, jsonify, render_template, request
except ImportError:
    Flask = None

try:
    from flask_socketio import SocketIO
except ImportError:
    SocketIO = None            # UI falls back to polling

from . import webapi

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')


def update_env_file(path, updates):
    """Merge key=value pairs into a .env file, preserving other lines.
    This is how the UI stores secrets — they never touch config.json."""
    lines = []
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            lines = [line.rstrip('\n') for line in f]
    keys = set(updates)
    kept = [line for line in lines
            if line.split('=', 1)[0].strip() not in keys]
    kept += [f"{key}={value}" for key, value in updates.items()]
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write("\n".join(kept) + "\n")
    os.replace(tmp, path)
    for key, value in updates.items():
        os.environ[key] = value      # visible to this process immediately


def create_app(db_path="algo_trading.db", status_path="runtime_status.json",
               config_path="config.json", control_path="control.json",
               env_path=".env"):
    if Flask is None:
        raise RuntimeError("Flask not installed — pip install flask")
    app = Flask(__name__, template_folder=TEMPLATE_DIR,
                static_folder=STATIC_DIR)

    # ---------------- helpers ----------------

    def query(sql, args=()):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        except sqlite3.OperationalError:
            rows = []               # table not created yet
        conn.close()
        return rows

    def runtime_status():
        try:
            with open(status_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def load_config_raw():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_config_raw(raw):
        tmp = config_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, config_path)

    def write_control(update):
        try:
            with open(control_path, 'r', encoding='utf-8') as f:
                control = json.load(f)
        except (OSError, ValueError):
            control = {'algo_enabled': True}
        control.update(update)
        tmp = control_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(control, f)
        os.replace(tmp, control_path)
        return control

    def ui_status():
        return webapi.status_to_ui(runtime_status(), load_config_raw())

    # ---------------- pages ----------------

    # The vendored navbar links to both / and /dashboard — serve both.
    @app.route('/')
    @app.route('/dashboard')
    def dashboard():
        return render_template('dashboard.html', config=ui_config_obj())

    @app.route('/analysis')
    def analysis():
        rows = query("SELECT * FROM trade_review ORDER BY closed DESC "
                     "LIMIT 500")
        stats = webapi.statistics_from_rows(rows)
        stats['breakeven_win_rate'] = stats['breakeven_wr']
        shadow_rows = query("SELECT * FROM shadow_trades "
                            "ORDER BY completed DESC LIMIT 50")
        touches = query("SELECT * FROM sd_touches "
                        "ORDER BY timestamp DESC LIMIT 500")
        return render_template(
            'analysis.html', config=ui_config_obj(), stats=stats,
            assets=ui_assets(),
            trades=[webapi.trade_to_ui(r) for r in rows],
            journal=[webapi.trade_to_ui(r) for r in rows],
            dd_trades=[webapi.excursion_row(r) for r in rows],
            sd_touches=touches,
            shadow={'count': len(shadow_rows), 'recent': shadow_rows},
            drawdown=webapi.drawdown_block(rows),
            equity=(runtime_status().get('equity')))

    def ui_assets():
        """{key: {...}} shape the templates iterate for the asset picker."""
        raw = load_config_raw()
        assets = raw.get('assets') or {}
        if not assets:
            from .config import AlgoTradingConfig
            assets = {k: dict(v)
                      for k, v in AlgoTradingConfig().ASSETS.items()}
        return {key: {
            'name': asset.get('name', key),
            'spot': (asset.get('spot_symbols') or [''])[0],
            'futures': (asset.get('futures_symbols') or [''])[0],
            'okx_spot': (asset.get('spot_symbols') or [''])[0],
            'okx_futures': (asset.get('futures_symbols') or [''])[0],
            'contract_size': asset.get('lot_size'),
        } for key, asset in assets.items()}

    @app.route('/settings')
    def settings():
        return render_template('settings.html', config=ui_config_obj(),
                               assets=ui_assets(), is_demo=False)

    @app.route('/setup')
    def setup():
        return render_template('setup.html', config=ui_config_obj())

    def ui_config_obj():
        """Config as a dot-accessible object for the Jinja templates."""
        data = webapi.to_ui_config(load_config_raw())
        return type('Cfg', (), {**data, 'get': data.get,
                                'to_dict': lambda self=None: data})()

    # ---------------- W3 API surface ----------------

    @app.route('/api/engine/status')
    def api_engine_status():
        return jsonify(ui_status())

    @app.route('/api/config', methods=['GET', 'POST'])
    def api_config():
        raw = load_config_raw()
        if request.method == 'GET':
            return jsonify(webapi.to_ui_config(raw))

        payload = request.get_json(silent=True) or {}
        status = runtime_status()
        in_trade = bool(status.get('positions'))

        # Native (sectioned) save used by tests/tools
        if 'sections' in payload or 'accounts' in payload \
                or 'leg_accounts' in payload or 'secrets' in payload:
            return _save_sectioned(raw, payload, status, in_trade)

        # Nexus UI save (flat W3 field names)
        new_beta = payload.get('hedge_ratio')
        old_beta = (raw.get('trading') or {}).get('HEDGE_RATIO', 1.0)
        if new_beta is not None and float(new_beta) != float(old_beta) \
                and in_trade:
            return jsonify({'error': 'Hedge ratio (beta) change rejected: a '
                            'position is open — it recomputes the whole '
                            'spread series'}), 409

        raw, env_updates, notes = webapi.apply_ui_config(raw, payload)
        if env_updates:
            update_env_file(env_path, env_updates)
        save_config_raw(raw)
        note = " ".join(notes) or \
            "Saved — the engine hot-reloads within ~10s."
        return jsonify({'success': True, 'status': 'ok', 'note': note})

    def _save_sectioned(raw, payload, status, in_trade):
        note = 'Saved — coordinator hot-reloads within ~10s'
        sections = payload.get('sections', {})
        new_beta = sections.get('TRADING', {}).get('HEDGE_RATIO')
        old_beta = (raw.get('trading') or {}).get('HEDGE_RATIO', 1.0)
        if new_beta is not None and new_beta != old_beta and in_trade:
            return jsonify({'error': 'Hedge ratio change rejected: a '
                            'position is open'}), 409
        for section, values in sections.items():
            key = webapi.SECTION_JSON_KEY.get(section)
            if key:
                raw.setdefault(key, {})
                raw[key].update(values)

        env_updates = dict(payload.get('secrets') or {})
        if payload.get('accounts'):
            for name, acct in payload['accounts'].items():
                password = (acct or {}).pop('_password', None)
                if password:
                    var = (acct.get('password_env')
                           or (raw.get('accounts', {}).get(name, {})
                               or {}).get('password_env')
                           or f"MT5_PASSWORD_{name.upper()}")
                    acct['password_env'] = var
                    env_updates[var] = password
                elif not acct.get('password_env'):
                    existing = (raw.get('accounts', {}).get(name, {})
                                or {}).get('password_env')
                    if existing:
                        acct['password_env'] = existing
            raw['accounts'] = payload['accounts']
            note = ('Saved. Account changes need a launcher restart.')
        if env_updates:
            update_env_file(env_path, env_updates)
            note = ('Saved (secrets written to .env). Restart the launcher '
                    'for credential changes to take effect.')
        if payload.get('leg_accounts'):
            raw['leg_accounts'] = payload['leg_accounts']
            note = 'Saved. Leg mapping changes need a launcher restart.'
        if payload.get('trading_mode') in ('paper', 'live'):
            if payload['trading_mode'] != raw.get('trading_mode', 'paper'):
                note = ('Saved. Trading-mode change takes effect when the '
                        'launcher is restarted.')
            raw['trading_mode'] = payload['trading_mode']
        save_config_raw(raw)
        return jsonify({'ok': True, 'success': True, 'note': note})

    @app.route('/api/engine/toggle-algo', methods=['POST'])
    def api_toggle_algo():
        current = runtime_status().get('algo_enabled', True)
        control = write_control({'algo_enabled': not current})
        return jsonify({'success': True,
                        'algo_enabled': control['algo_enabled']})

    @app.route('/api/engine/toggle', methods=['POST'])
    def api_toggle():
        return api_toggle_algo()

    @app.route('/api/engine/close-position', methods=['POST'])
    def api_close_position():
        payload = request.get_json(silent=True) or {}
        positions = runtime_status().get('positions') or []
        position_id = payload.get('position_id') or (
            positions[0]['position_id'] if positions else None)
        if not position_id:
            return jsonify({'success': False,
                            'error': 'No open position'}), 400
        write_control({'close': {'position_id': position_id,
                                 'ts': time.time()}})
        return jsonify({'success': True, 'position_id': position_id})

    @app.route('/api/engine/close', methods=['POST'])
    def api_close():
        return api_close_position()

    @app.route('/api/engine/open', methods=['POST'])
    @app.route('/api/manual-trade', methods=['POST'])
    def api_open():
        """Manual Spread Trade. With entry_spread the order is ARMED
        and fires when the spread reaches that level; without it the
        pair goes on immediately at market. exit_spread and overnight
        travel with the trade."""
        payload = request.get_json(silent=True) or {}
        if not payload.get('asset') or not payload.get('direction'):
            return jsonify({'success': False,
                            'error': 'asset and direction required'}), 400
        write_control({'open': {
            'asset': payload['asset'],
            'direction': payload['direction'],
            'lots': payload.get('lots'),
            'entry_spread': payload.get('entry_spread'),
            'exit_spread': payload.get('exit_spread'),
            'overnight': payload.get('overnight', 'ALLOW'),
            'ts': time.time()}})
        armed = payload.get('entry_spread') is not None
        return jsonify({'success': True, 'armed': armed,
                        'note': ('Armed — waiting for the spread to reach '
                                 'your entry level.' if armed else
                                 'Sent — opening at market now.')})

    @app.route('/api/manual-trade', methods=['DELETE'])
    @app.route('/api/manual-trade/cancel', methods=['POST'])
    def api_manual_cancel():
        """Disarm a pending manual trade (does not touch open
        positions — use the close button for those)."""
        write_control({'open': {'asset': None, 'ts': time.time()}})
        return jsonify({'success': True, 'note': 'Manual trade cancelled.'})

    @app.route('/api/manual-trade', methods=['GET'])
    def api_manual_status():
        status = runtime_status()
        order = status.get('manual_order')
        first = (status.get('assets') or [{}])[0]
        return jsonify({
            'armed': bool(order), 'order': order,
            'current_spread': first.get('swap_diff'),
            'asset': first.get('asset'),
        })

    @app.route('/api/engine/test', methods=['POST'])
    def api_test():
        kind = (request.get_json(silent=True) or {}).get('kind')
        if kind not in ('connectivity', 'orders'):
            return jsonify({'error': 'kind must be connectivity|orders'}), 400
        write_control({'test': {'kind': kind, 'ts': time.time()}})
        return jsonify({'ok': True, 'success': True})

    @app.route('/api/spread-history')
    def api_spread_history():
        n = min(int(request.args.get('n', 100)), 5000)
        asset = request.args.get('asset')
        where = "WHERE asset=?" if asset else ""
        args = ((asset, n) if asset else (n,))
        rows = query(f"SELECT actual_basis, swap_basis, z FROM market_data "
                     f"{where} ORDER BY timestamp DESC LIMIT ?", args)
        rows.reverse()
        return jsonify({
            'spreads': [(r['actual_basis'] or 0) - (r['swap_basis'] or 0)
                        for r in rows],
            'zscores': [r['z'] for r in rows],
        })

    @app.route('/api/trades')
    def api_trades():
        limit = min(int(request.args.get('limit', 50)), 1000)
        rows = query("SELECT * FROM trade_review ORDER BY closed DESC "
                     "LIMIT ?", (limit,))
        return jsonify([webapi.trade_to_ui(r) for r in rows])

    @app.route('/api/trade-journal')
    def api_trade_journal():
        rows = query("SELECT * FROM trade_review ORDER BY closed DESC "
                     "LIMIT 500")
        return jsonify({'trades': [webapi.trade_to_ui(r) for r in rows],
                        'statistics': webapi.statistics_from_rows(rows)})

    @app.route('/api/sd-touches')
    def api_sd_touches():
        asset = request.args.get('asset')
        limit = min(int(request.args.get('limit', 500)), 5000)
        where = "WHERE asset=?" if asset else ""
        args = ((asset, limit) if asset else (limit,))
        rows = query(f"SELECT * FROM sd_touches {where} "
                     f"ORDER BY timestamp DESC LIMIT ?", args)
        return jsonify(rows)

    @app.route('/api/sd-touches/clear', methods=['POST'])
    def api_sd_clear():
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM sd_touches")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        conn.close()
        return jsonify({'success': True})

    @app.route('/api/shadow-summary')
    def api_shadow_summary():
        rows = query("SELECT * FROM shadow_trades ORDER BY completed DESC "
                     "LIMIT 50")
        status = (runtime_status().get('shadow') or {})
        summary = {'count': len(rows), 'active': status.get('active', 0),
                   'tracking': status.get('tracking', []),
                   'recent': rows, 'open_live': None}
        if len(rows) >= 5:      # W3 rule: aggregates are noise below 5
            target = [r for r in rows if r['verdict'] == 'REVERTED_TO_TARGET']
            be = [r for r in rows if r['verdict'] == 'REVERTED_TO_BREAK_EVEN']

            def median(values):
                values = sorted(v for v in values if v is not None)
                return values[len(values) // 2] if values else None
            summary.update({
                'revert_target_rate': 100 * len(target) / len(rows),
                'revert_be_rate': 100 * (len(target) + len(be)) / len(rows),
                'median_target_min': median([r['hit_tp_min']
                                             for r in target]),
                'median_be_min': median([r['hit_be_min']
                                         for r in target + be]),
                'avg_peak_usd': sum(r['peak'] or 0 for r in rows) / len(rows),
                'reverted_target': len(target), 'reverted_be': len(be),
            })
        return jsonify(summary)

    @app.route('/api/open-position-live')
    def api_open_position_live():
        positions = runtime_status().get('positions') or []
        if not positions:
            return jsonify({'open': False, 'row': None})
        p = positions[0]
        return jsonify({'open': True, 'row': {
            'id': p.get('position_id'),
            'position_type': ('SHORT' if p.get('signal_type') == 'SELL_BASIS'
                              else 'LONG'),
            'exit_reason': 'OPEN', 'is_live': True,
            'pnl_usd': p.get('net_pnl'),
            'peak_net_usd': p.get('peak_pnl'),
            'trough_net_usd': p.get('trough_pnl'),
            'peak_minutes': None, 'trough_minutes': None,
            'quantity': p.get('lots'), 'notional_usd': p.get('notional'),
        }})

    @app.route('/api/account-info')
    def api_account_info():
        """Per-account margin picture. With two brokers, margin is
        managed separately on each account — the UI shows both, plus a
        combined roll-up, and flags the WEAKEST margin level (that is
        the one that gets you liquidated)."""
        status = runtime_status()
        accounts = list((status.get('accounts') or {}).values())
        raw = load_config_raw()
        legs = raw.get('leg_accounts') or {}
        if not accounts:                       # not connected yet
            for name in dict.fromkeys(
                    [legs.get('spot'), legs.get('futures')]):
                if name:
                    accounts.append({'account': name, 'login': None,
                                     'roles': [r for r, n in legs.items()
                                               if n == name],
                                     'connected': False})
        totals = {'equity': 0.0, 'balance': 0.0, 'margin': 0.0,
                  'margin_free': 0.0, 'profit': 0.0}
        weakest = None
        for acct in accounts:
            acct.setdefault('connected', acct.get('login') is not None)
            for key in totals:
                totals[key] += acct.get(key) or 0.0
            level = acct.get('margin_level')
            if level and (weakest is None or level < weakest['margin_level']):
                weakest = {'account': acct.get('account'),
                           'margin_level': level}
        totals['margin_level'] = (100 * totals['equity'] / totals['margin']
                                  if totals['margin'] else None)
        return jsonify({
            'exchange': 'MT5',
            'connected': any(a.get('connected') for a in accounts),
            'has_api_keys': True, 'has_adapters': bool(accounts),
            'is_demo': status.get('mode') != 'LIVE',
            'accounts': accounts, 'totals': totals, 'weakest': weakest,
            # Flat roll-up for the single-account header widgets
            'uid': ' / '.join(str(a.get('login')) for a in accounts
                              if a.get('login')) or '-',
            'equity': totals['equity'], 'available': totals['margin_free'],
            'balance': totals['balance'], 'margin': totals['margin'],
            'margin_free': totals['margin_free'],
            'margin_level': totals['margin_level'],
            'unrealized_pnl': totals['profit'],
        })

    @app.route('/api/active-orders')
    def api_active_orders():
        return jsonify([])

    @app.route('/api/exchange-positions')
    def api_exchange_positions():
        positions = runtime_status().get('positions') or []
        return jsonify({'exchange_has_position': bool(positions),
                        'positions': positions})

    @app.route('/api/beta-zscore')
    def api_beta_zscore():
        return jsonify(runtime_status().get('beta_drift')
                       or {'status': 'STABLE', 'zscore': 0})

    @app.route('/api/instruments')
    def api_instruments():
        raw = load_config_raw()
        symbols = []
        for asset in (raw.get('assets') or {}).values():
            symbols += list(asset.get('spot_symbols') or [])
            symbols += list(asset.get('futures_symbols') or [])
        return jsonify({'instruments': sorted(set(symbols)),
                        'spot': sorted(set(symbols)), 'swap': [],
                        'futures': sorted(set(symbols))})

    @app.route('/api/leg-prices')
    def api_leg_prices():
        status = runtime_status()
        first = (status.get('assets') or [{}])[0]
        return jsonify({'leg_a': first.get('spot_price'),
                        'leg_b': first.get('futures_price')})

    @app.route('/api/telegram/config', methods=['GET', 'POST'])
    def api_telegram_config():
        raw = load_config_raw()
        if request.method == 'GET':
            telegram = dict(raw.get('telegram') or {})
            telegram['telegram_bot_token'] = (
                '***' if os.environ.get('TELEGRAM_BOT_TOKEN') else '')
            telegram['telegram_chat_id'] = os.environ.get(
                'TELEGRAM_CHAT_ID', '')
            return jsonify(telegram)
        payload = request.get_json(silent=True) or {}
        env_updates = {}
        token = payload.get('telegram_bot_token') or payload.get('token')
        chat = payload.get('telegram_chat_id') or payload.get('chat_id')
        if token and token != '***':
            env_updates['TELEGRAM_BOT_TOKEN'] = token
        if chat:
            env_updates['TELEGRAM_CHAT_ID'] = str(chat)
        if env_updates:
            update_env_file(env_path, env_updates)
        raw.setdefault('telegram', {})
        for field, key in (('telegram_enabled', 'ENABLED'),
                           ('telegram_notify_trades', 'NOTIFY_TRADES'),
                           ('telegram_notify_signals', 'NOTIFY_SIGNALS'),
                           ('telegram_notify_errors', 'NOTIFY_ERRORS')):
            if field in payload:
                raw['telegram'][key] = bool(payload[field])
        save_config_raw(raw)
        return jsonify({'success': True,
                        'note': 'Saved. Send /start to the bot.'})

    @app.route('/api/telegram/test', methods=['POST'])
    def api_telegram_test():
        token = os.environ.get('TELEGRAM_BOT_TOKEN')
        chat = os.environ.get('TELEGRAM_CHAT_ID')
        if not token:
            return jsonify({'success': False,
                            'error': 'No bot token saved yet'}), 400
        try:
            import urllib.parse
            import urllib.request
            data = urllib.parse.urlencode({
                'chat_id': chat,
                'text': '✅ Nexus test message — Telegram is wired up.',
            }).encode()
            with urllib.request.urlopen(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=data, timeout=10) as response:
                ok = json.loads(response.read().decode()).get('ok')
            return jsonify({'success': bool(ok)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 400

    # -- MT5 broker/leg management (replaces W3's exchange endpoints) --

    @app.route('/api/exchanges', methods=['GET', 'POST'])
    def api_exchanges():
        raw = load_config_raw()
        if request.method == 'GET':
            accounts = raw.get('accounts') or {}
            legs = raw.get('leg_accounts') or {}
            return jsonify([{
                'id': name, 'name': name, 'exchange_type': 'MT5',
                'terminal_path': acct.get('terminal_path') or '',
                'login': acct.get('login') or '',
                'server': acct.get('server') or '',
                'endpoint': acct.get('endpoint') or '',
                'has_password': bool(acct.get('password_env')),
                'role': ('SPOT' if legs.get('spot') == name else
                         'FUTURES' if legs.get('futures') == name else 'NONE'),
                'is_active': name in legs.values(),
            } for name, acct in accounts.items()])

        payload = request.get_json(silent=True) or {}
        name = (payload.get('name') or payload.get('id') or '').strip()
        if not name:
            return jsonify({'success': False, 'error': 'name required'}), 400
        raw.setdefault('accounts', {})
        acct = raw['accounts'].setdefault(name, {})
        for field in ('terminal_path', 'server', 'endpoint'):
            if payload.get(field) is not None:
                acct[field] = payload[field]
        if payload.get('login'):
            acct['login'] = int(payload['login'])
        password = payload.get('password')
        if password:
            var = acct.get('password_env') or f"MT5_PASSWORD_{name.upper()}"
            acct['password_env'] = var
            update_env_file(env_path, {var: password})
        save_config_raw(raw)
        return jsonify({'success': True, 'id': name,
                        'note': 'Saved. Restart the launcher to connect.'})

    @app.route('/api/exchanges/<account_id>', methods=['DELETE'])
    def api_exchange_delete(account_id):
        raw = load_config_raw()
        if (raw.get('accounts') or {}).pop(account_id, None) is None:
            return jsonify({'success': False, 'error': 'unknown'}), 404
        legs = raw.get('leg_accounts') or {}
        for role, name in list(legs.items()):
            if name == account_id:
                legs.pop(role)
        save_config_raw(raw)
        return jsonify({'success': True})

    @app.route('/api/exchanges/<account_id>/test', methods=['POST'])
    def api_exchange_test(account_id):
        write_control({'test': {'kind': 'connectivity', 'ts': time.time()}})
        return jsonify({'success': True,
                        'note': 'Connectivity test queued — results appear '
                                'on the dashboard within ~5s.'})

    @app.route('/api/set-active-exchanges', methods=['POST'])
    def api_set_active_exchanges():
        payload = request.get_json(silent=True) or {}
        raw = load_config_raw()
        legs = raw.setdefault('leg_accounts', {})
        if payload.get('spot_id'):
            legs['spot'] = payload['spot_id']
        if payload.get('futures_id'):
            legs['futures'] = payload['futures_id']
        save_config_raw(raw)
        return jsonify({'success': True,
                        'note': 'Leg mapping saved — restart the launcher.'})

    # -- maintenance / data --

    @app.route('/api/trades/clear', methods=['POST'])
    def api_trades_clear():
        conn = sqlite3.connect(db_path)
        for table in ('trade_review', 'trades', 'positions',
                      'shadow_trades'):
            try:
                conn.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.close()
        return jsonify({'success': True})

    @app.route('/api/spread-history/clear', methods=['POST'])
    def api_spread_clear():
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DELETE FROM market_data")
            conn.commit()
        except sqlite3.OperationalError:
            pass
        conn.close()
        return jsonify({'success': True})

    @app.route('/api/reset-trades', methods=['POST'])
    def api_reset_trades():
        return api_trades_clear()

    @app.route('/api/reset-all', methods=['POST'])
    def api_reset_all():
        api_trades_clear()
        api_spread_clear()
        return jsonify({'success': True})

    @app.route('/api/untracked')
    def api_untracked():
        return jsonify(query("SELECT * FROM untracked_closes "
                             "ORDER BY timestamp DESC LIMIT 50"))

    # -- endpoints the W3 UI calls that have no MT5 equivalent --

    @app.route('/api/anthropic/status')
    def api_anthropic_status():
        return jsonify({'configured': False, 'enabled': False})

    @app.route('/api/ai-insights')
    def api_ai_insights():
        return jsonify([])

    @app.route('/api/learning-log')
    def api_learning_log():
        return jsonify([])

    @app.route('/api/spot-holdings')
    def api_spot_holdings():
        return jsonify({'holdings': []})

    @app.route('/api/exchange-orders')
    def api_exchange_orders():
        return jsonify([])

    # -- MT5 order tests (W3's test-order / test-suite panels) --

    def _test_state():
        results = runtime_status().get('test_results') or {}
        rows = results.get('results') or []
        return {
            'running': False,
            'kind': results.get('kind'),
            'ts': results.get('ts'),
            'results': rows,
            'scenarios': rows,
            'passed': sum(1 for r in rows if r.get('ok')),
            'failed': sum(1 for r in rows if not r.get('ok')),
            'total': len(rows),
        }

    @app.route('/api/test-suite/status')
    @app.route('/api/test-order/status')
    def api_test_suite_status():
        return jsonify(_test_state())

    @app.route('/api/test-suite/start', methods=['POST'])
    @app.route('/api/test-suite/run-scenario', methods=['POST'])
    @app.route('/api/test-order/open', methods=['POST'])
    def api_test_suite_start():
        """Run the MT5 order round-trip suite: far limit place ->
        verify resting -> cancel -> verify clean, then market open ->
        close by position ticket, on every configured leg."""
        write_control({'test': {'kind': 'orders', 'ts': time.time()}})
        return jsonify({'success': True, 'running': True,
                        'note': 'Order round-trip test queued. Requires '
                                'the algo stopped and a flat book; '
                                'results appear within ~5s.'})

    @app.route('/api/test-suite/stop', methods=['POST'])
    @app.route('/api/test-suite/reset', methods=['POST'])
    @app.route('/api/test-order/close', methods=['POST'])
    @app.route('/api/test-order/close-all', methods=['POST'])
    def api_test_suite_stop():
        """Order tests are single-shot and self-closing (every order
        they place is cancelled or closed in the same pass), so there
        is nothing to stop — this clears the displayed results."""
        write_control({'test': {'kind': None, 'ts': time.time()}})
        return jsonify({'success': True, 'running': False})

    # -- position/broker maintenance (MT5 equivalents of W3's crypto
    # housekeeping endpoints) --

    @app.route('/api/engine/sync-position', methods=['POST'])
    @app.route('/api/close-orphaned-spot', methods=['POST'])
    def api_sync_position():
        """Force a reconciliation pass: engine book vs broker book."""
        write_control({'reconcile': {'ts': time.time()}})
        return jsonify({'success': True,
                        'note': 'Reconciliation requested — orphans are '
                                'closed by ticket after 3 strikes and '
                                'booked to the untracked ledger.'})

    @app.route('/api/close-exchange-position', methods=['POST'])
    def api_close_exchange_position():
        return api_close_position()

    @app.route('/api/balance-debug')
    def api_balance_debug():
        return jsonify({'accounts': runtime_status().get('accounts') or {},
                        'note': 'Balances come from each MT5 leg runner.'})

    @app.route('/api/sweep-dust', methods=['POST'])
    @app.route('/api/engine/set-demo-mode', methods=['POST'])
    def api_not_applicable():
        return jsonify({'success': False, 'not_applicable': True,
                        'note': 'Not applicable to MT5. Demo vs live is a '
                                'property of each broker account; set the '
                                'account on the Brokers page and paper/live '
                                'in Settings.'}), 400

    # ---------------- native endpoints (tools, tests) ----------------

    @app.route('/api/summary')
    def api_summary():
        return jsonify(runtime_status())

    @app.route('/api/positions')
    def api_positions():
        return jsonify(runtime_status().get('positions', []))

    @app.route('/api/reviews')
    def api_reviews():
        limit = min(int(request.args.get('limit', 100)), 1000)
        return jsonify(query("SELECT * FROM trade_review "
                             "ORDER BY closed DESC LIMIT ?", (limit,)))

    @app.route('/api/market')
    def api_market():
        asset = request.args.get('asset', 'GOLD')
        limit = min(int(request.args.get('limit', 600)), 5000)
        return jsonify(query(
            "SELECT timestamp, spot_price, futures_price, actual_basis, "
            "swap_basis, swap_premium_pct, z, signal FROM market_data "
            "WHERE asset=? ORDER BY timestamp DESC LIMIT ?", (asset, limit)))

    @app.route('/api/analysis')
    def api_analysis():
        rows = query("SELECT realized_pnl, peak_pnl, peak_min, outcome "
                     "FROM trade_review WHERE realized_pnl IS NOT NULL "
                     "ORDER BY closed DESC")
        stats = webapi.statistics_from_rows(rows)
        tags = {}
        for r in rows:
            if r.get('outcome'):
                tags[r['outcome']] = tags.get(r['outcome'], 0) + 1
        return jsonify({
            'total': stats['total_trades'], 'winners': stats['winning_trades'],
            'losers': stats['losing_trades'], 'win_rate': stats['win_rate'],
            'total_pnl': stats['total_pnl'],
            'expectancy': stats['avg_pnl'], 'avg_win': stats['avg_win'],
            'avg_loss': stats['avg_loss'], 'rr': stats['reward_risk'],
            'pf': stats['profit_factor'], 'be_wr': stats['breakeven_wr'],
            'max_dd': stats['max_drawdown'],
            'median_peak_min': stats['median_peak_minutes'],
            'p70_peak': stats['p70_peak'], 'tags': tags,
        })

    @app.route('/api/shadow')
    def api_shadow():
        return api_shadow_summary()

    @app.route('/healthz')
    def healthz():
        return jsonify({'ok': True, 'time': datetime.now().isoformat()})

    # ---------------- live push (socket.io) ----------------
    # The Nexus dashboard shows Connected/Disconnected from a socket.io
    # session and refreshes prices on 'tick' / 'signal' events. The
    # coordinator writes runtime_status.json; this thread turns that
    # into the events the UI expects. Polling still works if
    # flask-socketio is missing — the page just falls back to it.

    app.socketio = None
    if SocketIO is not None:
        socketio = SocketIO(app, cors_allowed_origins='*',
                            async_mode='threading', logger=False,
                            engineio_logger=False)
        app.socketio = socketio

        def broadcast_loop():
            last = None
            while True:
                socketio.sleep(1.0)
                try:
                    status = ui_status()
                except Exception:
                    continue
                if not status.get('is_running'):
                    continue
                stamp = status.get('updated')
                if stamp == last:
                    continue          # coordinator hasn't refreshed yet
                last = stamp
                spot, futures = status.get('spot_tick'), \
                    status.get('futures_tick')
                if spot and futures:
                    trade = status.get('open_trade') or {}
                    socketio.emit('tick', {
                        'spot': spot, 'futures': futures,
                        'position_pnl': trade.get('unrealized_pnl'),
                        'timestamp': stamp})
                if status.get('signal'):
                    socketio.emit('signal', status['signal'])
                socketio.emit('status', {
                    'is_running': True,
                    'algo_enabled': status.get('algo_enabled'),
                    'paper_trading': status.get('paper_trading')})

        @socketio.on('connect')
        def on_connect():
            status = ui_status()
            socketio.emit('status', {
                'is_running': status.get('is_running'),
                'algo_enabled': status.get('algo_enabled'),
                'paper_trading': status.get('paper_trading')})

        socketio.start_background_task(broadcast_loop)

    return app


def run_app(app, host='127.0.0.1', port=8080):
    """Serve with socket.io when available, plain Flask otherwise."""
    if getattr(app, 'socketio', None) is not None:
        app.socketio.run(app, host=host, port=port,
                         allow_unsafe_werkzeug=True)
    else:
        app.run(host=host, port=port)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Nexus control panel")
    parser.add_argument('--db', default='algo_trading.db')
    parser.add_argument('--status', default='runtime_status.json')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--control', default='control.json')
    parser.add_argument('--env', default='.env')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()

    app = create_app(args.db, args.status, args.config, args.control,
                     args.env)
    print(f"Nexus control panel: http://{args.host}:{args.port}")
    run_app(app, args.host, args.port)


if __name__ == '__main__':
    main()
