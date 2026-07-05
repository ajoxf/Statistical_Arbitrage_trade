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
                 password_env="", server=None):
        self.name = name
        self.terminal_path = terminal_path
        self.login = int(login) if login else None
        self.password_env = password_env
        self.server = server

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
        }
        self.EXECUTION = {
            'SLIPPAGE_TOLERANCE': 1.0,
            'ORDER_TIMEOUT': 30,
            'MIN_TIME_BETWEEN_SIGNALS': 180,
            'RETRY_ATTEMPTS': 3,
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
                          ('execution', 'EXECUTION')]:
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
