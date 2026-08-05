"""Coordinator: fuses prices from both legs' accounts, runs the
z-score strategy, and routes each leg's orders to its own account.

Topology comes from config:
- spot and futures on DIFFERENT accounts → each account runs a leg
  runner process (endpoint in config); coordinator talks TCP to both.
- both legs on the SAME account → coordinator connects in-process.

    python run_coordinator.py --config config.json --mode paper
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from types import SimpleNamespace

from .broker import BrokerSession
from .config import AlgoTradingConfig
from .database import DataLogger
from .exits import ExitLadder, outcome_tag
from .legs import LocalLeg, RemoteLeg
from .marketdata import compute_market_data
from .models import Position, SignalType, Trade, OrderSide
from .notify import TelegramNotifier
from .pair_executor import PairExecutor
from .performance import PerformanceTracker
from .positions import PositionManager
from .reconcile import Reconciler
from .risk import RiskManager
from .signals import SignalGenerator, ZSignalGenerator
from .spread import SpreadStats


class PaperExecutor:
    """Simulated fills at the current touch — the whole position
    lifecycle (entries, exit ladder, reviews, breakers) runs
    identically to LIVE, no orders leave the machine."""

    def __init__(self, spot_leg, futures_leg):
        self.spot_leg = spot_leg
        self.futures_leg = futures_leg

    def _fill(self, leg, symbol, side):
        tick = leg.tick(symbol)
        if not tick:
            return None
        return tick['ask'] if side is OrderSide.BUY else tick['bid']

    def execute_trade_pair(self, asset, signal_type, lot_size,
                           spot_symbol, futures_symbol):
        if signal_type == SignalType.SELL_BASIS:
            spot_side, fut_side = OrderSide.BUY, OrderSide.SELL
        elif signal_type == SignalType.BUY_BASIS:
            spot_side, fut_side = OrderSide.SELL, OrderSide.BUY
        else:
            raise ValueError(f"Invalid signal type: {signal_type}")

        spot_trade = Trade(spot_symbol, spot_side, lot_size)
        fut_trade = Trade(futures_symbol, fut_side, lot_size)
        spot_trade.executed_price = self._fill(self.spot_leg, spot_symbol,
                                               spot_side)
        fut_trade.executed_price = self._fill(self.futures_leg,
                                              futures_symbol, fut_side)
        if spot_trade.executed_price is None \
                or fut_trade.executed_price is None:
            spot_trade.status = fut_trade.status = "ERROR"
            return False, spot_trade, fut_trade
        spot_trade.status = fut_trade.status = "EXECUTED"
        logging.info("PAPER pair: %s %s — spot %.2f @ %.2f, fut %.2f @ %.2f",
                     asset, signal_type.value, lot_size,
                     spot_trade.executed_price, lot_size,
                     fut_trade.executed_price)
        return True, spot_trade, fut_trade

    def execute_close_pair(self, position, reason=None):
        close_spot = Trade(position.spot_trade.symbol,
                           position.spot_trade.side.opposite,
                           position.spot_trade.lot_size)
        close_fut = Trade(position.futures_trade.symbol,
                          position.futures_trade.side.opposite,
                          position.futures_trade.lot_size)
        close_spot.executed_price = self._fill(
            self.spot_leg, close_spot.symbol, close_spot.side)
        close_fut.executed_price = self._fill(
            self.futures_leg, close_fut.symbol, close_fut.side)
        ok = close_spot.executed_price is not None \
            and close_fut.executed_price is not None
        close_spot.status = close_fut.status = "EXECUTED" if ok else "ERROR"
        return ok, close_spot, close_fut


class Coordinator:
    def __init__(self, config, trading_mode="PAPER", config_path=None):
        self.config = config
        self.trading_mode = trading_mode
        self.is_running = False

        # Web-UI bridge: settings hot-reload + start/stop + manual close
        self.config_path = config_path
        self._config_mtime = (os.path.getmtime(config_path)
                              if config_path and os.path.exists(config_path)
                              else 0)
        self.control_path = 'control.json'
        self._control_mtime = 0
        self._last_close_ts = 0
        self.algo_enabled = True       # entries only; exits ALWAYS run

        self.spot_leg, self.futures_leg = self._resolve_legs()

        self.data_logger = DataLogger()
        if trading_mode == "LIVE":
            self.executor = PairExecutor(config, self.spot_leg,
                                         self.futures_leg)
        else:
            self.executor = PaperExecutor(self.spot_leg, self.futures_leg)
        self.position_manager = PositionManager(self.data_logger)
        self.risk_manager = RiskManager(config)
        self.performance_tracker = PerformanceTracker()
        self.exit_ladder = ExitLadder(config)

        self.use_z = config.SIGNALS.get('USE_Z_SIGNALS', True)
        self.z_gen = ZSignalGenerator(config, clock=time.time)
        self.legacy_gen = SignalGenerator(config)
        self.stats = {}                # asset -> SpreadStats
        self.reconciler = None         # built in start()

        self.notifier = TelegramNotifier(config)
        self.notifier.command_handler = self._telegram_command
        self._was_halted = False
        self.status_path = 'runtime_status.json'

        self.active_assets = {}
        self.last_signals = {}
        self.last_data = {}

        logging.info("Coordinator initialized: spot on [%s], futures on "
                     "[%s], mode %s, signals %s", self.spot_leg.name,
                     self.futures_leg.name, trading_mode,
                     'z-score' if self.use_z else 'fixed premium')

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

    def _each_leg(self):
        seen = {}
        for leg in (self.spot_leg, self.futures_leg):
            seen[id(leg)] = leg
        return seen.values()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def start(self):
        for leg in self._each_leg():
            if not leg.connect():
                logging.error("Could not connect leg '%s'", leg.name)
                return False
            account = leg.account_info()
            if account:
                logging.info("Leg '%s': login %s @ %s, equity %s",
                             leg.name, account.get('login'),
                             account.get('server'), account.get('equity'))

        self.config.validate_expiries()
        if not self._setup_symbols():
            return False

        for asset_key in self.active_assets:
            self.stats[asset_key] = SpreadStats(self.config.SIGNALS,
                                                clock=time.time)

        self.reconciler = Reconciler(
            self.config, self.position_manager, self.data_logger,
            self.risk_manager,
            {'spot': self.spot_leg, 'futures': self.futures_leg},
            clock=time.time)

        if self.trading_mode == "LIVE":
            self._recover_positions()
            self.reconciler.check()   # reconcile BEFORE acting

        self.notifier.notify_startup(
            self.trading_mode, self.spot_leg.name, self.futures_leg.name,
            list(self.active_assets))
        return True

    def _recover_positions(self):
        """Rebuild open positions from the DB after a restart."""
        for state in self.data_logger.load_open_position_states():
            try:
                position = Position.from_dict(state)
            except (KeyError, ValueError) as e:
                logging.error("Could not recover position state: %s", e)
                continue
            self.position_manager.restore_position(position)

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

    def process_asset(self, asset_key, market_data):
        contract_size = self.config.ASSETS[asset_key]['lot_size']
        stats = self.stats.get(asset_key)
        z = None
        if stats is not None:
            stats.update(market_data['swap_diff'])
            z = stats.z
            self.z_gen.update(asset_key, z)

        active = self.position_manager.get_positions_for_asset(asset_key)

        # -- exits first (risk before opportunity) --
        z_home = (z is not None
                  and abs(z) <= self.config.SIGNALS['EXIT_Z'])
        for position_id, position in list(active.items()):
            if z_home:
                position.z_reverted = True   # for the outcome tag
            self.position_manager.update_position_pnl(
                position_id, market_data['spot_price'],
                market_data['futures_price'],
                market_data['swap_premium_pct'],
                contract_size=contract_size)

            reason = self._exit_reason(position, z, market_data)
            if reason:
                self._close(position_id, position, reason, contract_size, z)

        # -- entries (only while the algo is enabled; exits above
        # always run — stopping the algo never abandons a position) --
        if not self.algo_enabled:
            self.last_signals[asset_key] = SignalType.NO_SIGNAL
            self.data_logger.log_market_data(asset_key, market_data,
                                             SignalType.NO_SIGNAL, z=z)
            return
        active = self.position_manager.get_positions_for_asset(asset_key)
        signal = self._entry_signal(asset_key, stats, market_data, active,
                                    contract_size)
        self.last_signals[asset_key] = signal or SignalType.NO_SIGNAL
        self.data_logger.log_market_data(asset_key, market_data,
                                         self.last_signals[asset_key], z=z)
        if signal:
            self._enter(asset_key, signal, market_data, stats, contract_size)

    def _exit_reason(self, position, z, market_data):
        if self.use_z and position.exit_plan:
            age = (datetime.now() - position.entry_time).total_seconds()
            return self.exit_ladder.evaluate(
                position, position.exit_plan, z,
                position.unrealized_pnl, age,
                spread=market_data.get('swap_diff'))
        # Legacy premium-based paths
        hit, action = self.risk_manager.check_position_risk(
            position, market_data['swap_premium_pct'])
        if hit:
            return action
        signal = self.legacy_gen.generate_signal(
            position.asset, market_data,
            {position.position_id: position})
        if isinstance(signal, tuple):
            return "SIGNAL_EXIT"
        return None

    def _entry_signal(self, asset_key, stats, market_data, active,
                      contract_size):
        clip = self._clip_lots()
        if self.use_z:
            if stats is None:
                return None
            return self.z_gen.entry_signal(asset_key, stats, market_data,
                                           active, clip, contract_size)
        signal = self.legacy_gen.generate_signal(asset_key, market_data,
                                                 active)
        return signal if signal in (SignalType.SELL_BASIS,
                                    SignalType.BUY_BASIS) else None

    def _clip_lots(self):
        return self.config.TRADING.get('CLIP_LOTS', 1.0) \
            * self.risk_manager.size_multiplier()

    def _enter(self, asset_key, signal_type, market_data, stats,
               contract_size):
        clip = self._clip_lots()

        valid, reason = self.risk_manager.validate_new_position(
            asset_key, signal_type, clip, self.position_manager)
        if not valid:
            logging.info("Entry rejected for %s: %s", asset_key, reason)
            return

        # Exit plan is computed BEFORE entering — a trade whose cost
        # floor exceeds plausible reversion is refused outright
        plan = None
        if self.use_z and stats is not None:
            plan = self.exit_ladder.build_plan(
                clip, contract_size, stats.z, stats.sigma,
                stats.half_life_sec, market_data)
            if plan is None:
                return

        asset = self.active_assets[asset_key]
        success, spot_trade, futures_trade = \
            self.executor.execute_trade_pair(
                asset_key, signal_type, clip,
                asset['spot_symbol'], asset['futures_symbol'])
        if not success:
            logging.error("Pair entry failed for %s %s", asset_key,
                          signal_type.value)
            return

        position = self.position_manager.create_position(
            asset_key, signal_type, spot_trade, futures_trade,
            market_data['swap_premium_pct'])
        if plan and spot_trade.lot_size and clip:
            # Rescale dollar levels if we filled less than the clip
            scale = spot_trade.lot_size / clip
            for key in ('tp_usd', 'stop_usd', 'rt_cost_usd'):
                if plan.get(key):
                    plan[key] *= scale
            # Freeze the display/exit anchors: entry spread, the mean
            # at entry (for spread-mode exits), and the BE/EX/TP/SL
            # SPREAD levels for the in-position card
            plan['entry_mu'] = stats.mu if stats else None
            plan['entry_spread'] = market_data['swap_diff']
            plan['levels'] = self.exit_ladder.spread_levels(
                plan, market_data['swap_diff'],
                spot_trade.lot_size * contract_size, signal_type)
        position.exit_plan = plan
        self.data_logger.save_position_state(position)
        self.risk_manager.record_trade(asset_key, lots=spot_trade.lot_size)
        self.notifier.notify_trade_opened(
            position, market_data, z=stats.z if stats else None)
        logging.info("Position opened: %s (%.2f lots, %s)",
                     position.position_id, spot_trade.lot_size,
                     self.trading_mode)

    def _close(self, position_id, position, reason, contract_size, z):
        contract = contract_size
        closed = self.position_manager.close_position(
            position_id, reason, self.executor, contract_size=contract)
        if not closed:
            return
        self.performance_tracker.update_with_closed_position(position)
        self.risk_manager.on_position_closed(position.realized_pnl)
        self.z_gen.notify_close(position.asset, reason,
                                position.signal_type)
        tag = outcome_tag(reason, position.z_reverted)
        self.data_logger.log_trade_review(position, exit_z=z, outcome=tag)
        self.notifier.notify_trade_closed(position, exit_z=z, outcome=tag)

        halted, why = self.risk_manager.halted()
        if halted and not self._was_halted:
            self.notifier.notify_breaker(why)
        self._was_halted = halted

        extremes = ""
        if position.peak_pnl is not None:
            extremes = (f" | peak/trough ${position.peak_pnl:+,.2f} "
                        f"({position.peak_min:.0f}m) / "
                        f"${position.trough_pnl:+,.2f} "
                        f"({position.trough_min:.0f}m)")
        logging.info("Closed %s: %s [%s] — realized $%.2f%s "
                     "(streak %d, day $%.0f)",
                     position_id, reason, tag, position.realized_pnl,
                     extremes, self.risk_manager.consecutive_losses,
                     self.risk_manager.daily_realized_pnl)

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def _position_snapshot(self):
        rows = []
        for position in self.position_manager.get_active_positions().values():
            age = datetime.now() - position.entry_time
            plan = position.exit_plan or {}
            rows.append({
                'position_id': position.position_id,
                'asset': position.asset,
                'signal_type': position.signal_type.value,
                'lots': position.spot_trade.lot_size,
                'entry_premium': position.entry_premium,
                'unrealized_pnl': position.unrealized_pnl,
                'net_pnl': (position.unrealized_pnl
                            - plan.get('rt_cost_usd', 0.0)),
                'age': f"{age.total_seconds() / 3600:.1f}h",
                'age_sec': age.total_seconds(),
                'entry_spot': position.spot_trade.executed_price,
                'entry_fut': position.futures_trade.executed_price,
                'entry_z': plan.get('entry_z'),
                'tp_usd': plan.get('tp_usd'),
                'stop_usd': plan.get('stop_usd'),
                'rt_cost_usd': plan.get('rt_cost_usd'),
                'max_hold_sec': plan.get('max_hold_sec'),
                'levels': plan.get('levels'),
                'peak_pnl': position.peak_pnl,
                'trough_pnl': position.trough_pnl,
            })
        return rows

    # ------------------------------------------------------------------
    # Web-UI bridge
    # ------------------------------------------------------------------

    def _maybe_reload_config(self):
        """Hot-apply settings saved by the web UI (config.json mtime)."""
        if not self.config_path:
            return
        try:
            mtime = os.path.getmtime(self.config_path)
        except OSError:
            return
        if mtime == self._config_mtime:
            return
        self._config_mtime = mtime
        try:
            fresh = AlgoTradingConfig.from_file(self.config_path)
        except (ValueError, OSError, KeyError) as e:
            logging.error("Config reload failed (keeping current): %s", e)
            return
        positions_open = bool(self.position_manager.get_active_positions())
        self.config.hot_apply(fresh, positions_open=positions_open)

    def _read_control(self):
        """control.json: {'algo_enabled': bool, 'close': {'position_id',
        'ts'}} written by the web UI."""
        try:
            mtime = os.path.getmtime(self.control_path)
        except OSError:
            return
        if mtime == self._control_mtime:
            return
        self._control_mtime = mtime
        try:
            with open(self.control_path, 'r', encoding='utf-8') as f:
                control = json.load(f)
        except (OSError, ValueError):
            return

        enabled = bool(control.get('algo_enabled', True))
        if enabled != self.algo_enabled:
            self.algo_enabled = enabled
            logging.warning("Algo %s via web UI (exits keep running)",
                            "ENABLED" if enabled else "DISABLED")

        close = control.get('close') or {}
        ts = close.get('ts', 0)
        if close.get('position_id') and ts > self._last_close_ts:
            self._last_close_ts = ts
            position_id = close['position_id']
            position = self.position_manager.positions.get(position_id)
            if position:
                logging.warning("MANUAL CLOSE requested via web UI: %s",
                                position_id)
                contract = self.config.ASSETS.get(
                    position.asset, {}).get('lot_size', 1.0)
                self._close(position_id, position, "MANUAL_CLOSE",
                            contract, None)

    def _write_runtime_status(self, all_market_data):
        """Refresh runtime_status.json for the read-only dashboard.
        Atomic replace so the dashboard never reads a half-written file."""
        halted, why = self.risk_manager.halted()
        target = self.config.TRADING.get('DAILY_LOT_TARGET', 0)
        payload = {
            'mode': self.trading_mode,
            'updated': datetime.now().strftime('%H:%M:%S'),
            'algo_enabled': self.algo_enabled,
            'halted': halted,
            'halt_reason': why,
            'daily_pnl': self.risk_manager.daily_realized_pnl,
            'assets': [{
                'asset': asset_key,
                'z': (self.stats[asset_key].z
                      if asset_key in self.stats else None),
                'basis': md['actual_basis'],
                'lots_today': self.risk_manager.lots_traded_today(asset_key),
                'lot_target': target,
            } for asset_key, md in all_market_data.items()],
            'positions': self._position_snapshot(),
        }
        try:
            tmp = self.status_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
            os.replace(tmp, self.status_path)
        except OSError as e:
            logging.debug("Could not write runtime status: %s", e)

    def _telegram_command(self, command):
        if command == '/status':
            halted, why = self.risk_manager.halted()
            lines = [f"Mode: {self.trading_mode}"
                     + (f" | HALTED: {why}" if halted else "")]
            for asset_key, md in self.last_data.items():
                stats = self.stats.get(asset_key)
                z = stats.z if stats else None
                lines.append(
                    f"{asset_key}: basis {md['actual_basis']:.2f}, "
                    f"z {'warm-up' if z is None else f'{z:+.2f}'}, "
                    f"{self.risk_manager.lots_traded_today(asset_key):.0f} "
                    f"lots today")
            return "\n".join(lines)
        if command == '/positions':
            rows = self._position_snapshot()
            if not rows:
                return "No open positions"
            return "\n".join(
                f"{r['position_id']} {r['asset']} {r['signal_type']} "
                f"{r['lots']:.0f} lots, P&L ${r['unrealized_pnl']:,.0f}, "
                f"age {r['age']}" for r in rows)
        if command == '/pnl':
            m = self.performance_tracker.get_metrics()
            return (f"Day: ${self.risk_manager.daily_realized_pnl:,.0f} | "
                    f"Total: ${m['total_pnl']:,.0f} | "
                    f"Trades: {m['total_trades']} | "
                    f"Win rate: {m['win_rate']:.1f}%")
        return None

    def log_status(self, all_market_data):
        self._write_runtime_status(all_market_data)
        target = self.config.TRADING.get('DAILY_LOT_TARGET', 0)
        halted, why = self.risk_manager.halted()
        for asset_key, md in all_market_data.items():
            stats = self.stats.get(asset_key)
            z = stats.z if stats else None
            done = self.risk_manager.lots_traded_today(asset_key)
            progress = (f" | today {done:.0f}/{target:.0f} lots"
                        if target else "")
            state = f" | HALTED: {why}" if halted else ""
            z_str = f"{z:+.2f}" if z is not None else "warm-up"
            logging.info(
                "%s spot %.2f | fut %.2f | swap_diff %+.2f | z %s | "
                "%s%s%s",
                asset_key, md['spot_price'], md['futures_price'],
                md['swap_diff'], z_str,
                getattr(self.last_signals.get(asset_key), 'value',
                        'NO_SIGNAL'), progress, state)

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
                    for asset_key, md in all_market_data.items():
                        self.process_asset(asset_key, md)
                    if loop_count % 20 == 1:
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

                self._read_control()
                if loop_count % 20 == 0:      # ~10s
                    self._maybe_reload_config()

                if self.reconciler and self.trading_mode == "LIVE" \
                        and self.reconciler.due():
                    for action, leg_name, detail in self.reconciler.check():
                        self.notifier.notify_reconcile(action, leg_name,
                                                       str(detail))

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
        for leg in self._each_leg():
            if not leg.ping():
                leg.close()
                leg.connect()

    def stop(self):
        self.is_running = False
        active = self.position_manager.get_active_positions()
        if active and self.trading_mode == "LIVE":
            logging.info("Closing %d active positions on shutdown",
                         len(active))
            for position_id, position in list(active.items()):
                contract = self.config.ASSETS.get(
                    position.asset, {}).get('lot_size', 1.0)
                self._close(position_id, position, "SYSTEM_SHUTDOWN",
                            contract, None)

        for leg in self._each_leg():
            leg.close()

        m = self.performance_tracker.get_metrics()
        self.notifier.notify_shutdown(m)
        self.notifier.stop()
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

    coordinator = Coordinator(config, trading_mode=mode,
                              config_path=args.config)
    if not coordinator.start():
        print("Startup failed — are the leg runners started and logged in?")
        sys.exit(1)

    try:
        coordinator.run()
    except KeyboardInterrupt:
        coordinator.stop()


if __name__ == '__main__':
    main()
