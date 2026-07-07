"""Configuration: file-based with sane defaults.

Credentials never live in the JSON file — each account names an
environment variable (password_env) that holds its password. Copy
.env.example to .env and config.example.json to config.json.
"""

import copy
import json
import logging
import os
from datetime import datetime
from typing import Optional

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv optional; env vars can be set by the shell
    load_dotenv = None


class AccountConfig:
    """One MT5 account/terminal."""

    def __init__(self, name, terminal_path=None, login=None,
                 password_env="", server=None, endpoint=None):
        self.name = name
        self.terminal_path = terminal_path
        self.login = int(login) if login else None
        self.password_env = password_env
        self.server = server
        # host:port where this account's leg runner listens. Accounts
        # with an endpoint are driven by a separate process (required
        # for two simultaneous MT5 connections); without one, the
        # coordinator connects to the terminal in-process.
        self.endpoint = endpoint

    @property
    def password(self) -> Optional[str]:
        return os.environ.get(self.password_env) if self.password_env else None


DEFAULT_ASSETS = {
    'GOLD': {
        'name': 'GOLD',
        'spot_symbols': ['XAUUSD_', 'XAUUSD', 'GOLD'],
        'futures_symbols': ['GC1225', 'XAUUSD.f', 'GCZ4'],
        'futures_expiry': datetime(2025, 11, 26),
        'risk_free_rate': 0.0425,
        'multiplier': 1.0,
        'lot_size': 100,      # oz per lot (contract size)
        'swap_charge': 0.0,
        'enabled': True,
    },
    'SILVER': {
        'name': 'SILVER',
        'spot_symbols': ['XAGUSD_', 'XAGUSD', 'SILVER'],
        'futures_symbols': ['SI1225', 'XAGUSD.f', 'SIU4'],
        'futures_expiry': datetime(2025, 11, 26),
        'risk_free_rate': 0.0425,
        'multiplier': 1.0,
        'lot_size': 5000,     # oz per lot (contract size)
        'swap_charge': 0.0,
        'enabled': True,
    },
}


class AlgoTradingConfig:
    """All tunables for the trading system."""

    def __init__(self):
        self.SIGNAL_THRESHOLDS = {
            'PREMIUM_ENTRY': 20.0,
            'PREMIUM_EXIT': 5.0,
            'DISCOUNT_ENTRY': -15.0,
            'DISCOUNT_EXIT': -5.0,
        }
        self.RISK_LIMITS = {
            'MAX_POSITIONS_PER_ASSET': 3,
            'MAX_LOT_SIZE': 1.0,
            'MAX_DAILY_TRADES': 20,
            'STOP_LOSS_PCT': 5.0,
            'MAX_EXPOSURE_USD': 100000,
            # Circuit breakers
            'DAILY_MAX_LOSS_USD': 0.0,   # 0 = off
            'LOSS_STREAK_REDUCE': 3,     # cut size after N straight losses
            'STREAK_SIZE_CUT': 0.2,      # -20% clip size
            'LOSS_STREAK_PAUSE': 6,      # halt entries after N straight
        }
        self.EXECUTION = {
            'SLIPPAGE_TOLERANCE': 1.0,
            'ORDER_TIMEOUT': 30,
            'MIN_TIME_BETWEEN_SIGNALS': 180,
            'RETRY_ATTEMPTS': 3,
            # Limit-first execution (saves the spread when it fills):
            # rest at the peg -> re-peg via order-modify -> on timeout
            # cancel, verify fills, cross the remainder at market.
            'ENTRY_STYLE': 'market',        # 'limit' or 'market'
            'PEG_OFFSET_POINTS': 0.0,       # improve peg by N points
            'REPEG_INTERVAL_SEC': 2.0,      # re-peg cadence while resting
            'LIMIT_TIMEOUT_SEC': 15.0,      # patience: first (spot) leg
            'HEDGE_TIMEOUT_SEC': 4.0,       # patience: hedge leg (short —
                                            # unhedged time is real risk)
            'EXIT_TIMEOUT_SEC': 15.0,       # patience: non-urgent closes
            'ON_TIMEOUT': 'cross',          # 'cross' (market) or 'abort'
            'ORDER_POLL_SEC': 0.5,
            # Keep a partially-hedged position only if the matched size
            # is at least this fraction of the intended clip.
            'MIN_MATCHED_FRACTION': 0.4,
        }
        self.SIGNALS = {
            'USE_Z_SIGNALS': True,     # z-score on swap_diff (fixed
                                       # premium thresholds when False)
            'ENTRY_Z': 3.0,
            'EXIT_Z': 0.5,
            'STOP_Z': 4.5,             # entry ceiling AND stop backstop
            'LOOKBACK_SEC': 7200,
            'STATS_INTERVAL_SEC': 300, # freeze mu/sigma between refreshes
            'MIN_SAMPLES': 300,        # warm-up before any signal
            'TREND_FILTER': True,      # rising S -> SHORT-only, etc.
            'TREND_WINDOW_SEC': 900,
            'ENTRY_COOLDOWN_SEC': 60,
            'STOP_COOLDOWN_SEC': 300,
        }
        self.COSTS = {
            'COMMISSION_PER_LOT_SPOT': 0.0,   # round-turn, per lot
            'COMMISSION_PER_LOT_FUT': 0.0,    # round-turn, per lot
            'SPREAD_COST_FACTOR': 1.0,        # <1 once limit fills prove out
            'MIN_EDGE_MULTIPLE': 1.5,         # capture must be >= this x cost
            'TARGET_FRACTION': 0.5,           # fraction of |z|*sigma targeted
        }
        self.RECONCILE = {
            'SYNC_INTERVAL_SEC': 20,
            'STRIKES': 3,            # consecutive mismatches before acting
        }
        # Token/chat id come from .env (TELEGRAM_BOT_TOKEN,
        # TELEGRAM_CHAT_ID) — these flags only gate what gets sent
        self.TELEGRAM = {
            'ENABLED': True,
            'NOTIFY_TRADES': True,
            'NOTIFY_SIGNALS': True,
            'NOTIFY_ERRORS': True,
            'NOTIFY_SYSTEM': True,
            'COMMANDS': True,        # /status /positions /pnl
        }
        self.EXITS = {
            'USE_SIGMA_TARGET': True,   # TP = TARGET_FRACTION*|z|*sigma*oz
            'TP_USD_PER_LOT': 0.0,      # fallback TP when sigma target off
            'COST_FLOOR_MULT': 1.2,     # TP never below this x round-trip cost
            'STOP_USD_PER_LOT': 30.0,   # catastrophe stop (~30c/oz on gold)
            'RR': 0.3,                  # stop also capped at TP/RR
            'GATE_FLOOR_USD': 0.0,      # reversion exit needs net >= this
            'MAX_HOLD_HALF_LIVES': 4.0,
            'MAX_HOLD_FALLBACK_MIN': 240,
        }
        self.TRADING = {
            'CLIP_LOTS': 1.0,          # lots per entry (per leg)
            'SLICE_LOTS': 0.0,         # child-order size; 0 = no slicing
            'DAILY_LOT_TARGET': 0.0,   # throughput target/day (NOT a cap)
            'HEDGE_RATIO': 1.0,        # futures lots per spot lot
            'POLL_INTERVAL_SEC': 0.5,
        }
        self.ASSETS = copy.deepcopy(DEFAULT_ASSETS)
        # Attaches to whatever terminal is already running when no
        # path/login is configured (legacy single-account behavior).
        self.accounts = {'default': AccountConfig('default')}
        self.leg_accounts = {'spot': 'default', 'futures': 'default'}

    @classmethod
    def from_file(cls, path):
        if load_dotenv:
            load_dotenv()

        cfg = cls()
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)

        for key, attr in [('signal_thresholds', 'SIGNAL_THRESHOLDS'),
                          ('risk_limits', 'RISK_LIMITS'),
                          ('execution', 'EXECUTION'),
                          ('trading', 'TRADING'),
                          ('signals', 'SIGNALS'),
                          ('costs', 'COSTS'),
                          ('exits', 'EXITS'),
                          ('reconcile', 'RECONCILE'),
                          ('telegram', 'TELEGRAM')]:
            if key in raw:
                getattr(cfg, attr).update(raw[key])

        if 'assets' in raw:
            cfg.ASSETS = {}
            for asset_key, a in raw['assets'].items():
                a = dict(a)
                if isinstance(a.get('futures_expiry'), str):
                    a['futures_expiry'] = datetime.fromisoformat(a['futures_expiry'])
                cfg.ASSETS[asset_key] = a

        if 'accounts' in raw:
            cfg.accounts = {
                name: AccountConfig(name, **acct)
                for name, acct in raw['accounts'].items()
            }
        if 'leg_accounts' in raw:
            cfg.leg_accounts = raw['leg_accounts']

        for leg, acct_name in cfg.leg_accounts.items():
            if acct_name not in cfg.accounts:
                raise ValueError(
                    f"leg_accounts.{leg} refers to unknown account '{acct_name}'")

        logging.info("Configuration loaded from %s", path)
        return cfg

    def validate_expiries(self):
        """Warn about expired futures contracts (they disable signals)."""
        stale = [k for k, a in self.ASSETS.items()
                 if a.get('enabled') and a['futures_expiry'] <= datetime.now()]
        for k in stale:
            logging.warning(
                "%s futures_expiry %s is in the past — swap basis will be 0 "
                "and no signals will fire. Update the contract in config.",
                k, self.ASSETS[k]['futures_expiry'].date())
        return stale
