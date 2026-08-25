"""Main trading system: wiring, market data, signal processing, loop."""

import logging
import sys
import time
from datetime import datetime, time as dt_time

import pytz

from .broker import BrokerSession
from .database import DataLogger
from .execution import OrderManager
from . import marketdata
from .marketdata import compute_market_data
from .models import SignalType, TradingSession
from .performance import PerformanceTracker
from .positions import PositionManager
from .risk import RiskManager
from .signals import SignalGenerator


class AlgorithmicTradingSystem:
    def __init__(self, config, trading_mode="PAPER"):
        self.config = config
        self.trading_mode = trading_mode  # "PAPER" or "LIVE"
        self.is_running = False

        self.broker = self._build_broker_session()
        self.data_logger = DataLogger()
        self.order_manager = OrderManager(config, self.broker)
        self.position_manager = PositionManager(self.data_logger)
        self.risk_manager = RiskManager(config)
        self.signal_generator = SignalGenerator(config)
        self.performance_tracker = PerformanceTracker()

        self.active_assets = {}
        self.last_market_data = {}
        self.last_signals = {}
        self.update_counter = 0

        self.trading_tz = pytz.timezone('US/Eastern')
        self.session_schedule = {
            TradingSession.ASIAN_PRE: (dt_time(18, 0), dt_time(21, 30)),
            TradingSession.CHINA_OPEN: (dt_time(21, 30), dt_time(23, 59)),
            TradingSession.ASIAN_LATE: (dt_time(0, 0), dt_time(3, 0)),
            TradingSession.LONDON_OPEN: (dt_time(3, 0), dt_time(6, 0)),
            TradingSession.EUROPEAN: (dt_time(6, 0), dt_time(9, 30)),
            TradingSession.US_OPEN: (dt_time(9, 30), dt_time(12, 0)),
            TradingSession.US_AFTERNOON: (dt_time(12, 0), dt_time(16, 0)),
            TradingSession.AFTER_HOURS: (dt_time(16, 0), dt_time(18, 0)),
        }

        logging.info("Algorithmic Trading System initialized in %s mode",
                     trading_mode)

    def _build_broker_session(self):
        """Resolve which account this process connects to.

        One process can hold exactly one MT5 connection. If spot and
        futures legs are configured for different accounts, running
        both from a single process is impossible — that needs the
        two-process coordinator (not yet implemented; see CLAUDE.md).
        """
        spot_acct = self.config.leg_accounts.get('spot', 'default')
        fut_acct = self.config.leg_accounts.get('futures', 'default')

        if spot_acct != fut_acct:
            logging.warning(
                "leg_accounts assigns spot to '%s' and futures to '%s'. "
                "The MetaTrader5 package supports ONE connection per "
                "process, so this build connects ONLY to '%s'. "
                "Simultaneous two-account streaming requires one process "
                "per account plus a coordinator (pending).",
                spot_acct, fut_acct, spot_acct)

        return BrokerSession(self.config.accounts[spot_acct])

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def initialize_broker(self):
        logging.info("Connecting to MT5...")
        if not self.broker.initialize():
            return False
        self.config.validate_expiries()
        return self.setup_all_symbols()

    def setup_all_symbols(self):
        logging.info("Setting up symbols...")
        for asset_key, asset_config in self.config.ASSETS.items():
            if not asset_config['enabled']:
                continue

            spot_symbol = next(
                (s for s in asset_config['spot_symbols']
                 if self.broker.ensure_symbol(s)), None)
            futures_symbol = next(
                (s for s in asset_config['futures_symbols']
                 if self.broker.ensure_symbol(s)), None)

            if spot_symbol and futures_symbol:
                self.active_assets[asset_key] = {
                    'config': asset_config,
                    'spot_symbol': spot_symbol,
                    'futures_symbol': futures_symbol,
                    'last_data': None,
                }
                logging.info("%s: %s + %s", asset_key, spot_symbol,
                             futures_symbol)
            else:
                logging.warning("%s: Missing symbols - Spot: %s, Futures: %s",
                                asset_key, spot_symbol, futures_symbol)

        return len(self.active_assets) > 0

    def get_current_session(self):
        current_time = datetime.now(self.trading_tz).time()
        for session, (start, end) in self.session_schedule.items():
            if start > end:  # crosses midnight
                if current_time >= start or current_time <= end:
                    return session
            elif start <= current_time <= end:
                return session
        return TradingSession.AFTER_HOURS

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_market_data(self, asset_key):
        if asset_key not in self.active_assets:
            return None
        try:
            asset = self.active_assets[asset_key]
            spot_tick = self.broker.symbol_tick(asset['spot_symbol'])
            futures_tick = self.broker.symbol_tick(asset['futures_symbol'])
            if not spot_tick or not futures_tick:
                return None
            return compute_market_data(
                asset['config'], spot_tick, futures_tick,
                self.config.TRADING.get('HEDGE_RATIO', 1.0))
        except Exception as e:
            logging.error("Error getting market data for %s: %s", asset_key, e)
            return None

    def get_all_market_data(self):
        all_data = {}
        for asset_key in self.active_assets:
            market_data = self.get_market_data(asset_key)
            if market_data:
                all_data[asset_key] = market_data
                self.active_assets[asset_key]['last_data'] = market_data
            else:
                last = self.active_assets[asset_key]['last_data']
                if last and (datetime.now() - last['timestamp']
                             ).total_seconds() < 30:
                    all_data[asset_key] = last
        return all_data

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    def process_trading_signals(self, all_market_data):
        for asset_key, market_data in all_market_data.items():
            active_positions = \
                self.position_manager.get_positions_for_asset(asset_key)
            contract_size = self.config.ASSETS[asset_key]['lot_size']

            for position_id, position in list(active_positions.items()):
                # The touches this position would CLOSE at, never the
                # mids — see positions.update_position_pnl.
                mark_spot, mark_fut = marketdata.closing_prices(
                    market_data, position.signal_type)
                self.position_manager.update_position_pnl(
                    position_id,
                    mark_spot,
                    mark_fut,
                    market_data['basis_pct'],
                    contract_size=contract_size,
                )

                needs_action, action_type = \
                    self.risk_manager.check_position_risk(
                        position, market_data['basis_pct'])
                if needs_action:
                    if self.position_manager.close_position(
                            position_id, action_type, self.order_manager):
                        self.performance_tracker.update_with_closed_position(
                            position)

            signal = self.signal_generator.generate_signal(
                asset_key, market_data, active_positions)
            self.last_signals[asset_key] = signal
            self.data_logger.log_market_data(asset_key, market_data, signal)

            if signal in (SignalType.SELL_BASIS, SignalType.BUY_BASIS):
                self.execute_entry_signal(asset_key, signal, market_data)
            elif isinstance(signal, tuple) and len(signal) == 2:
                position_id, _close_signal = signal
                position = self.position_manager.positions.get(position_id)
                if position and self.position_manager.close_position(
                        position_id, "SIGNAL_EXIT", self.order_manager):
                    self.performance_tracker.update_with_closed_position(
                        position)

    def execute_entry_signal(self, asset_key, signal_type, market_data):
        lot_size = self.config.RISK_LIMITS['MAX_LOT_SIZE']

        valid, reason = self.risk_manager.validate_new_position(
            asset_key, signal_type, lot_size, self.position_manager)
        if not valid:
            logging.info("Signal rejected for %s: %s", asset_key, reason)
            return

        asset = self.active_assets[asset_key]

        if self.trading_mode == "LIVE":
            success, spot_trade, futures_trade = \
                self.order_manager.execute_trade_pair(
                    asset_key, signal_type, lot_size,
                    asset['spot_symbol'], asset['futures_symbol'])
            if success:
                position = self.position_manager.create_position(
                    asset_key, signal_type, spot_trade, futures_trade,
                    market_data['basis_pct'])
                self.risk_manager.record_trade(asset_key)
                logging.info("Position opened: %s - %s %s",
                             position.position_id, asset_key,
                             signal_type.value)
            else:
                logging.error("Failed to execute trades for %s %s",
                              asset_key, signal_type.value)
        else:
            logging.info("PAPER TRADE: %s %s at premium %.2f%%",
                         asset_key, signal_type.value,
                         market_data['basis_pct'])
            self.risk_manager.record_trade(asset_key)

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def print_trading_display(self, all_market_data):
        self.update_counter += 1
        if self.update_counter == 1 or self.update_counter % 50 == 0:
            print("\033[H\033[2J", end="", flush=True)
        else:
            print("\033[H", end="", flush=True)

        session = self.get_current_session()
        now = datetime.now().strftime('%H:%M:%S')

        print("ALGORITHMIC BASIS TRADING SYSTEM - GOLD & SILVER")
        print("=" * 84)
        print(f"Session: {session.value:<12} | Time: {now} | "
              f"Mode: {self.trading_mode:<5} | Account: "
              f"{self.broker.account.name}")
        print("=" * 84)
        print()

        for asset_key in ['GOLD', 'SILVER']:
            if asset_key in self.active_assets and asset_key in all_market_data:
                self.print_asset_trading_data(asset_key,
                                              all_market_data[asset_key])

        self.print_trading_status()
        sys.stdout.flush()

    def print_asset_trading_data(self, asset_key, md):
        print(f"{md['asset_name']}")
        print("=" * 84)
        print(f"SPOT       | {md['spot_price']:>8.2f} | "
              f"Bid: {md['spot_bid']:>10.4f} | Ask: {md['spot_ask']:>10.4f} | "
              f"Spr: {md['spot_spread']:>6.1f}{md['spread_unit']}")
        print(f"FUTURES    | {md['futures_price']:>8.2f} | "
              f"Bid: {md['futures_bid']:>10.4f} | "
              f"Ask: {md['futures_ask']:>10.4f} | "
              f"Spr: {md['futures_spread']:>6.1f}{md['spread_unit']}")
        print(f"Actual Basis    | {md['actual_basis']:>8.2f} | "
              f"Market Pricing | Days to Expiry: {md['days_to_expiry']:>4.0f}")
        print(f"Spread          | {md['spread']:>+8.2f} | "
              f"{md['spread_formula']}")

        signal = self.last_signals.get(asset_key, SignalType.NO_SIGNAL)
        signal_str = signal.value if hasattr(signal, 'value') else str(signal)
        status = ("EXPENSIVE" if md['spread'] > 5
                  else "CHEAP" if md['spread'] < -5 else "FAIR")
        print(f"Basis           | {md['basis_pct']:>+7.2f}% | "
              f"Status: {status:>9} | Signal: {signal_str}")

        active = self.position_manager.get_positions_for_asset(asset_key)
        if active:
            print("ACTIVE POSITIONS:")
            for pid, pos in active.items():
                age = datetime.now() - pos.entry_time
                age_str = f"{age.seconds // 3600}h {(age.seconds % 3600) // 60}m"
                print(f"  {pid}: {pos.signal_type.value} | "
                      f"Entry: {pos.entry_premium:+.1f}% | "
                      f"Current: {pos.current_premium:+.1f}% | "
                      f"P&L: ${pos.unrealized_pnl:>+8.0f} | Age: {age_str}")
        else:
            print("No active positions")
        print()

    def print_trading_status(self):
        m = self.performance_tracker.get_metrics()
        active = self.position_manager.get_active_positions()
        max_pos = (self.config.RISK_LIMITS['MAX_POSITIONS_PER_ASSET']
                   * max(len(self.active_assets), 1))

        print("TRADING STATUS")
        print("=" * 84)
        print(f"Positions: {len(active)}/{max_pos} | "
              f"Daily P&L: ${m['daily_pnl']:>+8.0f} | "
              f"Total P&L: ${m['total_pnl']:>+8.0f}")
        print(f"Daily Trades: {m['daily_trades']}/"
              f"{self.config.RISK_LIMITS['MAX_DAILY_TRADES']} | "
              f"Win Rate: {m['win_rate']:>5.1f}% | "
              f"Max Drawdown: ${m['max_drawdown']:>8.0f}")

        risk_status = "NORMAL"
        if len(active) >= max_pos:
            risk_status = "MAX_POSITIONS"
        elif (m['daily_trades'] >=
              self.config.RISK_LIMITS['MAX_DAILY_TRADES'] * 0.8):
            risk_status = "HIGH_FREQUENCY"
        print(f"Risk Status: {risk_status} | Mode: {self.trading_mode} | "
              f"Updated: {datetime.now().strftime('%H:%M:%S')}")
        print("=" * 84)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def trading_loop(self):
        logging.info("Starting trading loop...")
        consecutive_errors = 0
        max_consecutive_errors = 10
        loop_count = 0
        last_successful_time = datetime.now()

        while self.is_running:
            try:
                loop_start = time.time()
                loop_count += 1

                if loop_count % 100 == 0 and not self.broker.is_alive():
                    logging.warning("MT5 connection lost, reconnecting...")
                    self.broker.shutdown()
                    if not self.broker.initialize():
                        logging.error("Failed to reconnect to MT5")
                        consecutive_errors += 1
                        time.sleep(5)
                        continue

                all_market_data = self.get_all_market_data()

                if all_market_data:
                    consecutive_errors = 0
                    last_successful_time = datetime.now()
                    self.last_market_data = all_market_data
                    self.process_trading_signals(all_market_data)
                    self.print_trading_display(all_market_data)
                else:
                    consecutive_errors += 1
                    stale_for = (datetime.now()
                                 - last_successful_time).total_seconds()
                    if stale_for > 30:
                        print("\033[H\033[2J", end="", flush=True)
                        print("=" * 84)
                        print("CONNECTION ISSUE DETECTED")
                        print(f"No market data for {stale_for:.0f}s | "
                              f"errors: {consecutive_errors}")
                        print("=" * 84)
                        sys.stdout.flush()

                if consecutive_errors > max_consecutive_errors:
                    logging.error("Too many consecutive errors: %s",
                                  consecutive_errors)
                    self.broker.shutdown()
                    if self.initialize_broker():
                        consecutive_errors = 0
                        time.sleep(2)
                    else:
                        time.sleep(10)

                loop_duration = time.time() - loop_start
                time.sleep(max(0.5 - loop_duration, 0.1))

                if loop_count % 1000 == 0:
                    logging.info("Trading running: %s loops", loop_count)

            except KeyboardInterrupt:
                logging.info("Trading stopped by user")
                break
            except Exception as e:
                logging.error("Error in trading loop: %s", e)
                consecutive_errors += 1
                time.sleep(2)

        logging.info("Trading loop stopped")

    def stop(self):
        logging.info("Stopping trading system...")
        self.is_running = False

        active = self.position_manager.get_active_positions()
        if active and self.trading_mode == "LIVE":
            print("Closing active positions...")
            for position_id in list(active):
                self.position_manager.close_position(
                    position_id, "SYSTEM_SHUTDOWN", self.order_manager)

        self.broker.shutdown()

        m = self.performance_tracker.get_metrics()
        print("\nFINAL TRADING SUMMARY")
        print("=" * 84)
        print(f"Total P&L: ${m['total_pnl']:>+8.0f} | "
              f"Trades: {m['total_trades']} | "
              f"Win Rate: {m['win_rate']:>5.1f}% | "
              f"Max Drawdown: ${m['max_drawdown']:>8.0f}")
        print("=" * 84)
