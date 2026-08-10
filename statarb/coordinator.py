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
import math
import os
import sys
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from .broker import BrokerSession
from .config import AlgoTradingConfig
from .database import DataLogger
from .exits import ExitLadder, outcome_tag, overnight_exit
from .legs import LocalLeg, RemoteLeg
from . import costs as costs_mod
from . import fairvalue
from . import hedgeratio
from .marketdata import compute_market_data
from .models import Position, SignalType, Trade, OrderSide
from .notify import TelegramNotifier
from .pair_executor import PairExecutor
from .performance import PerformanceTracker
from .positions import PositionManager
from .reconcile import Reconciler
from .risk import RiskManager
from . import diagnostics, scenarios
from .shadow import ShadowTracker
from .signals import SignalGenerator, ZSignalGenerator
from .spread import SpreadStats
from . import sizing
from . import slippage
from . import webapi


class PaperExecutor:
    """Simulated fills at the current touch — the whole position
    lifecycle (entries, exit ladder, reviews, breakers) runs
    identically to LIVE, no orders leave the machine."""

    def __init__(self, spot_leg, futures_leg, config=None):
        self.spot_leg = spot_leg
        self.futures_leg = futures_leg
        # Only needed to price the slippage report in dollars; paper
        # measures it the same way LIVE does, so the operator can read
        # the number before risking anything.
        self.config = config

    def _fill(self, leg, symbol, side):
        tick = leg.tick(symbol)
        if not tick:
            return None
        return tick['ask'] if side is OrderSide.BUY else tick['bid']

    def _slippage(self, asset_key, signal_type, closing, spot_trade,
                  futures_trade, reference):
        contract = 0.0
        beta = 1.0
        if self.config is not None:
            contract = float((self.config.ASSETS.get(asset_key) or {})
                             .get('lot_size', 0.0) or 0.0)
            beta = self.config.TRADING.get('HEDGE_RATIO', 1.0)
        report = slippage.build(
            signal_type, closing, beta,
            (spot_trade.lot_size or 0.0) * contract,
            spot_trade.side, futures_trade.side, reference,
            spot_trade.executed_price, futures_trade.executed_price,
            spot_trade.symbol, futures_trade.symbol)
        if report:
            logging.info("[SLIPPAGE] paper %s %s: %s",
                         'exit' if closing else 'entry',
                         signal_type.value, slippage.summarise(report))
        legs = (report or {}).get('legs') or {}
        spot_trade.requested_price = (legs.get('spot') or {}).get('quote')
        futures_trade.requested_price = (legs.get('futures') or {}).get('quote')
        return report

    def execute_trade_pair(self, asset, signal_type, lot_size,
                           spot_symbol, futures_symbol, tag='BASIS_ARB',
                           reference=None):
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
        spot_trade.slippage = self._slippage(
            asset, signal_type, False, spot_trade, fut_trade, reference)
        return True, spot_trade, fut_trade

    def execute_close_pair(self, position, reason=None, reference=None):
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
        if ok:
            close_spot.slippage = self._slippage(
                position.asset, position.signal_type, True,
                close_spot, close_fut, reference)
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
        self._last_recon_ts = 0
        self._last_scenario_ts = 0
        self._last_diag_ts = 0
        self._last_order_log = 0.0
        self._last_logged_quote = {}   # asset -> last quote_id persisted
        self._last_status_state = {}   # asset -> last LOGGED state
        self._plan_refusal = None      # why the last entry was blocked
        self._meta_cache = {}          # (leg, symbol) -> volume limits
        self._server_clock = {}        # leg -> broker clock offset vs UTC
        self._scenario_result = None   # last round-trip scenario outcome
        self._last_confirmation = None  # last MT5 ticket read-back
        self._diagnostics = None       # last connectivity checklist
        self._symbol_search = None     # last symbol lookup for the UI
        self._test_results = None
        self.manual_order = None       # armed Manual Spread Trade
        self.manual_note = None        # last manual-trade outcome, for the UI
        self.algo_enabled = True       # entries only; exits ALWAYS run

        self.spot_leg, self.futures_leg = self._resolve_legs()

        self.data_logger = DataLogger()
        if trading_mode == "LIVE":
            self.executor = PairExecutor(config, self.spot_leg,
                                         self.futures_leg)
        else:
            self.executor = PaperExecutor(self.spot_leg, self.futures_leg,
                                          config)
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
        self._accounts_cache = None    # (accounts, equity) — see
        self._accounts_at = 0.0        # _account_snapshot
        self._last_status_write = 0.0  # for the measured write interval
        self.shadow = ShadowTracker(self.data_logger)
        self._last_z = {}          # asset -> z (for SD-touch detection)
        self._last_beta_block = None   # so the refusal logs once, not 3/sec

        self.active_assets = {}
        self.last_signals = {}
        self.last_data = {}

        self._prime_control()

        logging.info("Coordinator initialized: spot on [%s], futures on "
                     "[%s], mode %s, signals %s", self.spot_leg.name,
                     self.futures_leg.name, trading_mode,
                     'z-score' if self.use_z else 'fixed premium')

    def _resolve_legs(self):
        accounts = self.config.accounts
        legs = self.config.leg_accounts or {}
        if not accounts:
            raise ValueError(
                "No MT5 accounts configured. Add one on the Exchanges "
                "page (Add / Edit MT5 Broker), then restart the launcher.")

        names = {}
        for role in ('spot', 'futures'):
            name = legs.get(role)
            if not name:
                raise ValueError(
                    f"No account is mapped to the {role.upper()} leg. On "
                    f"the Exchanges page, edit an account and set its Leg "
                    f"to {'Spot' if role == 'spot' else 'Futures'} "
                    f"(configured accounts: {', '.join(accounts) or 'none'})"
                    f". Then restart the launcher.")
            if name not in accounts:
                raise ValueError(
                    f"The {role.upper()} leg is mapped to account "
                    f"'{name}', which no longer exists (configured: "
                    f"{', '.join(accounts)}). Re-assign the leg on the "
                    f"Exchanges page and restart the launcher.")
            names[role] = name

        spot_name, fut_name = names['spot'], names['futures']
        spot_acct = self.config.accounts[spot_name]
        fut_acct = self.config.accounts[fut_name]

        legs = {}
        for acct in (spot_acct, fut_acct):
            if acct.name in legs:
                continue
            if acct.endpoint:
                try:
                    legs[acct.name] = RemoteLeg(acct.name, acct.endpoint)
                except ValueError as e:
                    raise ValueError(
                        f"Account '{acct.name}': {e} Fix it on the "
                        f"Exchanges page (Settings > MT5 Brokers) and "
                        f"restart the launcher.") from None
            else:
                legs[acct.name] = LocalLeg(BrokerSession(acct))

        # Two accounts at the SAME broker still need two separate
        # terminal INSTALLATIONS — one terminal holds one login, and a
        # second instance of the same install shares its data folder.
        paths = {}
        for acct in (spot_acct, fut_acct):
            path = (getattr(acct, 'terminal_path', None) or '').strip()
            if path:
                paths.setdefault(path.lower(), []).append(acct.name)
        for path, names in paths.items():
            if len(set(names)) > 1:
                raise ValueError(
                    f"Accounts {' and '.join(sorted(set(names)))} both "
                    f"point at the same MT5 installation ({path}). One "
                    f"terminal serves ONE account — install a second copy "
                    f"of MetaTrader 5 (or a portable copy) for the other "
                    f"account and give it its own path on the Exchanges "
                    f"page.")

        local_count = sum(1 for leg in legs.values()
                          if isinstance(leg, LocalLeg))
        if len(legs) > 1 and local_count == 1:
            local = next(leg.name for leg in legs.values()
                         if isinstance(leg, LocalLeg))
            logging.warning(
                "Account '%s' has no leg runner endpoint, so the "
                "coordinator holds its MT5 connection itself. That works, "
                "but its terminal must be logged in BEFORE the coordinator "
                "starts and a coordinator restart drops the session. Give "
                "it an endpoint (e.g. 127.0.0.1:9102) for a clean split.",
                local)
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
            logging.error(
                "No tradable asset: not one configured pair resolved to "
                "symbols that exist on BOTH accounts. Fix the symbols on "
                "the Exchanges page (the log above lists what each "
                "account offers), then restart the launcher.")
            return False

        for asset_key in self.active_assets:
            self.stats[asset_key] = SpreadStats(self.config.SIGNALS,
                                                clock=time.time)
            self._warm_start(asset_key)

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
                self._adopt_broker_specs(asset_key, asset_cfg,
                                         spot_symbol, futures_symbol)
                self._adopt_hedge_ratio(asset_key, asset_cfg,
                                        spot_symbol, futures_symbol)
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
                for role, resolved, leg, candidates in (
                        ('spot', spot_symbol, self.spot_leg,
                         asset_cfg['spot_symbols']),
                        ('futures', futures_symbol, self.futures_leg,
                         asset_cfg['futures_symbols'])):
                    if resolved:
                        continue
                    logging.warning(
                        "  %s leg: none of %s exist on account '%s'",
                        role, list(candidates), leg.name)
                    self._suggest_symbols(asset_key, role, leg, candidates)

        return len(self.active_assets) > 0

    def _adopt_broker_specs(self, asset_key, asset_cfg, spot_symbol,
                            futures_symbol):
        """Take contract size, expiry and swap from the TERMINAL rather
        than from anything typed in. The broker's spec is what actually
        determines P&L and margin, so a typed value can only ever be
        right by luck — and wrong by 2x silently."""
        persist = {}
        reports = {}
        for role, leg, symbol in (('spot', self.spot_leg, spot_symbol),
                                  ('futures', self.futures_leg,
                                   futures_symbol)):
            reader = getattr(leg, 'symbol_report', None)
            if reader is None:
                continue
            try:
                report = reader(symbol)
            except Exception as e:
                logging.debug("No symbol specs for %s: %s", symbol, e)
                continue
            if report.get('found'):
                reports[role] = report

        spot_report = reports.get('spot')
        if spot_report and spot_report.get('contract_size'):
            broker_size = float(spot_report['contract_size'])
            configured = asset_cfg.get('lot_size')
            if configured and abs(broker_size - float(configured)) > 1e-9:
                logging.warning(
                    "%s: contract size is %g per lot on '%s' (config said "
                    "%g) — using the broker's number for P&L and sizing",
                    asset_key, broker_size, self.spot_leg.name,
                    float(configured))
            else:
                logging.info("%s: contract size %g per lot (from %s)",
                             asset_key, broker_size, spot_symbol)
            asset_cfg['lot_size'] = broker_size
            persist['lot_size'] = broker_size

        fut_report = reports.get('futures')
        if fut_report:
            fut_size = fut_report.get('contract_size')
            spot_size = asset_cfg.get('lot_size')
            if fut_size:
                # Leg B's OWN contract size. sizing.plan reads this as
                # `fut_lot_size` and falls back to leg A's when it is
                # missing — and nothing used to set it, so two legs with
                # different contract sizes were silently sized as if
                # they matched. That is the "wrong by 2x silently" this
                # method exists to prevent, in the method itself.
                asset_cfg['fut_lot_size'] = float(fut_size)
                persist['fut_lot_size'] = float(fut_size)
            if fut_size and spot_size and abs(fut_size - spot_size) > 1e-9:
                # NOT a reason to touch HEDGE_RATIO. Beta is the PRICE
                # coefficient of the spread (futures - beta * spot);
                # the contract sizes are handled by the hedge formula
                # L_B = L_A * C_A / (beta * C_B). Changing beta to
                # "compensate" would redefine the series the z-score is
                # measured on.
                logging.warning(
                    "%s: spot is %g/lot but futures is %g/lot — the hedge "
                    "is sized from both contract sizes, so the lot counts "
                    "will differ by %.4gx. This is NOT a reason to change "
                    "HEDGE_RATIO (%.4f), which is the spread's price "
                    "coefficient, not a lot ratio.",
                    asset_key, spot_size, fut_size, spot_size / fut_size,
                    self.config.TRADING.get('HEDGE_RATIO', 1.0))

            expiry = fut_report.get('expiry')
            if expiry and not asset_cfg.get('futures_expiry'):
                asset_cfg['futures_expiry'] = datetime.fromtimestamp(expiry)
                logging.info(
                    "%s: futures contract expires %s (read from the broker)",
                    asset_key, asset_cfg['futures_expiry'].date())

        # Leg A's expiry, for a FUTURE_FUTURE calendar spread. A spot
        # symbol reports none, which is how the fair value knows the
        # pair is spot-vs-future without being told twice.
        leg_a_expiry = (spot_report or {}).get('expiry')
        if leg_a_expiry and not asset_cfg.get('spot_expiry'):
            asset_cfg['spot_expiry'] = datetime.fromtimestamp(leg_a_expiry)
            logging.info("%s: Leg A contract expires %s (read from the "
                         "broker)", asset_key,
                         asset_cfg['spot_expiry'].date())

        self._persist_specs(asset_key, persist)
        self._log_spread_definition(asset_key)
        self._log_fair_value(asset_key, asset_cfg)

    def _adopt_hedge_ratio(self, asset_key, asset_cfg, spot_symbol,
                           futures_symbol):
        """Re-derive HEDGE_RATIO when the pair it was set for is gone.

        Operator, 2026-08-10: "Can you make sure the Hedge Ratio is
        calculated and changed everytime the pair is changed?" — after
        66.94, computed for XAGUSD/XAUUSD, was left behind on
        USOIL/UKOIL and defined the spread as -5469.59 on legs priced
        82.61 and 86.05.

        Runs here, inside _setup_symbols, for a reason: `_series_key`
        includes beta, and `_warm_start` seeds the rolling window from
        rows matching that key. Changing beta after the warm start
        would seed the window on the old series and hand the strategy a
        mu and sigma the live spread never visits.

        Two things it will not do:

        * Touch beta while a position is open. Beta defines the series
          the position was entered on; redefining it underneath a live
          trade orphans its entry geometry. The book is read from the
          DB because position recovery has not run yet at this point.
        * Overwrite a beta that is stamped for THIS pair. Beta is a
          strategy parameter and an operator who tuned it keeps their
          number — the stamp is what separates "tuned" from "stale".
        """
        beta = float(self.config.TRADING.get('HEDGE_RATIO', 1.0) or 1.0)
        stamp = self.config.TRADING.get('HEDGE_RATIO_FOR')
        signature = hedgeratio.pair_signature(spot_symbol, futures_symbol)

        prices = {}
        for role, leg, symbol in (('a', self.spot_leg, spot_symbol),
                                  ('b', self.futures_leg, futures_symbol)):
            try:
                tick = leg.tick(symbol)
            except Exception:
                tick = None
            prices[role] = ((tick['bid'] + tick['ask']) / 2
                            if tick and tick.get('bid') and tick.get('ask')
                            else None)

        new_beta, why = hedgeratio.resolve(
            beta, stamp, asset_cfg.get('pair_type'), spot_symbol,
            futures_symbol, prices['a'], prices['b'])

        if new_beta is None:
            if stamp != signature and prices['a'] and prices['b']:
                self._persist_trading({'HEDGE_RATIO_FOR': signature})
                self.config.TRADING['HEDGE_RATIO_FOR'] = signature
            logging.info("%s: %s", asset_key, why)
            return

        open_positions = self.data_logger.load_open_position_states()
        if open_positions:
            logging.critical(
                "%s: HEDGE_RATIO should be %g — %s. NOT changing it: %d "
                "position(s) are open and beta defines the series they "
                "were entered on. Close them, then restart.",
                asset_key, new_beta, why, len(open_positions))
            return

        logging.warning("%s: HEDGE_RATIO %g -> %g — %s",
                        asset_key, beta, new_beta, why)
        self.config.TRADING['HEDGE_RATIO'] = new_beta
        self.config.TRADING['HEDGE_RATIO_FOR'] = signature
        self._persist_trading({'HEDGE_RATIO': new_beta,
                               'HEDGE_RATIO_FOR': signature})

    def _persist_trading(self, updates):
        """Write TRADING keys back to config.json.

        `_persist_specs` is deliberately specs-only — strategy
        parameters belong to the operator. This is the one exception
        and it is narrow: a beta computed for a pair that is no longer
        configured is not the operator's choice, it is a leftover, and
        leaving it in the file means the Exchanges checklist keeps
        reporting a fault the engine has already corrected in memory.
        """
        if not self.config_path or not updates:
            return
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            trading = raw.setdefault('trading', {})
            if all(trading.get(k) == v for k, v in updates.items()):
                return
            trading.update(updates)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(raw, f, indent=2)
            # Adopt our own write, or hot_apply reads the mtime change
            # as an operator edit on the next pass.
            self._config_mtime = os.path.getmtime(self.config_path)
            logging.info("Saved to config: %s",
                         ', '.join(f'{k}={v}' for k, v in updates.items()))
        except (OSError, ValueError) as e:
            logging.debug("Could not persist trading config: %s", e)

    def _log_fair_value(self, asset_key, asset_cfg):
        """Reference only — say once at startup whether a fair value is
        available and why not when it isn't."""
        pair_type = (asset_cfg.get('pair_type') or 'SPOT_FUTURE').upper()
        if pair_type not in fairvalue.BASIS_TYPES:
            logging.info("%s: pair type %s — no theoretical fair value "
                         "(no arbitrage ties the two legs together)",
                         asset_key, pair_type)
            return
        value, detail = fairvalue.fair_spread(
            asset_cfg, 1.0, 1.0, self.config.TRADING.get('HEDGE_RATIO', 1.0))
        if value is None:
            logging.info("%s: pair type %s, but no fair value — %s",
                         asset_key, pair_type, detail)
        else:
            logging.info("%s: pair type %s — fair value shown on the "
                         "dashboard for REFERENCE, never used as a signal",
                         asset_key, pair_type)

    def _persist_specs(self, asset_key, updates):
        """Write adopted broker specs back to config.json.

        `_adopt_broker_specs` took the terminal's contract size, expiry
        and swap as authoritative but only ever updated the IN-MEMORY
        config. So config.json kept whatever was typed there originally,
        the Exchanges checklist read that file and warned "broker says
        1000 but the asset is configured as 100" on every single run,
        and the fix it offered — "set the contract size in Settings" —
        pointed at a control that does not exist, because the owner had
        it removed precisely on the grounds that MT5 already knows.
        An unfixable warning trains the operator to ignore the
        checklist, which is the opposite of what it is for.

        SPECS ONLY. Symbols, legs and strategy parameters belong to the
        operator and are never written back from here.
        """
        if not self.config_path or not updates:
            return
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
            asset = raw.setdefault('assets', {}).setdefault(asset_key, {})
            if all(asset.get(k) == v for k, v in updates.items()):
                return                       # already agrees; don't churn
            asset.update(updates)
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(raw, f, indent=2)
            # Adopt our own write, or the mtime change looks like an
            # operator edit and hot_apply logs "assets change requires a
            # restart" on every startup.
            self._config_mtime = os.path.getmtime(self.config_path)
            logging.info("%s: saved broker specs to config (%s)", asset_key,
                         ', '.join(f'{k}={v}' for k, v in updates.items()))
        except (OSError, ValueError) as e:
            logging.debug("Could not persist broker specs: %s", e)

    def _series_key(self, asset_key):
        """Which spread series this is: the two symbols and the hedge
        ratio. Change any of them and the old numbers describe a
        different series that must not seed the new one."""
        asset = self.active_assets.get(asset_key) or {}
        beta = self.config.TRADING.get('HEDGE_RATIO', 1.0)
        # Defensive: this only labels a log row, and must never be the
        # reason the trading loop raises.
        return (f"{asset.get('spot_symbol', '?')}|"
                f"{asset.get('futures_symbol', '?')}|{beta:.6f}")

    def _warm_start(self, asset_key):
        """Refill the rolling window from quotes already on disk."""
        stats = self.stats.get(asset_key)
        if stats is None:
            return 0
        window = self.config.SIGNALS.get('LOOKBACK_SEC', 7200)
        since = datetime.now() - timedelta(seconds=window)
        try:
            rows = self.data_logger.recent_spreads(
                asset_key, self._series_key(asset_key), since)
        except Exception as e:
            logging.warning("%s: could not read stored quotes (%s) — "
                            "starting the window empty", asset_key, e)
            return 0
        seeded = stats.seed(rows)
        if not seeded:
            logging.info(
                "%s: no stored quotes inside the %.0f-minute window — "
                "warming up from scratch", asset_key, window / 60)
            return 0
        logging.info(
            "%s: warm start — %d stored quotes recovered, %.0f minutes of "
            "history, mu=%.4f sigma=%.4f", asset_key, seeded,
            stats.history_sec / 60, stats.mu or 0.0, stats.sigma or 0.0)
        return seeded

    def _log_spread_definition(self, asset_key):
        """Say in the log exactly what the number on the dashboard is.
        "The spread seems incorrect" is usually the operator checking it
        against the two prices beside it."""
        beta = self.config.TRADING.get('HEDGE_RATIO', 1.0)
        logging.info("%s spread = futures - %g x spot (HEDGE_RATIO)",
                     asset_key, beta)

    def _suggest_symbols(self, asset_key, role, leg, candidates):
        """Tell the operator what this broker DOES call the instrument —
        a bare 'symbol not found' leaves them guessing, and broker
        naming is the single most common setup failure."""
        finder = getattr(leg, 'find_symbols', None)
        if finder is None:
            return
        hints = {asset_key.upper()}
        for candidate in candidates:
            letters = ''.join(c for c in str(candidate) if c.isalpha())
            if len(letters) >= 3:
                hints.add(letters[:3].upper())
        found = {}
        for hint in hints:
            try:
                for row in (finder(hint, 12) or ()):
                    found[row['symbol']] = row.get('description', '')
            except Exception:
                return
        if not found:
            logging.warning(
                "    account '%s' has nothing matching %s — is this the "
                "right account for the %s leg?",
                leg.name, sorted(hints), role)
            return
        listing = ', '.join(f"{name} ({desc})" if desc else name
                            for name, desc in list(found.items())[:8])
        logging.warning(
            "    account '%s' offers: %s", leg.name, listing)
        logging.warning(
            "    set the %s symbol to one of those on the Exchanges page "
            "(the %s leg must point at the account that actually lists "
            "the instrument)", role, role)

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
                SimpleNamespace(**futures_tick),
                self.config.TRADING.get('HEDGE_RATIO', 1.0))
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
            stats.update(market_data['spread'],
                         market_data.get('quote_id'))
            z = stats.z
            self.z_gen.update(asset_key, z)

        self._detect_sd_touches(asset_key, z, market_data['spread'])
        self.shadow.update(asset_key, market_data['spot_price'],
                           market_data['futures_price'])
        # An armed Manual Spread Trade watches the spread on every
        # tick, independently of the algo switch.
        self._check_manual_arm(asset_key, market_data)

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
                market_data['basis_pct'],
                contract_size=contract_size)

            reason = self._exit_reason(position, z, market_data)
            if reason and self._close_is_due(position):
                self._close(position_id, position, reason, contract_size, z,
                            spread=market_data.get('spread'),
                            market_data=market_data)

        # -- entries (only while the algo is enabled; exits above
        # always run — stopping the algo never abandons a position) --
        if not self.algo_enabled:
            self.last_signals[asset_key] = SignalType.NO_SIGNAL
            self._log_quote(asset_key, market_data, SignalType.NO_SIGNAL, z)
            return
        active = self.position_manager.get_positions_for_asset(asset_key)
        signal = self._entry_signal(asset_key, stats, market_data, active,
                                    contract_size)
        self.last_signals[asset_key] = signal or SignalType.NO_SIGNAL
        self._log_quote(asset_key, market_data,
                        self.last_signals[asset_key], z)
        if signal:
            self._enter(asset_key, signal, market_data, stats, contract_size)

    def _log_quote(self, asset_key, market_data, signal, z):
        """Persist one QUOTE, not one poll.

        market_data was written to the table on every loop — three
        times a second — while the brokers tick far more slowly, so the
        same quote was stored hundreds of times over. Two consequences,
        both live:

        1. A warm start seeded the rolling window from those rows, so
           the window filled with POLL duplicates and the counter read
           ~24,000 instead of the real quote count, then fell for two
           hours as the duplicates aged out. That decline is what the
           operator saw and could not explain (2026-08-07).
        2. Worse, it silently undid the quote_id fix ACROSS RESTARTS.
           Sigma is poll-rate invariant live, but a window seeded with
           repeats has a deflated variance — the same collapse that
           produced z = +53,026 on 2026-08-06.

        It also wrote ~288k rows per asset per day for no added
        information."""
        quote_id = market_data.get('quote_id')
        if quote_id is not None and \
                self._last_logged_quote.get(asset_key) == quote_id:
            return False
        self._last_logged_quote[asset_key] = quote_id
        self.data_logger.log_market_data(
            asset_key, market_data, signal, z=z,
            series_key=self._series_key(asset_key))
        return True

    CLOSE_RETRY_SEC = 5.0        # don't hammer a broker that says no
    CLOSE_ESCALATE_AFTER = 5     # ...but never go quiet about it

    def _close_is_due(self, position):
        """Rate-limit retries of a close the broker keeps refusing.

        The position stays ACTIVE so the exit ladder keeps asking for
        it (that is the fix for it vanishing entirely), which without a
        limit would re-send the order on every poll — three times a
        second."""
        failures = getattr(position, 'close_failures', 0)
        if not failures:
            return True
        last = getattr(position, 'last_close_attempt', None)
        if last and (datetime.now() - last).total_seconds() \
                < self.CLOSE_RETRY_SEC:
            return False
        if failures == self.CLOSE_ESCALATE_AFTER:
            logging.critical(
                "%s has failed to close %d times (%s). It is STILL OPEN at "
                "the broker. Retries continue every %.0fs, but CLOSE IT BY "
                "HAND if this persists.", position.position_id, failures,
                getattr(position, 'last_close_error', 'unknown'),
                self.CLOSE_RETRY_SEC)
        return True

    def _exit_reason(self, position, z, market_data):
        plan = position.exit_plan or {}
        # Overnight rule from a manual trade — checked before the
        # ladder so the session cutoff is never missed.
        overnight = overnight_exit(
            plan.get('overnight_mode'),
            position.unrealized_pnl - plan.get('rt_cost_usd', 0.0),
            datetime.now(),
            self.config.MANUAL.get('OVERNIGHT_CLOSE_HOUR', 16),
            self.config.MANUAL.get('OVERNIGHT_CLOSE_MINUTE', 55))
        if overnight:
            return overnight
        if self.use_z and position.exit_plan:
            age = (datetime.now() - position.entry_time).total_seconds()
            return self.exit_ladder.evaluate(
                position, position.exit_plan, z,
                position.unrealized_pnl, age,
                spread=market_data.get('spread'))
        # Legacy premium-based paths
        hit, action = self.risk_manager.check_position_risk(
            position, market_data['basis_pct'])
        if hit:
            return action
        signal = self.legacy_gen.generate_signal(
            position.asset, market_data,
            {position.position_id: position})
        if isinstance(signal, tuple):
            return "SIGNAL_EXIT"
        return None

    def _implausible_spread(self, md):
        """Is the configured spread a difference between these two
        prices at all? Returns the reason it is not, or None.

        A pair spread is a small DIFFERENCE between two comparable
        prices. When it dwarfs the prices themselves, HEDGE_RATIO is
        wrong and every number downstream — mu, sigma, z, the exit
        levels — describes a series that does not exist.

        Live 2026-08-10, three times in one day and always the same
        way: the contract-size check advised "or correct HEDGE_RATIO
        for the difference", the operator set beta to 10, and
        USOIL/UKOIL at 81.76/85.07 became a spread of -732.53. Then
        beta 0.0149. Then, on switching the pair back to USOIL/UKOIL,
        the 66.94 left over from XAGUSD/XAUUSD, giving -5443.86 on legs
        priced 82.61 and 86.05. Beta is the PRICE coefficient; contract
        sizes are handled by the hedge formula.

        A HEDGE_RATIO carried across an instrument change is the common
        thread, and nothing stops an operator changing the symbols
        without it. So this is a BLOCK on entries, not a warning: the
        engine will not open a position on a series it can show is not
        the difference between the two prices it is quoting. Exits are
        evaluated before this and are unaffected — a wrong beta must
        never trap a live position.
        """
        spot_px = md.get('spot_price') or 0
        fut_px = md.get('futures_price') or 0
        beta = self.config.TRADING.get('HEDGE_RATIO', 1.0) or 1.0
        # One threshold, shared with the startup adoption — the engine
        # must never adopt a beta it will then refuse to trade on.
        if hedgeratio.implausible(beta, spot_px, fut_px,
                                  md.get('spread')) is None:
            return None
        ratio = (fut_px / spot_px) if spot_px else 0
        return (f'spread {md["spread"]:+.2f} dwarfs the leg prices '
                f'({spot_px:.2f} / {fut_px:.2f}) — HEDGE_RATIO '
                f'{beta:g} is wrong, so mu, sigma, z and every exit '
                f'level describe a series that does not exist. Entries '
                f'are blocked until it is fixed; exits still run. The '
                f'price ratio is {ratio:.4f} — use 1 for the same '
                f'underlying (spot vs its own future), or near the '
                f'price ratio for two different instruments. Beta is '
                f'the spread\'s price coefficient, NOT a contract-size '
                f'or lot ratio; differing contract sizes are already '
                f'handled when sizing the hedge.')

    def _entry_signal(self, asset_key, stats, market_data, active,
                      contract_size):
        broken = self._implausible_spread(market_data)
        if broken:
            if self._last_beta_block != broken:
                self._last_beta_block = broken
                logging.error("Entries blocked: %s", broken)
            return None
        self._last_beta_block = None
        clip = self._clip_lots(asset_key, market_data)
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

    def _symbol_meta(self, leg, symbol):
        """Volume step / minimum / maximum for one symbol, cached.

        Read from the LEG, not from the executor: PaperExecutor has no
        `_meta`, so the previous lookup silently returned nothing in
        paper mode and the plan was computed with no step and no
        minimum at all — fractional lots no broker would accept, and
        the minimum-notional guard never fired. Paper is supposed to
        mirror LIVE, so it has to see the same instrument limits.

        Cached because a RemoteLeg answers over IPC and this runs on
        every poll; symbol specs do not change while we are running.
        """
        if not symbol or leg is None:
            return None
        key = (getattr(leg, 'name', '?'), symbol)
        if key not in self._meta_cache:
            try:
                meta = leg.ensure_symbol(symbol)
            except Exception:
                meta = None
            self._meta_cache[key] = meta if (meta and meta.get('ok')) else None
        return self._meta_cache[key]

    def _sizing_plan(self, asset_key, market_data=None):
        """Resolve this entry's lots for BOTH legs.

        In 'notional' mode the operator sets the money per leg on the
        Settings page and the lots come from the live price, so the
        same setting means the same risk on gold and on oil. In 'lots'
        mode CLIP_LOTS is the anchor, as before. Either way the hedge
        leg is derived from leg A and the two CONTRACT SIZES, never
        from the lot count alone (statarb/sizing.py).
        """
        market_data = market_data or (
            self.active_assets.get(asset_key) or {}).get('last_data') or {}
        asset = self.config.ASSETS.get(asset_key) or {}
        contract_a = float(asset.get('lot_size') or 1.0)
        contract_b = float(asset.get('fut_lot_size') or contract_a)
        legs = self.active_assets.get(asset_key) or {}
        meta_a = self._symbol_meta(self.spot_leg, legs.get('spot_symbol'))
        meta_b = self._symbol_meta(self.futures_leg,
                                   legs.get('futures_symbol'))
        return sizing.plan(
            self.config, contract_a, contract_b,
            market_data.get('spot_price'), market_data.get('futures_price'),
            meta_a=meta_a, meta_b=meta_b,
            size_multiplier=self.risk_manager.size_multiplier())

    def _clip_lots(self, asset_key=None, market_data=None):
        """Leg A lots for the next entry."""
        if asset_key is None:
            return self.config.TRADING.get('CLIP_LOTS', 1.0) \
                * self.risk_manager.size_multiplier()
        return self._sizing_plan(asset_key, market_data)['leg_a_lots']

    def _enter(self, asset_key, signal_type, market_data, stats,
               contract_size):
        size = self._sizing_plan(asset_key, market_data)
        clip = size['leg_a_lots']
        if size.get('reason') or clip <= 0:
            logging.warning("Entry rejected for %s: %s", asset_key,
                            size.get('reason') or 'sizing resolved to 0 lots')
            return

        valid, reason = self.risk_manager.validate_new_position(
            asset_key, signal_type, clip, self.position_manager)
        if not valid:
            logging.info("Entry rejected for %s: %s", asset_key, reason)
            return

        self._open_position(asset_key, signal_type, clip, market_data,
                            stats, contract_size)

    def _open_position(self, asset_key, signal_type, lots, market_data,
                       stats, contract_size, manual=False,
                       exit_spread=None, stop_spread=None, overnight=None):
        """Shared entry path for signal and manual trades: exit plan
        BEFORE orders (a trade whose cost floor exceeds plausible
        reversion is refused), execute, attach frozen levels."""
        plan = None
        if (self.use_z and stats is not None) or manual:
            warm = stats is not None and stats.warm
            # A manual trade carries the operator's OWN take-profit, so
            # the viability test measures the distance THEY chose. The
            # engine vetoing it against a sigma-derived target nobody
            # asked for is it substituting its opinion for the
            # operator's (2026-08-07: a hand-placed trade with a 2.12
            # spread target — $212 against $59 of cost — was refused
            # because a full reversion of the CURRENT z was worth $15).
            manual_target = None
            if manual and exit_spread is not None \
                    and market_data.get('spread') is not None:
                manual_target = (abs(market_data['spread']
                                     - float(exit_spread))
                                 * lots * contract_size)
            plan = self.exit_ladder.build_plan(
                lots, contract_size,
                stats.z if warm else None,
                stats.sigma if warm else None,
                stats.half_life_sec if stats else None, market_data,
                manual_target_usd=manual_target)
            if plan is None:
                self._plan_refusal = self.exit_ladder.last_refusal
                return None
            if manual:
                plan['source'] = 'MANUAL'
                # The operator's own take-profit, stop and overnight
                # rule travel with the trade.
                if exit_spread is not None:
                    plan['manual_exit_spread'] = float(exit_spread)
                if stop_spread is not None:
                    plan['manual_stop_spread'] = float(stop_spread)
                plan['overnight_mode'] = overnight or 'ALLOW'

        asset = self.active_assets[asset_key]
        success, spot_trade, futures_trade = \
            self.executor.execute_trade_pair(
                asset_key, signal_type, lots,
                asset['spot_symbol'], asset['futures_symbol'],
                tag='MANUAL' if manual else 'BASIS_ARB',
                # The snapshot the DECISION was made on, so slippage is
                # measured from the prices the signal actually saw.
                reference=market_data)
        if not success:
            logging.error("Pair entry failed for %s %s", asset_key,
                          signal_type.value)
            return None

        position = self.position_manager.create_position(
            asset_key, signal_type, spot_trade, futures_trade,
            market_data['basis_pct'])
        position.entry_slippage = spot_trade.slippage
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
            plan['entry_spread'] = market_data['spread']
            plan['levels'] = self.exit_ladder.spread_levels(
                plan, market_data['spread'],
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
        if self.trading_mode == "LIVE":
            self._confirm_with_mt5(position, 'entry')
        return position

    def _close(self, position_id, position, reason, contract_size, z,
               spread=None, market_data=None):
        contract = contract_size
        # The exit decision's own snapshot, so the close is scored on
        # what it saw rather than on a tick read after the fact.
        if market_data is None:
            market_data = (self.active_assets.get(position.asset) or {}) \
                .get('last_data')
        closed = self.position_manager.close_position(
            position_id, reason, self.executor, contract_size=contract,
            reference=market_data)
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
        if self.trading_mode == "LIVE":
            self._confirm_with_mt5(position, 'exit')

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
                'gate_floor_usd': plan.get('gate_floor_usd'),
                'expectancy': plan.get('expectancy'),
                'max_hold_sec': plan.get('max_hold_sec'),
                'half_life_min': ((plan.get('half_life_sec') or 0) / 60
                                  or None),
                'notional': ((position.spot_trade.executed_price or 0)
                             * position.spot_trade.lot_size
                             * self.config.ASSETS.get(position.asset, {})
                             .get('lot_size', 1.0)),
                'levels': plan.get('levels'),
                'peak_pnl': position.peak_pnl,
                'trough_pnl': position.trough_pnl,
                # What the signal wanted vs what MT5 gave us, so the
                # cost of getting in is visible while the trade is
                # still open rather than only in the post-mortem.
                'entry_slippage': position.entry_slippage,
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

    # One-shot commands in control.json, by their watermark attribute.
    # Everything here EXECUTES something (places orders, runs a scenario
    # of real round trips, force-closes a position), so it must fire once
    # only, when the operator asks for it — never again on restart.
    _CONTROL_COMMANDS = (
        ('close', '_last_close_ts'),
        ('open', '_last_open_ts'),
        ('test', '_last_test_ts'),
        ('diagnose', '_last_diag_ts'),
        ('scenario', '_last_scenario_ts'),
        ('reconcile', '_last_recon_ts'),
    )

    def _prime_control(self):
        """Adopt whatever is already in control.json WITHOUT executing it.

        control.json is a persistent file, and every command in it carries
        a `ts`. The watermarks all started at 0, so the first
        `_read_control()` of a fresh process saw every historical command
        as newer than "never seen" and ran the lot.

        Live 2026-08-07, on a plain restart, in under half a second: an
        armed manual order re-armed and immediately TRIGGERED (the spread
        was already through its level), opening a second unintended LIVE
        gold pair; a SCENARIO of real min-lot round trips re-ran; a
        reconciliation and a connectivity diagnose re-ran too.

        So startup adopts the file's timestamps as already-seen. Only
        commands written AFTER the process starts are acted on.
        `algo_enabled` is deliberately excluded: it is persistent state
        (the operator stopped the algo and it must stay stopped across a
        restart), not a command.
        """
        try:
            self._control_mtime = os.path.getmtime(self.control_path)
            with open(self.control_path, 'r', encoding='utf-8') as f:
                control = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(control, dict):
            return

        self.algo_enabled = bool(control.get('algo_enabled', True))

        adopted = []
        for key, attr in self._CONTROL_COMMANDS:
            cmd = control.get(key) or {}
            ts = cmd.get('ts', 0) if isinstance(cmd, dict) else 0
            if ts:
                setattr(self, attr, ts)
                adopted.append(key)
        if adopted:
            logging.info("Ignoring %s already in control.json from a "
                         "previous session — one-shot commands are not "
                         "replayed on startup", ', '.join(sorted(adopted)))
        if not self.algo_enabled:
            logging.warning("Algo is DISABLED (carried over from "
                            "control.json) — exits keep running")

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
        if ts > self._last_open_ts:
            self._last_open_ts = ts
            if not open_cmd.get('asset'):
                if self.manual_order:
                    logging.warning("Manual trade CANCELLED via web UI")
                    self.manual_note = {'ok': True, 'ts': time.time(),
                                        'text': 'armed order cancelled'}
                self.manual_order = None
            elif open_cmd.get('entry_spread') is None:
                # Fire now at market (immediate manual trade)
                self._manual_open(open_cmd['asset'],
                                  open_cmd.get('direction', ''),
                                  open_cmd.get('lots'),
                                  exit_spread=open_cmd.get('exit_spread'),
                                  stop_spread=open_cmd.get('stop_spread'),
                                  overnight=open_cmd.get('overnight'))
            else:
                self._arm_manual(open_cmd)

        test_cmd = control.get('test') or {}
        ts = test_cmd.get('ts', 0)
        if ts > self._last_test_ts:
            self._last_test_ts = ts
            if test_cmd.get('kind'):
                self._run_tests(test_cmd['kind'])
            else:
                self._test_results = None      # UI cleared the results

        diag_cmd = control.get('diagnose') or {}
        ts = diag_cmd.get('ts', 0)
        if ts > self._last_diag_ts:
            self._last_diag_ts = ts
            if diag_cmd.get('find_symbols') is not None:
                self._find_symbols(diag_cmd)
            else:
                self._run_diagnostics(diag_cmd)

        scenario_cmd = control.get('scenario') or {}
        ts = scenario_cmd.get('ts', 0)
        if ts > self._last_scenario_ts:
            self._last_scenario_ts = ts
            if scenario_cmd.get('type'):
                self._run_scenario(scenario_cmd)
            else:
                self._scenario_result = None   # UI cleared the results

        recon_cmd = control.get('reconcile') or {}
        ts = recon_cmd.get('ts', 0)
        if ts > self._last_recon_ts:
            self._last_recon_ts = ts
            if self.reconciler:
                logging.warning("Reconciliation requested via web UI")
                for action, leg_name, detail in self.reconciler.check():
                    self.notifier.notify_reconcile(action, leg_name,
                                                   str(detail))

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
        if kind == 'orders':
            self._poll_order_logs(interval=0, hours=1)
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
            # Read it back out of the terminal — the proof that the
            # order reached MT5 and is not just our own return value.
            verifier = getattr(leg, 'verify_order', None)
            if verifier and tickets:
                found = verifier(tickets[0])
                deals = (found or {}).get('deals') or []
                add(leg.name, f'MT5 record {symbol}',
                    (found or {}).get('confirmed'),
                    (f"deal {deals[-1]['deal_id']} order "
                     f"{deals[-1]['order_id']} @ {deals[-1]['price']}"
                     if deals else (found or {}).get('error', 'no record')))
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

    # ------------------------------------------------------------------
    # Connectivity checklist (Exchanges page)
    # ------------------------------------------------------------------

    def _diag_asset(self, asset_key=None):
        if not self.active_assets:
            return None, None
        key = (asset_key if asset_key in self.active_assets
               else next(iter(self.active_assets)))
        return key, self.active_assets[key]

    def _run_diagnostics(self, spec):
        """Ask both terminals what they are, what they allow and what
        their symbols look like, then check the pair fits together.
        Read-only — this places no orders, so it may run at any time."""
        ts = spec.get('ts')
        asset_key, asset = self._diag_asset(spec.get('asset'))
        if not asset:
            self._diagnostics = {
                'ts': ts, 'overall': 'FAIL', 'checks': [
                    {'scope': 'ENGINE', 'name': 'Assets', 'status': 'FAIL',
                     'message': 'No active asset — the coordinator has not '
                                'connected to its legs yet.'}],
                'passed': 0, 'warnings': 0, 'failed': 1, 'info': 0,
                'ran_at': datetime.now().strftime('%H:%M:%S'), 'legs': {}}
            self._write_runtime_status(self.last_data or {})
            return self._diagnostics

        asset_cfg = self.config.ASSETS.get(asset_key, {})
        sides, raw = {}, {}
        for role, leg, symbol_key in (('spot', self.spot_leg, 'spot_symbol'),
                                      ('futures', self.futures_leg,
                                       'futures_symbol')):
            symbol = asset[symbol_key]
            terminal = leg.terminal_report()
            symbol_report = leg.symbol_report(symbol)
            sides[role] = {'account': leg.name, 'role': role,
                           'terminal': terminal, 'symbol': symbol_report,
                           'asset': asset_cfg}
            raw[leg.name] = {'terminal': terminal, 'symbol': symbol_report,
                             'role': role}

        expected = {name: acct.login
                    for name, acct in self.config.accounts.items()
                    if getattr(acct, 'login', None)}
        report = diagnostics.build_report(
            self.config, sides['spot'], sides['futures'],
            expected_logins=expected,
            leverages={'spot': self.config.EXITS.get('SPOT_LEVERAGE')
                       or self.config.EXITS.get('LEVERAGE'),
                       'futures': self.config.EXITS.get('FUT_LEVERAGE')
                       or self.config.EXITS.get('LEVERAGE')})
        report.update({'ts': ts, 'asset': asset_key, 'legs': raw})
        self._diagnostics = report
        logging.info("[DIAGNOSE] %s: %d pass, %d warn, %d fail",
                     report['overall'], report['passed'],
                     report['warnings'], report['failed'])
        self._write_runtime_status(self.last_data or {})
        return report

    def _find_symbols(self, spec):
        """Symbol search on one account — brokers name gold half a dozen
        ways, so the operator looks it up instead of guessing."""
        pattern = spec.get('find_symbols') or ''
        role = spec.get('leg', 'spot')
        leg = self.futures_leg if role == 'futures' else self.spot_leg
        try:
            found = leg.find_symbols(pattern, spec.get('limit', 40))
        except Exception as e:
            logging.warning("Symbol search failed on '%s': %s", leg.name, e)
            found = None
        self._symbol_search = {
            'ts': spec.get('ts'), 'leg': role, 'account': leg.name,
            'pattern': pattern,
            'symbols': found or [],
            'error': None if found is not None else
            f"Could not read symbols from '{leg.name}'",
        }
        self._write_runtime_status(self.last_data or {})
        return self._symbol_search

    def _scenario_legs(self, asset_key=None):
        """Build the spot/futures pair the scenario runner acts on:
        each leg's account, its symbol on that account, the asset's
        contract size and that leg's commission."""
        if not self.active_assets:
            return None, None, 'No active asset — is the coordinator '\
                               'connected to both legs?'
        asset_key = (asset_key if asset_key in self.active_assets
                     else next(iter(self.active_assets)))
        asset = self.active_assets[asset_key]
        # Each leg's OWN contract size. Passing Leg A's to both made
        # every futures P&L in the scenario report wrong by the ratio
        # between them — invisible on gold (100 oz both legs), a 50x
        # error on a gold/silver pair.
        cfg = self.config.ASSETS.get(asset_key, {})
        contract_a = cfg.get('lot_size', 100.0)
        contract_b = cfg.get('fut_lot_size') or contract_a
        spot = scenarios.Leg(
            self.spot_leg, asset['spot_symbol'], 'SPOT', contract_a,
            self.config.COSTS.get('COMMISSION_PER_LOT_SPOT', 0.0))
        futures = scenarios.Leg(
            self.futures_leg, asset['futures_symbol'], 'FUTURES', contract_b,
            self.config.COSTS.get('COMMISSION_PER_LOT_FUT', 0.0))
        return spot, futures, None

    def _scenario_stats(self, asset_key):
        """The live spread and its z, so the scenario report carries
        the same numbers the strategy would have acted on."""
        def snapshot():
            data = self.get_market_data(asset_key)
            if not data:
                return None
            stats = self.stats.get(asset_key)
            return (data['spread'],
                    stats.mu if stats else None,
                    stats.sigma if stats else None,
                    stats.z if stats else None)
        return snapshot

    def _run_scenario(self, spec):
        """Run ONE round-trip scenario at minimum volume on the real
        accounts (the Exchanges page's suite). Runs INLINE in the
        trading loop on purpose: a RemoteLeg holds a single socket, so
        a background thread would interleave requests on it. Scenarios
        are bounded (a limit waits at most ~15s) and the whole feature
        requires the algo stopped and a flat book anyway."""
        started = datetime.now()
        name = (f"{spec.get('type')} {spec.get('mode')} "
                f"{spec.get('variant', 'normal')}")

        def finish(success, detail):
            self._scenario_result = {
                'id': spec.get('id'), 'type': spec.get('type'),
                'mode': spec.get('mode'),
                'variant': spec.get('variant', 'normal'),
                'ts': spec.get('ts'), 'success': bool(success),
                'detail': detail,
                'ran_at': started.strftime('%H:%M:%S'),
            }
            logging.warning("[SCENARIO] %s: %s", name,
                            'PASS' if success else 'FAIL')
            for line in (detail or '').splitlines():
                logging.info("[SCENARIO]   %s", line)
            # Whatever just hit the terminal belongs in the Exchange
            # Order Log NOW, not at the next 30s poll.
            self._poll_order_logs(interval=0, hours=1)
            # The UI is waiting on this one result, and the routine
            # status refresh is ~10s away — publish it now.
            self._write_runtime_status(self.last_data or {})
            return self._scenario_result

        if self.algo_enabled:
            return finish(False, 'Stop the algo before running order '
                                 'scenarios — they place REAL orders.')
        if self.position_manager.get_active_positions():
            return finish(False, 'Close open positions first — scenarios '
                                 'need a flat book.')

        # The engine's book being empty is not enough: a leaked fill or
        # a failed close leaves a position the ENGINE does not know
        # about, and running more scenarios on top just piles them up
        # (seen 2026-08-06: eleven orphans accumulated while the suite
        # kept reporting PASS). Ask the BROKER.
        stranded = []
        for leg in self._each_leg():
            try:
                live = leg.positions()
            except Exception:
                live = None
            if live is None:
                return finish(False, f"Cannot read {leg.name}'s open "
                                     f"positions — refusing to place test "
                                     f"orders blind.")
            stranded += [f"{p['symbol']} {p['side']} {p['volume']} "
                         f"(ticket {p['ticket']}) on {leg.name}"
                         for p in live]
        if stranded:
            return finish(
                False,
                f"{len(stranded)} bot position(s) are still open on the "
                f"broker — close them before running more scenarios, or "
                f"every run adds to the pile:\n  "
                + "\n  ".join(stranded[:10])
                + ("\n  …" if len(stranded) > 10 else ""))
        for leg in self._each_leg():
            if not leg.ping():
                return finish(False, f"Leg '{leg.name}' is not connected.")

        spot, futures, error = self._scenario_legs(spec.get('asset'))
        if error:
            return finish(False, error)

        asset_key = (spec.get('asset') if spec.get('asset')
                     in self.active_assets
                     else next(iter(self.active_assets)))
        def scenario_sleep(seconds):
            """Sleep, but keep the engine breathing.

            A scenario blocks the trading loop for its whole duration.
            At the half-second default that is invisible; at a two-minute
            hold it would stop the price feed dead, age the statistics
            window out and leave the dashboard frozen with a live
            position on the book. Poll the feed across the wait so the
            window keeps filling and the operator can watch the position
            they have open.
            """
            time.sleep(seconds)
            try:
                for key, md in self.get_all_market_data().items():
                    stats = self.stats.get(key)
                    if stats is not None:
                        stats.update(md['spread'], md.get('quote_id'))
                    self.last_data[key] = md
                self._write_runtime_status(self.last_data or {})
            except Exception:
                pass          # a scenario must not die on a feed hiccup

        runner = scenarios.ScenarioRunner(
            spot, futures, spread_stats=self._scenario_stats(asset_key),
            hedge_ratio=self.config.TRADING.get('HEDGE_RATIO', 1.0),
            hedge_mode=self.config.TRADING.get('HEDGE_MODE', 'units'),
            sleep=scenario_sleep,
            hold_sec=spec.get('hold_sec', scenarios.DEFAULT_HOLD_SEC))
        try:
            outcome = runner.run(spec['type'], spec.get('mode', 'MARKET'),
                                 spec.get('variant', 'normal'))
        except Exception as e:
            logging.error("Scenario %s blew up: %s", name, e)
            return finish(False, f'Scenario error: {e}')
        return finish(outcome['success'], outcome['detail'])

    def _arm_manual(self, open_cmd):
        """Park a manual trade until the spread reaches the operator's
        entry level. The level geometry is checked HERE too, not only
        when it fires: an armed order can sit for hours, and finding
        out the stop was upside-down at the moment of execution is too
        late to be told about it."""
        try:
            signal_type = SignalType(open_cmd.get('direction', ''))
        except ValueError:
            return self._manual_reject(
                f"bad direction {open_cmd.get('direction')!r}")
        entry = float(open_cmd['entry_spread'])
        bad = self.check_manual_levels(signal_type, entry,
                                       open_cmd.get('exit_spread'),
                                       open_cmd.get('stop_spread'))
        if bad:
            return self._manual_reject(bad)
        self.manual_order = dict(open_cmd)
        logging.warning(
            "Manual trade ARMED: %s %s at spread %.4f "
            "(TP %s, SL %s, overnight %s)",
            open_cmd['asset'], open_cmd.get('direction'), entry,
            open_cmd.get('exit_spread'), open_cmd.get('stop_spread'),
            open_cmd.get('overnight', 'ALLOW'))
        self.manual_note = {
            'ok': True, 'ts': time.time(),
            'text': f"armed at spread {entry:g} — fires when the spread "
                    f"reaches it, even with the algo stopped"}

    def _check_manual_arm(self, asset_key, market_data):
        """An armed manual trade fires when the spread reaches the
        operator's entry level, in the direction they chose."""
        order = self.manual_order
        if not order or order.get('asset') != asset_key:
            return
        if self.position_manager.get_positions_for_asset(asset_key):
            return                       # already in the trade
        spread = market_data.get('spread')
        entry = order.get('entry_spread')
        if spread is None or entry is None:
            return
        direction = order.get('direction', '')
        # SELL_BASIS profits as the spread falls -> arm above the level
        reached = (spread >= float(entry) if direction == 'SELL_BASIS'
                   else spread <= float(entry))
        if not reached:
            return
        logging.warning("Manual trade TRIGGERED: spread %.4f reached %.4f",
                        spread, float(entry))
        self.manual_order = None
        self._manual_open(asset_key, direction, order.get('lots'),
                          exit_spread=order.get('exit_spread'),
                          stop_spread=order.get('stop_spread'),
                          overnight=order.get('overnight'))

    def _manual_reject(self, why):
        """Every manual-trade refusal used to end in a log line the
        operator never sees — they press Activate and nothing happens.
        Publish it so the panel can say why."""
        logging.warning("Manual trade rejected: %s", why)
        self.manual_note = {'ok': False, 'text': why, 'ts': time.time()}
        return None

    @staticmethod
    def check_manual_levels(signal_type, entry, take_profit, stop):
        """Are the operator's three levels the right way round? One
        rule, shared with the HTTP API and the browser (webapi), so a
        trade cannot be refused in one place and accepted in another.
        Returns an error string, or None when the geometry is sound."""
        return webapi.manual_level_error(
            signal_type.value if hasattr(signal_type, 'value')
            else signal_type, entry, take_profit, stop)

    def _manual_open(self, asset_key, direction, lots=None,
                     exit_spread=None, stop_spread=None, overnight=None):
        """Manual spread trade from the web UI: bypasses SIGNAL gates
        (z, trend, cooldowns, edge filter) but NEVER risk limits or
        circuit breakers. Exits are managed by the normal ladder plus
        whichever take-profit/stop SPREAD levels the operator named."""
        if asset_key not in self.active_assets:
            return self._manual_reject(f"unknown asset {asset_key}")
        try:
            signal_type = SignalType(direction)
        except ValueError:
            return self._manual_reject(f"bad direction {direction!r}")
        if signal_type not in (SignalType.SELL_BASIS,
                               SignalType.BUY_BASIS):
            return self._manual_reject(f"direction {direction} is not a "
                                       f"spread trade")

        halted, why = self.risk_manager.halted()
        if halted:
            return self._manual_reject(f"circuit breaker ({why})")
        lots = float(lots or self.config.TRADING.get('CLIP_LOTS', 1.0))
        if lots > self.config.RISK_LIMITS['MAX_LOT_SIZE']:
            return self._manual_reject(
                f"{lots:g} lots exceeds MAX_LOT_SIZE "
                f"{self.config.RISK_LIMITS['MAX_LOT_SIZE']:g}")
        active = self.position_manager.get_positions_for_asset(asset_key)
        if len(active) >= self.config.RISK_LIMITS['MAX_POSITIONS_PER_ASSET']:
            return self._manual_reject("max positions per asset reached")

        market_data = self.get_market_data(asset_key) \
            or self.active_assets[asset_key]['last_data']
        if not market_data:
            return self._manual_reject("no market data")

        # Levels are checked against the price we are actually filling
        # at, not the level that armed the order — the spread has moved
        # since, and the trade lives or dies on where it opens.
        bad = self.check_manual_levels(signal_type, market_data.get('spread'),
                                       exit_spread, stop_spread)
        if bad:
            return self._manual_reject(bad)

        contract_size = self.config.ASSETS[asset_key]['lot_size']
        stats = self.stats.get(asset_key)
        logging.warning(
            "MANUAL SPREAD TRADE via web UI: %s %s %.2f lots "
            "(spread %.4f, TP %s, SL %s)",
            asset_key, signal_type.value, lots,
            market_data.get('spread') or 0.0,
            f"{exit_spread:g}" if exit_spread is not None else "engine",
            f"{stop_spread:g}" if stop_spread is not None else "engine")
        self._plan_refusal = None
        position = self._open_position(
            asset_key, signal_type, lots, market_data, stats,
            contract_size, manual=True, exit_spread=exit_spread,
            stop_spread=stop_spread, overnight=overnight)
        if position is None:
            # Say WHY. "Not filled — see the log" sent the operator to
            # a log file to find a decision the engine had already made
            # and could simply have reported.
            self.manual_note = {
                'ok': False, 'ts': time.time(),
                'text': self._plan_refusal
                        or 'the pair did not execute — check the log for '
                           'the broker error'}
        else:
            self.manual_note = {
                'ok': True, 'ts': time.time(),
                'text': f"opened {position.position_id} "
                        f"({signal_type.value}, {lots:g} lots)"}
        return position

    # Balance/equity is one IPC round-trip per account into MT5. The
    # status file is now written on every poll (a few times a second) so
    # the operator's prices are live; account figures do not move at
    # that rate and must not be re-fetched at it. The margin breaker
    # reads this cache, so keep the interval well inside a reconcile.
    ACCOUNT_REFRESH_SEC = 5.0
    STATUS_LOG_SEC = 300.0       # heartbeat fallback; see LOG_HEARTBEAT_SEC
    CONFIG_RELOAD_SEC = 10.0     # hot-apply of the safe config sections

    def _sizing_and_cost(self, asset_key, md, stats):
        """What the next trade would cost and whether the edge covers it.

        The engine already computes all of this inside the edge filter
        on every tick; it was simply never published, so the dashboard's
        cost and sizing cards sat empty. With gold measuring ~1.4 bps of
        combined bid-ask against a sigma far below it, this is the most
        decision-relevant number on the page.
        """
        size = self._sizing_plan(asset_key, md)
        lots = size['leg_a_lots']
        contract = size['leg_a_contract']
        spot_notional = size['leg_a_notional_usd']
        fut_notional = size['leg_b_notional_usd']

        block = {
            'clip_lots': lots, 'contract_size': contract,
            'spot_notional': spot_notional, 'fut_notional': fut_notional,
            'order_mode': str(self.config.EXECUTION.get(
                'ENTRY_STYLE', 'market')).upper(),
            # The whole sizing decision, so the card can show how the
            # lots were arrived at and how balanced the pair ends up.
            'sizing': size,
        }
        try:
            cost = costs_mod.round_trip_cost(
                md, lots, contract, self.config.COSTS,
                lots_b=size.get('leg_b_lots'),
                contract_b=size.get('leg_b_contract'))
            capture = costs_mod.expected_capture(
                stats.z if stats else None,
                stats.sigma if stats else None, lots, contract,
                self.config.COSTS)
        except Exception:
            return block

        # Per leg, in ITS OWN lots — the same split round_trip_cost
        # uses. A combined "x lots" figure is meaningless once the two
        # legs trade different lot counts.
        lots_b = size.get('leg_b_lots') or lots
        contract_b = size.get('leg_b_contract') or contract
        factor = self.config.COSTS.get('SPREAD_COST_FACTOR', 1.0)
        spot_spread = (md.get('spot_ask') or 0) - (md.get('spot_bid') or 0)
        fut_spread = ((md.get('futures_ask') or 0)
                      - (md.get('futures_bid') or 0))
        leg_a_cost = spot_spread * lots * contract * factor
        leg_b_cost = fut_spread * lots_b * contract_b * factor
        commission_a = (self.config.COSTS.get('COMMISSION_PER_LOT_SPOT', 0.0)
                        * lots)
        commission_b = (self.config.COSTS.get('COMMISSION_PER_LOT_FUT', 0.0)
                        * lots_b)
        commissions = commission_a + commission_b
        # Quoted against ONE leg's notional, the convention a pair trade
        # is normally costed in — and the same basis as "combined
        # bid-ask in bps".
        denom = spot_notional or 1.0
        # What the pre-trade balance guard would require to open this
        # clip. Knowable with a FLAT book, which is exactly when the
        # operator needs it: it answers "can this account afford the
        # configured CLIP_LOTS?" before any order is sent. Unlevered
        # legs are treated as cash (leverage 1), matching how the
        # sizing card labels them.
        exits = self.config.EXITS
        shared = exits.get('LEVERAGE', 0) or 0
        spot_lev = exits.get('SPOT_LEVERAGE', 0) or shared or 1.0
        fut_lev = exits.get('FUT_LEVERAGE', 0) or shared or 1.0
        buffer_pct = exits.get('M2M_BUFFER_PCT', 0) or 0
        block.update({
            'spot_margin': spot_notional / spot_lev,
            'fut_margin': fut_notional / fut_lev,
            'capital_required': ((spot_notional / spot_lev
                                  + fut_notional / fut_lev)
                                 * (1 + buffer_pct / 100.0)),
            'capital_buffer_pct': buffer_pct,
        })
        block.update({
            'rt_cost_usd': cost,
            'rt_fees_usd': commissions,
            'rt_spread_usd': max(cost - commissions, 0.0),
            'rt_cost_bps': cost / denom * 1e4,
            'rt_fees_bps': commissions / denom * 1e4,
            # The inputs, so the card can show the derivation rather
            # than a bare number the operator has to take on trust.
            'rt_spot_spread': ((md.get('spot_ask') or 0)
                               - (md.get('spot_bid') or 0)),
            'rt_fut_spread': ((md.get('futures_ask') or 0)
                              - (md.get('futures_bid') or 0)),
            'rt_spread_factor': self.config.COSTS.get(
                'SPREAD_COST_FACTOR', 1.0),
            'rt_commission_per_lot': (
                self.config.COSTS.get('COMMISSION_PER_LOT_SPOT', 0.0)
                + self.config.COSTS.get('COMMISSION_PER_LOT_FUT', 0.0)),
            # Split per leg, and named, so the card can show a line per
            # thing actually paid rather than one combined figure the
            # operator has to take apart to check against a broker's
            # schedule.
            'rt_commission_spot': self.config.COSTS.get(
                'COMMISSION_PER_LOT_SPOT', 0.0),
            'rt_commission_fut': self.config.COSTS.get(
                'COMMISSION_PER_LOT_FUT', 0.0),
            'rt_spot_symbol': (self.active_assets.get(asset_key) or {})
            .get('spot_symbol'),
            'rt_fut_symbol': (self.active_assets.get(asset_key) or {})
            .get('futures_symbol'),
            'rt_units': lots * contract,
            # Each leg's OWN figures, computed HERE with the same
            # arithmetic as the cost model. The card used to multiply
            # both legs by one contract size and one lot count of its
            # own, which is how it printed "XAUUSD 0.2200 x 5000" for a
            # 100-unit contract. It now renders these rather than
            # deriving anything, so display and model cannot diverge.
            'rt_contract_a': contract, 'rt_contract_b': contract_b,
            'rt_lots_a': lots, 'rt_lots_b': lots_b,
            'rt_leg_a_cost': leg_a_cost, 'rt_leg_b_cost': leg_b_cost,
            'rt_commission_a': commission_a,
            'rt_commission_b': commission_b,
            # Per ONE lot, and the lot count separately. The operator
            # reasons about a single lot ("one round trip costs me
            # $120") and then scales it; a combined figure at 6.41 lots
            # is a number they have to divide before it means anything.
            'rt_cost_per_lot': (cost / lots) if lots else None,
            'rt_lots': lots,
            'rt_contract_size': contract,
            'capture_usd': capture,
            'edge_ratio': (capture / cost) if cost else None,
            'edge_required': self.config.COSTS.get('MIN_EDGE_MULTIPLE', 1.5),
            # The edge test stated in MONEY. A bare "0.15x vs 1.5x" says
            # a trade was refused without saying by how much, and the
            # gap is the whole decision: $62 of capture against $615 of
            # requirement is a different conversation from $600 vs $615.
            'edge_capture_fraction': self.config.COSTS.get(
                'TARGET_FRACTION', 0.5),
            'edge_z': (abs(stats.z) if stats and stats.z is not None
                       else None),
            'edge_sigma': stats.sigma if stats else None,
            # Per lot as well as scaled, for the same reason the round
            # trip is: "$5 of capture against $64 of cost" is a
            # comparison an operator can hold in their head, and it
            # does not change when the position size does.
            'edge_capture_per_lot': (capture / lots) if lots else None,
            # The verdict itself. The Filters card has an Edge badge
            # that reads this; nothing ever published it, so the badge
            # showed "-" while the table directly beneath it spelled out
            # the whole shortfall. None (not False) without a usable z:
            # "not measured yet" is a different statement from "the
            # edge failed", and warm-up should not read as a rejection.
            'edge_ok': (
                None if (stats is None or stats.z is None)
                else capture >= cost * self.config.COSTS.get(
                    'MIN_EDGE_MULTIPLE', 1.5)),
            'edge_required_usd': (
                cost * self.config.COSTS.get('MIN_EDGE_MULTIPLE', 1.5)),
            'edge_gap_usd': (
                capture - cost * self.config.COSTS.get(
                    'MIN_EDGE_MULTIPLE', 1.5)),
        })
        return block

    def _account_snapshot(self):
        """(accounts, equity), refreshed at most every few seconds."""
        now = time.time()
        if (self._accounts_cache is not None
                and now - self._accounts_at < self.ACCOUNT_REFRESH_SEC):
            return self._accounts_cache
        roles = {}
        for role, leg in (('spot', self.spot_leg),
                          ('futures', self.futures_leg)):
            roles.setdefault(leg.name, []).append(role)
        accounts, equity = {}, None
        for leg in self._each_leg():
            info = leg.account_info()
            if info:
                info = dict(info)
                info['account'] = leg.name
                info['roles'] = roles.get(leg.name, [])
                accounts[leg.name] = info
                equity = (equity or 0) + (info.get('equity') or 0)
        # The margin breaker acts on the weakest account, so it gets the
        # per-account picture every time this actually refreshes.
        self.risk_manager.update_accounts(accounts)
        self._accounts_cache = (accounts, equity)
        self._accounts_at = now
        return self._accounts_cache

    def _write_runtime_status(self, all_market_data):
        """Refresh runtime_status.json for the read-only dashboard.
        Atomic replace so the dashboard never reads a half-written file."""
        halted, why = self.risk_manager.halted()
        target = self.config.TRADING.get('DAILY_LOT_TARGET', 0)
        assets = []
        for asset_key, md in all_market_data.items():
            stats = self.stats.get(asset_key)
            age_ms = (datetime.now() - md['timestamp']).total_seconds() * 1000
            assets.append({
                'asset': asset_key,
                'z': stats.z if stats else None,
                'mu': stats.mu if stats else None,
                'sigma': stats.sigma if stats else None,
                'half_life_min': ((stats.half_life_sec / 60)
                                  if stats and stats.half_life_sec else None),
                'samples': len(stats.samples) if stats else 0,
                # The window count is an occupancy, not a total, so it
                # falls when quotes slow down. Publish the RATE beside
                # it — that is the quantity actually changing, and the
                # early warning for a window going thin.
                'quote_rate_per_min': (stats.quote_rate_per_min
                                       if stats else None),
                'min_samples': self.config.SIGNALS.get('MIN_SAMPLES'),
                'lookback': self.config.SIGNALS.get('LOOKBACK_SEC'),
                'suggested_lookback_sec': (stats.suggested_lookback_sec
                                           if stats else None),
                'history_sec': stats.history_sec if stats else 0.0,
                'min_history_sec': (stats.min_history_sec if stats else 0.0),
                'degenerate': bool(stats.degenerate) if stats else False,
                'trend_slope': (stats.trend_slope() if stats else None),
                # A direct reading of the AR(1) fit we already do:
                # _estimate_half_life returns None when phi <= 0 or
                # phi >= 1, i.e. when the window is NOT mean-reverting.
                # No separate model, and no invented number.
                'regime': ('COLLECTING' if not (stats and stats.warm)
                           else 'MEAN_REVERTING'
                           if (stats and stats.half_life_sec)
                           else 'TRENDING'),
                'basis': md['actual_basis'],
                'spread': md['spread'],
                'hedge_ratio': md.get('hedge_ratio', 1.0),
                'spread_formula': md.get('spread_formula'),
                # Reference only — see fairvalue.py.
                'pair_type': md.get('pair_type'),
                'fair_value': md.get('fair_value'),
                'fair_gap': md.get('fair_gap'),
                'fair_detail': md.get('fair_detail'),
            })
            assets[-1].update(self._sizing_and_cost(asset_key, md, stats))
            assets[-1].update({
                'spot_price': md['spot_price'],
                'spot_bid': md['spot_bid'], 'spot_ask': md['spot_ask'],
                'futures_price': md['futures_price'],
                'fut_bid': md['futures_bid'], 'fut_ask': md['futures_ask'],
                'tick_age_ms': age_ms,
                'lots_today': self.risk_manager.lots_traded_today(asset_key),
                'lot_target': target,
            })
        accounts, equity = self._account_snapshot()
        now_mono = time.time()
        write_ms = (round((now_mono - self._last_status_write) * 1000)
                    if self._last_status_write else None)
        self._last_status_write = now_mono
        payload = {
            'mode': self.trading_mode,
            # Milliseconds matter: the webapp's socket bridge only emits
            # when this stamp CHANGES, so a whole-second stamp would cap
            # the dashboard at one price update per second.
            'updated': datetime.now().strftime('%H:%M:%S.%f')[:-3],
            # Measured, not configured: how long since the previous
            # write. If this reads 300ms and the screen still looks
            # slow, the engine is fine and the browser end is the
            # problem — and vice versa.
            'write_interval_ms': write_ms,
            'poll_interval_sec': self.config.TRADING.get(
                'POLL_INTERVAL_SEC', 0.3),
            'algo_enabled': self.algo_enabled,
            'halted': halted,
            'halt_reason': why,
            'daily_pnl': self.risk_manager.daily_realized_pnl,
            'accounts': accounts,
            'equity': equity,
            'tick_age_ms': min((a['tick_age_ms'] for a in assets),
                               default=None),
            'assets': assets,
            'positions': self._position_snapshot(),
            'shadow': {'active': len(self.shadow.active),
                       'tracking': self.shadow.snapshot()},
            'test_results': self._test_results,
            'scenario_result': self._scenario_result,
            'order_confirmation': self._last_confirmation,
            'diagnostics': self._diagnostics,
            'symbol_search': self._symbol_search,
            'margin_breaker': self._margin_breaker_state(),
            'manual_order': self.manual_order,
            'manual_note': self.manual_note,
            # The same what-is-working breakdown the log prints, so a
            # UI can show it without re-deriving any of the verdicts.
            'health': {key: [
                {'subsystem': name, 'state': state, 'detail': detail}
                for name, state, detail in self._health(key, md)]
                for key, md in (all_market_data or {}).items()},
        }
        try:
            tmp = self.status_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
            os.replace(tmp, self.status_path)
        except OSError as e:
            logging.debug("Could not write runtime status: %s", e)

    def _confirm_with_mt5(self, position, what='entry'):
        """After we place orders, read the tickets back OUT of MT5 and
        say so in the log. `order_send` returning success is only the
        broker acknowledging the request; this is its RECORD of the
        deal — the difference matters when an operator is asking
        whether the orders really went to the terminal."""
        confirmations = []
        for leg, trade in ((self.spot_leg, position.spot_trade),
                           (self.futures_leg, position.futures_trade)):
            verifier = getattr(leg, 'verify_order', None)
            if verifier is None:
                continue
            for ticket in (trade.position_tickets or []):
                try:
                    found = verifier(ticket)
                except Exception as e:
                    found = {'ticket': ticket, 'confirmed': False,
                             'error': str(e)}
                confirmations.append(found)
                if found.get('confirmed'):
                    deals = found.get('deals') or []
                    detail = (f"deal {deals[-1]['deal_id']} order "
                              f"{deals[-1]['order_id']} "
                              f"{deals[-1]['volume']} @ {deals[-1]['price']}"
                              if deals else
                              f"{found.get('volume')} @ {found.get('price')}")
                    logging.info(
                        "[MT5 CONFIRMED] %s %s ticket %s on %s — %s",
                        position.position_id, what, ticket, leg.name, detail)
                else:
                    logging.error(
                        "[MT5 NOT CONFIRMED] %s %s ticket %s on %s — %s. "
                        "The engine thinks this order exists but the "
                        "terminal has no record of it.",
                        position.position_id, what, ticket, leg.name,
                        found.get('error', 'no record'))
        if confirmations:
            self._last_confirmation = {
                'position_id': position.position_id, 'what': what,
                'at': datetime.now().strftime('%H:%M:%S'),
                'confirmed': sum(1 for c in confirmations
                                 if c.get('confirmed')),
                'total': len(confirmations),
                'tickets': confirmations,
            }
        # The rows are in MT5 now — pull them straight into the
        # Exchange Order Log instead of waiting for the 30s poll.
        self._poll_order_logs(interval=0, hours=1)
        return confirmations

    def _note_server_clock(self, leg_name, rows):
        """Say once, in the log, how far each broker's clock sits from
        ours. "The MT5 History is not matching the Exchange Order Log"
        is most often this and nothing else, and it is invisible until
        someone states the number.

        Compared to the MINUTE, not the second. The offset is measured
        as `tick.time - time.time()`, and a tick stamp has one-second
        resolution against a continuous clock, so the raw number
        alternates between (say) 10800 and 10799 from one poll to the
        next. Dedup on the exact value therefore never matched and the
        line reprinted every 30s at WARNING — the same log flood the
        operator asked to be rid of, wearing a different hat. A clock
        offset that genuinely CHANGES (a DST roll) moves by 30 minutes
        or more, so the minute is the honest resolution and a change at
        that resolution is worth a WARNING."""
        offset = next((r.get('server_offset_sec') for r in rows
                       if r.get('server_offset_sec') is not None), None)
        if offset is None:
            return
        minutes = int(round(offset / 60.0))
        if self._server_clock.get(leg_name) == minutes:
            return
        first = leg_name not in self._server_clock
        self._server_clock[leg_name] = minutes
        local = -time.timezone if not time.localtime().tm_isdst \
            else -time.altzone
        logging.log(logging.INFO if first else logging.WARNING,
                    "Leg '%s': broker clock is UTC%+.1fh; this machine is "
                    "UTC%+.1fh. MT5's History shows broker time, and so "
                    "does the Exchange Order Log.",
                    leg_name, minutes / 60.0, local / 3600.0)

    def _poll_order_logs(self, interval=30.0, hours=24):
        """Pull each account's raw MT5 order/deal activity into the
        broker_orders table so the dashboard's Exchange Order Log shows
        BOTH accounts side by side. The web app is a separate process
        and never touches MT5, so the coordinator is the only place
        this can be read from.

        Manual trades placed in the terminal are included on purpose —
        an operator eyeballing the log wants to see everything the
        account did, with `is_bot` distinguishing ours."""
        now = time.time()
        if now - self._last_order_log < interval:
            return 0
        self._last_order_log = now

        rows, polled = [], set()
        for leg in self._each_leg():
            try:
                fetched = leg.order_log(hours)
            except Exception as e:
                logging.warning("Order log unavailable for leg '%s': %s",
                                leg.name, e)
                continue
            if fetched is None:        # IPC failure — not "no activity"
                logging.debug("Order log unreadable for leg '%s'", leg.name)
                continue
            rows.extend(fetched)
            polled.add(leg.name)
            self._note_server_clock(leg.name, fetched)

        if not polled:
            return 0
        try:
            return self.data_logger.record_broker_orders(rows, polled)
        except Exception as e:
            logging.error("Could not persist order log: %s", e)
            return 0

    # -- Telegram command handlers (W3 command set, MT5-adapted) --------

    def _margin_breaker_state(self):
        limits = self.config.RISK_LIMITS
        weakest = self.risk_manager.weakest_margin()
        halted, why = self.risk_manager.margin_halt()
        return {
            'enabled': bool(limits.get('MARGIN_BREAKER_ENABLED')),
            'halt_level': limits.get('MARGIN_HALT_LEVEL'),
            'reduce_enabled': bool(limits.get('MARGIN_REDUCE_ENABLED')),
            'reduce_level': limits.get('MARGIN_REDUCE_LEVEL'),
            'weakest_account': weakest[0] if weakest else None,
            'weakest_level': weakest[1] if weakest else None,
            'size_multiplier': self.risk_manager.margin_size_multiplier(),
            'halted': halted, 'reason': why,
        }

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
                f"spread {md['spread']:+.2f} | "
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

    # WARN is deliberately NOT in the "held up by" set: it reports
    # something worth reading that is not stopping anything, so calling
    # it blocked would send the operator hunting for a halt that isn't
    # there.
    OK, BLOCKED, FAILED, IDLE, WARN = 'OK', 'BLOCKED', 'FAILED', '--', 'WARN'

    def _health(self, asset_key, md):
        """What is working and what is not, subsystem by subsystem.

        Operator, 2026-08-07: "I am more interested to get details on
        what is working and what is not working". A price tick repeated
        every few seconds never answered that; this does, in one block,
        naming the thing that is stopping the engine trading rather
        than leaving it to be inferred from the absence of trades.

        Returns [(subsystem, state, detail)] — detail carries the live
        numbers, state carries the verdict, and only the STATE is used
        to decide whether to reprint (see log_status), so a drifting
        sigma does not count as news.
        """
        cfg = self.config.SIGNALS
        stats = self.stats.get(asset_key)
        rows = []

        # -- feed --
        age_ms = md.get('tick_age_ms')
        rate = stats.quote_rate_per_min if stats else None
        if age_ms is not None and age_ms > 10_000:
            rows.append(('feed', self.FAILED,
                         f'no tick for {age_ms / 1000:.0f}s — '
                         f'check both terminals'))
        elif rate is not None and rate < 6:
            rows.append(('feed', self.BLOCKED,
                         f'only {rate:.0f} quotes/min — too thin to '
                         f'trust a standard deviation'))
        else:
            rows.append(('feed', self.OK,
                         f'{rate:.0f} quotes/min' if rate
                         else 'ticking'))

        # -- beta sanity --
        beta_problem = self._implausible_spread(md)
        if beta_problem:
            rows.append(('beta', self.BLOCKED, beta_problem))

        # -- pair definition --
        # Reference only, so it never blocks; but a basis label that
        # cannot describe this pair belongs in the log rather than
        # only on a card nobody is looking at.
        if md.get('fair_warning'):
            rows.append(('pair', self.WARN, md['fair_warning']))

        # -- statistics --
        if stats is None:
            rows.append(('stats', self.FAILED, 'no window for this asset'))
        elif stats.warm:
            rows.append(('stats', self.OK,
                         f'warm — mu {stats.mu:.4f}, sigma '
                         f'{stats.sigma:.4f}'))
        elif stats.degenerate and len(stats.samples) >= \
                cfg.get('MIN_SAMPLES', 0):
            rows.append(('stats', self.BLOCKED,
                         'enough quotes but the spread has barely moved '
                         '— sigma too small for a usable z'))
        else:
            need_hist = stats.min_history_sec - stats.history_sec
            need_qty = cfg.get('MIN_SAMPLES', 0) - len(stats.samples)
            # Round UP: a gate with 20 seconds left is not "0 more
            # minutes needed", which reads as the counter being broken.
            gate = (f'{math.ceil(need_hist / 60):.0f} more minutes'
                    if need_hist > 0 else f'{need_qty:.0f} more quotes')
            rows.append(('stats', self.BLOCKED,
                         f'still collecting — {gate} needed'))

        # -- sizing --
        size = self._sizing_plan(asset_key, md)
        if size.get('reason'):
            rows.append(('sizing', self.BLOCKED, size['reason']))
        else:
            rows.append(('sizing', self.OK,
                         f"{size['leg_a_lots']:g} / {size['leg_b_lots']:g} "
                         f"lots, {size['imbalance_pct']:+.1f}% balance"))

        # -- entries --
        held = len(self.position_manager.get_positions_for_asset(asset_key))
        halted, why = self.risk_manager.halted()
        blocking = getattr(self.z_gen, '_blocking', {}).get(asset_key)
        if not self.algo_enabled:
            rows.append(('entries', self.IDLE,
                         'algo stopped (exits and armed manual trades '
                         'still run)'))
        elif halted:
            rows.append(('entries', self.BLOCKED, f'circuit breaker: {why}'))
        elif held:
            rows.append(('entries', self.IDLE,
                         f'{held} position(s) already open'))
        elif blocking:
            rows.append(('entries', self.BLOCKED, f'{blocking} gate'))
        elif stats is not None and stats.warm and stats.z is not None:
            rows.append(('entries', self.OK,
                         f'armed — z {stats.z:+.2f}, need '
                         f"|z| >= {cfg['ENTRY_Z']:g}"))
        else:
            rows.append(('entries', self.BLOCKED, 'waiting on statistics'))

        # -- exits --
        stuck = [p for p in self.position_manager
                 .get_positions_for_asset(asset_key).values()
                 if getattr(p, 'close_failures', 0)]
        if stuck:
            worst = max(stuck, key=lambda p: p.close_failures)
            rows.append(('exits', self.FAILED,
                         f'{len(stuck)} position(s) WILL NOT CLOSE — '
                         f'{worst.position_id} failed '
                         f'{worst.close_failures}x: '
                         f'{worst.last_close_error}. Still open at the '
                         f'broker; close by hand if this persists'))
        else:
            rows.append(('exits', self.OK if held else self.IDLE,
                         f'{held} position(s) being managed' if held
                         else 'flat'))

        # -- risk --
        target = self.config.TRADING.get('DAILY_LOT_TARGET', 0)
        done = self.risk_manager.lots_traded_today(asset_key)
        rows.append(('risk', self.BLOCKED if halted else self.OK,
                     why if halted else
                     (f'no breaker, {done:.0f}/{target:.0f} lots today'
                      if target else 'no breaker tripped')))
        return rows

    def _status_state(self, asset_key, md):
        """The verdicts only — what changing would be news. Live
        numbers move on every tick and must not trigger a reprint."""
        return tuple(state for _, state, _ in self._health(asset_key, md))

    def _status_line(self, asset_key, md, prefix=''):
        """One health block. Prices first, because they anchor the
        rest, then every subsystem with its verdict and the reason it
        holds — so "what is working and what is not" is readable
        without inferring it from the absence of trades."""
        rows = self._health(asset_key, md)
        blocked = [name for name, state, _ in rows
                   if state in (self.BLOCKED, self.FAILED)]
        headline = ('all systems go' if not blocked
                    else 'held up by: ' + ', '.join(blocked))
        logging.info(
            "%s%s spot %.2f | fut %.2f | spread %+.2f — %s",
            prefix, asset_key, md['spot_price'], md['futures_price'],
            md['spread'], headline)
        for name, state, detail in rows:
            logging.info("    %-8s %-7s %s", name, state, detail)

    def log_status(self, all_market_data, heartbeat=False):
        """Log what CHANGED, plus an occasional heartbeat.

        Operator, 2026-08-07: "in the log lets only have relevant and
        important live updates and information - not every second
        updates". A fixed cadence line wrote the same sentence 360
        times an hour with only the prices differing — and the prices
        are on the dashboard, live, which the log is not competing
        with. What the log is for is the record of what happened: went
        warm, lost the feed, started/stopped signalling, halted. Those
        are logged the moment they occur; the heartbeat exists only so
        a quiet log still proves the engine is alive.
        """
        for asset_key, md in all_market_data.items():
            state = self._status_state(asset_key, md)
            changed = self._last_status_state.get(asset_key) != state
            if not changed and not heartbeat:
                continue
            self._last_status_state[asset_key] = state
            self._status_line(asset_key, md, '' if changed else '[heartbeat] ')

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def run(self):
        self.is_running = True
        poll = self.config.TRADING.get('POLL_INTERVAL_SEC', 0.3)
        consecutive_errors = 0
        # Housekeeping runs on the CLOCK, not on a loop count — otherwise
        # changing the poll rate silently changes how often the status
        # line is logged and how often config is re-read.
        last_log = last_reload = 0.0

        logging.info("Coordinator loop started (poll %.2fs)", poll)
        while self.is_running:
            try:
                started = time.time()

                all_market_data = self.get_all_market_data()
                if all_market_data:
                    consecutive_errors = 0
                    self.last_data = all_market_data
                    for asset_key, md in all_market_data.items():
                        self.process_asset(asset_key, md)
                    # Every poll: the dashboard reads this file, so its
                    # prices are only as live as this write.
                    self._write_runtime_status(all_market_data)
                    # Transitions go out immediately; the heartbeat is
                    # deliberately rare (LOG_HEARTBEAT_SEC, 5 min).
                    beat = (started - last_log
                            >= self.config.TRADING.get('LOG_HEARTBEAT_SEC',
                                                       self.STATUS_LOG_SEC))
                    if beat:
                        last_log = started
                    self.log_status(all_market_data, heartbeat=beat)
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
                if started - last_reload >= self.CONFIG_RELOAD_SEC:
                    last_reload = started
                    self._maybe_reload_config()
                self._poll_order_logs()

                if self.reconciler and self.trading_mode == "LIVE" \
                        and self.reconciler.due():
                    for action, leg_name, detail in self.reconciler.check():
                        self.notifier.notify_reconcile(action, leg_name,
                                                       str(detail))

                # Re-read each pass: POLL_INTERVAL_SEC is hot-reloadable
                # and a value captured before the loop could never apply.
                poll = self.config.TRADING.get('POLL_INTERVAL_SEC', 0.3)
                time.sleep(max(poll - (time.time() - started), 0.02))

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
        # State the size the engine will ACTUALLY use. This banner is
        # the last thing between the operator and real orders, and it
        # printed "Clip size: 50.0 lots/leg" on a box running notional
        # sizing at 1.15 lots — a 43x overstatement on the one screen
        # that exists to make the operator stop and check.
        target = config.TRADING.get('DAILY_LOT_TARGET', 0)
        if config.TRADING.get('SIZING_MODE', 'lots') == 'notional':
            size = (f"Sizing: ${config.TRADING.get('NOTIONAL_PER_LEG_USD', 0):,.0f}"
                    f" notional/leg (lots derived from live price)")
        else:
            size = f"Clip size: {config.TRADING.get('CLIP_LOTS', 1.0)} lots/leg"
        print(f"{size} | Daily target: {target} lots")
        if not args.yes and input(
                "Type 'START' to begin live trading: "
                ).strip().upper() != "START":
            print("Cancelled")
            sys.exit(0)

    try:
        coordinator = Coordinator(config, trading_mode=mode,
                                  config_path=args.config)
    except ValueError as e:
        # Configuration the operator can fix — say so plainly instead
        # of printing a traceback into the launcher window.
        logging.error("Cannot start: %s", e)
        print(f"\nCannot start: {e}\n")
        sys.exit(1)
    if not coordinator.start():
        print("\nStartup failed. The log above says which check failed — "
              "usually a symbol that does not exist on that account, or a "
              "terminal that is not logged in.\n"
              "Run  python check_mt5.py  for a full report.\n")
        sys.exit(1)

    try:
        coordinator.run()
    except KeyboardInterrupt:
        coordinator.stop()


if __name__ == '__main__':
    main()
