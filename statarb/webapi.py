"""Translation layer between the Nexus UI (W3 field names) and this
engine's sectioned MT5 config.

The vendored templates speak W3's flat config vocabulary
(entry_threshold, profit_target_capital_pct, ...). The engine stores
sectioned MT5 config (SIGNALS.ENTRY_Z, EXITS.TP_CAPITAL_PCT, ...).
Everything in this module is that mapping, in both directions, so the
UI stays byte-identical to W3 while the engine stays MT5-native.
"""

from datetime import datetime


def _expiry_or_raise(value):
    """A contract expiry from the UI -> the ISO string config stores.

    Kept as a STRING rather than a datetime because this dict is written
    straight to config.json; `AlgoTradingConfig` parses it back on load.
    A date this cannot read raises, so the save reports it — silently
    dropping an expiry would leave the operator looking at a carry card
    that says "rolling contract" for a future they just dated.
    """
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        raise ValueError(f"{text!r} is not a date (use YYYY-MM-DD)")


def _expiry_has_passed(value, now=None):
    try:
        when = (value if isinstance(value, datetime)
                else datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return False
    return when <= (now or datetime.now())

# W3 field -> (config section, key). Values pass through unchanged
# unless listed in the transform tables below.
FIELD_MAP = {
    # Z-score thresholds
    'entry_threshold': ('SIGNALS', 'ENTRY_Z'),
    'exit_threshold': ('SIGNALS', 'EXIT_Z'),
    'stop_loss_threshold': ('SIGNALS', 'STOP_Z'),
    'max_entry_z': ('SIGNALS', 'MAX_ENTRY_Z'),
    'exit_signal_mode': ('SIGNALS', 'EXIT_MODE'),
    'lookback_period': ('SIGNALS', 'LOOKBACK_SEC'),
    'stats_update_interval': ('SIGNALS', 'STATS_INTERVAL_SEC'),
    'min_samples': ('SIGNALS', 'MIN_SAMPLES'),
    'min_history_sec': ('SIGNALS', 'MIN_HISTORY_SEC'),
    'min_sigma': ('SIGNALS', 'MIN_SIGMA'),
    'max_abs_z': ('SIGNALS', 'MAX_ABS_Z'),
    'trend_direction_filter': ('SIGNALS', 'TREND_FILTER'),
    'trend_window_sec': ('SIGNALS', 'TREND_WINDOW_SEC'),
    'entry_cooldown_seconds': ('SIGNALS', 'ENTRY_COOLDOWN_SEC'),
    'stop_cooldown_seconds': ('SIGNALS', 'STOP_COOLDOWN_SEC'),
    'use_z_signals': ('SIGNALS', 'USE_Z_SIGNALS'),

    # Exits
    'z_stop_exit_enabled': ('EXITS', 'Z_STOP_EXIT_ENABLED'),
    'use_sigma_target': ('EXITS', 'USE_SIGMA_TARGET'),
    'profit_target_capital_pct': ('EXITS', 'TP_CAPITAL_PCT'),
    'profit_target_usd': ('EXITS', 'TP_USD_PER_LOT'),
    'profit_target_min_cost_mult': ('EXITS', 'COST_FLOOR_MULT'),
    'max_hold_halflife_mult': ('EXITS', 'MAX_HOLD_HALF_LIVES'),
    'max_hold_minutes': ('EXITS', 'MAX_HOLD_FALLBACK_MIN'),
    'hard_max_hold_minutes': ('EXITS', 'HARD_MAX_HOLD_MIN'),
    'hard_time_stop_mult': ('EXITS', 'HARD_TIME_STOP_MULT'),
    'max_hold_z_progress_min': ('EXITS', 'MAX_HOLD_PROGRESS_SUPPRESS'),
    'stop_loss_capital_pct': ('EXITS', 'STOP_CAPITAL_PCT'),
    'max_loss_usd': ('EXITS', 'STOP_USD_PER_LOT'),
    'min_entry_rr_multiple': ('EXITS', 'RR'),
    'exit_profit_gate_usd': ('EXITS', 'GATE_FLOOR_USD'),
    'm2m_buffer_pct': ('EXITS', 'M2M_BUFFER_PCT'),
    'leverage': ('EXITS', 'LEVERAGE'),
    'spot_leverage': ('EXITS', 'SPOT_LEVERAGE'),
    'futures_leverage': ('EXITS', 'FUT_LEVERAGE'),

    # Costs / edge filter
    'profit_target_sigma_frac': ('COSTS', 'TARGET_FRACTION'),
    'min_std_multiple': ('COSTS', 'MIN_EDGE_MULTIPLE'),
    'commission_per_lot_spot': ('COSTS', 'COMMISSION_PER_LOT_SPOT'),
    'commission_per_lot_futures': ('COSTS', 'COMMISSION_PER_LOT_FUT'),
    'spread_cost_factor': ('COSTS', 'SPREAD_COST_FACTOR'),

    # Sizing (MT5: lots, not USD notional)
    'hedge_ratio': ('TRADING', 'HEDGE_RATIO'),
    'clip_lots': ('TRADING', 'CLIP_LOTS'),
    'slice_lots': ('TRADING', 'SLICE_LOTS'),
    # W3's sizing model, restored 2026-08-07: the operator saves the
    # money per LEG and the lots follow from the live price.
    'sizing_mode': ('TRADING', 'SIZING_MODE'),
    'position_size_usd': ('TRADING', 'NOTIONAL_PER_LEG_USD'),
    'hedge_mode': ('TRADING', 'HEDGE_MODE'),
    'daily_lot_target': ('TRADING', 'DAILY_LOT_TARGET'),
    'poll_interval_sec': ('TRADING', 'POLL_INTERVAL_SEC'),

    # Execution
    'entry_execution_mode': ('EXECUTION', 'ENTRY_STYLE'),
    'limit_order_timeout_sec': ('EXECUTION', 'LIMIT_TIMEOUT_SEC'),
    'hedge_timeout_sec': ('EXECUTION', 'HEDGE_TIMEOUT_SEC'),
    'exit_timeout_sec': ('EXECUTION', 'EXIT_TIMEOUT_SEC'),
    'limit_peg_interval': ('EXECUTION', 'REPEG_INTERVAL_SEC'),
    'limit_order_price_offset_bps': ('EXECUTION', 'PEG_OFFSET_POINTS'),
    'min_fill_ratio': ('EXECUTION', 'MIN_MATCHED_FRACTION'),
    'on_timeout': ('EXECUTION', 'ON_TIMEOUT'),
    'slippage_tolerance': ('EXECUTION', 'SLIPPAGE_TOLERANCE'),

    # Risk / breakers
    'max_positions': ('RISK_LIMITS', 'MAX_POSITIONS_PER_ASSET'),
    'max_lot_size': ('RISK_LIMITS', 'MAX_LOT_SIZE'),
    'max_daily_trades': ('RISK_LIMITS', 'MAX_DAILY_TRADES'),
    'daily_max_loss_usd': ('RISK_LIMITS', 'DAILY_MAX_LOSS_USD'),
    'loss_streak_reduce': ('RISK_LIMITS', 'LOSS_STREAK_REDUCE'),
    'loss_streak_pause': ('RISK_LIMITS', 'LOSS_STREAK_PAUSE'),
    'margin_breaker_enabled': ('RISK_LIMITS', 'MARGIN_BREAKER_ENABLED'),
    'margin_halt_level': ('RISK_LIMITS', 'MARGIN_HALT_LEVEL'),
    'margin_min_free_usd': ('RISK_LIMITS', 'MARGIN_MIN_FREE_USD'),
    'margin_reduce_enabled': ('RISK_LIMITS', 'MARGIN_REDUCE_ENABLED'),
    'margin_reduce_level': ('RISK_LIMITS', 'MARGIN_REDUCE_LEVEL'),
    'margin_min_size_fraction': ('RISK_LIMITS', 'MARGIN_MIN_SIZE_FRACTION'),

    # Reconciliation
    'sync_interval_sec': ('RECONCILE', 'SYNC_INTERVAL_SEC'),
    'reconcile_strikes': ('RECONCILE', 'STRIKES'),

    # Telegram toggles (token/chat live in .env)
    'telegram_enabled': ('TELEGRAM', 'ENABLED'),
    'telegram_notify_trades': ('TELEGRAM', 'NOTIFY_TRADES'),
    'telegram_notify_errors': ('TELEGRAM', 'NOTIFY_ERRORS'),
    'telegram_notify_signals': ('TELEGRAM', 'NOTIFY_SIGNALS'),
}

SECTION_JSON_KEY = {
    'SIGNAL_THRESHOLDS': 'signal_thresholds', 'RISK_LIMITS': 'risk_limits',
    'EXECUTION': 'execution', 'TRADING': 'trading', 'SIGNALS': 'signals',
    'COSTS': 'costs', 'EXITS': 'exits', 'RECONCILE': 'reconcile',
    'TELEGRAM': 'telegram',
}

# Values the UI sends uppercase but the engine stores lowercase
LOWERCASE_FIELDS = {'entry_execution_mode', 'exit_signal_mode', 'on_timeout'}
BOOL_FIELDS = {'z_stop_exit_enabled', 'trend_direction_filter',
               'use_sigma_target', 'use_z_signals', 'telegram_enabled',
               'telegram_notify_trades', 'telegram_notify_errors',
               'telegram_notify_signals', 'margin_breaker_enabled',
               'margin_reduce_enabled'}


def _defaults():
    from .config import AlgoTradingConfig
    return AlgoTradingConfig()


def to_ui_config(raw, defaults=None):
    """Sectioned config.json -> flat W3 field names for the UI."""
    defaults = defaults or _defaults()
    out = {}
    for field, (section, key) in FIELD_MAP.items():
        merged = dict(getattr(defaults, section))
        merged.update(raw.get(SECTION_JSON_KEY[section], {}))
        value = merged.get(key)
        if field in LOWERCASE_FIELDS and isinstance(value, str):
            value = value.upper() if field == 'entry_execution_mode' else value
        out[field] = value

    assets = raw.get('assets') or {
        k: dict(v) for k, v in defaults.ASSETS.items()}
    asset_key = next((k for k, v in assets.items() if v.get('enabled', True)),
                     'GOLD')
    asset = assets.get(asset_key, {})
    out['asset'] = asset_key
    out['spot_symbol'] = (asset.get('spot_symbols') or [''])[0]
    out['futures_symbol'] = (asset.get('futures_symbols') or [''])[0]
    out['contract_size'] = asset.get('lot_size')
    out['swap_charge'] = asset.get('swap_charge')
    # Hand-entered swap, per lot per night, per leg. MT5 reports its own
    # in swap_mode units this cannot always convert, so the operator
    # needs to be able to say what the broker actually charges.
    out['swap_spot_per_lot'] = asset.get('swap_spot_per_lot')
    out['swap_futures_per_lot'] = asset.get('swap_futures_per_lot')
    out['pair_type'] = (asset.get('pair_type') or 'SPOT_FUTURE').upper()
    out['carry_rate_pct'] = round(
        (asset.get('risk_free_rate') or 0.0) * 100, 4)
    for field in ('futures_expiry', 'spot_expiry'):
        expiry = asset.get(field)
        out[field] = (expiry.isoformat()[:10]
                      if hasattr(expiry, 'isoformat') else expiry)
    out['paper_trading'] = raw.get('trading_mode', 'paper') != 'live'
    out['algo_enabled'] = raw.get('algo_enabled', True)
    out['accounts'] = raw.get('accounts', {})
    out['leg_accounts'] = raw.get('leg_accounts', {})
    return out


def apply_ui_config(raw, payload):
    """Flat W3 field names from the UI -> sectioned config.json (in
    place). Returns (raw, env_updates, notes)."""
    env_updates, notes = {}, []
    for field, value in payload.items():
        mapping = FIELD_MAP.get(field)
        if not mapping or value is None:
            continue
        section, key = mapping
        json_key = SECTION_JSON_KEY[section]
        raw.setdefault(json_key, {})
        if field in BOOL_FIELDS:
            value = value in (True, 'true', 'True', 'on', 1, '1')
        elif field in LOWERCASE_FIELDS:
            value = str(value).lower()
        else:
            try:
                value = float(value)
                if value == int(value) and key in (
                        'MIN_SAMPLES', 'LOOKBACK_SEC', 'STATS_INTERVAL_SEC',
                        'MIN_HISTORY_SEC',
                        'MAX_POSITIONS_PER_ASSET', 'MAX_DAILY_TRADES',
                        'LOSS_STREAK_REDUCE', 'LOSS_STREAK_PAUSE',
                        'SYNC_INTERVAL_SEC', 'STRIKES', 'TREND_WINDOW_SEC',
                        'ENTRY_COOLDOWN_SEC', 'STOP_COOLDOWN_SEC'):
                    value = int(value)
            except (TypeError, ValueError):
                pass
        raw[json_key][key] = value

    # Symbols / contract specs live under assets
    asset_key = payload.get('asset')
    if asset_key:
        raw.setdefault('assets', {})
        asset = raw['assets'].setdefault(asset_key, {})
        asset.setdefault('name', asset_key)
        asset.setdefault('enabled', True)
        if payload.get('spot_symbol'):
            asset['spot_symbols'] = [payload['spot_symbol']]
        if payload.get('futures_symbol'):
            asset['futures_symbols'] = [payload['futures_symbol']]
        for field, key in (('contract_size', 'lot_size'),
                           ('swap_charge', 'swap_charge')):
            if payload.get(field) not in (None, ''):
                asset[key] = payload[field]
        # CLEARABLE fields — blank means "use whatever MT5 reports", so
        # they cannot ride the skip-if-blank loop above, which can only
        # ever set a value and never take one away. An override the
        # operator cannot remove is worse than no override: it would
        # outlive the pair it was typed for, and for an EXPIRY that is
        # the difference between a convergence date and a rolling
        # contract that has none.
        for field, cast in (('swap_spot_per_lot', float),
                            ('swap_futures_per_lot', float),
                            ('futures_expiry', _expiry_or_raise),
                            ('spot_expiry', _expiry_or_raise)):
            if field not in payload:
                continue
            if payload[field] in (None, ''):
                asset.pop(field, None)
                continue
            try:
                asset[field] = cast(payload[field])
            except (TypeError, ValueError) as exc:
                notes.append(f"{field}: {exc} — left unchanged.")
        # A date already gone is accepted — the operator may be recording
        # a contract that has just rolled — but it is SAID, because from
        # the engine's side a passed expiry and a rolling contract are
        # the same thing: no days left, so no convergence to price.
        if str(asset.get('futures_expiry') or '') and \
                _expiry_has_passed(asset['futures_expiry']):
            notes.append(
                f"Futures expiry {asset['futures_expiry']} has already "
                f"passed, so there are no days left to converge over and "
                f"the carry card will stay hidden. Set the live "
                f"contract's date.")
        if payload.get('pair_type'):
            asset['pair_type'] = str(payload['pair_type']).upper()
        # Carry rate arrives as a percentage from the UI and is stored
        # as a fraction. Fair value is the ONLY thing that reads it.
        if payload.get('carry_rate_pct') not in (None, ''):
            try:
                asset['risk_free_rate'] = float(
                    payload['carry_rate_pct']) / 100.0
            except (TypeError, ValueError):
                pass
        asset.setdefault('risk_free_rate', 0.0425)
        asset.setdefault('multiplier', 1.0)

    if 'paper_trading' in payload:
        paper = payload['paper_trading'] in (True, 'true', 'on', 1, '1')
        new_mode = 'paper' if paper else 'live'
        if new_mode != raw.get('trading_mode', 'paper'):
            notes.append("Trading mode change applies when the launcher "
                         "is restarted.")
        raw['trading_mode'] = new_mode

    return raw, env_updates, notes


def trade_to_ui(row):
    """trade_review row -> the W3 journal/trade shape the UI renders."""
    pnl = row.get('realized_pnl')
    notional = row.get('notional') or 0
    entry_spread = row.get('entry_spread')
    exit_spread = row.get('exit_spread')
    return {
        'id': row.get('position_id'),
        'asset': row.get('asset'),
        'position_type': ('SHORT' if (row.get('entry_z') or 0) > 0
                          else 'LONG'),
        'entry_time': row.get('opened'),
        'exit_time': row.get('closed'),
        'entry_zscore': row.get('entry_z'),
        'exit_zscore': row.get('exit_z'),
        'entry_spread': entry_spread,
        'exit_spread': exit_spread,
        'be_spread': row.get('be_spread'),
        'ex_spread': row.get('ex_spread'),
        'tp_spread': row.get('tp_spread'),
        'sl_spread': row.get('sl_spread'),
        'quantity': row.get('lots'),
        'notional_usd': notional,
        'pnl_usd': pnl,
        'pnl_pct': (100 * pnl / notional) if (notional and pnl is not None)
                   else None,
        'pnl_pct_on_capital': (100 * pnl / notional)
                              if (notional and pnl is not None) else None,
        'exit_reason': row.get('exit_reason'),
        'outcome': row.get('outcome'),
        'peak_net_usd': row.get('peak_pnl'),
        'trough_net_usd': row.get('trough_pnl'),
        'peak_minutes': row.get('peak_min'),
        'trough_minutes': row.get('trough_min'),
        # Execution quality for this trade: the round-trip slippage in
        # dollars, and each side separately. None where the trade
        # predates the measurement — never 0, which would read as a
        # perfect fill.
        'slip_usd': row.get('slip_usd'),
        'entry_slip_usd': row.get('entry_slip_usd'),
        'exit_slip_usd': row.get('exit_slip_usd'),
        'entry_slip_spread': row.get('entry_slip_spread'),
        'exit_slip_spread': row.get('exit_slip_spread'),
        'is_open': False,
        'is_paper': False,
    }


def statistics_from_rows(rows):
    """Aggregate stats block for the analysis page tiles."""
    pnls = [r['realized_pnl'] for r in rows
            if r.get('realized_pnl') is not None]
    if not pnls:
        return {'total_trades': 0, 'winning_trades': 0, 'losing_trades': 0,
                'win_rate': 0, 'total_pnl': 0, 'avg_pnl': 0, 'avg_win': 0,
                'avg_loss': 0, 'reward_risk': 0, 'profit_factor': 0,
                'breakeven_wr': 0, 'expectancy_r': 0, 'max_drawdown': 0,
                'max_drawdown_pct': 0, 'current_drawdown': 0,
                'current_drawdown_pct': 0, 'p70_peak': 0,
                'median_peak_minutes': 0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    gross_win, gross_loss = sum(wins), -sum(losses)
    rr = (avg_win / abs(avg_loss)) if avg_loss else 0.0
    peak = dd = run = 0.0
    for p in reversed(pnls):          # rows arrive newest-first
        run += p
        peak = max(peak, run)
        dd = max(dd, peak - run)
    peaks = sorted(r['peak_pnl'] for r in rows
                   if r.get('peak_pnl') is not None)
    peak_mins = sorted(r['peak_min'] for r in rows
                       if r.get('peak_min') is not None
                       and (r.get('realized_pnl') or 0) > 0)
    total = sum(pnls)
    return {
        'total_trades': len(pnls), 'winning_trades': len(wins),
        'losing_trades': len(losses),
        'win_rate': 100 * len(wins) / len(pnls),
        'total_pnl': total, 'avg_pnl': total / len(pnls),
        'expectancy': total / len(pnls),
        'avg_win': avg_win, 'avg_loss': avg_loss, 'reward_risk': rr,
        'profit_factor': (gross_win / gross_loss) if gross_loss else 0.0,
        'breakeven_wr': (100 / (1 + rr)) if rr else 0.0,
        'expectancy_r': ((total / len(pnls)) / abs(avg_loss))
                        if avg_loss else 0.0,
        'max_drawdown': dd, 'max_drawdown_pct': 0.0,
        'current_drawdown': max(0.0, peak - run),
        'current_drawdown_pct': 0.0,
        'p70_peak': peaks[int(0.7 * (len(peaks) - 1))] if peaks else 0.0,
        'median_peak_minutes': (peak_mins[len(peak_mins) // 2]
                                if peak_mins else 0.0),
    }


def slippage_block(rows):
    """Realised execution cost across closed trades.

    This is the other half of the audit CLAUDE.md asks for — "alarm if
    modeled >= 2x realized" was unanswerable while nothing measured
    what execution actually cost. `cost_est` is what the model charged
    the trade; crossing + slippage is what it really paid.

    Counts are per SIDE (entry and exit are separate observations), and
    trades where nothing was measured are excluded rather than counted
    as zero — a zero would drag the average toward "flawless"."""
    def measured(key):
        return [r[key] for r in rows if r.get(key) is not None]

    entries = measured('entry_slip_usd')
    exits = measured('exit_slip_usd')
    sides = entries + exits
    round_trips = measured('slip_usd')
    cross = measured('entry_cross_spread') + measured('exit_cross_spread')
    # Modelled vs realised, per trade, over the trades where BOTH are
    # known. Realised = the crossing actually paid PLUS the slippage —
    # the model's cost_est is meant to cover the whole round trip, so
    # comparing it against slippage alone would flatter it.
    modelled, realised = [], []
    for r in rows:
        cross_usd = [r.get('entry_cross_usd'), r.get('exit_cross_usd')]
        if r.get('cost_est') is None or r.get('slip_usd') is None \
                or any(c is None for c in cross_usd):
            continue
        modelled.append(r['cost_est'])
        realised.append(sum(cross_usd) + r['slip_usd'])

    worst = max(sides, key=abs) if sides else None
    avg_modelled = (sum(modelled) / len(modelled)) if modelled else None
    avg_realised = (sum(realised) / len(realised)) if realised else None
    return {
        'measured_sides': len(sides),
        'measured_round_trips': len(round_trips),
        'avg_slip_usd': (sum(sides) / len(sides)) if sides else None,
        'total_slip_usd': sum(sides) if sides else None,
        'avg_entry_slip_usd': (sum(entries) / len(entries)
                               if entries else None),
        'avg_exit_slip_usd': (sum(exits) / len(exits)) if exits else None,
        'avg_round_trip_slip_usd': (sum(round_trips) / len(round_trips)
                                    if round_trips else None),
        'avg_crossing_spread': (sum(cross) / len(cross)) if cross else None,
        'worst_slip_usd': worst,
        'improved_sides': len([s for s in sides if s < 0]),
        'compared_trades': len(modelled),
        'avg_modelled_cost_usd': avg_modelled,
        'avg_realised_cost_usd': avg_realised,
        # CLAUDE.md: "alarm if modeled >= 2x realized — an inflated
        # cost model silently blocks every good trade."
        'model_ratio': (avg_modelled / avg_realised)
                       if avg_modelled is not None and avg_realised else None,
    }


def excursion_row(row):
    """trade_review row -> the MAE/MFE excursion row the analysis page's
    'Max Drawdown by Trade' table renders."""
    pnl = row.get('realized_pnl') or 0.0
    notional = row.get('notional') or 0.0
    peak = row.get('peak_pnl')
    trough = row.get('trough_pnl')
    target = row.get('capture_target')
    # The template formats these unconditionally ('%.2f' % value) and
    # renders MAE as a positive magnitude behind a minus sign, so they
    # must always be numbers, never None.
    mae_usd = abs(min(trough or 0.0, 0.0))
    mfe_usd = max(peak or 0.0, 0.0)
    return {
        'id': row.get('position_id'), 'trade_id': row.get('position_id'),
        'position_type': ('SHORT' if (row.get('entry_z') or 0) > 0
                          else 'LONG'),
        'exit_reason': row.get('exit_reason'),
        'mae_usd': mae_usd, 'mfe_usd': mfe_usd,
        'mae_pct': (100 * mae_usd / notional) if notional else None,
        'mfe_pct': (100 * mfe_usd / notional) if notional else None,
        'mae_eq_pct': (100 * mae_usd / notional) if notional else None,
        'pnl_usd': pnl, 'exit_net_usd': pnl, 'current_net_usd': pnl,
        'pnl_pct': (100 * pnl / notional) if notional else None,
        'pnl_eq_pct': (100 * pnl / notional) if notional else None,
        'utilization_pct': (100 * pnl / mfe_usd) if mfe_usd else None,
        'peak_net_usd': peak, 'peak_minutes': row.get('peak_min'),
        'trough_minutes': row.get('trough_min'),
        'mins_since_entry': None,
        'target_usd': target,
        'hit_target': bool(target and pnl >= target),
        'hit_be': pnl >= 0, 'hit_be_min': None,
    }


def drawdown_block(rows):
    """Equity-curve drawdown summary for the analysis tiles."""
    pnls = [r['realized_pnl'] for r in rows
            if r.get('realized_pnl') is not None]
    peak = run = max_dd = 0.0
    for pnl in reversed(pnls):        # rows arrive newest-first
        run += pnl
        peak = max(peak, run)
        max_dd = max(max_dd, peak - run)
    current = max(0.0, peak - run)
    return {
        'max_usd': max_dd, 'current_usd': current,
        'peak_equity_usd': peak,
        'max_pct': (100 * max_dd / peak) if peak else 0.0,
        'current_pct': (100 * current / peak) if peak else 0.0,
    }


def manual_level_error(direction, entry, take_profit, stop):
    """Manual Spread Trade: are Entry / Take Profit / Stop the right
    way round? Mirrors Coordinator.check_manual_levels so the browser,
    the HTTP API and the engine all refuse the same geometry.

    A short-spread trade (SELL_BASIS) profits as the spread falls, so
    its target is BELOW entry and its stop ABOVE; long-spread is the
    mirror. Returns an error string, or None when the levels are
    sound."""
    if entry is None:
        return None                     # nothing to measure against
    down = direction == 'SELL_BASIS'
    side = 'short' if down else 'long'
    if take_profit is not None:
        if (take_profit < entry) != down:
            return (f"Take profit {take_profit:g} is on the losing side "
                    f"of entry {entry:g}. A {side}-spread trade takes "
                    f"profit {'below' if down else 'above'} entry.")
    if stop is not None:
        if (stop > entry) != down:
            return (f"Stop loss {stop:g} is on the winning side of entry "
                    f"{entry:g}. A {side}-spread trade stops out "
                    f"{'above' if down else 'below'} entry — as placed "
                    f"it would fire the moment the trade went right.")
    return None


def _pending_symbols(first, config_raw):
    """Saved symbols that the running engine has not picked up.

    Returns {'spot': (running, configured), ...} for whichever legs
    disagree, or {} when they match or the engine has not published
    what it is streaming. Symbols are structural — a save only takes
    effect on restart — so this is the difference between "the card is
    wrong" and "the card is waiting for you".
    """
    running = {'spot': first.get('rt_spot_symbol'),
               'futures': first.get('rt_fut_symbol')}
    if not any(running.values()):
        return {}
    assets = (config_raw or {}).get('assets') or {}
    asset = next((v for v in assets.values() if v.get('enabled', True)), {})
    configured = {'spot': (asset.get('spot_symbols') or [''])[0],
                  'futures': (asset.get('futures_symbols') or [''])[0]}
    return {leg: {'running': running[leg], 'configured': configured[leg]}
            for leg in ('spot', 'futures')
            if running[leg] and configured[leg]
            and running[leg] != configured[leg]}


def status_to_ui(status, config_raw):
    """runtime_status.json -> the /api/engine/status shape the Nexus
    dashboard consumes."""
    assets = status.get('assets') or []
    first = assets[0] if assets else {}
    positions = status.get('positions') or []
    open_position = positions[0] if positions else None
    # The engine's resolved sizing decision: lots, notional and margin
    # per leg, and the leverage each was computed with.
    sizing_block = first.get('sizing') or {}

    signal = None
    if first:
        signal = {
            'zscore': first.get('z'),
            'spread': first.get('spread', first.get('basis')),
            # The pieces the spread is made of, so the number on the
            # card can be checked against the two prices beside it.
            'raw_basis': first.get('basis'),
            'spread_hedge_ratio': first.get('hedge_ratio'),
            'spread_formula': first.get('spread_formula'),
            # Reference only — the dashboard labels it as such and no
            # part of the engine reads it back.
            'pair_type': first.get('pair_type'),
            'fair_value': first.get('fair_value'),
            'fair_gap': first.get('fair_gap'),
            'fair_detail': first.get('fair_detail'),
            'fair_inputs': first.get('fair_inputs'),
            # Cost / edge, in the W3 field names the Filters card reads.
            # These were never published, which is why that card showed
            # "-" against every row.
            'std_ratio': first.get('edge_ratio'),
            'std_ratio_required': first.get('edge_required'),
            # The Edge badge's verdict. It belongs in THIS block, not at
            # the top level: updateSignal() is called as
            # `updateSignal(d.signal)`, so everything the Filters card
            # reads is looked up inside `signal`. The badge sat at "-"
            # because nothing ever published the field at all, and
            # publishing it one level up would have left it there.
            'std_filter_ok': first.get('edge_ok'),
            # The symbols the engine is ACTUALLY streaming, and whether
            # they still match config.json. Symbols are structural, so
            # a save takes effect only on restart — and the leg cards
            # were labelled from config while priced from the engine.
            # Live 2026-08-10 that put "XAGUSD 82.0050" and "XAUUSD
            # 85.3500" on screen: new labels over the old pair's oil
            # prices, a picture that cannot be true. In the SIGNAL
            # block because updateSignal() is called as
            # updateSignal(d.signal).
            'leg_a_symbol': first.get('rt_spot_symbol'),
            'leg_b_symbol': first.get('rt_fut_symbol'),
            'symbols_pending_restart': _pending_symbols(first, config_raw),
            'edge_capture_fraction': first.get('edge_capture_fraction'),
            'edge_z': first.get('edge_z'),
            'edge_sigma': first.get('edge_sigma'),
            'edge_capture_per_lot': first.get('edge_capture_per_lot'),
            'edge_required_usd': first.get('edge_required_usd'),
            'edge_gap_usd': first.get('edge_gap_usd'),
            'edge_z_needed': first.get('edge_z_needed'),
            'edge_cost_in_sigmas': first.get('edge_cost_in_sigmas'),
            'edge_reachable': first.get('edge_reachable'),
            'edge_entry_ceiling': first.get('edge_entry_ceiling'),
            'carry': first.get('carry'),
            'round_trip_cost_bps': first.get('rt_cost_bps'),
            'rt_cost_per_lot': first.get('rt_cost_per_lot'),
            'rt_lots': first.get('rt_lots'),
            'rt_contract_size': first.get('rt_contract_size'),
            'rt_contract_a': first.get('rt_contract_a'),
            'rt_contract_b': first.get('rt_contract_b'),
            'rt_lots_a': first.get('rt_lots_a'),
            'rt_lots_b': first.get('rt_lots_b'),
            'rt_leg_a_cost': first.get('rt_leg_a_cost'),
            'rt_leg_b_cost': first.get('rt_leg_b_cost'),
            'rt_commission_a': first.get('rt_commission_a'),
            'rt_commission_b': first.get('rt_commission_b'),
            'rt_commission_spot': first.get('rt_commission_spot'),
            'rt_commission_fut': first.get('rt_commission_fut'),
            'rt_spot_symbol': first.get('rt_spot_symbol'),
            'rt_fut_symbol': first.get('rt_fut_symbol'),
            'round_trip_fees_bps': first.get('rt_fees_bps'),
            'round_trip_slippage_bps': None,
            'round_trip_cost_usd': first.get('rt_cost_usd'),
            # The cost's own INPUTS, so the Filters card can spell the
            # calculation out with live numbers instead of asking the
            # operator to take a lone figure on trust.
            'rt_spot_spread': first.get('rt_spot_spread'),
            'rt_fut_spread': first.get('rt_fut_spread'),
            'rt_spread_factor': first.get('rt_spread_factor'),
            'rt_commission_per_lot': first.get('rt_commission_per_lot'),
            'rt_units': first.get('rt_units'),
            'rt_leg_a_notional': first.get('spot_notional'),
            'expected_capture_usd': first.get('capture_usd'),
            'fee_bps_used': (round(first['rt_fees_bps'], 2)
                             if first.get('rt_fees_bps') is not None else None),
            'order_mode': first.get('order_mode'),
            # Position sizing. The anchor is either CLIP_LOTS or, in
            # notional mode, the money the operator saved per leg —
            # `sizing` carries the whole resolved decision including
            # each leg's lots, margin, and how balanced the pair is.
            'leg_a_notional': first.get('spot_notional'),
            'leg_b_notional': first.get('fut_notional'),
            'clip_lots': first.get('clip_lots'),
            'contract_size': first.get('contract_size'),
            'sizing': first.get('sizing'),
            # Per-leg leverage as the ENGINE currently has it. The card
            # already read these two keys; nothing published them, so it
            # kept the value baked into the page at load time and showed
            # the same leverage however the settings changed. Now they
            # follow a hot-reload without touching the browser.
            'leg_a_leverage': (sizing_block or {}).get('leg_a_leverage'),
            'leg_b_leverage': (sizing_block or {}).get('leg_b_leverage'),
            'min_notional_usd': (sizing_block or {}).get('min_notional_usd'),
            'notional_gap_pct': (sizing_block or {}).get('notional_gap_pct'),
            'lot_step_usd': (sizing_block or {}).get('lot_step_usd'),
            'hedge_mode': (sizing_block or {}).get('hedge_mode'),
            'dollar_neutral_beta': (sizing_block or {}).get(
                'dollar_neutral_beta'),
            'beta_gap_pct': (sizing_block or {}).get('beta_gap_pct'),
            'leg_a_margin': (sizing_block or {}).get('leg_a_margin_usd'),
            'leg_b_margin': (sizing_block or {}).get('leg_b_margin_usd'),
            'mean': first.get('mu'), 'std': first.get('sigma'),
            # The Statistics & Regime card is rendered by updateSignal(),
            # which receives THIS block — not the top level. Publishing
            # spread_mean/spread_std only at the top level left the card
            # reading undefined, which is why it showed 0.00 and then a
            # dash while `regime` (already in here) worked fine.
            'spread_mean': first.get('mu'), 'spread_std': first.get('sigma'),
            'trend_slope': first.get('trend_slope'),
            'half_life': first.get('half_life_min'),
            'hurst': None, 'regime': first.get('regime'),
            'data_points': first.get('samples'),
            # data_points is a rolling OCCUPANCY of the window, so it
            # falls whenever quotes arrive more slowly than they did a
            # window ago. Shown alone it reads as data being lost; the
            # rate is what is actually changing.
            'quote_rate_per_min': first.get('quote_rate_per_min'),
            # What the warm-up bar must count against: MIN_SAMPLES is
            # what actually gates trading. LOOKBACK_SEC is a window in
            # SECONDS — showing "181 / 7,200" compared a sample count
            # to a duration and never filled.
            'lookback': first.get('min_samples') or first.get('lookback'),
            'lookback_sec': first.get('lookback'),
            # The SECOND warm-up gate: elapsed collection time. Both
            # must clear, and the banner names whichever is binding.
            'history_sec': first.get('history_sec'),
            'min_history_sec': first.get('min_history_sec'),
            # A window suggestion in the SAME unit as the setting —
            # seconds. W3's tile showed "pts", a tick count, for a
            # value this engine has never measured in ticks.
            'suggested_lookback_sec': first.get('suggested_lookback_sec'),
            'data_ready': first.get('z') is not None,
            # Enough quotes but no usable z: the window's sigma has
            # collapsed. Without this the banner says "collecting data"
            # while the counter sits well past its target.
            'degenerate': bool(first.get('degenerate')),
            'hedge_ratio': (config_raw.get('trading') or {}).get(
                'HEDGE_RATIO', 1.0),
            'entry_threshold': (config_raw.get('signals') or {}).get(
                'ENTRY_Z'),
            'current_position': open_position['signal_type']
                                if open_position else 'NONE',
        }

    open_trade = None
    if open_position:
        levels = open_position.get('levels') or {}
        open_trade = {
            'id': open_position.get('position_id'),
            'asset': open_position.get('asset'),
            'position_type': ('SHORT' if open_position.get('signal_type')
                              == 'SELL_BASIS' else 'LONG'),
            'quantity': open_position.get('lots'),
            'entry_spot_price': open_position.get('entry_spot'),
            'entry_futures_price': open_position.get('entry_fut'),
            'entry_spread': levels.get('entry_spread'),
            'entry_zscore': open_position.get('entry_z'),
            'unrealized_pnl': open_position.get('unrealized_pnl'),
            'pnl_usd': open_position.get('net_pnl'),
            'peak_net_usd': open_position.get('peak_pnl'),
            'trough_net_usd': open_position.get('trough_pnl'),
            'notional_usd': open_position.get('notional'),
            'age_seconds': open_position.get('age_sec'),
            'max_hold_minutes': (open_position.get('max_hold_sec') or 0) / 60,
            'half_life_minutes': open_position.get('half_life_min'),
            'exit_target_usd': open_position.get('tp_usd'),
            'exit_stop_usd': open_position.get('stop_usd'),
            # Which of the three stop knobs BOUND, and the win rate
            # this geometry needs just to break even.
            'stop_source': open_position.get('stop_source'),
            'breakeven_win_rate': open_position.get('breakeven_win_rate'),
            'exit_gate_floor_usd': open_position.get('gate_floor_usd'),
            # What the frozen geometry was worth going in. None (not 0)
            # whenever it could not be computed.
            'expectancy': open_position.get('expectancy') or {},
            'spread_levels': {
                'break_even': levels.get('be'),
                'gate_release': levels.get('ex'),
                'take_profit': levels.get('tp'),
                'stop': levels.get('sl'),
                'favorable': levels.get('favorable'),
                # Set when the operator named the level by hand on the
                # Manual Spread Trade panel — worth saying, because it
                # explains a TP/SL the engine's own ladder would not
                # have chosen.
                'manual_take_profit': levels.get('manual_tp'),
                'manual_stop': levels.get('manual_sl'),
            } if levels else None,
            'entry_slippage': open_position.get('entry_slippage'),
            'is_open': True,
            'is_paper': status.get('mode') != 'LIVE',
        }

    spot_tick = futures_tick = None
    if first.get('spot_price') is not None:
        spot_tick = {'bid': first.get('spot_bid'), 'ask': first.get('spot_ask'),
                     'last': first.get('spot_price')}
        futures_tick = {'bid': first.get('fut_bid'), 'ask': first.get('fut_ask'),
                        'last': first.get('futures_price')}

    return {
        'is_running': bool(status),
        'algo_enabled': status.get('algo_enabled', False),
        'paper_trading': status.get('mode') != 'LIVE',
        'asset': first.get('asset'),
        'position': open_trade['position_type'] if open_trade else 'NONE',
        'signal': signal,
        'spot_tick': spot_tick,
        'futures_tick': futures_tick,
        'open_trade': open_trade,
        'tick_age_ms': status.get('tick_age_ms'),
        # The Statistics & Regime card reads these at the TOP level; the
        # signal block's 'mean'/'std' were never seen by it, so it
        # rendered 0.00 against a live mu of 58.8.
        'spread_mean': first.get('mu'),
        'spread_std': first.get('sigma'),
        'half_life': first.get('half_life_min'),
        'trend_slope': first.get('trend_slope'),
        'regime': first.get('regime'),
        # This engine does not compute Hurst. Publishing None keeps the
        # card honest — it used to default to a fabricated 0.5000, which
        # reads as a measurement.
        'hurst': None,
        'hurst_ok': None,
        # How fast the engine is ACTUALLY refreshing, measured.
        'write_interval_ms': status.get('write_interval_ms'),
        'poll_interval_sec': status.get('poll_interval_sec'),
        'ws_connected': bool(status),
        'sl_cooldown_remaining': 0,
        'daily_loss_usd': min(0.0, status.get('daily_pnl', 0.0)),
        'daily_pnl': status.get('daily_pnl', 0.0),
        'halted': status.get('halted', False),
        'halt_reason': status.get('halt_reason'),
        'shadow_active': (status.get('shadow') or {}).get('active', 0),
        'execution_backend': 'MT5',
        'test_results': status.get('test_results'),
        'updated': status.get('updated'),
    }
