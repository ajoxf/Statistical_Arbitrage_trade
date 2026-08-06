"""The spread the strategy trades, built from two ticks.

    spread = futures - hedge_ratio * spot

Owner's definition (2026-08-06). Leg B minus the hedge ratio times Leg
A, and nothing else: no carry term, no swap cost, no dependence on the
futures expiry. The hedge ratio is the same number that sizes the
hedge, so the spread is exactly the P&L of the pair per unit — which is
why HEDGE_RATIO is structural and cannot change under an open position.

This replaced a carry-detrended spread (basis minus a swap-implied
basis). The theory there was that the raw basis drifts toward zero as
the contract approaches expiry, biasing a rolling mean. It does, but
the drift is spread over months while the window is hours: on gold at a
59-point basis four months out, that is ~0.03 of drift across a
two-hour window — far below the noise the z-score is measuring. What
the carry term did do reliably was make the spread depend on a swap
number nobody could verify.
"""

from datetime import datetime

from .fairvalue import fair_value_block


def compute_market_data(asset_cfg, spot_tick, futures_tick,
                        hedge_ratio=1.0):
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

    beta = float(hedge_ratio or 1.0)
    spread = futures_price - beta * spot_price
    actual_basis = futures_price - spot_price      # raw, for reference

    # Expiry is OPTIONAL and no longer touches the spread — it is kept
    # only so the operator can see how far out the contract is and be
    # warned when it has rolled.
    expiry = asset_cfg.get('futures_expiry')
    time_to_expiry = ((expiry - datetime.now()).total_seconds()
                      / (365.25 * 24 * 3600)) if expiry else None
    days_to_expiry = time_to_expiry * 365.25 if time_to_expiry else 0

    snapshot = {
        'asset_name': asset_cfg['name'],
        'timestamp': datetime.now(),
        'quote_id': quote_id,
        'spot_price': spot_price,
        'futures_price': futures_price,
        'spot_bid': spot_tick.bid,
        'spot_ask': spot_tick.ask,
        'futures_bid': futures_tick.bid * multiplier,
        'futures_ask': futures_tick.ask * multiplier,
        'spot_spread': (spot_tick.ask - spot_tick.bid) * 100,
        'futures_spread': (futures_tick.ask - futures_tick.bid) * 100,
        'spread_unit': '¢',
        'spread': spread,
        'actual_basis': actual_basis,
        'hedge_ratio': beta,
        'basis_pct': (actual_basis / spot_price * 100) if spot_price else 0.0,
        'time_to_expiry': time_to_expiry,
        'days_to_expiry': days_to_expiry,
        # Spelled out so the number on the card can be checked against
        # the two prices beside it.
        'spread_formula': f"spread = futures - {beta:g} x spot",
    }
    # Reference only. Nothing in the signal, sizing or exit path reads
    # these keys — the traded spread above is already final.
    snapshot.update(fair_value_block(asset_cfg, spot_price, futures_price,
                                     spread, beta))
    return snapshot
