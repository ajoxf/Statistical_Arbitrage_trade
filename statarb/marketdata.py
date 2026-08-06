"""Basis / swap-premium math, shared by the single-account system and
the multi-account coordinator."""

import math
from datetime import datetime


def calculate_swap_basis(asset_cfg, spot_price, time_to_expiry):
    """Swap-implied futures price and basis from real carry cost."""
    position_value = spot_price * asset_cfg['lot_size']
    daily_swap_rate = asset_cfg['swap_charge'] / position_value
    annual_swap_rate = daily_swap_rate * 365

    swap_futures_price = spot_price * math.exp(annual_swap_rate * time_to_expiry)
    swap_basis = swap_futures_price - spot_price
    return swap_futures_price, swap_basis, annual_swap_rate


def compute_market_data(asset_cfg, spot_tick, futures_tick):
    """Build the market-data snapshot from two ticks.

    Ticks are any objects with bid/ask/last attributes (mt5 ticks or
    SimpleNamespace built from IPC dicts).
    """
    multiplier = asset_cfg.get('multiplier', 1.0)

    # Identity of the two quotes this snapshot was built from. Two polls
    # that read the same pair of ticks are ONE observation of the
    # spread, however many times we looked; SpreadStats uses this to
    # keep its window a series of quote events rather than of poll
    # iterations. Prices join the tick times because some brokers stamp
    # ticks only to the second.
    quote_id = "{}:{}/{}|{}:{}/{}".format(
        getattr(spot_tick, 'time', ''), spot_tick.bid, spot_tick.ask,
        getattr(futures_tick, 'time', ''), futures_tick.bid, futures_tick.ask)

    spot_price = (spot_tick.last if spot_tick.last > 0
                  else (spot_tick.bid + spot_tick.ask) / 2)
    futures_price = (futures_tick.last if futures_tick.last > 0
                     else (futures_tick.bid + futures_tick.ask) / 2) * multiplier

    actual_basis = futures_price - spot_price

    # Expiry is OPTIONAL. With one, the spread is carry-detrended:
    # swap_diff = basis - the basis the swap cost implies. Without one
    # (a rolling/perpetual contract, or simply not configured), the
    # strategy trades the RAW basis. It must never silently become
    # zero — a zero spread means z never moves and nothing ever
    # trades, which looks identical to a dead engine.
    expiry = asset_cfg.get('futures_expiry')
    time_to_expiry = ((expiry - datetime.now()).total_seconds()
                      / (365.25 * 24 * 3600)) if expiry else None
    days_to_expiry = time_to_expiry * 365.25 if time_to_expiry else 0

    if time_to_expiry and time_to_expiry > 0:
        swap_futures_price, swap_basis, annual_swap_rate = \
            calculate_swap_basis(asset_cfg, spot_price, time_to_expiry)
        if abs(swap_basis) > 0.001:
            swap_premium_pct = ((actual_basis - swap_basis)
                                / abs(swap_basis)) * 100
        else:
            swap_premium_pct = (actual_basis / spot_price) * 100
        swap_diff = actual_basis - swap_basis
        carry_adjusted = True
    else:
        swap_futures_price = futures_price
        swap_basis = annual_swap_rate = 0
        swap_diff = actual_basis          # trade the raw basis
        swap_premium_pct = (actual_basis / spot_price) * 100
        carry_adjusted = False

    return {
        'asset_name': asset_cfg['name'],
        'timestamp': datetime.now(),
        'quote_id': quote_id,
        'spot_price': spot_price,
        'futures_price': futures_price,
        'swap_futures_price': swap_futures_price,
        'spot_bid': spot_tick.bid,
        'spot_ask': spot_tick.ask,
        'futures_bid': futures_tick.bid * multiplier,
        'futures_ask': futures_tick.ask * multiplier,
        'spot_spread': (spot_tick.ask - spot_tick.bid) * 100,
        'futures_spread': (futures_tick.ask - futures_tick.bid) * 100,
        'spread_unit': '¢',
        'actual_basis': actual_basis,
        'swap_basis': swap_basis,
        'swap_premium_pct': swap_premium_pct,
        'swap_diff': swap_diff,
        'annual_swap_rate': annual_swap_rate,
        'time_to_expiry': time_to_expiry,
        'days_to_expiry': days_to_expiry,
        'carry_adjusted': carry_adjusted,
    }
