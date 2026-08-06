"""Theoretical fair value of the spread — REFERENCE ONLY.

Owner, 2026-08-06: "we calculate the Fair value and display it on the
dashboard - only for reference - no interference with the signal".

Nothing in the signal, sizing or exit path may read anything in this
module. The engine trades `spread = futures - HEDGE_RATIO * spot`,
z-scored against its own rolling mean; fair value sits beside that
number and answers a different question — is the mean the z-score is
anchored on anywhere near what carry says it should be? A large gap
usually means the CONFIGURATION is wrong (wrong contract month, wrong
multiplier, a per-contract quote where a per-ounce one was assumed),
not that the trade is good.

This is deliberately kept out of marketdata's hot path arithmetic and
returns None whenever it cannot answer honestly. A missing fair value
is a fine outcome; a guessed one is not — that is exactly how the old
carry-detrended spread went wrong.

Pair types:

  SPOT_FUTURE    Leg A is spot, Leg B a dated future on the same
                 underlying. Fair futures price is the spot compounded
                 at the carry rate to expiry.
  FUTURE_FUTURE  Both legs dated futures on the same underlying (a
                 calendar spread). Fair value of the far leg is the
                 near leg compounded over the gap between expiries.
  RELATED        Two different instruments (WTI vs BRENT, gold vs
                 silver). No arbitrage forces them together, so there
                 is no fair value — the only meaningful anchor is the
                 empirical mean, which SpreadStats already provides.
"""

import math
from datetime import datetime

SPOT_FUTURE = 'SPOT_FUTURE'
FUTURE_FUTURE = 'FUTURE_FUTURE'
RELATED = 'RELATED'

PAIR_TYPES = (SPOT_FUTURE, FUTURE_FUTURE, RELATED)

# A basis pair is one where the two legs are the same underlying, so
# carry ties them together. Only these have a fair value.
BASIS_TYPES = (SPOT_FUTURE, FUTURE_FUTURE)

YEAR_SECONDS = 365.25 * 24 * 3600


def years_until(expiry, now=None):
    """Year fraction to expiry, or None if it is missing or passed."""
    if not expiry:
        return None
    now = now or datetime.now()
    years = (expiry - now).total_seconds() / YEAR_SECONDS
    return years if years > 0 else None


def fair_spread(asset_cfg, spot_price, futures_price, hedge_ratio=1.0,
                now=None):
    """Theoretical value of `futures - hedge_ratio * spot`.

    Returns (fair_value, detail) where detail explains the derivation or
    says why there isn't one. fair_value is None whenever the inputs
    cannot support an honest answer.
    """
    pair_type = (asset_cfg.get('pair_type') or SPOT_FUTURE).upper()
    if pair_type not in BASIS_TYPES:
        return None, ('two different instruments — no arbitrage forces '
                      'them together, so the rolling mean is the only '
                      'anchor')

    beta = float(hedge_ratio or 1.0)
    rate = asset_cfg.get('risk_free_rate')
    if rate is None:
        return None, 'no carry rate configured'
    rate = float(rate)

    far = years_until(asset_cfg.get('futures_expiry'), now)
    if far is None:
        return None, ('Leg B has no expiry in the future — a rolling '
                      'contract has no carry to compute')

    if pair_type == FUTURE_FUTURE:
        near = years_until(asset_cfg.get('spot_expiry'), now)
        if near is None:
            return None, ('Leg A has no expiry — a calendar spread needs '
                          'both, set it on the Exchanges page')
        gap = far - near
        if gap <= 0:
            return None, 'Leg A expires after Leg B — check the symbols'
        fair_far = spot_price * math.exp(rate * gap)
        detail = (f'Leg A {spot_price:.2f} compounded at {rate * 100:.2f}% '
                  f'over {gap * 365.25:.0f} days between expiries')
    else:
        fair_far = spot_price * math.exp(rate * far)
        detail = (f'spot {spot_price:.2f} compounded at {rate * 100:.2f}% '
                  f'over {far * 365.25:.0f} days to expiry')

    return fair_far - beta * spot_price, detail


def fair_value_block(asset_cfg, spot_price, futures_price, spread,
                     hedge_ratio=1.0, now=None):
    """The reference block the dashboard shows under the spread."""
    value, detail = fair_spread(asset_cfg, spot_price, futures_price,
                                hedge_ratio, now)
    return {
        'pair_type': (asset_cfg.get('pair_type') or SPOT_FUTURE).upper(),
        'fair_value': value,
        'fair_gap': (spread - value) if value is not None else None,
        'fair_detail': detail,
    }
