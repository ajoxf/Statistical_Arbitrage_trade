"""
Flask Web Application

Provides the web UI for the Multi-Broker Arbitrage System.

Pages:
- Dashboard: Real-time monitoring
- Settings: Trading parameters configuration
- Setup: Broker configuration
- SD Analysis: Standard deviation touch analysis
"""

import asyncio
import threading
import json
import os
from datetime import datetime
from functools import wraps
from typing import Optional
import logging

from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_socketio import SocketIO, emit

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.manager import DatabaseManager
from database.models import TradingConfig, Broker, Trade
from core.trading_engine import TradingEngine, EngineState

# Active broker config file (workaround for database not saving new fields)
ACTIVE_BROKER_FILE = Path(__file__).parent.parent / "active_brokers.json"


def save_active_brokers(spot_id, futures_id):
    """Save active broker IDs to JSON file"""
    data = {
        'active_spot_broker': spot_id,
        'active_futures_broker': futures_id
    }
    with open(ACTIVE_BROKER_FILE, 'w') as f:
        json.dump(data, f)
    logging.getLogger(__name__).info(f"[BROKERS] Saved active brokers to file: {data}")


def load_active_brokers():
    """Load active broker IDs from JSON file"""
    if ACTIVE_BROKER_FILE.exists():
        try:
            with open(ACTIVE_BROKER_FILE, 'r') as f:
                data = json.load(f)
                return data.get('active_spot_broker'), data.get('active_futures_broker')
        except Exception as e:
            logging.getLogger(__name__).error(f"[BROKERS] Error loading active brokers: {e}")
    return None, None

logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = 'multi-broker-arb-secret-key'

# SocketIO for real-time updates
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Global instances
db: Optional[DatabaseManager] = None
engine: Optional[TradingEngine] = None
engine_loop: Optional[asyncio.AbstractEventLoop] = None


def init_app(db_path: str = "trading.db"):
    """Initialize application components"""
    global db, engine

    db = DatabaseManager(db_path)
    db.initialize()

    # Engine will be initialized on first start
    engine = None


def get_db() -> DatabaseManager:
    """Get database manager"""
    global db
    if db is None:
        db = DatabaseManager("trading.db")
        db.initialize()
    return db


# ==================== Routes ====================

@app.route('/')
def index():
    """Redirect to dashboard"""
    return redirect(url_for('dashboard'))


@app.route('/dashboard')
def dashboard():
    """Dashboard page - real-time monitoring"""
    database = get_db()
    config = database.get_config()
    brokers = database.get_brokers()
    open_trades = database.get_open_trades()
    recent_trades = database.get_trades(limit=10)
    stats = database.get_trade_statistics()

    # Get active brokers from JSON file (workaround for database not persisting)
    spot_broker_id, futures_broker_id = load_active_brokers()

    active_spot = None
    active_futures = None
    for b in brokers:
        if spot_broker_id and b.broker_id == spot_broker_id:
            active_spot = b
        if futures_broker_id and b.broker_id == futures_broker_id:
            active_futures = b

    return render_template(
        'dashboard.html',
        config=config,
        brokers=brokers,
        active_spot=active_spot,
        active_futures=active_futures,
        open_trades=open_trades,
        recent_trades=recent_trades,
        stats=stats,
        engine_state=engine.state.value if engine else "STOPPED"
    )


@app.route('/settings')
def settings():
    """Settings page - trading parameters"""
    database = get_db()
    config = database.get_config()
    brokers = database.get_brokers()

    # Separate brokers by role
    spot_brokers = [b for b in brokers if b.role == 'SPOT']
    futures_brokers = [b for b in brokers if b.role == 'FUTURES']

    # Load active broker IDs from JSON file
    spot_broker_id, futures_broker_id = load_active_brokers()

    return render_template('settings.html', config=config,
                           spot_brokers=spot_brokers,
                           futures_brokers=futures_brokers,
                           active_spot_id=spot_broker_id,
                           active_futures_id=futures_broker_id)


@app.route('/setup')
def setup():
    """Setup page - broker configuration"""
    database = get_db()
    config = database.get_config()
    brokers = database.get_brokers()

    return render_template('setup.html', config=config, brokers=brokers)


@app.route('/analysis')
def analysis():
    """SD Analysis page"""
    database = get_db()
    config = database.get_config()
    sd_stats = database.get_sd_touch_stats()
    sd_touches = database.get_sd_touches(limit=100)
    limit_stats = database.get_limit_order_stats()

    return render_template(
        'analysis.html',
        config=config,
        sd_stats=sd_stats,
        sd_touches=sd_touches,
        limit_stats=limit_stats
    )


# ==================== API Routes ====================

@app.route('/api/status')
def api_status():
    """Get current system status"""
    if engine:
        return jsonify(engine.get_status())
    else:
        database = get_db()
        config = database.get_config()
        brokers = database.get_brokers()

        return jsonify({
            'state': 'STOPPED',
            'config': config.to_dict(),
            'brokers': {b.broker_id: {'status': b.status, 'role': b.role} for b in brokers},
            'market': {},
            'position': {'has_position': False}
        })


@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """Get or update trading configuration"""
    database = get_db()

    if request.method == 'POST':
        data = request.get_json()
        logger.info(f"[CONFIG] Saving config: {data}")

        config = database.get_config()

        # Update fields from request
        for key, value in data.items():
            if hasattr(config, key):
                current_val = getattr(config, key)
                # Handle string fields (may be None initially)
                if key in ['active_spot_broker', 'active_futures_broker', 'asset_name',
                           'spot_symbol', 'futures_symbol', 'futures_expiry',
                           'lookback_unit', 'order_type', 'selected_asset']:
                    # String fields - keep as string or None
                    setattr(config, key, value if value else None)
                elif isinstance(current_val, bool) or key in ['hurst_enabled', 'std_filter_enabled',
                                                               'close_before_overnight', 'paper_mode', 'algo_enabled']:
                    setattr(config, key, bool(value))
                elif isinstance(current_val, int):
                    setattr(config, key, int(value) if value else 0)
                elif isinstance(current_val, float):
                    setattr(config, key, float(value) if value else 0.0)
                else:
                    setattr(config, key, value)

        logger.info(f"[CONFIG] Active brokers - Spot: {config.active_spot_broker}, Futures: {config.active_futures_broker}")

        # Save active brokers to JSON file (workaround for database not persisting new fields)
        save_active_brokers(config.active_spot_broker, config.active_futures_broker)

        # Verify the save worked
        saved_spot, saved_futures = load_active_brokers()
        logger.info(f"[CONFIG] Verified saved - Spot: {saved_spot}, Futures: {saved_futures}")

        database.update_config(config)

        # Reload config in engine if running
        if engine:
            engine.reload_config()

        return jsonify({'success': True, 'config': config.to_dict()})

    else:
        config = database.get_config()
        return jsonify(config.to_dict())


@app.route('/api/brokers', methods=['GET', 'POST'])
def api_brokers():
    """Get or add broker configuration"""
    database = get_db()

    if request.method == 'POST':
        data = request.get_json()

        broker = Broker(
            broker_id=data.get('broker_id'),
            name=data.get('name'),
            broker_type=data.get('broker_type', 'MT5'),
            role=data.get('role'),
            mt5_path=data.get('mt5_path'),
            mt5_account=data.get('mt5_account'),
            mt5_server=data.get('mt5_server'),
            mt5_password=data.get('mt5_password'),
            fix_host=data.get('fix_host'),
            fix_port=data.get('fix_port'),
            fix_sender_comp=data.get('fix_sender_comp'),
            fix_target_comp=data.get('fix_target_comp'),
            fix_username=data.get('fix_username'),
            fix_password=data.get('fix_password'),
            flex_host=data.get('flex_host'),
            flex_port=data.get('flex_port'),
            flex_api_key=data.get('flex_api_key'),
            ib_host=data.get('ib_host'),
            ib_port=data.get('ib_port'),
            ib_client_id=data.get('ib_client_id'),
            symbol=data.get('symbol', ''),
            contract_size=float(data.get('contract_size', 100.0)),
            commission_per_lot=float(data.get('commission_per_lot', 0.0)),
            min_volume=float(data.get('min_volume', 0.01))
        )

        database.add_broker(broker)
        return jsonify({'success': True, 'broker': broker.to_dict()})

    else:
        brokers = database.get_brokers()
        return jsonify([b.to_dict() for b in brokers])


@app.route('/api/brokers/<broker_id>', methods=['GET', 'PUT', 'DELETE'])
def api_broker(broker_id):
    """Get, update, or delete a specific broker"""
    database = get_db()

    if request.method == 'DELETE':
        database.delete_broker(broker_id)
        return jsonify({'success': True})

    elif request.method == 'PUT':
        data = request.get_json()
        broker = database.get_broker(broker_id)
        if broker:
            for key, value in data.items():
                if hasattr(broker, key):
                    setattr(broker, key, value)
            database.add_broker(broker)
            return jsonify({'success': True, 'broker': broker.to_dict()})
        return jsonify({'success': False, 'error': 'Broker not found'}), 404

    else:
        broker = database.get_broker(broker_id)
        if broker:
            return jsonify(broker.to_dict())
        return jsonify({'error': 'Broker not found'}), 404


@app.route('/api/brokers/<broker_id>/test', methods=['POST'])
def api_broker_test(broker_id):
    """Test connectivity for a specific broker"""
    database = get_db()
    broker = database.get_broker(broker_id)

    if not broker:
        return jsonify({'success': False, 'error': 'Broker not found'}), 404

    try:
        import time
        start_time = time.time()

        # Create adapter based on broker type
        if broker.broker_type == 'OKX':
            from adapters.okx_adapter import OKXAdapter
            from adapters.base import BrokerConfig
            import os

            config = BrokerConfig(
                broker_id=broker.broker_id,
                name=broker.name,
                role=broker.role,
                backend_type='OKX',
                okx_api_key=broker.okx_api_key or os.environ.get('OKX_API_KEY', ''),
                okx_api_secret=broker.okx_api_secret or os.environ.get('OKX_API_SECRET', ''),
                okx_passphrase=broker.okx_passphrase or os.environ.get('OKX_PASSPHRASE', ''),
                okx_simulated=broker.okx_simulated if hasattr(broker, 'okx_simulated') else True,
                okx_account_type=broker.okx_account_type if hasattr(broker, 'okx_account_type') else 'spot',
                symbol=broker.symbol
            )

            adapter = OKXAdapter(config)

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                connected = loop.run_until_complete(adapter.connect())
                if not connected:
                    return jsonify({'success': False, 'error': 'Failed to connect to OKX'})

                # Get account info and price
                account = loop.run_until_complete(adapter.get_account_info())
                tick = loop.run_until_complete(adapter.get_tick(broker.symbol))
                loop.run_until_complete(adapter.disconnect())

                latency_ms = int((time.time() - start_time) * 1000)

                # Update broker status in database
                broker.status = 'CONNECTED'
                broker.latency_ms = latency_ms
                database.add_broker(broker)

                result = {
                    'success': True,
                    'latency_ms': latency_ms,
                    'broker_type': 'OKX'
                }

                if account:
                    result['account_info'] = {
                        'balance': account.balance,
                        'equity': account.equity,
                        'currency': 'USDT'
                    }

                if tick:
                    result['price_info'] = {
                        'symbol': broker.symbol,
                        'bid': tick.bid,
                        'ask': tick.ask
                    }

                return jsonify(result)

            finally:
                loop.close()

        elif broker.broker_type == 'MT5':
            # MT5 test using MetaTrader5 library
            try:
                import MetaTrader5 as mt5

                # Initialize MT5 connection
                if not mt5.initialize():
                    error_code = mt5.last_error()
                    return jsonify({
                        'success': False,
                        'error': f'Failed to initialize MT5. Error: {error_code}. Ensure MT5 terminal is running and algo trading is enabled.'
                    })

                # Get account info
                account_info = mt5.account_info()
                if account_info is None:
                    mt5.shutdown()
                    return jsonify({
                        'success': False,
                        'error': 'Could not get account info. Please log in to MT5.'
                    })

                # Get symbol info and price
                symbol = broker.symbol
                symbol_info = mt5.symbol_info(symbol)
                tick = mt5.symbol_info_tick(symbol)

                latency_ms = int((time.time() - start_time) * 1000)

                # Update broker status
                broker.status = 'CONNECTED'
                broker.latency_ms = latency_ms
                database.add_broker(broker)

                result = {
                    'success': True,
                    'latency_ms': latency_ms,
                    'broker_type': 'MT5',
                    'account_info': {
                        'login': account_info.login,
                        'server': account_info.server,
                        'balance': account_info.balance,
                        'equity': account_info.equity,
                        'currency': account_info.currency
                    }
                }

                if tick:
                    result['price_info'] = {
                        'symbol': symbol,
                        'bid': tick.bid,
                        'ask': tick.ask
                    }
                elif symbol_info is None:
                    result['warning'] = f'Symbol "{symbol}" not found in Market Watch. Add it to see prices.'

                mt5.shutdown()
                return jsonify(result)

            except ImportError:
                return jsonify({
                    'success': False,
                    'error': 'MetaTrader5 Python library not installed. Run: pip install MetaTrader5'
                })
            except Exception as e:
                return jsonify({
                    'success': False,
                    'error': f'MT5 error: {str(e)}'
                })

        elif broker.broker_type in ['FIX', 'FLEXTRADE']:
            # FIX test - placeholder
            return jsonify({
                'success': False,
                'error': 'FIX connection test not implemented yet.'
            })

        elif broker.broker_type == 'IB':
            # IB test - placeholder
            return jsonify({
                'success': False,
                'error': 'Interactive Brokers connection test not implemented yet.'
            })

        else:
            return jsonify({
                'success': False,
                'error': f'Unknown broker type: {broker.broker_type}'
            })

    except Exception as e:
        # Update broker status to error
        broker.status = 'ERROR'
        database.add_broker(broker)

        logger.error(f"Broker test error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/brokers/<broker_id>/diagnose', methods=['POST'])
def api_broker_diagnose(broker_id):
    """Comprehensive diagnostic for broker connectivity issues"""
    database = get_db()
    broker = database.get_broker(broker_id)

    if not broker:
        return jsonify({'success': False, 'error': 'Broker not found'}), 404

    diagnostics = {
        'broker_id': broker_id,
        'broker_type': broker.broker_type,
        'broker_name': broker.name,
        'checks': [],
        'suggestions': [],
        'overall_status': 'UNKNOWN'
    }

    def add_check(name, status, message, details=None):
        check = {'name': name, 'status': status, 'message': message}
        if details:
            check['details'] = details
        diagnostics['checks'].append(check)
        return status == 'PASS'

    try:
        if broker.broker_type == 'OKX':
            from adapters.okx_adapter import OKXAdapter
            from adapters.base import BrokerConfig
            import os
            import aiohttp

            # Check 1: Configuration
            api_key = broker.okx_api_key or os.environ.get('OKX_API_KEY', '')
            api_secret = broker.okx_api_secret or os.environ.get('OKX_API_SECRET', '')
            passphrase = broker.okx_passphrase or os.environ.get('OKX_PASSPHRASE', '')

            if not api_key:
                add_check('API Key', 'FAIL', 'API Key is missing')
                diagnostics['suggestions'].append({
                    'issue': 'Missing API Key',
                    'fix': 'Add your OKX API Key in the broker configuration or set OKX_API_KEY environment variable',
                    'steps': [
                        '1. Log in to OKX',
                        '2. Go to Account > API',
                        '3. Create a new API key with trading permissions',
                        '4. Copy the API Key and paste it in the broker config'
                    ]
                })
            else:
                add_check('API Key', 'PASS', f'API Key configured (ends with ...{api_key[-4:]})')

            if not api_secret:
                add_check('API Secret', 'FAIL', 'API Secret is missing')
                diagnostics['suggestions'].append({
                    'issue': 'Missing API Secret',
                    'fix': 'Add your OKX API Secret Key',
                    'steps': ['The secret is shown only once when creating the API key']
                })
            else:
                add_check('API Secret', 'PASS', 'API Secret configured')

            if not passphrase:
                add_check('Passphrase', 'FAIL', 'API Passphrase is missing')
                diagnostics['suggestions'].append({
                    'issue': 'Missing Passphrase',
                    'fix': 'Add your OKX API Passphrase (set during API key creation)',
                    'steps': ['This is the passphrase you created with your API key']
                })
            else:
                add_check('Passphrase', 'PASS', 'Passphrase configured')

            # Check 2: Symbol format
            symbol = broker.symbol or ''
            if not symbol:
                add_check('Symbol', 'FAIL', 'No trading symbol configured')
                diagnostics['suggestions'].append({
                    'issue': 'Missing Symbol',
                    'fix': 'Configure the trading symbol',
                    'steps': [
                        'For Spot: Use format like BTC-USDT, ETH-USDT',
                        'For Futures: Use format like BTC-USDT-SWAP or BTC-USDT-240329'
                    ]
                })
            elif '-' not in symbol:
                add_check('Symbol Format', 'WARN', f'Symbol "{symbol}" may not be in OKX format')
                diagnostics['suggestions'].append({
                    'issue': 'Incorrect Symbol Format',
                    'fix': 'OKX uses dash-separated symbols',
                    'steps': [
                        f'Current: {symbol}',
                        'Expected format: BTC-USDT (spot) or BTC-USDT-SWAP (perpetual)',
                        'Check OKX trading page for exact symbol names'
                    ]
                })
            else:
                add_check('Symbol Format', 'PASS', f'Symbol "{symbol}" appears valid')

            # Check 3: Network connectivity
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def test_network():
                try:
                    is_simulated = broker.okx_simulated if hasattr(broker, 'okx_simulated') else True
                    base_url = 'https://www.okx.com' if not is_simulated else 'https://www.okx.com'

                    async with aiohttp.ClientSession() as session:
                        async with session.get(f'{base_url}/api/v5/public/time', timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                server_time = data.get('data', [{}])[0].get('ts', 'unknown')
                                return True, f'Connected to OKX (server time: {server_time})'
                            return False, f'HTTP {resp.status}'
                except asyncio.TimeoutError:
                    return False, 'Connection timeout - check your internet connection'
                except aiohttp.ClientError as e:
                    return False, f'Network error: {str(e)}'
                except Exception as e:
                    return False, f'Error: {str(e)}'

            network_ok, network_msg = loop.run_until_complete(test_network())
            if network_ok:
                add_check('Network', 'PASS', network_msg)
            else:
                add_check('Network', 'FAIL', network_msg)
                diagnostics['suggestions'].append({
                    'issue': 'Network Connection Failed',
                    'fix': 'Check your internet connection and firewall settings',
                    'steps': [
                        'Verify internet connectivity',
                        'Check if OKX is accessible from your location',
                        'Ensure firewall allows HTTPS connections to okx.com'
                    ]
                })

            # Check 4: Authentication test (only if credentials exist)
            if api_key and api_secret and passphrase and network_ok:
                async def test_auth():
                    try:
                        config = BrokerConfig(
                            broker_id=broker.broker_id,
                            name=broker.name,
                            role=broker.role,
                            backend_type='OKX',
                            okx_api_key=api_key,
                            okx_api_secret=api_secret,
                            okx_passphrase=passphrase,
                            okx_simulated=broker.okx_simulated if hasattr(broker, 'okx_simulated') else True,
                            okx_account_type=broker.okx_account_type if hasattr(broker, 'okx_account_type') else 'spot',
                            symbol=symbol
                        )
                        adapter = OKXAdapter(config)
                        connected = await adapter.connect()

                        if connected:
                            account = await adapter.get_account_info()
                            await adapter.disconnect()
                            if account:
                                return True, f'Authenticated successfully (Balance: {account.balance:.2f} USDT)', account
                            return True, 'Authenticated but could not fetch account info', None
                        return False, 'Authentication failed - check API credentials', None
                    except Exception as e:
                        error_msg = str(e)
                        if 'Invalid API-key' in error_msg or '50111' in error_msg:
                            return False, 'Invalid API Key', None
                        elif 'Invalid Sign' in error_msg or '50113' in error_msg:
                            return False, 'Invalid signature - check API Secret', None
                        elif 'Invalid Passphrase' in error_msg or '50114' in error_msg:
                            return False, 'Invalid Passphrase', None
                        elif 'permission' in error_msg.lower() or '50110' in error_msg:
                            return False, 'API key lacks required permissions', None
                        return False, f'Auth error: {error_msg}', None

                auth_ok, auth_msg, account = loop.run_until_complete(test_auth())
                if auth_ok:
                    add_check('Authentication', 'PASS', auth_msg)

                    # Check 5: Symbol validity (fetch price)
                    async def test_symbol():
                        try:
                            config = BrokerConfig(
                                broker_id=broker.broker_id,
                                name=broker.name,
                                role=broker.role,
                                backend_type='OKX',
                                okx_api_key=api_key,
                                okx_api_secret=api_secret,
                                okx_passphrase=passphrase,
                                okx_simulated=broker.okx_simulated if hasattr(broker, 'okx_simulated') else True,
                                okx_account_type=broker.okx_account_type if hasattr(broker, 'okx_account_type') else 'spot',
                                symbol=symbol
                            )
                            adapter = OKXAdapter(config)
                            await adapter.connect()
                            tick = await adapter.get_tick(symbol)
                            await adapter.disconnect()

                            if tick and tick.bid > 0:
                                return True, f'Symbol valid - Bid: {tick.bid}, Ask: {tick.ask}', tick
                            return False, f'Symbol "{symbol}" returned no price data', None
                        except Exception as e:
                            return False, f'Symbol error: {str(e)}', None

                    if symbol:
                        symbol_ok, symbol_msg, tick = loop.run_until_complete(test_symbol())
                        if symbol_ok:
                            add_check('Symbol Validation', 'PASS', symbol_msg)
                        else:
                            add_check('Symbol Validation', 'FAIL', symbol_msg)
                            diagnostics['suggestions'].append({
                                'issue': 'Invalid Trading Symbol',
                                'fix': f'The symbol "{symbol}" is not valid or not available',
                                'steps': [
                                    'Check OKX for the correct symbol name',
                                    'Spot symbols: BTC-USDT, ETH-USDT, etc.',
                                    'Perpetual swaps: BTC-USDT-SWAP',
                                    'Futures: BTC-USDT-240329 (with expiry date)'
                                ]
                            })
                else:
                    add_check('Authentication', 'FAIL', auth_msg)
                    if 'Invalid API Key' in auth_msg:
                        diagnostics['suggestions'].append({
                            'issue': 'Invalid API Key',
                            'fix': 'The API key is not recognized by OKX',
                            'steps': [
                                'Verify the API key is copied correctly (no extra spaces)',
                                'Check if the API key has been deleted on OKX',
                                'Create a new API key if needed'
                            ]
                        })
                    elif 'Secret' in auth_msg:
                        diagnostics['suggestions'].append({
                            'issue': 'Invalid API Secret',
                            'fix': 'The API secret does not match the key',
                            'steps': [
                                'The secret is only shown once during creation',
                                'If lost, delete and recreate the API key'
                            ]
                        })
                    elif 'Passphrase' in auth_msg:
                        diagnostics['suggestions'].append({
                            'issue': 'Invalid Passphrase',
                            'fix': 'The passphrase is incorrect',
                            'steps': [
                                'This is the passphrase YOU created with the API key',
                                'It is NOT the same as your account password',
                                'If forgotten, delete and recreate the API key'
                            ]
                        })
                    elif 'permission' in auth_msg.lower():
                        diagnostics['suggestions'].append({
                            'issue': 'Insufficient API Permissions',
                            'fix': 'Enable required permissions for the API key',
                            'steps': [
                                'Go to OKX > Account > API',
                                'Edit the API key permissions',
                                'Enable: Read, Trade (and Withdraw if needed)',
                                'For futures: Enable futures trading permission'
                            ]
                        })

            loop.close()

        elif broker.broker_type == 'MT5':
            # MT5 comprehensive diagnostics
            try:
                import MetaTrader5 as mt5
                add_check('MT5 Library', 'PASS', 'MetaTrader5 Python library is installed')

                # Check 1: Initialize MT5
                if mt5.initialize():
                    add_check('MT5 Terminal', 'PASS', 'Connected to MT5 terminal')

                    # Check 2: Account info
                    account_info = mt5.account_info()
                    if account_info:
                        add_check('Account Login', 'PASS',
                                  f'Logged in as {account_info.login} on {account_info.server}')
                        add_check('Account Balance', 'PASS',
                                  f'Balance: {account_info.balance:.2f} {account_info.currency}')

                        # Check trading permissions
                        if account_info.trade_allowed:
                            add_check('Trading Permission', 'PASS', 'Trading is allowed on this account')
                        else:
                            add_check('Trading Permission', 'FAIL', 'Trading is NOT allowed')
                            diagnostics['suggestions'].append({
                                'issue': 'Trading Not Allowed',
                                'fix': 'Enable trading on your MT5 account',
                                'steps': [
                                    'Check if your account has trading privileges',
                                    'Contact your broker if trading is disabled',
                                    'Ensure you are not on a read-only/investor account'
                                ]
                            })
                    else:
                        add_check('Account Login', 'FAIL', 'Not logged in to any account')
                        diagnostics['suggestions'].append({
                            'issue': 'Not Logged In',
                            'fix': 'Log in to your MT5 trading account',
                            'steps': [
                                '1. Open MT5 terminal',
                                '2. File > Login to Trade Account',
                                '3. Enter your credentials'
                            ]
                        })

                    # Check 3: Symbol validation
                    symbol = broker.symbol
                    if symbol:
                        symbol_info = mt5.symbol_info(symbol)
                        if symbol_info:
                            add_check('Symbol', 'PASS', f'Symbol "{symbol}" found')

                            # Check if symbol is visible in Market Watch
                            if symbol_info.visible:
                                add_check('Market Watch', 'PASS', f'Symbol is visible in Market Watch')
                            else:
                                add_check('Market Watch', 'WARN', f'Symbol not in Market Watch')
                                # Try to add it
                                mt5.symbol_select(symbol, True)
                                diagnostics['suggestions'].append({
                                    'issue': 'Symbol Not in Market Watch',
                                    'fix': f'Add {symbol} to Market Watch',
                                    'steps': [
                                        f'Right-click Market Watch > Symbols',
                                        f'Search for {symbol} and click Show'
                                    ]
                                })

                            # Get price
                            tick = mt5.symbol_info_tick(symbol)
                            if tick and tick.bid > 0:
                                add_check('Price Data', 'PASS', f'Bid: {tick.bid}, Ask: {tick.ask}')
                            else:
                                add_check('Price Data', 'WARN', 'No price data available')
                        else:
                            add_check('Symbol', 'FAIL', f'Symbol "{symbol}" not found')
                            diagnostics['suggestions'].append({
                                'issue': 'Symbol Not Found',
                                'fix': f'The symbol "{symbol}" does not exist on this broker',
                                'steps': [
                                    'Check the exact symbol name in MT5 Market Watch',
                                    'Symbols vary by broker (e.g., XAUUSD, GOLD, GOLD_CASH)',
                                    'Update the broker config with the correct symbol'
                                ]
                            })
                    else:
                        add_check('Symbol', 'FAIL', 'No symbol configured')

                    # Check 4: Algo trading
                    terminal_info = mt5.terminal_info()
                    if terminal_info:
                        if terminal_info.trade_allowed:
                            add_check('Algo Trading', 'PASS', 'Algo trading is enabled')
                        else:
                            add_check('Algo Trading', 'FAIL', 'Algo trading is DISABLED')
                            diagnostics['suggestions'].append({
                                'issue': 'Algo Trading Disabled',
                                'fix': 'Enable algorithmic trading in MT5',
                                'steps': [
                                    '1. Tools > Options > Expert Advisors',
                                    '2. Check "Allow algorithmic trading"',
                                    '3. Check "Allow DLL imports" if needed',
                                    '4. Click OK and restart MT5'
                                ]
                            })

                    mt5.shutdown()
                else:
                    error = mt5.last_error()
                    add_check('MT5 Terminal', 'FAIL', f'Cannot connect to MT5: {error}')
                    diagnostics['suggestions'].append({
                        'issue': 'MT5 Not Running',
                        'fix': 'Start MetaTrader 5 terminal',
                        'steps': [
                            '1. Open MetaTrader 5 application',
                            '2. Log in to your trading account',
                            '3. Wait for connection to establish',
                            '4. Try the test again'
                        ]
                    })

            except ImportError:
                add_check('MT5 Library', 'FAIL', 'MetaTrader5 library not installed')
                diagnostics['suggestions'].append({
                    'issue': 'Missing MT5 Library',
                    'fix': 'Install the MetaTrader5 Python package',
                    'steps': [
                        'Run: pip install MetaTrader5',
                        'Restart the application after installation'
                    ]
                })
            except Exception as e:
                add_check('MT5 Error', 'FAIL', f'Unexpected error: {str(e)}')

        elif broker.broker_type in ['FIX', 'FLEXTRADE']:
            add_check('FIX Connection', 'INFO', 'FIX protocol requires gateway configuration')
            diagnostics['suggestions'].append({
                'issue': 'FIX Connection Setup',
                'fix': 'Configure FIX gateway settings',
                'steps': [
                    '1. Verify FIX gateway host and port',
                    '2. Check SenderCompID and TargetCompID',
                    '3. Ensure firewall allows the connection',
                    '4. Verify SSL/TLS certificates if required'
                ]
            })

        elif broker.broker_type == 'IB':
            add_check('IB Gateway', 'INFO', 'Interactive Brokers requires TWS or IB Gateway')
            diagnostics['suggestions'].append({
                'issue': 'IB Connection Setup',
                'fix': 'Ensure TWS or IB Gateway is running',
                'steps': [
                    '1. Start TWS or IB Gateway',
                    '2. Enable API connections in settings',
                    '3. Add your IP to trusted IPs',
                    '4. Note the socket port (default: 7497 for TWS, 4001 for Gateway)'
                ]
            })

        # Calculate overall status
        statuses = [c['status'] for c in diagnostics['checks']]
        if all(s == 'PASS' for s in statuses):
            diagnostics['overall_status'] = 'HEALTHY'
        elif 'FAIL' in statuses:
            diagnostics['overall_status'] = 'ERROR'
        elif 'WARN' in statuses:
            diagnostics['overall_status'] = 'WARNING'
        else:
            diagnostics['overall_status'] = 'INCOMPLETE'

        return jsonify(diagnostics)

    except Exception as e:
        logger.error(f"Diagnostic error: {e}")
        diagnostics['checks'].append({
            'name': 'System Error',
            'status': 'FAIL',
            'message': str(e)
        })
        diagnostics['overall_status'] = 'ERROR'
        return jsonify(diagnostics)


@app.route('/api/trades')
def api_trades():
    """Get trade history"""
    database = get_db()
    status = request.args.get('status')
    limit = int(request.args.get('limit', 100))

    trades = database.get_trades(status=status, limit=limit)
    return jsonify([t.to_dict() for t in trades])


@app.route('/api/trades/stats')
def api_trade_stats():
    """Get trade statistics"""
    database = get_db()
    stats = database.get_trade_statistics()
    return jsonify(stats)


@app.route('/api/sd-touches')
def api_sd_touches():
    """Get SD touch log"""
    database = get_db()
    limit = int(request.args.get('limit', 100))
    sd_level = request.args.get('sd_level')

    touches = database.get_sd_touches(sd_level=sd_level, limit=limit)
    return jsonify([t.to_dict() for t in touches])


@app.route('/api/sd-touches/stats')
def api_sd_stats():
    """Get SD touch statistics"""
    database = get_db()
    stats = database.get_sd_touch_stats()
    return jsonify(stats)


@app.route('/api/limit-orders/stats')
def api_limit_stats():
    """Get limit order statistics"""
    database = get_db()
    stats = database.get_limit_order_stats()
    return jsonify(stats)


@app.route('/api/engine/start', methods=['POST'])
def api_engine_start():
    """Start trading engine"""
    global engine, engine_loop

    if engine and engine.state == EngineState.RUNNING:
        return jsonify({'success': False, 'error': 'Engine already running'})

    try:
        # Create event loop in background thread
        def run_engine():
            global engine, engine_loop
            engine_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(engine_loop)

            engine = TradingEngine(db_path="trading.db")

            # Register callbacks for SocketIO updates
            engine.on_tick(lambda m: socketio.emit('tick', {
                'spread': m.spread,
                'zscore': m.zscore,
                'spot_bid': m.spot_bid,
                'spot_ask': m.spot_ask,
                'futures_bid': m.futures_bid,
                'futures_ask': m.futures_ask
            }))

            engine.on_signal(lambda s: socketio.emit('signal', s.to_dict()))

            engine.on_trade(lambda action, t: socketio.emit('trade', {
                'action': action,
                'trade': t.to_dict()
            }))

            engine_loop.run_until_complete(engine.initialize())
            engine_loop.run_until_complete(engine.start())
            engine_loop.run_forever()

        thread = threading.Thread(target=run_engine, daemon=True)
        thread.start()

        return jsonify({'success': True, 'message': 'Engine starting'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/engine/stop', methods=['POST'])
def api_engine_stop():
    """Stop trading engine"""
    global engine, engine_loop

    if not engine:
        return jsonify({'success': False, 'error': 'Engine not running'})

    try:
        if engine_loop:
            asyncio.run_coroutine_threadsafe(engine.stop(), engine_loop)
            engine_loop.call_soon_threadsafe(engine_loop.stop)

        return jsonify({'success': True, 'message': 'Engine stopping'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/engine/toggle-algo', methods=['POST'])
def api_toggle_algo():
    """Toggle algorithm on/off"""
    database = get_db()
    data = request.get_json()
    enabled = data.get('enabled', False)

    database.update_config_field('algo_enabled', enabled)

    if engine:
        engine.reload_config()

    return jsonify({'success': True, 'algo_enabled': enabled})


@app.route('/api/clear-data', methods=['POST'])
def api_clear_data():
    """Clear historical data"""
    database = get_db()
    data = request.get_json()
    data_type = data.get('type', 'all')

    if data_type == 'prices' or data_type == 'all':
        database.clear_price_history()

    if data_type == 'trades' or data_type == 'all':
        database.clear_trades()

    if data_type == 'sd_touches' or data_type == 'all':
        database.clear_sd_touches()

    return jsonify({'success': True})


# ==================== Manual Trading API ====================

def get_okx_adapter():
    """Get or create OKX adapter for manual trading"""
    from adapters.okx_adapter import OKXAdapter
    from adapters.base import BrokerConfig
    import os

    config = BrokerConfig(
        broker_id='manual_okx',
        name='Manual OKX',
        role='SPOT',
        backend_type='OKX',
        okx_api_key=os.environ.get('OKX_API_KEY', ''),
        okx_api_secret=os.environ.get('OKX_API_SECRET', ''),
        okx_passphrase=os.environ.get('OKX_PASSPHRASE', ''),
        okx_simulated=os.environ.get('OKX_SIMULATED', 'true').lower() == 'true',
        okx_account_type='spot',
        symbol='BTC-USDT'
    )

    return OKXAdapter(config)


@app.route('/api/manual/test-connection', methods=['POST'])
def api_manual_test_connection():
    """Test OKX connection"""
    try:
        adapter = get_okx_adapter()

        # Run async connect in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            connected = loop.run_until_complete(adapter.connect())
            if connected:
                account = loop.run_until_complete(adapter.get_account_info())
                loop.run_until_complete(adapter.disconnect())

                if account:
                    return jsonify({
                        'success': True,
                        'message': 'Connected successfully',
                        'account': {
                            'balance': account.balance,
                            'equity': account.equity,
                            'currency': 'USDT'
                        }
                    })
                else:
                    return jsonify({
                        'success': True,
                        'message': 'Connected (mock mode or no account info)'
                    })
            else:
                return jsonify({
                    'success': False,
                    'error': 'Failed to connect'
                })
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Test connection error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/manual/place-order', methods=['POST'])
def api_manual_place_order():
    """Place a manual order via OKX"""
    try:
        data = request.get_json()
        symbol = data.get('symbol', 'BTC-USDT')
        side = data.get('side', 'BUY')
        price = data.get('price')
        size = data.get('size', 0.001)
        order_type = data.get('order_type', 'limit')

        # Determine if this is swap or spot
        is_swap = '-SWAP' in symbol

        from adapters.okx_adapter import OKXAdapter
        from adapters.base import BrokerConfig, OrderSide
        import os

        config = BrokerConfig(
            broker_id='manual_okx',
            name='Manual OKX',
            role='FUTURES' if is_swap else 'SPOT',
            backend_type='OKX',
            okx_api_key=os.environ.get('OKX_API_KEY', ''),
            okx_api_secret=os.environ.get('OKX_API_SECRET', ''),
            okx_passphrase=os.environ.get('OKX_PASSPHRASE', ''),
            okx_simulated=os.environ.get('OKX_SIMULATED', 'true').lower() == 'true',
            okx_account_type='swap' if is_swap else 'spot',
            symbol=symbol
        )

        adapter = OKXAdapter(config)
        order_side = OrderSide.BUY if side == 'BUY' else OrderSide.SELL

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            connected = loop.run_until_complete(adapter.connect())
            if not connected:
                return jsonify({
                    'success': False,
                    'error': 'Failed to connect to OKX'
                })

            if order_type == 'market':
                result = loop.run_until_complete(
                    adapter.place_market_order(symbol, order_side, size)
                )
            else:
                if not price:
                    # Get current price if not provided
                    tick = loop.run_until_complete(adapter.get_tick(symbol))
                    if tick:
                        price = tick.bid if side == 'SELL' else tick.ask
                    else:
                        return jsonify({
                            'success': False,
                            'error': 'Could not get price. Please specify a price.'
                        })

                result = loop.run_until_complete(
                    adapter.place_limit_order(symbol, order_side, size, price)
                )

            loop.run_until_complete(adapter.disconnect())

            if result and result.success:
                return jsonify({
                    'success': True,
                    'order_id': result.ticket,
                    'fill_price': result.price,
                    'message': 'Order placed successfully'
                })
            else:
                return jsonify({
                    'success': False,
                    'error': result.error if result else 'Unknown error'
                })

        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Place order error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/manual/get-price', methods=['POST'])
def api_manual_get_price():
    """Get current price for a symbol"""
    try:
        data = request.get_json()
        symbol = data.get('symbol', 'BTC-USDT')

        # Determine if this is swap or spot
        is_swap = '-SWAP' in symbol

        from adapters.okx_adapter import OKXAdapter
        from adapters.base import BrokerConfig
        import os

        config = BrokerConfig(
            broker_id='manual_okx',
            name='Manual OKX',
            role='FUTURES' if is_swap else 'SPOT',
            backend_type='OKX',
            okx_api_key=os.environ.get('OKX_API_KEY', ''),
            okx_api_secret=os.environ.get('OKX_API_SECRET', ''),
            okx_passphrase=os.environ.get('OKX_PASSPHRASE', ''),
            okx_simulated=os.environ.get('OKX_SIMULATED', 'true').lower() == 'true',
            okx_account_type='swap' if is_swap else 'spot',
            symbol=symbol
        )

        adapter = OKXAdapter(config)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            connected = loop.run_until_complete(adapter.connect())
            if not connected:
                return jsonify({
                    'success': False,
                    'error': 'Failed to connect'
                })

            tick = loop.run_until_complete(adapter.get_tick(symbol))
            loop.run_until_complete(adapter.disconnect())

            if tick:
                spread = tick.ask - tick.bid
                spread_pct = (spread / tick.bid) * 100 if tick.bid > 0 else 0
                return jsonify({
                    'success': True,
                    'symbol': symbol,
                    'bid': f'{tick.bid:.2f}',
                    'ask': f'{tick.ask:.2f}',
                    'spread': f'{spread:.2f} ({spread_pct:.4f}%)',
                    'timestamp': tick.timestamp.isoformat() if tick.timestamp else None
                })
            else:
                return jsonify({
                    'success': False,
                    'error': 'Could not get price'
                })

        finally:
            loop.close()

    except Exception as e:
        logger.error(f"Get price error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


# ==================== Broker Update API ====================

@app.route('/api/brokers/<broker_id>/update', methods=['POST'])
def api_broker_update(broker_id):
    """Update broker settings (swap charge, expiry, etc.)"""
    try:
        data = request.get_json()
        database = get_db()
        broker = database.get_broker(broker_id)

        if not broker:
            return jsonify({'success': False, 'error': 'Broker not found'})

        # Update allowed fields
        if 'swap_charge' in data:
            broker.swap_charge = float(data['swap_charge'])
        if 'futures_expiry' in data:
            broker.futures_expiry = data['futures_expiry']
        if 'contract_size' in data:
            broker.contract_size = float(data['contract_size'])

        database.add_broker(broker)
        logger.info(f"[BROKER] Updated {broker_id}: swap={broker.swap_charge}, expiry={broker.futures_expiry}")

        return jsonify({'success': True, 'broker': broker.to_dict()})

    except Exception as e:
        logger.error(f"Broker update error: {e}")
        return jsonify({'success': False, 'error': str(e)})


# ==================== Test Order API ====================

@app.route('/api/run-tests', methods=['POST'])
def api_run_tests():
    """Run the test suite and return results"""
    import subprocess

    try:
        # Get the project root directory (parent of feature_files)
        project_root = Path(__file__).parent.parent

        # Run pytest with verbose output
        result = subprocess.run(
            ['python', '-m', 'pytest', 'tests/test_trading_system.py', '-v', '--tb=short'],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=300  # 5 minute timeout
        )

        # Parse the output to get summary
        output = result.stdout + result.stderr

        # Determine if tests passed
        passed = result.returncode == 0

        return jsonify({
            'success': True,
            'passed': passed,
            'return_code': result.returncode,
            'output': output,
            'summary': 'All tests passed!' if passed else 'Some tests failed.'
        })

    except subprocess.TimeoutExpired:
        return jsonify({
            'success': False,
            'error': 'Test execution timed out (5 minute limit)'
        })
    except FileNotFoundError:
        return jsonify({
            'success': False,
            'error': 'Python or pytest not found. Make sure pytest is installed: pip install pytest'
        })
    except Exception as e:
        logger.error(f"Test execution error: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        })


@app.route('/api/test-order', methods=['POST'])
def api_test_order():
    """Execute a test order on active broker"""
    try:
        data = request.get_json()
        leg = data.get('leg')  # 'spot' or 'futures'
        direction = data.get('direction')  # 'buy' or 'sell'

        database = get_db()

        # Load active brokers from JSON file (consistent with rest of app)
        spot_broker_id, futures_broker_id = load_active_brokers()

        # Get active broker based on leg
        if leg == 'spot':
            broker_id = spot_broker_id
        else:
            broker_id = futures_broker_id

        if not broker_id:
            return jsonify({'success': False, 'error': f'No active {leg} broker selected'})

        broker = database.get_broker(broker_id)
        if not broker:
            return jsonify({'success': False, 'error': f'Broker {broker_id} not found'})

        # Execute based on broker type
        if broker.broker_type == 'MT5':
            try:
                import MetaTrader5 as mt5

                if not mt5.initialize():
                    return jsonify({'success': False, 'error': 'Failed to initialize MT5'})

                symbol = broker.symbol
                symbol_info = mt5.symbol_info(symbol)

                if symbol_info is None:
                    mt5.shutdown()
                    return jsonify({'success': False, 'error': f'Symbol {symbol} not found'})

                if not symbol_info.visible:
                    mt5.symbol_select(symbol, True)

                # Get minimum volume
                min_volume = symbol_info.volume_min
                tick = mt5.symbol_info_tick(symbol)

                if not tick:
                    mt5.shutdown()
                    return jsonify({'success': False, 'error': 'Could not get price'})

                # Prepare order
                if direction == 'buy':
                    order_type = mt5.ORDER_TYPE_BUY
                    price = tick.ask
                else:
                    order_type = mt5.ORDER_TYPE_SELL
                    price = tick.bid

                request_order = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": min_volume,
                    "type": order_type,
                    "price": price,
                    "deviation": 20,
                    "magic": 123456,
                    "comment": "Test Order",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }

                # Send order
                result = mt5.order_send(request_order)
                mt5.shutdown()

                if result.retcode != mt5.TRADE_RETCODE_DONE:
                    return jsonify({
                        'success': False,
                        'error': f'Order failed: {result.comment} (code: {result.retcode})'
                    })

                return jsonify({
                    'success': True,
                    'message': f'{direction.upper()} {min_volume} {symbol} @ {price:.2f}',
                    'ticket': result.order,
                    'volume': min_volume,
                    'price': price
                })

            except ImportError:
                return jsonify({'success': False, 'error': 'MetaTrader5 library not installed'})
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})

        elif broker.broker_type == 'OKX':
            # OKX order execution
            return jsonify({'success': False, 'error': 'OKX test orders not implemented yet'})

        else:
            return jsonify({'success': False, 'error': f'Test orders not supported for {broker.broker_type}'})

    except Exception as e:
        logger.error(f"Test order error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/test-order-cycle', methods=['POST'])
def api_test_order_cycle():
    """Test full order cycle: open position, find by ticket, close by ticket, verify closure"""
    try:
        import MetaTrader5 as mt5
        import time

        data = request.get_json() or {}
        test_type = data.get('test_type', 'open_close')

        database = get_db()
        spot_broker_id, futures_broker_id = load_active_brokers()

        if not spot_broker_id:
            return jsonify({'success': False, 'error': 'No active spot broker configured'})

        broker = database.get_broker(spot_broker_id)
        if not broker:
            return jsonify({'success': False, 'error': 'Spot broker not found'})

        if broker.broker_type != 'MT5':
            return jsonify({'success': False, 'error': f'Order cycle test only supports MT5, got {broker.broker_type}'})

        # Initialize MT5
        if not mt5.initialize():
            return jsonify({'success': False, 'error': 'Failed to initialize MT5'})

        symbol = broker.symbol
        symbol_info = mt5.symbol_info(symbol)

        if symbol_info is None:
            mt5.shutdown()
            return jsonify({'success': False, 'error': f'Symbol {symbol} not found'})

        if not symbol_info.visible:
            mt5.symbol_select(symbol, True)

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            mt5.shutdown()
            return jsonify({'success': False, 'error': 'Could not get price'})

        min_volume = symbol_info.volume_min

        # Step 1: Open position
        open_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": min_volume,
            "type": mt5.ORDER_TYPE_BUY,
            "price": tick.ask,
            "deviation": 20,
            "magic": 987654,
            "comment": "Order Cycle Test",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        open_result = mt5.order_send(open_request)

        if open_result.retcode != mt5.TRADE_RETCODE_DONE:
            mt5.shutdown()
            return jsonify({
                'success': False,
                'error': f'Failed to open position: {open_result.comment} (code: {open_result.retcode})'
            })

        logger.info(f"Order cycle test: Opened position, order={open_result.order}")

        # Wait for position to appear
        time.sleep(0.5)

        # Step 2: Find position by magic number to get ticket
        positions = mt5.positions_get(symbol=symbol)
        position_ticket = None
        for pos in positions or []:
            if pos.magic == 987654:
                position_ticket = pos.ticket
                break

        if not position_ticket:
            mt5.shutdown()
            return jsonify({
                'success': False,
                'error': 'Could not find opened position by magic number'
            })

        logger.info(f"Order cycle test: Found position ticket={position_ticket}")

        # Step 3: For ticket_close test, verify we can find by ticket
        found_by_ticket = False
        if test_type == 'ticket_close':
            found_positions = mt5.positions_get(ticket=position_ticket)
            found_by_ticket = found_positions is not None and len(found_positions) == 1
            logger.info(f"Order cycle test: Found by ticket={found_by_ticket}")

        # Step 4: Close by ticket
        tick = mt5.symbol_info_tick(symbol)
        close_request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": min_volume,
            "type": mt5.ORDER_TYPE_SELL,
            "position": position_ticket,  # Close by ticket
            "price": tick.bid,
            "deviation": 20,
            "magic": 987654,
            "comment": "Order Cycle Close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        close_result = mt5.order_send(close_request)

        if close_result.retcode != mt5.TRADE_RETCODE_DONE:
            mt5.shutdown()
            return jsonify({
                'success': False,
                'error': f'Failed to close position: {close_result.comment} (code: {close_result.retcode})',
                'open_ticket': position_ticket
            })

        logger.info(f"Order cycle test: Closed position, order={close_result.order}")

        # Step 5: Verify closure
        time.sleep(0.5)
        remaining = mt5.positions_get(ticket=position_ticket)
        verified_closed = remaining is None or len(remaining) == 0

        # Get profit from the close deal
        profit = 0
        try:
            from datetime import datetime, timedelta
            deals = mt5.history_deals_get(datetime.now() - timedelta(minutes=1), datetime.now())
            for deal in (deals or []):
                if deal.position_id == position_ticket:
                    profit = deal.profit
                    break
        except:
            pass

        mt5.shutdown()

        logger.info(f"Order cycle test: Complete! verified_closed={verified_closed}, profit={profit}")

        return jsonify({
            'success': True,
            'open_ticket': position_ticket,
            'close_ticket': close_result.order,
            'symbol': symbol,
            'volume': min_volume,
            'profit': profit,
            'found_by_ticket': found_by_ticket if test_type == 'ticket_close' else None,
            'verified_closed': verified_closed
        })

    except ImportError:
        return jsonify({'success': False, 'error': 'MetaTrader5 library not installed'})
    except Exception as e:
        logger.error(f"Order cycle test error: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/broker/positions')
def api_broker_positions():
    """Get current open positions from active broker(s)"""
    try:
        database = get_db()
        spot_broker_id, futures_broker_id = load_active_brokers()

        if not spot_broker_id and not futures_broker_id:
            return jsonify({'success': False, 'error': 'No active brokers configured', 'positions': []})

        all_positions = []
        broker_types = set()

        # Get broker info
        for broker_id in [spot_broker_id, futures_broker_id]:
            if broker_id:
                broker = database.get_broker(broker_id)
                if broker:
                    broker_types.add(broker.broker_type)

        # Fetch from MT5 if any broker uses it
        if 'MT5' in broker_types:
            try:
                import MetaTrader5 as mt5
                if mt5.initialize():
                    positions = mt5.positions_get()
                    mt5.shutdown()
                    if positions:
                        for pos in positions:
                            all_positions.append({
                                'broker': 'MT5',
                                'ticket': pos.ticket,
                                'symbol': pos.symbol,
                                'type': 'BUY' if pos.type == 0 else 'SELL',
                                'volume': pos.volume,
                                'price_open': pos.price_open,
                                'price_current': pos.price_current,
                                'profit': pos.profit,
                                'swap': pos.swap,
                                'time': pos.time,
                                'comment': pos.comment
                            })
            except ImportError:
                pass

        # Fetch from OKX if any broker uses it (placeholder - needs OKX API implementation)
        if 'OKX' in broker_types:
            # TODO: Add OKX position fetching when API is configured
            pass

        return jsonify({'success': True, 'positions': all_positions, 'brokers': list(broker_types)})

    except Exception as e:
        logger.error(f"Broker positions error: {e}")
        return jsonify({'success': False, 'error': str(e), 'positions': []})


@app.route('/api/broker/history')
def api_broker_history():
    """Get trade history from active broker(s)"""
    try:
        from datetime import datetime, timedelta

        database = get_db()
        spot_broker_id, futures_broker_id = load_active_brokers()

        if not spot_broker_id and not futures_broker_id:
            return jsonify({'success': False, 'error': 'No active brokers configured', 'deals': []})

        days = int(request.args.get('days', 30))
        all_deals = []
        broker_types = set()

        # Get broker info
        for broker_id in [spot_broker_id, futures_broker_id]:
            if broker_id:
                broker = database.get_broker(broker_id)
                if broker:
                    broker_types.add(broker.broker_type)

        # Fetch from MT5 if any broker uses it
        if 'MT5' in broker_types:
            try:
                import MetaTrader5 as mt5
                if mt5.initialize():
                    from_date = datetime.now() - timedelta(days=days)
                    to_date = datetime.now()
                    deals = mt5.history_deals_get(from_date, to_date)
                    mt5.shutdown()

                    if deals:
                        for deal in deals:
                            # Include both entry and exit deals
                            all_deals.append({
                                'broker': 'MT5',
                                'ticket': deal.ticket,
                                'order': deal.order,
                                'time': deal.time,
                                'type': 'BUY' if deal.type == 0 else 'SELL',
                                'entry': 'IN' if deal.entry == 0 else 'OUT',
                                'symbol': deal.symbol,
                                'volume': deal.volume,
                                'price': deal.price,
                                'profit': deal.profit,
                                'swap': deal.swap,
                                'commission': deal.commission,
                                'comment': deal.comment
                            })
            except ImportError:
                pass

        # Fetch from OKX if any broker uses it (placeholder - needs OKX API implementation)
        if 'OKX' in broker_types:
            # TODO: Add OKX trade history fetching when API is configured
            pass

        # Sort by time descending (most recent first)
        all_deals.sort(key=lambda x: x['time'], reverse=True)

        return jsonify({'success': True, 'deals': all_deals, 'brokers': list(broker_types)})

    except Exception as e:
        logger.error(f"Broker history error: {e}")
        return jsonify({'success': False, 'error': str(e), 'deals': []})


# Legacy MT5-specific endpoints (redirect to generic)
@app.route('/api/mt5/positions')
def api_mt5_positions():
    """Legacy endpoint - redirects to generic broker positions"""
    return api_broker_positions()


@app.route('/api/mt5/history')
def api_mt5_history():
    """Legacy endpoint - redirects to generic broker history"""
    return api_broker_history()


# ==================== SocketIO Events ====================

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    logger.info("Client disconnected")


@socketio.on('request_status')
def handle_request_status():
    """Send current status to client"""
    if engine:
        emit('status', engine.get_status())


# ==================== Background Price Streaming ====================

price_streaming_active = False

def start_price_streaming():
    """Start background price streaming from active brokers"""
    global price_streaming_active

    if price_streaming_active:
        logger.info("[PRICES] Price streaming already active")
        return

    price_streaming_active = True
    logger.info("[PRICES] Starting price streaming thread")

    def stream_prices():
        global price_streaming_active
        import time
        import numpy as np
        from collections import deque

        log_counter = 0
        spread_history = deque(maxlen=2000)  # Store spread history for z-score calculation

        while price_streaming_active:
            try:
                database = get_db()

                # Load active brokers from JSON file (more reliable than database)
                spot_broker_id, futures_broker_id = load_active_brokers()

                # Log every 10 seconds
                log_counter += 1
                if log_counter % 10 == 1:
                    logger.info(f"[PRICES] Active brokers from file - Spot: {spot_broker_id}, Futures: {futures_broker_id}")

                if not spot_broker_id or not futures_broker_id:
                    time.sleep(1)
                    continue

                spot_broker = database.get_broker(spot_broker_id)
                futures_broker = database.get_broker(futures_broker_id)

                if not spot_broker or not futures_broker:
                    logger.warning(f"[PRICES] Broker not found - Spot: {spot_broker}, Futures: {futures_broker}")
                    time.sleep(2)
                    continue

                spot_bid, spot_ask = 0, 0
                futures_bid, futures_ask = 0, 0

                # Fetch MT5 prices
                if spot_broker.broker_type == 'MT5' or futures_broker.broker_type == 'MT5':
                    try:
                        import MetaTrader5 as mt5

                        if not mt5.initialize():
                            time.sleep(2)
                            continue

                        # Get spot price
                        if spot_broker.broker_type == 'MT5':
                            tick = mt5.symbol_info_tick(spot_broker.symbol)
                            if tick:
                                spot_bid = tick.bid
                                spot_ask = tick.ask

                        # Get futures price
                        if futures_broker.broker_type == 'MT5':
                            tick = mt5.symbol_info_tick(futures_broker.symbol)
                            if tick:
                                futures_bid = tick.bid
                                futures_ask = tick.ask

                        mt5.shutdown()

                    except ImportError:
                        pass
                    except Exception as e:
                        logger.error(f"MT5 price fetch error: {e}")

                # Emit price update
                if spot_bid > 0 or futures_bid > 0:
                    spread = ((futures_bid + futures_ask) / 2) - ((spot_bid + spot_ask) / 2) if spot_bid > 0 and futures_bid > 0 else 0

                    # Add spread to history for z-score calculation
                    if spread != 0:
                        spread_history.append(spread)

                    # Get lookback period from config
                    config = database.get_config()
                    lookback_period = config.lookback_period if config else 90

                    # Calculate mean, std, and z-score from spread history
                    mean_val = 0.0
                    std_val = 0.0
                    zscore = None  # None until lookback is complete
                    lookback_complete = len(spread_history) >= lookback_period

                    if lookback_complete:
                        # Use most recent lookback_period points
                        history_list = list(spread_history)[-lookback_period:]
                        mean_val = float(np.mean(history_list))
                        std_val = float(np.std(history_list))

                        if std_val > 0:
                            zscore = (spread - mean_val) / std_val

                    socketio.emit('tick', {
                        'spot_bid': spot_bid,
                        'spot_ask': spot_ask,
                        'futures_bid': futures_bid,
                        'futures_ask': futures_ask,
                        'spread': spread,
                        'zscore': zscore,
                        'mean': mean_val if lookback_complete else None,
                        'std': std_val if lookback_complete else None,
                        'history_count': len(spread_history),
                        'lookback_required': lookback_period,
                        'lookback_complete': lookback_complete
                    })

                time.sleep(0.3)  # Update every 0.3 seconds

            except Exception as e:
                logger.error(f"Price streaming error: {e}")
                time.sleep(1)

    thread = threading.Thread(target=stream_prices, daemon=True)
    thread.start()
    logger.info("Price streaming started")


@socketio.on('connect')
def handle_connect():
    """Handle client connection and start price streaming"""
    logger.info("Client connected")
    start_price_streaming()
    if engine:
        emit('status', engine.get_status())


# ==================== Entry Point ====================

def run_server(host: str = '0.0.0.0', port: int = 5000, debug: bool = False):
    """Run the Flask server"""
    init_app()
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_server(debug=True)
