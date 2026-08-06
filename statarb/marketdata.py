"""Basis / swap-premium math, shared by the single-account system and
the multi-account coordinator."""

import math
from datetime import datetime


def calculate_swap_basis(asset_cfg, spot_price, time_to_expiry):
    """Swap-implied futures price and basis from real carry cost.

    swap_charge is the cost of CARRYING THE SPOT LEG for one lot for one
    day, in account currency, as a positive number. It is the spot
    symbol's financing cost — not the futures symbol's, which is
    typically zero on a dated contract.
    """
    position_value = spot_price * asset_cfg['lot_size']
    daily_swap_rate = (asset_cfg.get('swap_charge') or 0.0) / position_value
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
        # Only claim a carry adjustment when one was actually made. With
        # swap_charge unset (0 — the default until the operator supplies
        # the spot leg's financing cost) swap_basis is identically zero
        # and swap_diff IS the raw basis. Reporting "carry-detrended"
        # there sent the operator looking for the difference between a
        # spread of 59 and a spread of 59.
        carry_adjusted = abs(swap_basis) > 1e-9
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
        # The spread the strategy trades, spelled out. One number on a
        # card cannot be checked against the two prices next to it —
        # the operator has to be able to see WHY 4328.80 and 4269.73
        # make a spread of 9.13 rather than 59.07.
        'spread_formula': (
            "swap_diff = (futures - spot) - carry"
            if carry_adjusted else "swap_diff = futures - spot"),
        'swap_charge': asset_cfg.get('swap_charge') or 0.0,
    }
