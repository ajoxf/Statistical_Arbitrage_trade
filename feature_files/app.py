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
            symbol=data.get('symbol', ''),
            contract_size=data.get('contract_size', 100.0),
            commission_per_lot=data.get('commission_per_lot', 0.0)
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
