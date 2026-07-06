"""Coordinator: fuses prices from both legs' accounts and routes each
leg's orders to its own account.

Topology comes from config:
- spot and futures on DIFFERENT accounts → each account runs a leg
  runner process (endpoint in config); the coordinator talks to both
  over localhost TCP. This is the only way to stream two MT5 accounts
  simultaneously (one MT5 connection per process).
- both legs on the SAME account → the coordinator connects to that
  one terminal in-process; no leg runners needed.

    python run_coordinator.py --config config.json --mode paper
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from types import SimpleNamespace

from .broker import BrokerSession
from .config import AlgoTradingConfig
from .database import DataLogger
from .legs import LocalLeg, RemoteLeg
from .marketdata import compute_market_data
from .models import SignalType
from .pair_executor import PairExecutor
from .performance import PerformanceTracker
from .positions import PositionManager
from .risk import RiskManager
from .signals import SignalGenerator


class Coordinator:
    def __init__(self, config, trading_mode="PAPER"):
        self.config = config
        self.trading_mode = trading_mode
        self.is_running = False

        self.spot_leg, self.futures_leg = self._resolve_legs()

        self.data_logger = DataLogger()
        self.pair_executor = PairExecutor(config, self.spot_leg,
                                          self.futures_leg)
        self.position_manager = PositionManager(self.data_logger)
        self.risk_manager = RiskManager(config)
        self.signal_generator = SignalGenerator(config)
        self.performance_tracker = PerformanceTracker()

        self.active_assets = {}
        self.last_signals = {}
        self.last_data = {}

        logging.info("Coordinator initialized: spot on [%s], futures on "
                     "[%s], mode %s", self.spot_leg.name,
                     self.futures_leg.name, trading_mode)

    def _resolve_legs(self):
        spot_name = self.config.leg_accounts.get('spot', 'default')
        fut_name = self.config.leg_accounts.get('futures', 'default')
        spot_acct = self.config.accounts[spot_name]
        fut_acct = self.config.accounts[fut_name]

        legs = {}
        for acct in (spot_acct, fut_acct):
            if acct.name in legs:
                continue
            if acct.endpoint:
                legs[acct.name] = RemoteLeg(acct.name, acct.endpoint)
            else:
                legs[acct.name] = LocalLeg(BrokerSession(acct))

        local_count = sum(1 for leg in legs.values()
                          if isinstance(leg, LocalLeg))
        if len(legs) > 1 and local_count > 1:
            raise ValueError(
                "Both accounts are configured without endpoints, but one "
                "process can hold only one MT5 connection. Give each "
                "account an endpoint (e.g. 127.0.0.1:9101 / 9102) and "
                "start a leg runner per account.")

        return legs[spot_acct.name], legs[fut_acct.name]

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def start(self):
        for leg in {id(self.spot_leg): self.spot_leg,
                    id(self.futures_leg): self.futures_leg}.values():
            if not leg.connect():
                logging.error("Could not connect leg '%s'", leg.name)
                return False
            account = leg.account_info()
            if account:
                logging.info("Leg '%s': login %s @ %s, equity %s",
                             leg.name, account.get('login'),
                             account.get('server'), account.get('equity'))

        self.config.validate_expiries()
        return self._setup_symbols()

    def _setup_symbols(self):
        for asset_key, asset_cfg in self.config.ASSETS.items():
            if not asset_cfg['enabled']:
                continue

            spot_symbol = next(
                (s for s in asset_cfg['spot_symbols']
                 if self.spot_leg.ensure_symbol(s).get('ok')), None)
            futures_symbol = next(
                (s for s in asset_cfg['futures_symbols']
                 if self.futures_leg.ensure_symbol(s).get('ok')), None)

            if spot_symbol and futures_symbol:
                self.active_assets[asset_key] = {
                    'config': asset_cfg,
                    'spot_symbol': spot_symbol,
                    'futures_symbol': futures_symbol,
                    'last_data': None,
                }
                logging.info("%s: %s [%s] + %s [%s]", asset_key,
                             spot_symbol, self.spot_leg.name,
                             futures_symbol, self.futures_leg.name)
            else:
                logging.warning("%s: missing symbols — spot: %s, futures: %s",
                                asset_key, spot_symbol, futures_symbol)

        return len(self.active_assets) > 0

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_market_data(self, asset_key):
        asset = self.active_assets[asset_key]
        spot_tick = self.spot_leg.tick(asset['spot_symbol'])
        futures_tick = self.futures_leg.tick(asset['futures_symbol'])
        if not spot_tick or not futures_tick:
            return None
        try:
            return compute_market_data(
                asset['config'],
                SimpleNamespace(**spot_tick),
                SimpleNamespace(**futures_tick))
        except Exception as e:
            logging.error("Market data error for %s: %s", asset_key, e)
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

    def process_signals(self, all_market_data):
        for asset_key, market_data in all_market_data.items():
            active = self.position_manager.get_positions_for_asset(asset_key)
            contract_size = self.config.ASSETS[asset_key]['lot_size']

            for position_id, position in list(active.items()):
                self.position_manager.update_position_pnl(
                    position_id, market_data['spot_price'],
                    market_data['futures_price'],
                    market_data['swap_premium_pct'],
                    contract_size=contract_size)

                hit, action = self.risk_manager.check_position_risk(
                    position, market_data['swap_premium_pct'])
                if hit and self.trading_mode == "LIVE":
                    if self.position_manager.close_position(
                            position_id, action, self.pair_executor):
                        self.performance_tracker.update_with_closed_position(
                            position)
                elif hit:
                    logging.info("PAPER: %s would trigger %s", position_id,
                                 action)

            signal = self.signal_generator.generate_signal(
                asset_key, market_data, active)
            self.last_signals[asset_key] = signal
            self.data_logger.log_market_data(asset_key, market_data, signal)

            if signal in (SignalType.SELL_BASIS, SignalType.BUY_BASIS):
                self._enter(asset_key, signal, market_data)
            elif isinstance(signal, tuple):
                position_id, _ = signal
                position = self.position_manager.positions.get(position_id)
                if self.trading_mode == "LIVE":
                    if position and self.position_manager.close_position(
                            position_id, "SIGNAL_EXIT", self.pair_executor):
                        self.performance_tracker.update_with_closed_position(
                            position)
                else:
                    logging.info("PAPER: would close %s (signal exit)",
                                 position_id)

    def _enter(self, asset_key, signal_type, market_data):
        clip = self.config.TRADING.get('CLIP_LOTS', 1.0)

        valid, reason = self.risk_manager.validate_new_position(
            asset_key, signal_type, clip, self.position_manager)
        if not valid:
            logging.info("Signal rejected for %s: %s", asset_key, reason)
            return

        asset = self.active_assets[asset_key]

        if self.trading_mode != "LIVE":
            logging.info("PAPER TRADE: %s %s %.0f lots at premium %.2f%%",
                         asset_key, signal_type.value, clip,
                         market_data['swap_premium_pct'])
            self.risk_manager.record_trade(asset_key, lots=clip)
            return

        success, spot_trade, futures_trade = \
            self.pair_executor.execute_trade_pair(
                asset_key, signal_type, clip,
                asset['spot_symbol'], asset['futures_symbol'])

        if success:
            position = self.position_manager.create_position(
                asset_key, signal_type, spot_trade, futures_trade,
                market_data['swap_premium_pct'])
            self.risk_manager.record_trade(asset_key,
                                           lots=spot_trade.lot_size)
            logging.info("Position opened: %s (%.2f lots)",
                         position.position_id, spot_trade.lot_size)
        else:
            logging.error("Pair entry failed for %s %s", asset_key,
                          signal_type.value)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def log_status(self, all_market_data):
        target = self.config.TRADING.get('DAILY_LOT_TARGET', 0)
        for asset_key, md in all_market_data.items():
            done = self.risk_manager.lots_traded_today(asset_key)
            progress = (f" | today {done:.0f}/{target:.0f} lots"
                        if target else "")
            signal = self.last_signals.get(asset_key, SignalType.NO_SIGNAL)
            signal_str = (signal.value if hasattr(signal, 'value')
                          else f"EXIT {signal[0]}")
            logging.info(
                "%s spot %.2f | fut %.2f | basis %.2f | swap %.2f | "
                "premium %+.2f%% | %s%s",
                asset_key, md['spot_price'], md['futures_price'],
                md['actual_basis'], md['swap_basis'],
                md['swap_premium_pct'], signal_str, progress)

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def run(self):
        self.is_running = True
        poll = self.config.TRADING.get('POLL_INTERVAL_SEC', 0.5)
        loop_count = 0
        consecutive_errors = 0

        logging.info("Coordinator loop started (poll %.2fs)", poll)
        while self.is_running:
            try:
                started = time.time()
                loop_count += 1

                all_market_data = self.get_all_market_data()
                if all_market_data:
                    consecutive_errors = 0
                    self.last_data = all_market_data
                    self.process_signals(all_market_data)
                    if loop_count % 20 == 1:   # status every ~10s
                        self.log_status(all_market_data)
                else:
                    consecutive_errors += 1
                    if consecutive_errors % 20 == 1:
                        logging.warning(
                            "No market data (%d consecutive) — legs alive? "
                            "spot=%s futures=%s", consecutive_errors,
                            self.spot_leg.ping(), self.futures_leg.ping())
                    if consecutive_errors >= 40:
                        self._reconnect_legs()
                        consecutive_errors = 0

                time.sleep(max(poll - (time.time() - started), 0.05))

            except KeyboardInterrupt:
                break
            except Exception as e:
                logging.error("Coordinator loop error: %s", e)
                consecutive_errors += 1
                time.sleep(2)

        self.stop()

    def _reconnect_legs(self):
        logging.warning("Reconnecting legs...")
        for leg in {id(self.spot_leg): self.spot_leg,
                    id(self.futures_leg): self.futures_leg}.values():
            if not leg.ping():
                leg.close()
                leg.connect()

    def stop(self):
        self.is_running = False
        active = self.position_manager.get_active_positions()
        if active and self.trading_mode == "LIVE":
            logging.info("Closing %d active positions on shutdown",
                         len(active))
            for position_id in list(active):
                self.position_manager.close_position(
                    position_id, "SYSTEM_SHUTDOWN", self.pair_executor)

        for leg in {id(self.spot_leg): self.spot_leg,
                    id(self.futures_leg): self.futures_leg}.values():
            leg.close()

        m = self.performance_tracker.get_metrics()
        logging.info("FINAL: P&L $%.0f | trades %d | win rate %.1f%% | "
                     "max drawdown $%.0f", m['total_pnl'],
                     m['total_trades'], m['win_rate'], m['max_drawdown'])


def main():
    parser = argparse.ArgumentParser(
        description="Basis trading coordinator (multi-account)")
    parser.add_argument('--config', required=True, help='Path to config JSON')
    parser.add_argument('--mode', choices=['paper', 'live'], default='paper')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - [coord] %(message)s',
        handlers=[
            logging.FileHandler('coordinator.log', encoding='utf-8'),
            logging.StreamHandler(),
        ],
    )

    config = AlgoTradingConfig.from_file(args.config)
    mode = args.mode.upper()

    if mode == "LIVE":
        print("WARNING: LIVE TRADING MODE - REAL MONEY AT RISK")
        clip = config.TRADING.get('CLIP_LOTS', 1.0)
        target = config.TRADING.get('DAILY_LOT_TARGET', 0)
        print(f"Clip size: {clip} lots/leg | Daily target: {target} lots")
        if input("Type 'START' to begin live trading: ").strip().upper() != "START":
            print("Cancelled")
            sys.exit(0)

    coordinator = Coordinator(config, trading_mode=mode)
    if not coordinator.start():
        print("Startup failed — are the leg runners started and logged in?")
        sys.exit(1)

    try:
        coordinator.run()
    except KeyboardInterrupt:
        coordinator.stop()


if __name__ == '__main__':
    main()
