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

    return render_template(
        'dashboard.html',
        config=config,
        brokers=brokers,
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

    return render_template('settings.html', config=config)


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

        config = database.get_config()

        # Update fields from request
        for key, value in data.items():
            if hasattr(config, key):
                # Type conversion
                field_type = type(getattr(config, key))
                if field_type == bool:
                    value = bool(value)
                elif field_type == int:
                    value = int(value)
                elif field_type == float:
                    value = float(value)
                setattr(config, key, value)

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
            okx_api_key=data.get('okx_api_key'),
            okx_api_secret=data.get('okx_api_secret'),
            okx_passphrase=data.get('okx_passphrase'),
            okx_simulated=data.get('okx_simulated', True),
            okx_account_type=data.get('okx_account_type', 'spot'),
            symbol=data.get('symbol', ''),
            contract_size=float(data.get('contract_size', 100.0)),
            commission_per_lot=float(data.get('commission_per_lot', 0.0)),
            swap_charge=float(data.get('swap_charge', 0.0)),
            futures_expiry=data.get('futures_expiry') or None
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
            # MT5 test - placeholder for now
            return jsonify({
                'success': False,
                'error': 'MT5 connection test not implemented yet. Please ensure MT5 terminal is running.'
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
            add_check('MT5 Terminal', 'INFO', 'MT5 requires the terminal to be running')
            diagnostics['suggestions'].append({
                'issue': 'MT5 Connection Setup',
                'fix': 'Ensure MetaTrader 5 terminal is properly configured',
                'steps': [
                    '1. Open MetaTrader 5 terminal',
                    '2. Log in to your trading account',
                    '3. Enable algo trading (Tools > Options > Expert Advisors)',
                    '4. Allow DLL imports if required',
                    '5. Ensure the symbol exists in Market Watch'
                ]
            })

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


# ==================== SocketIO Events ====================

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    logger.info("Client connected")
    if engine:
        emit('status', engine.get_status())


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    logger.info("Client disconnected")


@socketio.on('request_status')
def handle_request_status():
    """Send current status to client"""
    if engine:
        emit('status', engine.get_status())


# ==================== Entry Point ====================

def run_server(host: str = '0.0.0.0', port: int = 5000, debug: bool = False):
    """Run the Flask server"""
    init_app()
    socketio.run(app, host=host, port=port, debug=debug)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run_server(debug=True)
