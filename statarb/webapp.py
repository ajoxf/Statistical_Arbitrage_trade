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

import csv
import io
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime

try:
    from flask import Flask, Response, jsonify, render_template, request
except ImportError:
    Flask = None

try:
    from flask_socketio import SocketIO
except ImportError:
    SocketIO = None            # UI falls back to polling

from . import diagnostics, hedgeratio, ipc, scenarios, sizing, webapi
from .database import DataLogger

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(BASE_DIR, 'templates')
STATIC_DIR = os.path.join(BASE_DIR, 'static')

# How often the socket bridge looks for a fresh runtime_status.json.
# The dashboard's own UPDATE_INTERVAL is 300ms; this has to be at least
# that quick or the screen updates at THIS rate instead.
BROADCAST_INTERVAL_SEC = 0.2


def env_var_name(prefix, name):
    """A .env key that dotenv can actually read. An account called
    'Ut 2' would otherwise produce MT5_PASSWORD_UT 2 — a key with a
    space, which makes dotenv fail to parse the line and silently
    leaves the password unset, so MT5 login fails with no clue why."""
    cleaned = ''.join(ch if ch.isalnum() else '_' for ch in str(name))
    cleaned = '_'.join(part for part in cleaned.split('_') if part)
    return f"{prefix}{cleaned.upper() or 'DEFAULT'}"


def _env_quote(value):
    """Quote a value so any password (spaces, #, quotes) survives."""
    text = str(value).replace('\\', '\\\\').replace('"', '\\"')
    return f'"{text}"'


def update_env_file(path, updates):
    """Merge key=value pairs into a .env file, preserving other lines.
    This is how the UI stores secrets — they never touch config.json.

    Values are quoted, and lines whose key is not a legal env-var name
    are dropped: one malformed line makes dotenv give up on the
    statement and the credential it holds never reaches the engine."""
    lines, dropped = [], []
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            lines = [line.rstrip('\n') for line in f]

    updates = {env_var_name('', key) if not key.replace('_', '').isalnum()
               else key: value for key, value in updates.items()}
    keys = set(updates)
    kept = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            kept.append(line)
            continue
        key = stripped.split('=', 1)[0].strip()
        if key in keys:
            continue                      # replaced below
        if '=' not in stripped or not key or not key.replace('_', '')\
                .isalnum() or key[0].isdigit():
            dropped.append(line)          # dotenv cannot read this line
            continue
        kept.append(line)
    kept += [f"{key}={_env_quote(value)}" for key, value in updates.items()]

    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write("\n".join(kept) + "\n")
    os.replace(tmp, path)
    for key, value in updates.items():
        os.environ[key] = str(value)  # visible to this process immediately
    return dropped


def create_app(db_path="algo_trading.db", status_path="runtime_status.json",
               config_path="config.json", control_path="control.json",
               env_path=".env", scenario_timeout=90.0,
               diagnose_timeout=45.0):
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
        """The config, or the last good copy of it — never a silent {}.

        Every save on this page is a read-modify-write. Returning {} for
        a file that EXISTS but could not be parsed therefore does not
        degrade gracefully: the next save writes that {} back and takes
        the accounts, the leg mapping, the symbols and every setting
        with it.

        Live 2026-08-11: the coordinator rewrote config.json
        non-atomically while adopting broker specs, a page load read
        the half-written file, and the Exchanges page went from two
        accounts to "No accounts configured yet" while the engine
        carried on trading from its in-memory copy.

        Missing is legitimately empty (first run). Present-but-broken
        falls back to the backup, and failing that raises — refusing
        the save is the only safe answer.
        """
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            try:
                with open(config_path + '.bak', 'r', encoding='utf-8') as f:
                    backup = json.load(f)
            except (OSError, ValueError):
                raise RuntimeError(
                    f'config.json could not be read ({e}) and there is no '
                    f'usable backup beside it. Refusing to continue, '
                    f'because saving now would overwrite it with nothing.'
                ) from None
            logging.error('config.json unreadable (%s) — using config.json'
                          '.bak. The next save will rewrite the good copy.',
                          e)
            return backup

    # Top-level keys whose disappearance is a catastrophe rather than an
    # edit: they are what lets the engine start at all.
    _CONFIG_CRITICAL = ('accounts', 'leg_accounts', 'assets')

    def save_config_raw(raw, allow_shrink=False):
        """Write the config, keeping a backup and refusing to gut it.

        `allow_shrink` is for the endpoints that legitimately remove
        things (deleting an account or a pair). Everything else is a
        partial edit and must not be able to drop a section it never
        meant to touch.
        """
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                current = json.load(f)
        except (OSError, ValueError):
            current = None
        if current and not allow_shrink:
            lost = [key for key in _CONFIG_CRITICAL
                    if current.get(key) and not raw.get(key)]
            if lost:
                raise RuntimeError(
                    'refusing to save a config that would drop '
                    + ', '.join(lost)
                    + ' — this looks like a partial read, not an edit')
        if current is not None:
            # Last known-good, written before the new one goes down.
            tmp_bak = config_path + '.bak.tmp'
            with open(tmp_bak, 'w', encoding='utf-8') as f:
                json.dump(current, f, indent=2)
            os.replace(tmp_bak, config_path + '.bak')
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

    def normalise_endpoint(endpoint):
        """(ok, 'host:port') or (False, message). Blank is legal — it
        means this account has no leg runner (both legs, one account)."""
        if endpoint in (None, ''):
            return True, endpoint
        try:
            host, port = ipc.parse_endpoint(endpoint)
        except ValueError as e:
            return False, str(e)
        return True, f'{host}:{port}'

    def _endpoint_clash(raw, name, endpoint):
        """Is another account already on this endpoint? Message or None.

        Only one process can listen on a port. Two accounts sharing one
        means the second leg runner cannot start — or, if the first won
        the race, BOTH legs connect to it and trade the SAME MT5
        account while every screen reports two. Live 2026-08-11:
        'Utsav Khanchandani' and 'MT5' both saved at 127.0.0.1:9101.

        Caught here rather than only at startup, because the operator
        is on this page with the field in front of them; a restart
        later, the traceback is a long way from the cause.
        """
        if not endpoint:
            return None            # blank = no runner; any number may
        for other, acct in (raw.get('accounts') or {}).items():
            if other == name:
                continue
            if ((acct or {}).get('endpoint') or '').strip() == endpoint:
                return (f"Endpoint {endpoint} already belongs to account "
                        f"'{other}'. One port serves ONE leg runner — give "
                        f"this account its own (e.g. {_next_free_port(raw)}), "
                        f"or both legs would end up on the same terminal.")
        return None

    def _running_legs():
        """{account: 'SPOT'/'FUTURES'/'SPOT+FUTURES'} for the topology
        the coordinator PROCESS is running, which is not necessarily
        the one in config.json — accounts and leg mapping are
        structural and only take effect on a restart."""
        running = runtime_status().get('running_legs') or {}
        out = {}
        for role in ('spot', 'futures'):
            name = running.get(role)
            if name:
                out[name] = ('SPOT+FUTURES' if out.get(name)
                             else role.upper())
        return out

    def _login_clash(raw, name, login):
        """Is another account already using this login? Message or None.

        Two rows with one login is the shared-terminal fault a layer up:
        both legs would trade the SAME MT5 account, hedging against
        themselves, while every screen reports two accounts. One
        terminal holds one login, so two legs on one login is not a
        topology — it is the same account twice."""
        if not login:
            return None
        for other, acct in (raw.get('accounts') or {}).items():
            if other == name:
                continue
            if str((acct or {}).get('login') or '') == str(login):
                return (f"Login {login} already belongs to account "
                        f"'{other}'. Two accounts on one login is the same "
                        f"MT5 account twice — both legs would trade it and "
                        f"hedge against themselves.")
        return None

    def _terminal_clash(raw, name, path):
        """Is another account already using this MT5 installation?

        Completes the set with the port and the login guards: one
        account needs one PORT, one LOGIN and one TERMINAL. A terminal
        holds a single login, so two accounts sharing an installation
        are one account — both leg runners attach to it, both trade
        whatever it happens to be signed into, and the pair hedges
        against itself.

        The coordinator already refuses this at startup. Catching it at
        save is the difference between a corrected field and five
        restart attempts (live 2026-08-18, twice)."""
        path = (path or '').strip()
        if not path:
            return None          # blank = attach to whatever is open
        for other, acct in (raw.get('accounts') or {}).items():
            if other == name:
                continue
            if ((acct or {}).get('terminal_path')
                    or '').strip().lower() == path.lower():
                return (f"Account '{other}' already uses this MT5 "
                        f"installation. One terminal serves ONE login, so "
                        f"both legs would end up on the same account. "
                        f"Install a second copy of MetaTrader 5 in its own "
                        f"folder (or use a portable copy) and point this "
                        f"account at that one.")
        return None

    def _next_free_port(raw, host='127.0.0.1', first=9101):
        used = {(acct or {}).get('endpoint', '') for acct
                in (raw.get('accounts') or {}).values()}
        port = first
        while f'{host}:{port}' in used:
            port += 1
        return f'{host}:{port}'

    @app.errorhandler(RuntimeError)
    def _config_guard(error):
        """A refused or impossible config write, said out loud.

        These are raised by load_config_raw / save_config_raw when the
        file on disk cannot be trusted. A 500 page would leave the
        operator with a dead button and no idea the config was the
        problem."""
        logging.error('Config refused: %s', error)
        return jsonify({'success': False, 'error': str(error)}), 503

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
            slippage=webapi.slippage_block(rows),
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
        _stamp_beta(raw, old_beta)
        save_config_raw(raw)
        note = " ".join(notes) or \
            "Saved — the engine hot-reloads within ~10s."
        return jsonify({'success': True, 'status': 'ok', 'note': note})

    def _stamp_beta(raw, old_beta):
        """Record which PAIR a hand-set HEDGE_RATIO was chosen for.

        The engine re-derives beta at startup whenever the stamp does
        not match the running pair. Without stamping the operator's own
        save, this sequence loses their choice: change the symbols,
        type the right beta in Settings, restart — and the engine sees
        a stamp still naming the OLD pair and overwrites the number
        that was just typed. A beta the operator set IS set for the
        currently configured pair, by definition.
        """
        trading = raw.get('trading') or {}
        new_beta = trading.get('HEDGE_RATIO')
        if new_beta is None or float(new_beta) == float(old_beta or 0.0):
            return
        asset = next((v for v in (raw.get('assets') or {}).values()
                      if v.get('enabled', True)), {})
        spot = (asset.get('spot_symbols') or [''])[0]
        futures = (asset.get('futures_symbols') or [''])[0]
        if spot and futures:
            trading['HEDGE_RATIO_FOR'] = hedgeratio.pair_signature(spot,
                                                                   futures)
            raw['trading'] = trading

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
                           or env_var_name('MT5_PASSWORD_', name))
                    acct['password_env'] = var
                    env_updates[var] = password
                elif not acct.get('password_env'):
                    existing = (raw.get('accounts', {}).get(name, {})
                                or {}).get('password_env')
                    if existing:
                        acct['password_env'] = existing
            for name, acct in payload['accounts'].items():
                ok, endpoint_or_error = normalise_endpoint(
                    (acct or {}).get('endpoint'))
                if not ok:
                    return jsonify({'success': False,
                                    'error': f"{name}: "
                                             f"{endpoint_or_error}"}), 400
                if acct is not None:
                    acct['endpoint'] = endpoint_or_error
            # Same rule as the single-row save: one port, one runner.
            seen = {}
            for name, acct in payload['accounts'].items():
                endpoint = ((acct or {}).get('endpoint') or '').strip()
                if endpoint and endpoint in seen:
                    return jsonify({
                        'success': False,
                        'error': f"'{name}' and '{seen[endpoint]}' are both "
                                 f"on {endpoint}. One port serves ONE leg "
                                 f"runner, so both legs would end up on the "
                                 f"same terminal."}), 400
                if endpoint:
                    seen[endpoint] = name
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
        _stamp_beta(raw, old_beta)
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
        """Manual Spread Trade: ENTRY, TAKE PROFIT and STOP LOSS, all
        as spread levels. With entry_spread the order is ARMED and
        fires when the spread reaches that level; without it the pair
        goes on immediately at market. exit_spread (take profit),
        stop_spread and overnight travel with the trade.

        The level geometry is checked here as well as in the engine —
        the browser can be bypassed, and an upside-down stop is the
        one mistake on this panel that costs money immediately."""
        payload = request.get_json(silent=True) or {}
        if not payload.get('asset') or not payload.get('direction'):
            return jsonify({'success': False,
                            'error': 'asset and direction required'}), 400
        direction = payload['direction']
        if direction not in ('SELL_BASIS', 'BUY_BASIS'):
            return jsonify({'success': False,
                            'error': 'direction must be SELL_BASIS '
                                     '(short spread) or BUY_BASIS '
                                     '(long spread)'}), 400
        entry = payload.get('entry_spread')
        take_profit = payload.get('exit_spread')
        stop = payload.get('stop_spread')
        # Fire-now orders are measured against the live spread, since
        # that is what they will open at.
        reference = entry
        if reference is None:
            first = (runtime_status().get('assets') or [{}])[0]
            reference = first.get('spread')
        bad = webapi.manual_level_error(direction, reference,
                                        take_profit, stop)
        if bad:
            return jsonify({'success': False, 'error': bad}), 400
        write_control({'open': {
            'asset': payload['asset'],
            'direction': direction,
            'lots': payload.get('lots'),
            'entry_spread': entry,
            'exit_spread': take_profit,
            'stop_spread': stop,
            'overnight': payload.get('overnight', 'ALLOW'),
            'ts': time.time()}})
        armed = entry is not None
        return jsonify({'success': True, 'armed': armed,
                        'note': ('Armed — waiting for the spread to reach '
                                 'your entry level. This fires even while '
                                 'the algo is stopped.' if armed else
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
            'current_spread': first.get('spread'),
            'asset': first.get('asset'),
            # Whether the ENGINE accepted the last request. A refusal
            # (circuit breaker, max positions, an unfilled pair) used
            # to live only in the log file.
            'note': status.get('manual_note'),
            'algo_enabled': status.get('algo_enabled', True),
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
        rows = query(f"SELECT spread, z FROM market_data "
                     f"{where} ORDER BY timestamp DESC LIMIT ?", args)
        rows.reverse()
        return jsonify({
            'spreads': [r['spread'] or 0 for r in rows],
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
        live = status.get('accounts') or {}
        raw = load_config_raw()
        legs = raw.get('leg_accounts') or {}
        configured = raw.get('accounts') or {}

        # Every CONFIGURED account is listed, whether or not a leg
        # currently points at it and whether or not it has connected —
        # the operator needs to see what the engine knows about.
        accounts = []
        for name, cfg in configured.items():
            entry = dict(live.get(name) or {})
            entry.setdefault('account', name)
            entry.setdefault('login', cfg.get('login'))
            entry.setdefault('server', cfg.get('server'))
            entry['roles'] = [role for role, n in legs.items() if n == name]
            entry['connected'] = name in live
            entry['in_config'] = True
            accounts.append(entry)
        # Live but NOT in the saved config. Worth listing — it is real
        # money on a real account — but it must not look like the rows
        # above it. The engine keeps reporting whatever it loaded at
        # startup, so after a config is lost or edited this panel goes
        # on showing accounts the file no longer has, and the operator
        # reads two screens that flatly contradict each other
        # (2026-08-11: "No accounts are configured. Still it is
        # connected to accounts 100004 and 100006").
        for name, info in live.items():
            if name not in configured:
                entry = dict(info)
                entry['connected'] = True
                entry['roles'] = [r for r, n in legs.items() if n == name]
                entry['in_config'] = False
                accounts.append(entry)
        totals = {'equity': 0.0, 'balance': 0.0, 'margin': 0.0,
                  'margin_free': 0.0, 'profit': 0.0}
        weakest = None
        for acct in accounts:
            for key in totals:
                totals[key] += acct.get(key) or 0.0
            level = acct.get('margin_level')
            if level and (weakest is None or level < weakest['margin_level']):
                weakest = {'account': acct.get('account'),
                           'margin_level': level}
        totals['margin_level'] = (100 * totals['equity'] / totals['margin']
                                  if totals['margin'] else None)
        # What the NEXT trade would tie up. IMR/MMR/liquidation are
        # per-position and stay blank until one is open, but this is
        # knowable while flat — and it is the number that says whether
        # the configured clip is affordable at all.
        first_asset = (status.get('assets') or [{}])[0]
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
            'capital_required': first_asset.get('capital_required'),
            'capital_buffer_pct': first_asset.get('capital_buffer_pct'),
            'clip_lots': first_asset.get('clip_lots'),
        })

    @app.route('/api/active-orders')
    def api_active_orders():
        # STUB. The engine's resting limit orders are visible in the
        # Exchange Order Log (state 'working'); this W3 endpoint has
        # never been wired to them. Kept so the page's table renders
        # its empty state rather than erroring.
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
        """Mid prices for the two symbols the operator is looking at.

        The beta suggestion and the capital preview both read this. It
        used to return only the RUNNING asset's prices, so it went
        blank — "unavailable" — in exactly the situations where the
        operator needs it: setting a pair up for the first time, or
        after changing a symbol that the engine has not adopted yet.
        The symbols are passed in, so ask the broker about THOSE rather
        than reporting whatever the engine happens to be streaming.
        """
        wanted_a = (request.args.get('leg_a') or '').strip()
        wanted_b = (request.args.get('leg_b') or '').strip()
        first = (runtime_status().get('assets') or [{}])[0]
        prices = {'leg_a': None, 'leg_b': None}

        # The engine's own figures are free and already fresh, but only
        # when they belong to the symbols being asked about.
        if wanted_a and first.get('rt_spot_symbol') == wanted_a:
            prices['leg_a'] = first.get('spot_price')
        if wanted_b and first.get('rt_fut_symbol') == wanted_b:
            prices['leg_b'] = first.get('futures_price')

        for key, symbol, role in (('leg_a', wanted_a, 'spot'),
                                  ('leg_b', wanted_b, 'futures')):
            if prices[key] or not symbol:
                continue
            _, leg = leg_for_role(role)
            if leg is None:
                continue
            try:
                specs = leg.ensure_symbol(symbol) or {}
                bid, ask = specs.get('bid'), specs.get('ask')
                if bid and ask:
                    prices[key] = (bid + ask) / 2
                elif bid or ask:
                    prices[key] = bid or ask
            except Exception:
                pass                    # a blank suggestion, not a 500
            finally:
                close = getattr(leg, 'close', None)
                if close:
                    close()

        # The page reads success / suggested_beta / leg_a_price /
        # leg_b_price; this returned only {leg_a, leg_b}, so the Hedge
        # Ratio suggestion read "unavailable" from the day it shipped.
        a_price, b_price = prices['leg_a'], prices['leg_b']
        if not (a_price and b_price):
            return jsonify({'success': False, 'error': 'unavailable',
                            **prices})

        # WHAT to suggest depends on the pair, and the rule lives in
        # hedgeratio.suggest — the same function the engine adopts from
        # at startup. Two copies would let the page recommend one beta
        # while a restart silently applied another.
        asset = next((v for v in (load_config_raw().get('assets')
                                  or {}).values()
                      if v.get('enabled', True)), {})
        pair_type = (asset.get('pair_type') or 'SPOT_FUTURE').upper()
        beta, why = hedgeratio.suggest(pair_type, a_price, b_price)
        if beta is None:
            return jsonify({'success': False, 'error': why, **prices})
        return jsonify({'success': True, 'suggested_beta': beta,
                        'reason': why, 'pair_type': pair_type,
                        'leg_a_price': a_price, 'leg_b_price': b_price,
                        **prices})

    @app.route('/api/leg-specs')
    def api_leg_specs():
        """Each leg's tradable volumes, straight from the broker.

        The Full Order Test Suite must state the size it will send
        BEFORE it sends it, and it cannot wait for the engine: specs
        are only adopted once BOTH symbols resolve, so a single
        unresolved symbol left every row reading "—" on a page whose
        buttons place real orders. This asks the leg directly, the same
        way symbol search and [Diagnose] do, so one broken leg no
        longer hides the other leg's size.
        """
        raw = load_config_raw()
        assets = raw.get('assets') or {}
        asset_key = next((k for k, v in assets.items()
                          if v.get('enabled', True)), None)
        asset = assets.get(asset_key) or {}

        # The symbols the scenarios will ACTUALLY trade. The runner
        # builds its legs from the coordinator's active_assets, frozen
        # at startup, so after a symbol change the saved name and the
        # traded one differ until a restart. Quoting the saved one here
        # would print the new symbol's lot sizes above buttons that
        # send orders on the old symbol.
        first = (runtime_status().get('assets') or [{}])[0]
        running = {'spot': first.get('rt_spot_symbol'),
                   'futures': first.get('rt_fut_symbol')}
        out = {'asset': asset_key, 'legs': {},
               'pending_restart': webapi._pending_symbols(first, raw)}
        for role, key in (('spot', 'spot_symbols'),
                          ('futures', 'futures_symbols')):
            symbol = running.get(role) or (asset.get(key) or [''])[0]
            entry = {'symbol': symbol, 'ok': False}
            name, leg = leg_for_role(role)
            entry['account'] = name
            if symbol and leg is not None:
                try:
                    specs = leg.ensure_symbol(symbol) or {}
                except Exception as e:                  # broker/socket
                    specs = {'ok': False, 'error': str(e)}
                finally:
                    close = getattr(leg, 'close', None)
                    if close:
                        close()
                entry.update({
                    'ok': bool(specs.get('ok')),
                    'error': specs.get('error'),
                    'volume_min': specs.get('volume_min'),
                    'volume_step': specs.get('volume_step'),
                    'volume_max': specs.get('volume_max'),
                    'contract_size': specs.get('contract_size'),
                    'bid': specs.get('bid'), 'ask': specs.get('ask'),
                })
            elif not symbol:
                entry['error'] = f'No {role} symbol configured'
            else:
                entry['error'] = f'Cannot reach {name or "the account"}'
            out['legs'][role] = entry

        # What a SPREAD scenario sends: the smaller leg sized up so both
        # clear their minimum. Same function the runner calls, with the
        # same inputs — the ratio between the legs needs both CONTRACT
        # SIZES as well as beta, and the contract sizes come from the
        # config the runner reads (ScenarioRunner is built from
        # lot_size / fut_lot_size), falling back to the broker's own
        # figure so an unadopted spec still sizes rather than defaults.
        spot, fut = out['legs']['spot'], out['legs']['futures']
        if spot.get('ok') and fut.get('ok'):
            trading = raw.get('trading') or {}
            beta = trading.get('HEDGE_RATIO', 1.0) or 1.0
            hedge_mode = str(trading.get('HEDGE_MODE', 'units')
                             or 'units').lower()
            contract_a = asset.get('lot_size') or spot.get('contract_size')
            contract_b = (asset.get('fut_lot_size')
                          or fut.get('contract_size') or contract_a)

            def _mid(entry):
                bid, ask = entry.get('bid'), entry.get('ask')
                return (bid + ask) / 2 if (bid and ask) else None

            pair_a, pair_b = sizing.matched_minimum_lots(
                spot.get('volume_min') or 0.0, fut.get('volume_min') or 0.0,
                spot.get('volume_step'), fut.get('volume_step'), beta,
                contract_a, contract_b, mode=hedge_mode,
                price_a=_mid(spot), price_b=_mid(fut))
            out['pair'] = {'spot_lots': pair_a, 'futures_lots': pair_b,
                           'hedge_mode': hedge_mode, 'hedge_ratio': beta,
                           'leg_a_contract': contract_a,
                           'leg_b_contract': contract_b}
        return jsonify(out)

    @app.route('/api/volume')
    def api_volume():
        """Traded volume for today, this week and this month.

        Read straight from the trades table, so it survives restarts
        and does not depend on the coordinator being up — unlike the
        risk manager's lots-today, which is in-memory and resets."""
        summary = DataLogger(db_path=db_path).volume_summary()
        raw = load_config_raw()
        summary['daily_lot_target'] = (raw.get('trading') or {}).get(
            'DAILY_LOT_TARGET')
        return jsonify(summary)

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
            assets = raw.get('assets') or {}
            asset_key = next((k for k, v in assets.items()
                              if v.get('enabled', True)), 'GOLD')
            asset = assets.get(asset_key) or {}
            expiry = asset.get('futures_expiry')

            # ONE account can hold BOTH legs — that is the third
            # supported topology (one broker, one account, both
            # symbols). Returning a single role made that setup
            # unrepresentable: leg_accounts held {'spot': X,
            # 'futures': X}, the row badged X as SPOT only, and the
            # page then computed FUTURES as unmapped and showed "the
            # coordinator cannot start" over a coordinator that was
            # running perfectly well (live 2026-08-07).
            def roles_of(name):
                return [role.upper() for role in ('spot', 'futures')
                        if legs.get(role) == name]

            spot_symbol = (asset.get('spot_symbols') or [''])[0]
            fut_symbol = (asset.get('futures_symbols') or [''])[0]

            def symbol_of(name):
                roles = roles_of(name)
                if roles == ['FUTURES']:
                    return fut_symbol
                return spot_symbol if roles else ''

            return jsonify([{
                'id': name, 'name': name, 'exchange_type': 'MT5',
                'terminal_path': acct.get('terminal_path') or '',
                'login': acct.get('login') or '',
                'server': acct.get('server') or '',
                'endpoint': acct.get('endpoint') or '',
                'has_password': bool(acct.get('password_env')),
                'roles': roles_of(name),
                # `role` stays single-valued for callers that predate
                # dual-leg accounts; `roles` is the whole truth.
                'role': (roles_of(name) or ['NONE'])[0],
                'is_active': name in legs.values(),
                # The symbol this account trades, so the broker row is
                # the whole story for that leg (as in the old app).
                # A dual-leg account carries both.
                'asset': asset_key,
                'spot_symbol': (spot_symbol if 'SPOT' in roles_of(name)
                                else ''),
                'futures_symbol': (fut_symbol if 'FUTURES' in roles_of(name)
                                   else ''),
                'symbol': symbol_of(name),
                'contract_size': asset.get('lot_size'),
                'swap_charge': asset.get('swap_charge'),
                'futures_expiry': (expiry[:10] if isinstance(expiry, str)
                                   else None),
                # Two accounts on one port is not a configuration, it
                # is two legs on one terminal wearing two names.
                'endpoint_clash': _endpoint_clash(
                    raw, name, (acct.get('endpoint') or '').strip()),
                # Shown ON the row. A clash was only discoverable by
                # running the connectivity check, so the operator could
                # look straight at two rows holding one terminal and
                # see nothing wrong with either.
                'terminal_clash': _terminal_clash(
                    raw, name, acct.get('terminal_path')),
                'running_leg': _running_legs().get(name),
            } for name, acct in accounts.items()])

        payload = request.get_json(silent=True) or {}
        name = (payload.get('name') or payload.get('id') or '').strip()
        if not name:
            return jsonify({'success': False, 'error': 'name required'}), 400
        raw.setdefault('accounts', {})
        acct = raw['accounts'].setdefault(name, {})
        # What this row held BEFORE the edit. The clash guards must only
        # refuse a value being newly CLAIMED — refusing one that is
        # already there makes an existing clash unfixable, because every
        # save of either row (even one changing only the symbol) trips
        # over the other row still holding it. That is the state the
        # operator was in when this was written.
        was = {'terminal_path': (acct.get('terminal_path') or '').strip(),
               'endpoint': (acct.get('endpoint') or '').strip(),
               'login': str(acct.get('login') or '')}
        for field in ('terminal_path', 'server', 'endpoint'):
            if payload.get(field) is not None:
                acct[field] = payload[field]
        path_now = (acct.get('terminal_path') or '').strip()
        if path_now.lower() != was['terminal_path'].lower():
            clash = _terminal_clash(raw, name, path_now)
            if clash:
                return jsonify({'success': False, 'error': clash}), 400
        # A malformed endpoint takes the coordinator AND the leg runner
        # down at startup, so it is rejected here rather than saved.
        ok, endpoint_or_error = normalise_endpoint(acct.get('endpoint'))
        if not ok:
            return jsonify({'success': False,
                            'error': endpoint_or_error}), 400
        acct['endpoint'] = endpoint_or_error
        if (endpoint_or_error or '') != was['endpoint']:
            clash = _endpoint_clash(raw, name, endpoint_or_error)
            if clash:
                return jsonify({'success': False, 'error': clash}), 400
        if payload.get('login'):
            if str(int(payload['login'])) != was['login']:
                clash = _login_clash(raw, name, int(payload['login']))
                if clash:
                    return jsonify({'success': False, 'error': clash}), 400
            acct['login'] = int(payload['login'])
        password = payload.get('password')
        if password:
            var = (acct.get('password_env')
                   or env_var_name('MT5_PASSWORD_', name))
            acct['password_env'] = var
            update_env_file(env_path, {var: password})

        # Role, symbol and contract specs live on the broker row, the
        # way the old app did it: "this account, this leg, this
        # symbol". The role decides which side of the asset the symbol
        # is written to. BOTH is the one-account topology: a single
        # terminal serving Leg A and Leg B.
        role = (payload.get('role') or '').strip().upper()
        claimed = (('SPOT', 'FUTURES') if role == 'BOTH'
                   else (role,) if role in ('SPOT', 'FUTURES') else ())
        if claimed:
            legs = raw.setdefault('leg_accounts', {})
            for leg_role in claimed:
                legs[leg_role.lower()] = name
            # RELEASE any leg this account used to hold and no longer
            # claims. Without this, moving from BOTH back to a single
            # leg is unexpressible — the stale mapping survives every
            # save and the operator can never undo it from the UI.
            for leg_role in ('spot', 'futures'):
                if legs.get(leg_role) == name \
                        and leg_role.upper() not in claimed:
                    legs.pop(leg_role)
        symbol = (payload.get('symbol') or '').strip()
        # A dual-leg account needs both symbols on the one row.
        spot_symbol = (payload.get('spot_symbol') or '').strip()
        fut_symbol = (payload.get('futures_symbol') or '').strip()
        specs = {field: payload.get(field)
                 for field in ('contract_size', 'swap_charge',
                               'futures_expiry')
                 if payload.get(field) not in (None, '')}
        if symbol or spot_symbol or fut_symbol or specs:
            asset_key = (payload.get('asset')
                         or next((k for k, v in (raw.get('assets') or {})
                                  .items() if v.get('enabled', True)),
                                 'GOLD'))
            asset = raw.setdefault('assets', {}).setdefault(
                asset_key, {'name': asset_key, 'enabled': True})
            asset.setdefault('name', asset_key)
            asset.setdefault('enabled', True)
            asset.setdefault('risk_free_rate', 0.0425)
            asset.setdefault('multiplier', 1.0)
            # Which leg does the plain `symbol` field belong to? On a
            # single-leg row, whichever leg the row holds. On a BOTH
            # row the form labels it "Leg A — Spot symbol" and puts the
            # other leg in `futures_symbol`, so it is the SPOT one.
            #
            # BOTH used to match neither branch, so `symbol` was
            # silently DROPPED: live 2026-08-10 the operator saved
            # USOIL as Leg A on a one-account setup and it kept
            # reverting to the old BRENTCash, while the futures symbol
            # beside it saved every time.
            # The ROLE decides, with no fallbacks. `fut_symbol or symbol`
            # let a stale `futures_symbol` win over the box the operator
            # had just typed in: the second box is hidden on a
            # single-leg row but still submits, so saving EU50 on a
            # FUTURES row posted symbol=EU50 alongside
            # futures_symbol=UKOIL and quietly kept UKOIL (live
            # 2026-08-11, twice, "it still saves UKOIL").
            if role == 'FUTURES':
                fut_symbol, spot_symbol = symbol or fut_symbol, ''
            elif role == 'SPOT':
                spot_symbol, fut_symbol = symbol or spot_symbol, ''
            elif role == 'BOTH':
                spot_symbol = spot_symbol or symbol
            elif symbol:
                return jsonify({
                    'success': False,
                    'error': 'Choose a leg (Spot, Futures or Both) so '
                             'the symbol can be assigned'}), 400
            if spot_symbol and 'SPOT' in claimed:
                asset['spot_symbols'] = [spot_symbol]
            if fut_symbol and 'FUTURES' in claimed:
                asset['futures_symbols'] = [fut_symbol]
            # One account, one symbol, both legs is not a spread — it is
            # an instrument against itself, and it nets to zero minus
            # four commissions. Only refused when the SAME account holds
            # both legs: two different accounts quoting the same symbol
            # name is the cross-broker case and is the point.
            legs_now = raw.get('leg_accounts') or {}
            both_here = (legs_now.get('spot') == legs_now.get('futures')
                         == name)
            a = (asset.get('spot_symbols') or [''])[0]
            b = (asset.get('futures_symbols') or [''])[0]
            if both_here and a and a == b:
                return jsonify({
                    'success': False,
                    'error': f"Both legs would trade {a} on '{name}'. A "
                             f"spread needs two different instruments — "
                             f"set Leg A and Leg B to the two symbols of "
                             f"the pair."}), 400
            for field, key in (('contract_size', 'lot_size'),
                               ('swap_charge', 'swap_charge')):
                if field in specs:
                    asset[key] = float(specs[field])
            if specs.get('futures_expiry'):
                asset['futures_expiry'] = specs['futures_expiry']

        save_config_raw(raw)
        return jsonify({'success': True, 'id': name,
                        'note': 'Saved. Restart the launcher to connect.'})

    # -- pairs (assets) ------------------------------------------------
    #
    # Until now an asset could only be CREATED (implicitly, by saving a
    # symbol on a broker row) and never renamed, disabled or removed.
    # That is how a phantom pair born of the old label-into-the-key bug
    # ("XAUUSD_/GC1225") survived every attempt to clear it, and how a
    # SILVER pair with no futures symbol on the account kept warning at
    # every startup. An enabled asset the engine cannot resolve is
    # harmless only while it stays unresolvable — the day its symbols
    # exist, the coordinator trades it as a second live pair.

    ASSET_TABLES = ('trades', 'positions', 'market_data', 'trade_review',
                    'sd_touches', 'shadow_trades')

    @app.route('/api/assets', methods=['GET'])
    def api_assets():
        raw = load_config_raw()
        assets = raw.get('assets') or {}
        open_assets = {p.get('asset')
                       for p in (runtime_status().get('positions') or [])}
        return jsonify([{
            'key': key,
            'name': asset.get('name', key),
            'enabled': bool(asset.get('enabled', True)),
            'spot_symbol': (asset.get('spot_symbols') or [''])[0],
            'futures_symbol': (asset.get('futures_symbols') or [''])[0],
            'pair_type': (asset.get('pair_type') or 'SPOT_FUTURE').upper(),
            'has_open_position': key in open_assets,
        } for key, asset in assets.items()])

    # <path:> not <string:>. The pair that most needs deleting is
    # "XAUUSD_/GC1225" — the old label-into-the-key bug put a SLASH in
    # the key, and Werkzeug's default converter stops at one, so a
    # plain <key> route 404s on exactly the row it exists to remove
    # (whether the slash arrives raw or as %2F).
    @app.route('/api/assets/<path:key>', methods=['POST', 'DELETE'])
    def api_asset_edit(key):
        raw = load_config_raw()
        assets = raw.setdefault('assets', {})
        if key not in assets:
            return jsonify({'success': False,
                            'error': f'No pair named {key}'}), 404

        # A pair with money on it is not a config row. Renaming it
        # orphans the open position from its own history; deleting or
        # disabling it takes the position out of the exit loop.
        open_assets = {p.get('asset')
                       for p in (runtime_status().get('positions') or [])}
        if key in open_assets:
            return jsonify({
                'success': False,
                'error': f'{key} has an open position — close it first. '
                         f'Changing the pair now would leave that position '
                         f'without an exit.'}), 409

        if request.method == 'DELETE':
            assets.pop(key)
            save_config_raw(raw, allow_shrink=True)
            return jsonify({'success': True,
                            'note': f'Pair {key} removed. Restart the '
                                    f'launcher to apply. Its recorded '
                                    f'history is kept.'})

        payload = request.get_json(silent=True) or {}
        asset = assets[key]
        notes = []
        if 'enabled' in payload:
            asset['enabled'] = bool(payload['enabled'])
            notes.append('enabled' if asset['enabled'] else 'disabled')

        new_key = (payload.get('rename') or '').strip()
        if new_key and new_key != key:
            if new_key in assets:
                return jsonify({'success': False,
                                'error': f'A pair named {new_key} already '
                                         f'exists'}), 400
            assets[new_key] = assets.pop(key)
            assets[new_key]['name'] = payload.get('name') or new_key
            # Carry the recorded history across, or a rename silently
            # throws away the warm-start window and every past trade
            # for this pair. series_key still guards correctness — a
            # rename does not make old rows usable if the SYMBOLS
            # changed, it only stops them being stranded.
            moved = 0
            conn = sqlite3.connect(db_path)
            try:
                for table in ASSET_TABLES:
                    try:
                        cursor = conn.execute(
                            f'UPDATE {table} SET asset = ? WHERE asset = ?',
                            (new_key, key))
                        moved += cursor.rowcount or 0
                    except sqlite3.Error:
                        continue        # table absent on an older DB
                conn.commit()
            finally:
                conn.close()
            notes.append(f'renamed to {new_key} ({moved:,} history rows '
                         f'carried over)')
            key = new_key
        elif payload.get('name'):
            asset['name'] = payload['name']
            notes.append('renamed')

        save_config_raw(raw)
        return jsonify({'success': True, 'key': key,
                        'note': ('Pair ' + ', '.join(notes) +
                                 '. Restart the launcher to apply.')
                        if notes else 'No change.'})

    @app.route('/api/exchanges/<account_id>', methods=['DELETE'])
    def api_exchange_delete(account_id):
        raw = load_config_raw()
        if (raw.get('accounts') or {}).pop(account_id, None) is None:
            return jsonify({'success': False, 'error': 'unknown'}), 404
        legs = raw.get('leg_accounts') or {}
        for role, name in list(legs.items()):
            if name == account_id:
                legs.pop(role)
        save_config_raw(raw, allow_shrink=True)
        return jsonify({'success': True})

    @app.route('/api/exchanges/running')
    def api_exchanges_running():
        """What the coordinator is ACTUALLY trading, whatever the file
        says.

        The two can diverge completely, not just differ in detail: on
        2026-08-11 config.json lost its accounts while the engine went
        on trading 100004 and 100006, and the page could only say "No
        accounts configured yet" — the most reassuring possible wording
        for a config that could no longer start the system."""
        status = runtime_status()
        raw = load_config_raw()
        accounts = raw.get('accounts') or {}
        legs = raw.get('leg_accounts') or {}
        return jsonify({
            # Legs whose account has gone missing from the config. The
            # symbols and the mapping survive; only the credentials are
            # lost, so the name is the key to getting it all back.
            'mapped_but_missing': {role: name for role, name in legs.items()
                                   if name and name not in accounts},
            'legs': status.get('running_legs') or {},
            'endpoints': status.get('running_endpoints') or {},
            # runtime_status keys accounts by NAME. Iterating it as a
            # list of dicts raised, the page swallowed the failure, and
            # the warning that exists for exactly this state never
            # appeared.
            'accounts': sorted(status.get('accounts') or {}),
        })

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

    def _delete_rows(tables):
        """DELETE each table and report how many rows went.

        The counts are the point. Both reset buttons name what they
        removed ("X trades, Y SD touches"), and the browser reads them
        off a `deleted` dict the server never sent — so every reset
        ended in `TypeError: Cannot read properties of undefined
        (reading 'trades')` and the operator could not tell whether
        anything had been deleted at all.
        """
        deleted = {}
        conn = sqlite3.connect(db_path)
        for table in tables:
            try:
                deleted[table] = conn.execute(
                    f"DELETE FROM {table}").rowcount
            except sqlite3.OperationalError:
                deleted[table] = 0      # table not created yet
        conn.commit()
        conn.close()
        return deleted

    # The SD distribution IS the "SD analysis" the button offers to
    # reset, and it was not in this list — so it survived every reset
    # while the toast claimed it had gone.
    _TRADE_TABLES = ('trade_review', 'trades', 'positions',
                     'shadow_trades', 'sd_touches')

    @app.route('/api/trades/clear', methods=['POST'])
    def api_trades_clear():
        return jsonify({'success': True,
                        'deleted': _delete_rows(_TRADE_TABLES)})

    @app.route('/api/spread-history/clear', methods=['POST'])
    def api_spread_clear():
        deleted = _delete_rows(('market_data',))
        return jsonify({'success': True,
                        'deleted': {'spread_history':
                                    deleted.get('market_data', 0)}})

    @app.route('/api/reset-trades', methods=['POST'])
    def api_reset_trades():
        return api_trades_clear()

    @app.route('/api/reset-all', methods=['POST'])
    def api_reset_all():
        deleted = _delete_rows(_TRADE_TABLES)
        deleted['spread_history'] = _delete_rows(
            ('market_data',)).get('market_data', 0)
        return jsonify({'success': True, 'deleted': deleted})

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

    def order_source(row):
        """Who sent this order — read off the comment MT5 stored, so
        the answer comes from the terminal's own record rather than
        from anything the app remembers."""
        comment = (row.get('comment') or '').upper()
        if not row.get('is_bot'):
            return 'MANUAL (terminal)'
        if comment.startswith('SCENARIO'):
            return 'TEST SUITE'
        if comment.startswith('ORDER_TEST'):
            return 'ORDER TEST'
        if comment.startswith('MANUAL'):
            return 'MANUAL TRADE'
        return 'ALGO'

    def exchange_orders(limit=100, account=None, source_filter=None):
        """Both accounts' MT5 activity in one list, newest first. The
        coordinator polls each leg into broker_orders; leverage is
        stitched in per account from the live status file."""
        sql = 'SELECT * FROM broker_orders'
        args = []
        if account:
            sql += ' WHERE account = ?'
            args.append(account)
        # Order on REAL time, not on the stamp as stored. `filled_at` is
        # the broker's own wall clock encoded as an epoch, so two
        # accounts on different server timezones are not comparable:
        # live 2026-08-10 a UTC+3 broker's rows sorted three hours
        # "newer" than a UTC+0 broker's, pushing the genuinely newest
        # fills below the LIMIT where the operator could not see them
        # and reported the log as not updating. Subtracting each row's
        # own recorded offset puts every account on one clock; rows
        # from before the offset was captured fall back to raw, which
        # is what they were sorted on anyway.
        sql += (' ORDER BY (filled_at - COALESCE(server_offset_sec, 0)'
                ' * 1000) DESC LIMIT ?')
        args.append(limit)

        accounts = runtime_status().get('accounts') or {}
        rows = []
        for row in query(sql, tuple(args)):
            info = accounts.get(row.get('account')) or {}
            row['leverage'] = info.get('leverage')
            row['login'] = info.get('login')
            row['is_bot'] = bool(row.get('is_bot'))
            for key in ('quantity', 'fill_qty', 'fill_price', 'fee', 'pnl'):
                row[key] = row.get(key) or 0.0
            row['fee_ccy'] = row.get('fee_ccy') or info.get('currency') or ''
            row['order_id'] = str(row.get('order_id') or '')
            row['source'] = order_source(row)
            rows.append(row)
        if source_filter:
            rows = [r for r in rows if r['source'] == source_filter]
        return rows

    @app.route('/api/exchange-orders')
    def api_exchange_orders():
        limit = min(request.args.get('limit', 100, type=int), 1000)
        rows = exchange_orders(limit, request.args.get('account'),
                               request.args.get('source'))
        return jsonify({
            'orders': rows,
            'accounts': sorted({r['account'] for r in rows
                                if r.get('account')}),
            'sources': sorted({r['source'] for r in rows}),
            'count': len(rows),
        })

    @app.route('/api/exchange-orders/csv')
    def api_exchange_orders_csv():
        rows = exchange_orders(
            min(request.args.get('limit', 1000, type=int), 5000),
            request.args.get('account'), request.args.get('source'))
        columns = ['account', 'login', 'broker_time', 'local_time',
                   'source', 'symbol', 'inst_type',
                   'side', 'pos_side', 'order_type', 'quantity', 'fill_qty',
                   'fill_price', 'leverage', 'fee', 'fee_ccy', 'pnl',
                   'state', 'order_id', 'deal_id', 'position_id', 'is_bot',
                   'comment']
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            stamp = row.get('filled_at')
            offset = row.get('server_offset_sec')
            # broker_time reproduces MT5's History column (the stamp is
            # the server's wall clock encoded as an epoch, so it reads
            # back in UTC); local_time is the same instant here, and is
            # blank when the broker's offset could not be measured
            # rather than being quietly wrong.
            writer.writerow(dict(
                row,
                broker_time=(datetime.utcfromtimestamp(stamp / 1000)
                             .isoformat() if stamp else ''),
                local_time=(
                    datetime.fromtimestamp(stamp / 1000 - offset).isoformat()
                    if stamp and offset is not None else '')))
        return Response(
            out.getvalue(), mimetype='text/csv',
            headers={'Content-Disposition':
                     'attachment; filename=exchange_orders.csv'})

    # -- MT5 order tests (W3's test-order / test-suite panels) --

    def _test_state():
        """The per-leg order check, in the shape the vendored suite
        table renders: label / mode / order_type / status / detail.
        Feeding it our raw {leg, check, ok, detail} rows printed a
        table of 'undefined'."""
        results = runtime_status().get('test_results') or {}
        rows = results.get('results') or []
        scenarios = [{
            'id': f"{row.get('leg', '')}:{row.get('check', index)}",
            'label': row.get('check') or 'check',
            'mode': 'CHECK',
            'order_type': row.get('leg') or '',
            'status': 'pass' if row.get('ok') else 'fail',
            'detail': row.get('detail') or '',
            'cancel_test': False,
        } for index, row in enumerate(rows)]
        return {
            'running': False,
            'kind': results.get('kind'),
            'ts': results.get('ts'),
            'results': rows,
            'scenarios': scenarios,
            'passed': sum(1 for r in rows if r.get('ok')),
            'failed': sum(1 for r in rows if not r.get('ok')),
            # the vendored table reads these names for its counters
            'pass': sum(1 for r in rows if r.get('ok')),
            'fail': sum(1 for r in rows if not r.get('ok')),
            'current': len(rows),
            'total': len(rows),
            'order_mode': results.get('kind'),
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

    # -- connectivity checklist + symbol lookup (Exchanges page) --

    def leg_client(account_name):
        """A short-lived connection straight to that account's leg
        runner.

        Setup is a chicken-and-egg otherwise: symbol search and
        diagnostics used to go through the coordinator, but the
        coordinator will not start until the symbols and legs are
        already right. The leg runner is up as soon as its terminal is,
        so the UI asks it directly. This is a plain socket client — the
        web app still never imports MT5."""
        account = (load_config_raw().get('accounts') or {}).get(account_name)
        if not account or not account.get('endpoint'):
            return None
        try:
            from .legs import RemoteLeg
            leg = RemoteLeg(account_name, account['endpoint'], timeout=5.0)
        except ValueError:
            return None                    # malformed endpoint
        if not leg.connect(retries=1, delay=0.2):
            return None
        return leg

    def no_leg_reason(account_name):
        """WHY there is no leg client for this account.

        `leg_client` returns None for two completely different
        situations and the caller used to report only one of them:
        "Give the account an endpoint (e.g. 127.0.0.1:9101)". Live
        2026-08-11 the account HAD an endpoint (9102) and its runner
        was hung — so the advice was wrong, and worse, the port it
        suggested belonged to the OTHER account. The operator typed
        9101 into the field, which would have put both accounts on one
        port had the save not started refusing that.
        """
        raw = load_config_raw()
        account = (raw.get('accounts') or {}).get(account_name)
        if not account:
            return (f"'{account_name}' is not in the config. Add it "
                    f"above, then restart the launcher.")
        endpoint = (account.get('endpoint') or '').strip()
        if not endpoint:
            return (f"'{account_name}' has no leg-runner endpoint, so "
                    f"nothing is serving it. Give it one "
                    f"({_next_free_port(raw)}) and restart the launcher.")
        return (f"Nothing is listening on {endpoint} for "
                f"'{account_name}'. Its leg runner is not running — "
                f"either the launcher was not started, or that runner "
                f"is still waiting for its MT5 terminal to log in "
                f"(check leg_{account_name}.log, and that the terminal "
                f"at its configured path is open and logged into "
                f"{account.get('login') or 'this account'}).")

    def leg_for_role(role):
        """(account_name, connected leg or None) for 'spot'/'futures'."""
        legs = load_config_raw().get('leg_accounts') or {}
        name = legs.get(role)
        if not name:
            # Nothing mapped yet — during setup, any account with a
            # runner is better than refusing to answer.
            accounts = load_config_raw().get('accounts') or {}
            name = next((n for n, a in accounts.items() if a.get('endpoint')),
                        None)
        return name, (leg_client(name) if name else None)

    def ask_coordinator(payload, key, timeout=None):
        """Post a read-only request into control.json and wait for the
        coordinator to answer in runtime_status. Same file bridge the
        scenarios use — the web app never touches MT5 itself."""
        # Is the coordinator even running? It rewrites runtime_status
        # on EVERY poll (0.3s by default), so a file that has not been
        # touched in a quarter of a minute means nobody is listening on
        # the other end of this bridge. Without the check, every button
        # that falls back to the coordinator sits for the full 45s
        # before reporting what could be known immediately — and the
        # answer it eventually gives is the same one.
        if not _coordinator_is_writing():
            return None
        ts = time.time()
        write_control({'diagnose': dict(payload, ts=ts)})
        deadline = time.time() + (timeout or diagnose_timeout)
        while time.time() < deadline:
            answer = runtime_status().get(key) or {}
            if answer.get('ts') == ts:
                return answer
            time.sleep(0.3)
        return None

    def _coordinator_is_writing(stale_after=15.0):
        try:
            return (time.time() - os.path.getmtime(status_path)) < stale_after
        except OSError:
            return False

    def diagnose_via_leg_runners(asset_key=None):
        """Build the checklist by asking the leg runners directly.

        Works while the coordinator is down — which is exactly when the
        operator needs it, because a wrong symbol or an unmapped leg is
        what stops the coordinator starting."""
        raw = load_config_raw()
        accounts = raw.get('accounts') or {}
        legs = raw.get('leg_accounts') or {}
        assets = raw.get('assets') or {}
        asset_key = asset_key or next(
            (k for k, v in assets.items() if v.get('enabled', True)), 'GOLD')
        asset = assets.get(asset_key) or {}

        checks, sides, raw_legs = [], {}, {}
        for role, symbol_key in (('spot', 'spot_symbols'),
                                 ('futures', 'futures_symbols')):
            name = legs.get(role)
            if not name:
                checks.append({
                    'scope': 'ENGINE', 'name': f'{role.upper()} leg',
                    'status': 'FAIL',
                    'message': 'No account is mapped to this leg — the '
                               'coordinator cannot start',
                    'fix': ['Edit an account above and set its Leg, '
                            'then restart the launcher']})
                continue
            symbol = (asset.get(symbol_key) or [''])[0]
            leg = leg_client(name)
            if leg is None:
                checks.append({
                    'scope': f'{role.upper()} · {name}', 'name': 'Leg runner',
                    'status': 'FAIL',
                    'message': no_leg_reason(name),
                    'fix': [f'Read leg_{name}.log — a runner that is still '
                            f'connecting logs the attempt it is on',
                            'The terminal at this account\'s path must be '
                            'open and logged in; a runner will wait '
                            'indefinitely for one that never is',
                            'Restart the launcher once the terminal is up']})
                continue
            try:
                terminal = leg.terminal_report()
                report = (leg.symbol_report(symbol) if symbol else
                          {'symbol': '', 'found': False,
                           'error': 'No symbol set for this leg'})
            finally:
                leg.close()
            sides[role] = {'account': name, 'role': role,
                           'terminal': terminal, 'symbol': report,
                           'asset': asset}
            raw_legs[name] = {'terminal': terminal, 'symbol': report,
                              'role': role}

        if len(sides) == 2:
            from .config import AlgoTradingConfig
            config = AlgoTradingConfig.from_file(config_path) \
                if os.path.exists(config_path) else AlgoTradingConfig()
            expected = {name: acct.login
                        for name, acct in config.accounts.items()
                        if getattr(acct, 'login', None)}
            report = diagnostics.build_report(
                config, sides['spot'], sides['futures'],
                expected_logins=expected,
                leverages={'spot': config.EXITS.get('SPOT_LEVERAGE')
                           or config.EXITS.get('LEVERAGE'),
                           'futures': config.EXITS.get('FUT_LEVERAGE')
                           or config.EXITS.get('LEVERAGE')})
            report['checks'] = checks + report['checks']
        else:
            # Not enough to compare the pair — report what we have.
            checklist = diagnostics.Checklist()
            for role, side in sides.items():
                diagnostics.check_leg(
                    checklist, role, side['account'], side['terminal'],
                    side['symbol'], _fallback_config(), asset)
            report = checklist.result()
            report['checks'] = checks + report['checks']
        for key in ('passed', 'warnings', 'failed'):
            report.setdefault(key, 0)
        report['failed'] += sum(1 for c in checks if c['status'] == 'FAIL')
        report['overall'] = ('FAIL' if report['failed'] else
                             'WARN' if report['warnings'] else 'PASS')
        report['legs'] = raw_legs
        report['via'] = 'leg runners'
        report['ran_at'] = datetime.now().strftime('%H:%M:%S')
        return report if (sides or checks) else None

    def _fallback_config():
        from .config import AlgoTradingConfig
        if os.path.exists(config_path):
            return AlgoTradingConfig.from_file(config_path)
        return AlgoTradingConfig()

    @app.route('/api/brokers/diagnose', methods=['POST'])
    @app.route('/api/brokers/<account>/diagnose', methods=['POST'])
    def api_broker_diagnose(account=None):
        """The full connectivity checklist: each terminal (attached,
        logged in, algo trading, permissions, margin mode, leverage),
        each symbol (found, in Market Watch, priced, sizes, contract
        specs) and the pair (currency, hedge ratio, live basis)."""
        body = request.get_json(silent=True) or {}
        # Ask the leg runners directly first — this must work while the
        # coordinator is down, because that is when it is needed.
        report = diagnose_via_leg_runners(body.get('asset'))
        if report is None or not report.get('legs'):
            # No leg runner answered. A running coordinator holds the
            # connections instead (in-process topology), so ask it —
            # and only if that is silent too do we report the runner
            # failures, which say more than "no coordinator".
            from_coordinator = ask_coordinator({'asset': body.get('asset')},
                                               'diagnostics')
            report = from_coordinator if from_coordinator else report
        if report is None:
            return jsonify({
                'success': False, 'overall': 'FAIL', 'checks': [
                    {'scope': 'ENGINE', 'name': 'No engine reachable',
                     'status': 'FAIL',
                     'message': 'Neither a leg runner nor the coordinator '
                                'answered.',
                     'fix': ['Give each account a leg runner endpoint '
                             '(127.0.0.1:9101, then 9102)',
                             'Start the launcher: python start.py',
                             'Or run: python check_mt5.py']}],
                'passed': 0, 'warnings': 0, 'failed': 1})
        if account:
            report = dict(report, checks=[
                c for c in report['checks']
                if account in c.get('scope', '') or c['scope'] == 'PAIR'])
        report['success'] = report.get('overall') != 'FAIL'
        return jsonify(report)

    @app.route('/api/brokers/<account>/test', methods=['POST'])
    def api_broker_test(account):
        """Quick connection test for one account, in the old app's
        shape: latency, who is logged in, and a live price."""
        # Straight to this account's leg runner when it has one.
        client = leg_client(account)
        if client is not None:
            raw = load_config_raw()
            assets = raw.get('assets') or {}
            legs = raw.get('leg_accounts') or {}
            asset = next((v for v in assets.values()
                          if v.get('enabled', True)), {})
            key = ('futures_symbols' if legs.get('futures') == account
                   else 'spot_symbols')
            symbol_name = (asset.get(key) or [''])[0]
            try:
                terminal = client.terminal_report()
                symbol = (client.symbol_report(symbol_name) if symbol_name
                          else {'symbol': '', 'found': False})
            finally:
                client.close()
            leg = {'terminal': terminal, 'symbol': symbol}
        else:
            # Only ask the coordinator about an account it could
            # actually be holding. An account WITH an endpoint is
            # served by its own leg runner, and the coordinator reaches
            # it over that same port — so if the port is dead, the
            # coordinator cannot answer either and waiting out the
            # file-bridge timeout just makes the button hang before
            # giving the same answer.
            raw = load_config_raw()
            acct = (raw.get('accounts') or {}).get(account) or {}
            report = (None if (acct.get('endpoint') or '').strip()
                      else ask_coordinator({}, 'diagnostics'))
            if report is None:
                return jsonify({'success': False,
                                'error': no_leg_reason(account)})
            leg = (report.get('legs') or {}).get(account)
            if not leg:
                return jsonify({'success': False,
                                'error': f"'{account}' is not mapped to a "
                                         f"leg. Set its Leg above."})
        terminal, symbol = leg['terminal'], leg['symbol']
        if not terminal.get('logged_in'):
            return jsonify({'success': False,
                            'error': terminal.get('error')
                            or 'Terminal not attached or not logged in'})
        result = {
            'success': True,
            'latency_ms': round(terminal.get('ping_ms') or 0),
            'account_info': {
                'login': terminal.get('login'),
                'server': terminal.get('server'),
                'balance': terminal.get('balance'),
                'equity': terminal.get('equity'),
                'currency': terminal.get('currency'),
                'leverage': terminal.get('leverage'),
            },
        }
        if symbol.get('bid'):
            result['price_info'] = {'symbol': symbol['symbol'],
                                    'bid': symbol['bid'],
                                    'ask': symbol['ask']}
        elif not symbol.get('found'):
            result['warning'] = (f"Symbol \"{symbol.get('symbol')}\" not "
                                 f"found on this account.")
        else:
            result['warning'] = (f"{symbol['symbol']} has no quotes right "
                                 f"now (market closed?).")
        return jsonify(result)

    @app.route('/api/symbols/search')
    def api_symbol_search():
        """Search one account's symbol list — brokers name the same
        instrument differently, so the operator looks it up here rather
        than guessing at the spelling."""
        pattern = request.args.get('q', '')
        role = request.args.get('leg', 'spot')
        account = request.args.get('account')

        # Straight to the leg runner when there is one: this has to
        # work BEFORE the coordinator can start, since finding the
        # right symbol is what lets it start.
        name = account or leg_for_role(role)[0]
        leg = leg_client(name) if name else None
        if leg is not None:
            try:
                found = leg.find_symbols(pattern, 40)
            finally:
                leg.close()
            if found is not None:
                return jsonify({'leg': role, 'account': name,
                                'pattern': pattern, 'symbols': found,
                                'via': 'leg runner'})

        answer = ask_coordinator({'find_symbols': pattern, 'leg': role},
                                 'symbol_search')
        if answer is None:
            return jsonify({
                'symbols': [],
                'error': f"Cannot reach account '{name or role}'. Give it a "
                         f"leg runner endpoint (e.g. 127.0.0.1:9101) and "
                         f"restart the launcher, or start the coordinator."})
        return jsonify(answer)

    @app.route('/api/leg-symbols', methods=['GET', 'POST'])
    def api_leg_symbols():
        """Read/write the symbol each leg trades. Symbols are
        structural — the engine picks them up on the next launcher
        restart, which is what the response says."""
        raw = load_config_raw()
        assets = raw.get('assets') or {}
        asset_key = (request.get_json(silent=True) or {}).get('asset') \
            if request.method == 'POST' else request.args.get('asset')
        asset_key = asset_key or next(
            (k for k, v in assets.items() if v.get('enabled', True)), 'GOLD')
        legs = raw.get('leg_accounts') or {}

        if request.method == 'GET':
            asset = assets.get(asset_key) or {}
            return jsonify({
                'asset': asset_key,
                'spot_symbol': (asset.get('spot_symbols') or [''])[0],
                'futures_symbol': (asset.get('futures_symbols') or [''])[0],
                'spot_account': legs.get('spot'),
                'futures_account': legs.get('futures'),
                'assets': sorted(assets),
            })

        body = request.get_json(silent=True) or {}
        asset = assets.setdefault(asset_key, {'name': asset_key,
                                              'enabled': True})
        changed = []
        for field, key in (('spot_symbol', 'spot_symbols'),
                           ('futures_symbol', 'futures_symbols')):
            value = (body.get(field) or '').strip()
            if value and [value] != asset.get(key):
                asset[key] = [value]
                changed.append(f'{field} -> {value}')
        if body.get('futures_expiry'):
            asset['futures_expiry'] = body['futures_expiry']
            changed.append(f"expiry -> {body['futures_expiry']}")
        if body.get('contract_size'):
            asset['lot_size'] = float(body['contract_size'])
            changed.append(f"contract size -> {body['contract_size']}")
        if not changed:
            return jsonify({'success': True, 'note': 'Nothing changed.'})
        raw['assets'] = assets
        save_config_raw(raw)
        return jsonify({'success': True, 'changed': changed,
                        'note': 'Saved. Symbols are structural — restart '
                                'the launcher (start.py) for the engine to '
                                'trade them.'})

    # -- round-trip order scenarios (Exchanges page) --

    @app.route('/api/scenario-catalogue')
    def api_scenario_catalogue():
        """The scenario matrix the Exchanges page renders. Served from
        the engine so the table can never drift from what actually
        runs."""
        return jsonify({'scenarios': scenarios.CATALOGUE,
                        'spacing_sec': scenarios.RUN_SPACING_SEC})

    @app.route('/api/scenario-test', methods=['POST'])
    def api_scenario_test():
        """Run ONE round-trip scenario on the live accounts.

        The web app never touches MT5, so this hands the scenario to
        the coordinator through control.json and waits for it to
        publish the outcome — the caller still gets one synchronous
        {success, detail}, which is what the suite table expects."""
        spec = request.get_json(silent=True) or {}
        if not spec.get('type'):
            return jsonify({'success': False,
                            'detail': 'No scenario type given'}), 400
        ts = time.time()
        write_control({'scenario': {
            'id': spec.get('id'), 'type': spec['type'],
            'mode': spec.get('mode', 'MARKET'),
            'variant': spec.get('variant', 'normal'),
            'asset': spec.get('asset'),
            # How long to HOLD the position before closing. Clamped in
            # the runner; sent per request so it is a property of this
            # run rather than saved state nobody remembers setting.
            'hold_sec': spec.get('hold_sec'), 'ts': ts}})

        # A held scenario legitimately outruns the normal wait: the hold
        # is on top of the fill timeout and the round trip itself.
        deadline = time.time() + scenario_timeout + float(
            spec.get('hold_sec') or 0)
        while time.time() < deadline:
            result = runtime_status().get('scenario_result') or {}
            if result.get('ts') == ts:
                return jsonify(result)
            time.sleep(0.4)
        return jsonify({
            'success': False,
            'detail': 'No answer from the coordinator. Is it running? '
                      'Scenarios need the algo stopped and a flat book.'})

    @app.route('/api/scenario-test/status')
    def api_scenario_status():
        return jsonify(runtime_status().get('scenario_result') or {})

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
            "spread, basis_pct, z, signal FROM market_data "
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
            # Reads runtime_status.json and pushes what changed. Sleep
            # shorter than the coordinator's poll, or this thread — not
            # the feed — becomes what sets the dashboard's frame rate.
            last = None
            while True:
                socketio.sleep(BROADCAST_INTERVAL_SEC)
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


class _QuietAccessLog(logging.Filter):
    """Drop SUCCESSFUL request lines from werkzeug's access log.

    Operator, 2026-08-07: "Too many messages". The dashboard polls
    half a dozen endpoints continuously, so werkzeug was writing about
    five lines a second — every one of them a 200 for a request the
    page makes on a timer. None of it is information: a poll that
    worked is the expected case, and the flood buried the engine's own
    warnings, which are the reason to have a log at all.

    4xx and 5xx still come through, because a request that FAILED is
    exactly what someone reading this file is looking for.
    """

    _OK = re.compile(r'"\s+(?:2\d\d|30[0-9])\s+-\s*$')

    def filter(self, record):
        try:
            return not self._OK.search(record.getMessage())
        except Exception:
            return True


def quiet_access_log():
    """Install the filter on werkzeug's logger."""
    logging.getLogger('werkzeug').addFilter(_QuietAccessLog())


def run_app(app, host='127.0.0.1', port=8080):
    """Serve with socket.io when available, plain Flask otherwise."""
    quiet_access_log()
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
