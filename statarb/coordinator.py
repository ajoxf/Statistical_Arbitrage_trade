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
from .shadow import ShadowTracker
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
        self._last_open_ts = 0
        self._last_test_ts = 0
        self._test_results = None
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
        self.shadow = ShadowTracker(self.data_logger)
        self._last_z = {}          # asset -> z (for SD-touch detection)

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

        self._detect_sd_touches(asset_key, z, market_data['swap_diff'])
        self.shadow.update(asset_key, market_data['spot_price'],
                           market_data['futures_price'])

        active = self.position_manager.get_positions_for_asset(asset_key)

        # -- exits first (risk before opportunity) --
        z_home = (z is not None
                  and abs(z) <= self.config.SIGNALS['EXIT_Z'])
        for position_id, position in list(active.items()):
            if z_home:
                position.z_reverted = True   # for the outcome tag
            if z is not None:                # z path for the exit report
                position.z_min = z if position.z_min is None \
                    else min(position.z_min, z)
                position.z_max = z if position.z_max is None \
                    else max(position.z_max, z)
            self.position_manager.update_position_pnl(
                position_id, market_data['spot_price'],
                market_data['futures_price'],
                market_data['swap_premium_pct'],
                contract_size=contract_size)

            reason = self._exit_reason(position, z, market_data)
            if reason:
                self._close(position_id, position, reason, contract_size, z,
                            spread=market_data.get('swap_diff'))

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

    def _detect_sd_touches(self, asset_key, z, spread):
        """Record z crossings of integer sigma levels (SD-touch
        distribution — how often does the spread stretch to each
        level? Feeds threshold calibration)."""
        previous = self._last_z.get(asset_key)
        self._last_z[asset_key] = z
        if previous is None or z is None:
            return
        for level in (-3, -2, -1, 1, 2, 3):
            if previous < level <= z:
                self.data_logger.log_sd_touch(asset_key, level, 'UP',
                                              z, spread)
            elif previous > level >= z:
                self.data_logger.log_sd_touch(asset_key, level, 'DOWN',
                                              z, spread)

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

        self._open_position(asset_key, signal_type, clip, market_data,
                            stats, contract_size)

    def _open_position(self, asset_key, signal_type, lots, market_data,
                       stats, contract_size, manual=False):
        """Shared entry path for signal and manual trades: exit plan
        BEFORE orders (a trade whose cost floor exceeds plausible
        reversion is refused), execute, attach frozen levels."""
        plan = None
        if (self.use_z and stats is not None) or manual:
            warm = stats is not None and stats.warm
            plan = self.exit_ladder.build_plan(
                lots, contract_size,
                stats.z if warm else None,
                stats.sigma if warm else None,
                stats.half_life_sec if stats else None, market_data)
            if plan is None:
                return None
            if manual:
                plan['source'] = 'MANUAL'

        asset = self.active_assets[asset_key]
        success, spot_trade, futures_trade = \
            self.executor.execute_trade_pair(
                asset_key, signal_type, lots,
                asset['spot_symbol'], asset['futures_symbol'])
        if not success:
            logging.error("Pair entry failed for %s %s", asset_key,
                          signal_type.value)
            return None

        position = self.position_manager.create_position(
            asset_key, signal_type, spot_trade, futures_trade,
            market_data['swap_premium_pct'])
        if plan and spot_trade.lot_size and lots:
            # Rescale dollar levels if we filled less than requested
            scale = spot_trade.lot_size / lots
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
            position, market_data,
            z=stats.z if stats and stats.warm else None,
            contract_size=contract_size,
            is_paper=self.trading_mode != 'LIVE')
        logging.info("Position opened: %s (%.2f lots, %s%s)",
                     position.position_id, spot_trade.lot_size,
                     self.trading_mode, ", MANUAL" if manual else "")
        return position

    def _close(self, position_id, position, reason, contract_size, z,
               spread=None):
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
        notional = None
        if position.spot_trade.executed_price:
            notional = (position.spot_trade.executed_price
                        * position.spot_trade.lot_size * contract_size)
        self.data_logger.log_trade_review(position, exit_z=z, outcome=tag,
                                          exit_spread=spread,
                                          notional=notional)
        self.notifier.notify_trade_closed(
            position, exit_z=z, outcome=tag, exit_spread=spread,
            contract_size=contract_size,
            is_paper=self.trading_mode != 'LIVE')
        self.shadow.start(position, contract_size)

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

        open_cmd = control.get('open') or {}
        ts = open_cmd.get('ts', 0)
        if open_cmd.get('asset') and ts > self._last_open_ts:
            self._last_open_ts = ts
            self._manual_open(open_cmd['asset'],
                              open_cmd.get('direction', ''),
                              open_cmd.get('lots'))

        test_cmd = control.get('test') or {}
        ts = test_cmd.get('ts', 0)
        if test_cmd.get('kind') and ts > self._last_test_ts:
            self._last_test_ts = ts
            self._run_tests(test_cmd['kind'])

    # ------------------------------------------------------------------
    # MT5 connectivity & order tests (round trips, real orders)
    # ------------------------------------------------------------------

    def _run_tests(self, kind):
        """UI-triggered self-test. 'connectivity': ping/account/symbol/
        tick per leg. 'orders': ALSO places REAL minimum-volume orders —
        a far-away limit (place -> verify resting -> cancel -> verify
        gone) and a market round trip (open -> close by ticket).
        Order tests require algo OFF and a flat book."""
        results = []

        def add(leg_name, check, ok, detail=""):
            results.append({'leg': leg_name, 'check': check,
                            'ok': bool(ok), 'detail': str(detail)[:140]})
            logging.info("[TEST] [%s] %s: %s %s", leg_name, check,
                         'PASS' if ok else 'FAIL', detail)

        if kind == 'orders' and (self.algo_enabled or
                                 self.position_manager
                                 .get_active_positions()):
            add('-', 'preconditions', False,
                'Order tests need the algo STOPPED and a flat book')
            self._test_results = {'kind': kind, 'results': results,
                                  'ts': datetime.now().strftime('%H:%M:%S')}
            return

        role_map = [('spot', self.spot_leg, 'spot_symbol'),
                    ('futures', self.futures_leg, 'futures_symbol')]
        seen = set()
        for role, leg, symbol_key in role_map:
            if id(leg) in seen:
                continue
            seen.add(id(leg))
            add(leg.name, 'connection ping', leg.ping())
            info = leg.account_info()
            add(leg.name, 'account info', bool(info),
                info and f"login {info.get('login')} "
                         f"equity ${info.get('equity', 0):,.0f}")
            for r2, leg2, sym_key2 in role_map:
                if leg2 is not leg:
                    continue
                for asset_key, asset in self.active_assets.items():
                    symbol = asset[sym_key2]
                    meta = leg.ensure_symbol(symbol)
                    add(leg.name, f'symbol {symbol}', meta.get('ok'),
                        f"min {meta.get('volume_min')} "
                        f"step {meta.get('volume_step')}")
                    tick = leg.tick(symbol)
                    add(leg.name, f'tick {symbol}', bool(tick),
                        tick and f"bid {tick['bid']} ask {tick['ask']}")
                    if kind == 'orders' and meta.get('ok') and tick:
                        self._order_test(leg, symbol, meta, tick, add)

        self._test_results = {'kind': kind, 'results': results,
                              'ts': datetime.now().strftime('%H:%M:%S')}
        passed = sum(1 for r in results if r['ok'])
        self.notifier.notify_error(
            f"Self-test '{kind}': {passed}/{len(results)} checks passed") \
            if passed < len(results) else None

    def _order_test(self, leg, symbol, meta, tick, add):
        volume = meta.get('volume_min') or 0.01
        # 1. Resting limit far below the market: place -> verify -> cancel
        far_price = tick['bid'] * 0.98
        placed = leg.place_limit(symbol, 'BUY', volume, far_price,
                                 comment='ORDER_TEST')
        add(leg.name, f'limit place {symbol}', placed.get('ok'),
            placed.get('ticket') or placed.get('error'))
        if placed.get('ok'):
            state = leg.order_state(placed['ticket'])
            add(leg.name, f'limit resting {symbol}',
                state.get('still_open'), 'order visible as pending')
            cancelled = leg.cancel_order(placed['ticket'])
            add(leg.name, f'limit cancel {symbol}',
                cancelled.get('cancelled')
                and not cancelled.get('filled_volume'),
                'cancelled clean, no fills leaked')
        # 2. Market round trip at minimum volume: open -> close by ticket
        opened = leg.order(symbol, 'BUY', volume, comment='ORDER_TEST')
        add(leg.name, f'market open {symbol}', opened.get('ok'),
            f"filled {opened.get('filled_volume')} "
            f"@ {opened.get('price')}")
        if opened.get('ok'):
            tickets = opened.get('position_tickets') or []
            if tickets:
                closed = leg.close_ticket(
                    symbol, tickets[0],
                    opened.get('filled_volume') or volume, 'BUY',
                    comment='ORDER_TEST')
                add(leg.name, f'round trip close {symbol}',
                    closed.get('ok'),
                    f"closed by ticket {tickets[0]} "
                    f"@ {closed.get('price')}")
            else:
                reversed_order = leg.order(symbol, 'SELL',
                                           opened.get('filled_volume')
                                           or volume,
                                           comment='ORDER_TEST')
                add(leg.name, f'round trip close {symbol}',
                    reversed_order.get('ok'), 'netting-mode reverse')

    def _manual_open(self, asset_key, direction, lots=None):
        """Manual spread trade from the web UI: bypasses SIGNAL gates
        (z, trend, cooldowns, edge filter) but NEVER risk limits or
        circuit breakers. Exits are managed by the normal ladder."""
        if asset_key not in self.active_assets:
            logging.error("Manual trade rejected: unknown asset %s",
                          asset_key)
            return
        try:
            signal_type = SignalType(direction)
        except ValueError:
            logging.error("Manual trade rejected: bad direction %r",
                          direction)
            return
        if signal_type not in (SignalType.SELL_BASIS,
                               SignalType.BUY_BASIS):
            return

        halted, why = self.risk_manager.halted()
        if halted:
            logging.warning("Manual trade rejected: circuit breaker (%s)",
                            why)
            return
        lots = float(lots or self.config.TRADING.get('CLIP_LOTS', 1.0))
        if lots > self.config.RISK_LIMITS['MAX_LOT_SIZE']:
            logging.warning("Manual trade rejected: %s lots > MAX_LOT_SIZE",
                            lots)
            return
        active = self.position_manager.get_positions_for_asset(asset_key)
        if len(active) >= self.config.RISK_LIMITS['MAX_POSITIONS_PER_ASSET']:
            logging.warning("Manual trade rejected: max positions reached")
            return

        market_data = self.get_market_data(asset_key) \
            or self.active_assets[asset_key]['last_data']
        if not market_data:
            logging.error("Manual trade rejected: no market data")
            return
        contract_size = self.config.ASSETS[asset_key]['lot_size']
        stats = self.stats.get(asset_key)
        logging.warning("MANUAL SPREAD TRADE via web UI: %s %s %.2f lots",
                        asset_key, signal_type.value, lots)
        self._open_position(asset_key, signal_type, lots, market_data,
                            stats, contract_size, manual=True)

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
            'shadow': {'active': len(self.shadow.active),
                       'tracking': self.shadow.snapshot()},
            'test_results': self._test_results,
        }
        try:
            tmp = self.status_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
            os.replace(tmp, self.status_path)
        except OSError as e:
            logging.debug("Could not write runtime status: %s", e)

    # -- Telegram command handlers (W3 command set, MT5-adapted) --------

    def _telegram_command(self, command):
        parts = command.split()
        name, args = parts[0], parts[1:]
        handlers = {
            '/status': self._cmd_status, '/dashboard': self._cmd_dashboard,
            '/positions': self._cmd_positions, '/trades': self._cmd_trades,
            '/balance': self._cmd_balance, '/pnl': self._cmd_pnl,
            '/stats': self._cmd_stats, '/shadow': self._cmd_shadow,
            '/eod': self._cmd_eod, '/settings': self._cmd_settings,
            '/pause': self._cmd_pause, '/resume': self._cmd_resume,
            '/closeall': self._cmd_closeall,
        }
        if name == '/set':
            return self._cmd_set(args)
        handler = handlers.get(name)
        return handler() if handler else None

    def _cmd_status(self):
        halted, why = self.risk_manager.halted()
        lines = [f"Mode: <b>{self.trading_mode}</b> | Algo: "
                 f"{'ON' if self.algo_enabled else 'OFF'}"
                 + (f" | 🛑 HALTED: {why}" if halted else "")]
        for asset_key, md in self.last_data.items():
            stats = self.stats.get(asset_key)
            z = stats.z if stats else None
            lines.append(
                f"{asset_key}: basis {md['actual_basis']:.2f} | "
                f"swap_diff {md['swap_diff']:+.2f} | "
                f"z {'warm-up' if z is None else f'{z:+.2f}'} | "
                f"{self.risk_manager.lots_traded_today(asset_key):.0f} "
                f"lots today")
        return "\n".join(lines)

    def _cmd_dashboard(self):
        sections = [self._cmd_status(), "", self._cmd_positions(), "",
                    self._cmd_pnl()]
        shadow = len(self.shadow.active)
        if shadow:
            sections += ["", f"Shadow tracking: {shadow} live"]
        return "\n".join(sections)

    def _cmd_positions(self):
        rows = self._position_snapshot()
        if not rows:
            return "No open positions"
        out = []
        for r in rows:
            levels = r.get('levels') or {}
            line = (f"<b>{r['position_id']}</b> {r['asset']} "
                    f"{r['signal_type']} {r['lots']:.1f} lots\n"
                    f"  gross ${r['unrealized_pnl']:+,.0f} | "
                    f"net ${r['net_pnl']:+,.0f} | age {r['age']}")
            if levels:
                line += (f"\n  BE {levels.get('be', 0):+.3f} | "
                         f"TP {levels.get('tp') or 0:+.3f} | "
                         f"SL {levels.get('sl') or 0:+.3f}")
            if r.get('max_hold_sec'):
                line += (f"\n  hold {r['age_sec'] / 60:.0f}m of "
                         f"{r['max_hold_sec'] / 60:.0f}m max")
            out.append(line)
        return "\n".join(out)

    def _cmd_trades(self):
        rows = self.data_logger.recent_reviews(5)
        if not rows:
            return "No closed trades yet"
        out = ["<b>RECENT TRADES</b>"]
        for r in rows:
            pnl = r.get('realized_pnl') or 0
            out.append(
                f"{'🟢' if pnl >= 0 else '🔴'} {r['position_id']} "
                f"{r['asset']} ${pnl:+,.0f} | {r.get('exit_reason', '')} | "
                f"{r.get('outcome') or ''}\n"
                f"  z {r.get('entry_z') or 0:+.2f}→"
                f"{r.get('exit_z') or 0:+.2f} | "
                f"peak ${r.get('peak_pnl') or 0:+,.0f} "
                f"({r.get('peak_min') or 0:.0f}m)")
        return "\n".join(out)

    def _cmd_balance(self):
        out = ["<b>ACCOUNTS</b>"]
        for leg in self._each_leg():
            info = leg.account_info()
            if info:
                out.append(f"[{leg.name}] {info.get('server')} login "
                           f"{info.get('login')}\n"
                           f"  balance ${info.get('balance', 0):,.2f} | "
                           f"equity ${info.get('equity', 0):,.2f}")
            else:
                out.append(f"[{leg.name}] ⚠️ no account info")
        return "\n".join(out)

    def _cmd_pnl(self):
        m = self.performance_tracker.get_metrics()
        return (f"<b>P&L</b>\nDay: ${self.risk_manager.daily_realized_pnl:+,.0f}"
                f" | Total: ${m['total_pnl']:+,.0f}\n"
                f"Trades: {m['total_trades']} | Win rate: "
                f"{m['win_rate']:.1f}% | Max DD: ${m['max_drawdown']:,.0f}\n"
                f"Loss streak: {self.risk_manager.consecutive_losses}")

    def _cmd_stats(self):
        rows = self.data_logger.recent_reviews(200)
        pnls = [r['realized_pnl'] for r in rows
                if r.get('realized_pnl') is not None]
        if not pnls:
            return "No closed trades yet"
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        gross_loss = -sum(losses)
        rr = avg_win / abs(avg_loss) if avg_loss else 0
        peak = dd = run = 0.0
        for p in reversed(pnls):
            run += p
            peak = max(peak, run)
            dd = max(dd, peak - run)
        return (f"<b>EDGE STATS</b> ({len(pnls)} trades)\n"
                f"Win rate: {100 * len(wins) / len(pnls):.1f}% | "
                f"PF: {(sum(wins) / gross_loss) if gross_loss else 0:.2f}\n"
                f"Avg win ${avg_win:+,.0f} | avg loss ${avg_loss:+,.0f} | "
                f"R:R {rr:.2f}\n"
                f"Break-even WR: {100 / (1 + rr) if rr else 0:.1f}%\n"
                f"Expectancy: ${sum(pnls) / len(pnls):+,.0f}/trade | "
                f"Max DD: ${dd:,.0f}")

    def _cmd_shadow(self):
        rows = self.data_logger.recent_shadows(10)
        live = self.shadow.snapshot()
        out = [f"<b>WHAT-IF-HELD</b>  ({len(rows)} completed · "
               f"{len(live)} live)"]
        for s in live:
            out.append(f"⏳ {s['position_id']} exited as "
                       f"{s['exit_reason']}: now ${s['net'] or 0:+,.0f} "
                       f"({s['minutes']:.0f}m/{s['horizon_min']:.0f}m)")
        for r in rows[:5]:
            out.append(f"{r['position_id']} exited "
                       f"${r.get('exit_pnl') or 0:+,.0f} → held would be "
                       f"${r.get('what_if_net') or 0:+,.0f}: "
                       f"<b>{(r.get('verdict') or '').replace('_', ' ')}</b>")
        return "\n".join(out) if len(out) > 1 else "No shadows yet"

    def _cmd_eod(self):
        target = self.config.TRADING.get('DAILY_LOT_TARGET', 0)
        lines = ["<b>END-OF-DAY REPORT</b>", self._cmd_pnl(), ""]
        for asset_key in self.active_assets:
            done = self.risk_manager.lots_traded_today(asset_key)
            lines.append(f"{asset_key}: {done:.0f}"
                         + (f"/{target:.0f}" if target else "")
                         + " lots today")
        lines += ["", self._cmd_trades()]
        return "\n".join(lines)

    def _cmd_settings(self):
        out = ["<b>SETTINGS</b> (live values)"]
        for section in ('SIGNALS', 'EXITS', 'COSTS', 'TRADING'):
            values = getattr(self.config, section)
            out.append(f"<b>{section}</b>: " + ", ".join(
                f"{k}={v}" for k, v in values.items()))
        return "\n".join(out)

    def _cmd_set(self, args):
        if len(args) != 2:
            return "Usage: /set KEY value  (e.g. /set ENTRY_Z 2.5)"
        key, raw_value = args[0].upper(), args[1]
        for section in ('SIGNALS', 'EXITS', 'COSTS', 'TRADING',
                        'RISK_LIMITS', 'EXECUTION'):
            values = getattr(self.config, section)
            if key in values:
                if section == 'TRADING' and key == 'HEDGE_RATIO':
                    return "β is structural — change it in the web UI " \
                           "with a flat book + restart"
                old = values[key]
                try:
                    if isinstance(old, bool):
                        new = raw_value.lower() in ('1', 'true', 'on', 'yes')
                    elif isinstance(old, str):
                        new = raw_value
                    else:
                        new = float(raw_value)
                except ValueError:
                    return f"Bad value for {key}: {raw_value}"
                values[key] = new
                logging.warning("Telegram /set %s.%s: %s -> %s "
                                "(runtime only)", section, key, old, new)
                return (f"{section}.{key}: {old} → {new}\n"
                        f"⚠️ runtime only — save in the web UI to persist. "
                        f"Applies to the OPEN trade immediately.")
        return f"Unknown setting: {key}"

    def _cmd_pause(self):
        self.algo_enabled = False
        return "⏸ Entries PAUSED (exits keep running)"

    def _cmd_resume(self):
        self.algo_enabled = True
        return "▶️ Entries RESUMED"

    def _cmd_closeall(self):
        active = self.position_manager.get_active_positions()
        if not active:
            return "Nothing to close"
        count = 0
        for position_id, position in list(active.items()):
            contract = self.config.ASSETS.get(
                position.asset, {}).get('lot_size', 1.0)
            self._close(position_id, position, "MANUAL_CLOSE", contract,
                        None)
            count += 1
        return f"🚨 CLOSEALL: {count} position(s) sent to market close"

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
    parser.add_argument('--yes', action='store_true',
                        help='Skip the LIVE confirmation prompt (used by '
                             'the launcher; the UI mode toggle is consent)')
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
        if not args.yes and input(
                "Type 'START' to begin live trading: "
                ).strip().upper() != "START":
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
