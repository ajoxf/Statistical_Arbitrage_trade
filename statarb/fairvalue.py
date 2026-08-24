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
    value, detail, _ = _derive(asset_cfg, spot_price, hedge_ratio, now)
    return value, detail


def _derive(asset_cfg, spot_price, hedge_ratio=1.0, now=None):
    """(value, detail, inputs) — the derivation, and the NUMBERS it was
    built from so the card can show the arithmetic rather than only its
    answer. A bare "fair 48.88" is a figure the operator has no way to
    check, and catching a wrong contract month or multiplier is the
    entire reason fair value is on the screen."""
    pair_type = (asset_cfg.get('pair_type') or SPOT_FUTURE).upper()
    if pair_type not in BASIS_TYPES:
        return None, ('two different instruments — no arbitrage forces '
                      'them together, so the rolling mean is the only '
                      'anchor'), None

    beta = float(hedge_ratio or 1.0)
    rate = asset_cfg.get('risk_free_rate')
    if rate is None:
        return None, ('no carry rate configured — set it in '
                      'Settings > Pair Selection'), None
    rate = float(rate)

    far = years_until(asset_cfg.get('futures_expiry'), now)
    if far is None:
        return None, ('Leg B has no expiry in the future — a rolling '
                      'contract has no carry to compute'), None

    if pair_type == FUTURE_FUTURE:
        near = years_until(asset_cfg.get('spot_expiry'), now)
        if near is None:
            return None, ('Leg A has no expiry — a calendar spread needs '
                          'both, set it on the Exchanges page'), None
        gap = far - near
        if gap <= 0:
            return None, 'Leg A expires after Leg B — check the symbols', None
        years = gap
        detail = (f'Leg A {spot_price:.2f} compounded at the '
                  f'{rate * 100:.2f}% carry rate over {gap * 365.25:.0f} '
                  f'days between expiries (Settings > Pair Selection)')
        over = 'between expiries'
    else:
        years = far
        detail = (f'spot {spot_price:.2f} compounded at the '
                  f'{rate * 100:.2f}% carry rate over {far * 365.25:.0f} '
                  f'days to expiry (Settings > Pair Selection)')
        over = 'to expiry'

    fair_far = spot_price * math.exp(rate * years)
    inputs = {
        'base_price': spot_price,
        'rate_pct': rate * 100.0,
        'days': years * 365.25,
        'compounded': fair_far,
        'beta': beta,
        'over': over,
    }
    return fair_far - beta * spot_price, detail, inputs


#: How far the live spread may sit from the carry-implied one before
#: the pair_type itself is the likeliest explanation. Carry is a small,
#: slow number; a live spread several multiples away is not a mispricing
#: worth trading, it is a pair that carry does not describe.
IMPLAUSIBLE_GAP_MULT = 3.0


def mislabelled_pair(pair_type, spread, value):
    """Say when a basis label cannot be describing this pair.

    Live 2026-08-07: the operator repointed the engine at USOIL_U6 vs
    UKOIL_V6 — WTI against Brent — while pair_type still said
    SPOT_FUTURE from the gold setup. Carry over a couple of months on a
    $77 barrel is a few cents; the live spread was $5.03. The card
    dutifully rendered a theoretical basis two orders of magnitude away
    from the traded one, which reads as an enormous edge rather than as
    a mislabelled pair.

    WTI vs Brent is RELATED: no arbitrage ties them, so no fair value
    exists and none should be shown. This cannot detect that from
    prices alone — nothing in a quote says "different underlying" — but
    a gap this size is the symptom either way, and the other causes
    (wrong contract month, wrong multiplier, wrong contract size) are
    all worth the same warning.
    """
    if value is None or spread is None:
        return None
    slack = max(abs(value), 1e-9) * IMPLAUSIBLE_GAP_MULT
    if abs(spread - value) <= slack:
        return None
    return (f'The live spread ({spread:+.4f}) is nowhere near the '
            f'{pair_type} carry value ({value:+.4f}). Carry is small and '
            f'slow, so a gap this size usually means the pair is not a '
            f'basis pair at all (two related instruments are RELATED, '
            f'not {pair_type}) or the contract month, multiplier or '
            f'contract size is wrong. Reference only — it changes no '
            f'trading decision — but do not read it as edge.')


def fair_value_block(asset_cfg, spot_price, futures_price, spread,
                     hedge_ratio=1.0, now=None):
    """The reference block the dashboard shows under the spread."""
    value, detail, inputs = _derive(asset_cfg, spot_price,
                                    hedge_ratio, now)
    pair_type = (asset_cfg.get('pair_type') or SPOT_FUTURE).upper()
    return {
        'pair_type': pair_type,
        'fair_value': value,
        'fair_gap': (spread - value) if value is not None else None,
        'fair_detail': detail,
        'fair_inputs': inputs,
        'fair_warning': mislabelled_pair(pair_type, spread, value),
    }
