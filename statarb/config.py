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
        'spot_expiry': None,  # Leg A expiry — only a calendar spread
                              # (FUTURE_FUTURE) has one; read from the
                              # terminal, not typed in
        # What the two legs ARE. Decides whether a theoretical fair
        # value exists for the spread — reference only, never a signal
        # input. See fairvalue.py.
        'pair_type': 'SPOT_FUTURE',
        'risk_free_rate': 0.0425,   # annual carry: financing + storage
                                    # - convenience yield. Fair value
                                    # only; nothing else reads it.
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
        'spot_expiry': None,
        'pair_type': 'SPOT_FUTURE',
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
            # Margin breaker (per account — with two brokers each posts
            # its own margin; the WEAKEST account governs).
            'MARGIN_BREAKER_ENABLED': False,   # master on/off
            'MARGIN_HALT_LEVEL': 200.0,        # halt entries below this %
            'MARGIN_MIN_FREE_USD': 0.0,        # or below this free margin
            'MARGIN_REDUCE_ENABLED': False,    # taper clip size first
            'MARGIN_REDUCE_LEVEL': 400.0,      # start tapering below this %
            'MARGIN_MIN_SIZE_FRACTION': 0.25,  # never taper below this
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
            # Quote staleness. A pair trade is only as good as its WORSE
            # leg: the spread is a difference, so one lagging quote makes
            # it fictitious while the other leg ticks perfectly. Live
            # 2026-08-25 a take-profit fired on a spread that existed
            # only in our snapshot and filled 1.51 away, $15.10 of
            # slippage against a $9.40 target.
            #
            # Blocks ENTRIES and PROFIT-taking exits outright. A STOP is
            # only DEFERRED, and only for the grace below — a trade must
            # always have a stop, so an unrefreshed feed cannot become a
            # reason to hold a loser indefinitely.
            #
            # 2.0 was the first guess and it was too tight for a real
            # retail feed: live 2026-08-26 a healthy gold pair running
            # 102 quotes/min still had routine 2.0-2.5s gaps on one leg
            # or the other, so the guard sat on the threshold and
            # flipped continuously. Set it from the `oldest leg` figure
            # on the health line, which is there to be measured.
            'MAX_QUOTE_AGE_SEC': 5.0,       # 0 = off
            'STALE_STOP_GRACE_SEC': 10.0,
        }
        self.SIGNALS = {
            'USE_Z_SIGNALS': True,     # z-score on the spread (fixed
                                       # premium thresholds when False)
            'ENTRY_Z': 3.0,
            'EXIT_Z': 0.5,
            'MAX_ENTRY_Z': 4.5,        # entry ceiling — ALWAYS active;
                                       # entries live in [ENTRY_Z, this).
                                       # Keep the band >= 1 sigma wide.
            'STOP_Z': 4.5,             # z-stop threshold (see EXITS)
            # Exit trigger mode: 'zscore' (z back inside EXIT_Z),
            # 'spread' (spread crosses the mean frozen at entry),
            # 'hybrid' (either)
            'EXIT_MODE': 'zscore',
            'LOOKBACK_SEC': 7200,      # window DURATION in seconds (not
                                       # a tick count — the UI field is
                                       # "Lookback Window (seconds)")
            'LOOKBACK_HALF_LIVES': 6.0,  # suggestion only: the window the
                                         # dashboard proposes = this many
                                         # measured half-lives. Never
                                         # applied automatically.
            'STATS_INTERVAL_SEC': 300, # freeze mu/sigma between refreshes
            'MIN_SAMPLES': 300,        # warm-up: quotes needed
            # ...AND this much elapsed collection time. Owner, 2026-08-06:
            # "take 120 minutes of data - calculate mean and standard
            # deviation before going ahead". A quote COUNT alone is not
            # that: 300 quotes arrive in ~3 minutes on a live gold feed,
            # which is nothing like 2 hours of spread behaviour. Capped
            # at LOOKBACK_SEC (older data is discarded anyway).
            'MIN_HISTORY_SEC': 7200,   # 120 minutes
            # Degenerate-window guards. A quiet spread (or a feed
            # polled faster than it ticks) gives a sigma near zero and
            # a z in the thousands — meaningless, and a merely small
            # sigma puts z inside the entry band on noise.
            'MIN_SIGMA': 0.0,          # absolute floor (0 = off); set it
                                       # once the spread's real sigma is
                                       # known — it is the only guard
                                       # against a SMALL sigma putting z
                                       # inside the entry band on noise
            'MAX_ABS_Z': 25.0,         # beyond this the stats are junk
            # Which spread directions the algo may OPEN. 'both' (the
            # default), 'short' (sell leg B / buy leg A only) or 'long'.
            # Exits are never filtered — a position must always be able
            # to close, whatever the entry rule says today.
            'ALLOWED_DIRECTIONS': 'both',
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
            # Failed orphan closes before escalating instead of
            # retrying every pass forever (a position the broker will
            # not let us close needs a human, not another attempt).
            'CLOSE_ATTEMPTS': 3,
        }
        # Manual Spread Trade (dashboard panel) + overnight handling
        self.MANUAL = {
            'OVERNIGHT_CLOSE_HOUR': 16,     # local broker-session hour
            'OVERNIGHT_CLOSE_MINUTE': 55,
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
            # Take-profit precedence: sigma-fraction > %-of-capital > fixed $
            'USE_SIGMA_TARGET': True,   # TP = TARGET_FRACTION*|z|*sigma*oz
            'TP_CAPITAL_PCT': 0.0,      # TP = pct of capital_at_risk (0=off)
            'TP_USD_PER_LOT': 0.0,      # fixed-$ fallback
            'COST_FLOOR_MULT': 1.2,     # TP never below this x round-trip cost
            # Stop = the TIGHTER of all armed forms
            'STOP_USD_PER_LOT': 30.0,   # catastrophe stop (~30c/oz on gold)
            'STOP_CAPITAL_PCT': 0.0,    # pct of capital_at_risk (0=off)
            'RR': 0.3,                  # stop also capped at TP/RR
            'LEVERAGE': 0.0,            # enables %-capital forms; capital =
                                        # total notional / leverage
            # Per-leg leverage as set BY THE BROKER on each account
            # (MT5 leverage is broker-side; these mirror it so margin
            # and %-of-capital levels are right when the two accounts
            # differ — e.g. 100x spot, 500x futures). 0 = fall back to
            # LEVERAGE for both legs.
            'SPOT_LEVERAGE': 0.0,
            'FUT_LEVERAGE': 0.0,
            'M2M_BUFFER_PCT': 0.0,      # margin buffer on capital_at_risk
            # Reversion gate: floor decays to break-even past 1x max-hold,
            # releases entirely past 2x (deadlock fix — a fully reverted
            # trade must always have an exit)
            'GATE_FLOOR_USD': 0.0,
            'MAX_HOLD_HALF_LIVES': 4.0,
            'MAX_HOLD_FALLBACK_MIN': 240,
            # Floor under the half-life-derived max hold. The AR(1) fit
            # runs on consecutive QUOTES, about 0.6s apart on a live
            # gold feed, so a spread that is mostly tick noise fits a
            # tiny phi and a half-life of a few SECONDS. Live
            # 2026-08-07 that produced max_hold 12s and a hard time
            # stop at 36s: a manual trade with a $215 target was
            # force-closed 37 seconds after entry, paying the full
            # round trip with no chance of ever reaching it. A
            # reversion time shorter than this is a measurement
            # artefact, not a tradable horizon.
            'MIN_MAX_HOLD_SEC': 300.0,
            # Minimum expected value, in dollars, for a SIGNAL entry
            # (0 = off, and off is the default). The plan always
            # publishes its EV; this decides whether the EV can also
            # veto. Manual trades are never vetoed by it.
            'EV_MIN_USD': 0.0,
            # Suppress MAX_HOLD while z-progress toward home >= this,
            # ONLY when a TP exists (never wait for a TP that is off)
            'MAX_HOLD_PROGRESS_SUPPRESS': 0.5,
            # Hard time-stop: close ANY trade at this x max-hold
            # regardless of P&L (0 = off). Covers the sideways loser
            # that has no other clock.
            'HARD_TIME_STOP_MULT': 3.0,
            # Fixed-minutes hard cap (0 = off): exit before the spread
            # starts drifting, e.g. 90 minutes — P&L-agnostic
            'HARD_MAX_HOLD_MIN': 90.0,
            # z-stop demoted to entry-ceiling duty: in-trade risk is
            # DOLLARS. Fail-safe: auto-re-enabled whenever no dollar
            # stop is armed; would-have-fired occasions are logged.
            'Z_STOP_EXIT_ENABLED': False,
        }
        self.TRADING = {
            # 'lots'     -> CLIP_LOTS is the anchor (original behaviour)
            # 'notional' -> NOTIONAL_PER_LEG_USD is the anchor and the
            #               lots are derived from the live price. The
            #               only mode in which "balanced" means anything
            #               across two different instruments, because a
            #               lot is a different amount of money on each.
            'SIZING_MODE': 'lots',
            # How the hedge leg is sized against leg A:
            #   'units'    equal units (ounces/barrels) weighted by
            #              HEDGE_RATIO — the pair's P&L is then exactly
            #              the spread move, correct for a basis trade.
            #   'notional' equal MONEY on both legs — the pair trades
            #              the RETURN spread, correct for two related
            #              instruments with no arbitrage tying them.
            'HEDGE_MODE': 'units',
            'NOTIONAL_PER_LEG_USD': 0.0,   # per LEG, not per pair
            'CLIP_LOTS': 1.0,          # lots per entry (per leg)
            'SLICE_LOTS': 0.0,         # child-order size; 0 = no slicing
            'DAILY_LOT_TARGET': 0.0,   # throughput target/day (NOT a cap)
            # The price coefficient in spread = legB - HEDGE_RATIO*legA.
            # NOT the lot ratio: the hedge is derived from it and the two
            # contract sizes (statarb/sizing.py). Those coincide only at
            # beta 1 with equal contract sizes.
            'HEDGE_RATIO': 1.0,
            # Feed cadence, and therefore the dashboard's: the status
            # file is written once per poll. Matches the dashboard's own
            # 300ms refresh. Safe to run this fast since SpreadStats
            # dedups by quote_id — extra polls no longer touch sigma.
            'POLL_INTERVAL_SEC': 0.3,
            # What a shutdown does to an OPEN position.
            #   'ask'    prompt on the console and wait for an answer
            #            (operator, 2026-08-25) — the default.
            #   'always' close every position at market, the old
            #            unconditional behaviour.
            #   'never'  leave the book alone; restart recovery picks
            #            the position back up.
            # An unanswered prompt means 'never': closing at market is
            # irreversible and pays the round trip, so it must not be
            # what happens when nobody is at the keyboard.
            'CLOSE_ON_SHUTDOWN': 'ask',
            'SHUTDOWN_PROMPT_SEC': 30.0,
            # How long a health verdict must HOLD before the block is
            # reprinted. The log is event-driven, which is right, but
            # any gate sitting on its own threshold then flips on every
            # poll and each flip costs seven lines (live 2026-08-26: the
            # staleness guard at 2.0s on a feed with 2.0-2.5s gaps).
            # A state that changes back inside the dwell is not news; it
            # is counted and reported when something finally settles.
            # 0 = report every change immediately (the old behaviour).
            'LOG_STATE_DWELL_SEC': 5.0,
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
                          ('telegram', 'TELEGRAM'),
                          ('manual', 'MANUAL')]:
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

    # Sections safe to hot-apply while the coordinator runs. Structural
    # fields (accounts, leg mapping, symbols, HEDGE_RATIO) change the
    # spread series or the topology and require flat book + restart.
    HOT_SECTIONS = ('SIGNAL_THRESHOLDS', 'SIGNALS', 'EXITS', 'COSTS',
                    'RISK_LIMITS', 'EXECUTION', 'TELEGRAM', 'RECONCILE',
                    'MANUAL')
    # A key absent from here is saved to config.json and then IGNORED by
    # the running coordinator until a restart — with no warning, because
    # hot_apply only reports what it DID apply. SIZING_MODE and the leg
    # notional were missing when they were added, so the operator saved
    # notional sizing and the dashboard kept reporting the old clip
    # (2026-08-07). Anything the operator can change on the Settings
    # page and expects to take effect belongs in this tuple.
    #: Per-asset fields that hot-apply. Reference-only every one: they
    #: feed the Carry to Expiry card and nothing in the signal, sizing
    #: or exit path. Anything else under `assets` is structural and
    #: needs a restart.
    #: MT5 quotes swap_long and swap_short separately and they routinely
    #: differ in SIGN, so the override is per leg per SIDE. The two
    #: legacy single-value keys are kept here so a save can CLEAR them.
    CARRY_ASSET_KEYS = ('futures_expiry', 'spot_expiry',
                        'swap_spot_long_per_lot', 'swap_spot_short_per_lot',
                        'swap_futures_long_per_lot',
                        'swap_futures_short_per_lot',
                        'swap_spot_per_lot', 'swap_futures_per_lot')

    HOT_TRADING_KEYS = ('CLIP_LOTS', 'SLICE_LOTS', 'DAILY_LOT_TARGET',
                        'POLL_INTERVAL_SEC', 'SIZING_MODE',
                        'NOTIONAL_PER_LEG_USD', 'HEDGE_MODE',
                        'CLOSE_ON_SHUTDOWN', 'SHUTDOWN_PROMPT_SEC',
                        'LOG_STATE_DWELL_SEC')

    def hot_apply(self, fresh, positions_open=False):
        """Apply a freshly-loaded config to this live one in place.

        Returns (applied, blocked): section names applied, and reasons
        for anything refused. HEDGE_RATIO is structural — changing it
        recomputes the whole spread series, so it is REJECTED while a
        position is open (and needs a restart regardless). Note:
        changed risk settings apply to any OPEN trade immediately.
        """
        applied, blocked = [], []
        for section in self.HOT_SECTIONS:
            if getattr(self, section) != getattr(fresh, section):
                getattr(self, section).update(getattr(fresh, section))
                applied.append(section)
        for key in self.HOT_TRADING_KEYS:
            if self.TRADING.get(key) != fresh.TRADING.get(key):
                self.TRADING[key] = fresh.TRADING[key]
                applied.append(f'TRADING.{key}')

        if fresh.TRADING.get('HEDGE_RATIO') != self.TRADING.get('HEDGE_RATIO'):
            if positions_open:
                blocked.append('HEDGE_RATIO change rejected: position open')
            else:
                blocked.append('HEDGE_RATIO change requires a restart '
                               '(recomputes the spread series)')
        # The carry inputs hot-apply, and the rest of `assets` does not.
        # Symbols, contract sizes and beta define the series and the
        # orders, so changing them under a running engine is genuinely a
        # restart. The expiry and the two swap overrides are REFERENCE
        # ONLY — signals, sizing and exits never read them, and the one
        # thing they do feed is a dashboard card. Blocking the whole
        # section meant an operator set the expiry, saw nothing change,
        # and had no way to tell the value had not been rejected
        # (operator, 2026-08-24: "cannot see - What the spread should be
        # based on the Swap and Expiry Date Calculation?" — their own log
        # carried the answer, "assets change requires a restart", ten
        # lines above the trade).
        for key, asset in self.ASSETS.items():
            for field in self.CARRY_ASSET_KEYS:
                new = (fresh.ASSETS.get(key) or {}).get(field)
                if asset.get(field) != new:
                    if new is None:
                        asset.pop(field, None)
                    else:
                        asset[field] = new
                    applied.append(f'{key}.{field}')

        def _structural(assets):
            return {k: {f: v for f, v in a.items()
                        if f not in self.CARRY_ASSET_KEYS}
                    for k, a in assets.items()}

        for name, check in [
                ('accounts', {k: vars(a) for k, a in fresh.accounts.items()}
                 != {k: vars(a) for k, a in self.accounts.items()}),
                ('leg_accounts', fresh.leg_accounts != self.leg_accounts),
                ('assets', _structural(fresh.ASSETS)
                 != _structural(self.ASSETS))]:
            if check:
                blocked.append(f'{name} change requires a restart')
        if applied:
            logging.info("Config hot-reloaded: %s", ', '.join(applied))
        for reason in blocked:
            logging.warning("Config reload: %s", reason)
        return applied, blocked

    def validate_expiries(self):
        """Warn about expired futures contracts.

        Expiry is OPTIONAL — leave it unset for a rolling contract and
        the engine trades the raw basis instead of the carry-detrended
        spread. Only a date that has PASSED is worth a warning."""
        stale = []
        for key, asset in self.ASSETS.items():
            expiry = asset.get('futures_expiry')
            if not asset.get('enabled') or not expiry:
                continue
            if expiry <= datetime.now():
                stale.append(key)
                logging.warning(
                    "%s futures_expiry %s has passed — the carry "
                    "adjustment is off and the engine is trading the RAW "
                    "basis. Set the live contract's expiry, or clear it "
                    "for a rolling contract.", key, expiry.date())
        return stale
