"""Theoretical fair value of the spread — REFERENCE ONLY.

Owner, 2026-08-06: "based on the Expiry and other calculations we
calculate the Fair value and display it on the dashboard - only for
reference - no interference with the signal".

The last section of this file is the important one: it pins the
"no interference" half of that instruction, because the previous build
DID let a theoretical carry into the traded series and that is what
made the spread unreconcilable.
"""

import math
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from statarb import fairvalue
from statarb.marketdata import compute_market_data


NOW = datetime(2026, 8, 6, 12, 0)


def gold(config, **overrides):
    asset = dict(config.ASSETS['GOLD'])
    asset.setdefault('pair_type', 'SPOT_FUTURE')
    asset.update(overrides)
    return asset


def ticks(spot=4269.73, futures=4328.80):
    return (SimpleNamespace(bid=spot - 0.1, ask=spot + 0.1, last=spot,
                            time=1000),
            SimpleNamespace(bid=futures - 0.1, ask=futures + 0.1,
                            last=futures, time=1000))


# --- spot vs future: carry to expiry ------------------------------------

def test_a_basis_pair_has_a_fair_value(config):
    asset = gold(config, pair_type='SPOT_FUTURE',
                 futures_expiry=NOW + timedelta(days=140),
                 risk_free_rate=0.0425)
    value, detail = fairvalue.fair_spread(asset, 4269.73, 4328.80, 1.0,
                                          now=NOW)
    years = 140 / 365.25
    expected = 4269.73 * math.exp(0.0425 * years) - 4269.73
    assert value == pytest.approx(expected)
    assert '4.25%' in detail and '140 days' in detail


def test_the_hedge_ratio_enters_the_fair_value_the_same_way(config):
    """Fair value must be comparable to the LIVE spread, so it uses the
    same formula: futures - beta * spot."""
    asset = gold(config, futures_expiry=NOW + timedelta(days=140))
    full, _ = fairvalue.fair_spread(asset, 4269.73, 4328.80, 1.0, now=NOW)
    half, _ = fairvalue.fair_spread(asset, 4269.73, 4328.80, 0.5, now=NOW)
    assert half == pytest.approx(full + 0.5 * 4269.73)


def test_a_higher_carry_rate_means_a_wider_fair_basis(config):
    asset = gold(config, futures_expiry=NOW + timedelta(days=140))
    low, _ = fairvalue.fair_spread(dict(asset, risk_free_rate=0.01),
                                   4269.73, 4328.80, 1.0, now=NOW)
    high, _ = fairvalue.fair_spread(dict(asset, risk_free_rate=0.08),
                                    4269.73, 4328.80, 1.0, now=NOW)
    assert high > low > 0


# --- future vs future: carry between the two expiries -------------------

def test_a_calendar_spread_uses_the_gap_between_expiries(config):
    asset = gold(config, pair_type='FUTURE_FUTURE',
                 spot_expiry=NOW + timedelta(days=30),
                 futures_expiry=NOW + timedelta(days=120),
                 risk_free_rate=0.0425)
    value, detail = fairvalue.fair_spread(asset, 4269.73, 4328.80, 1.0,
                                          now=NOW)
    gap = 90 / 365.25
    assert value == pytest.approx(4269.73 * math.exp(0.0425 * gap) - 4269.73)
    assert 'between expiries' in detail


def test_a_calendar_spread_without_leg_a_expiry_says_so(config):
    asset = gold(config, pair_type='FUTURE_FUTURE', spot_expiry=None,
                 futures_expiry=NOW + timedelta(days=120))
    value, detail = fairvalue.fair_spread(asset, 4269.73, 4328.80, 1.0,
                                          now=NOW)
    assert value is None
    assert 'Leg A has no expiry' in detail


def test_legs_in_the_wrong_order_are_refused(config):
    asset = gold(config, pair_type='FUTURE_FUTURE',
                 spot_expiry=NOW + timedelta(days=120),
                 futures_expiry=NOW + timedelta(days=30))
    value, detail = fairvalue.fair_spread(asset, 4269.73, 4328.80, 1.0,
                                          now=NOW)
    assert value is None and 'check the symbols' in detail


# --- two different instruments have no fair value -----------------------

def test_wti_versus_brent_has_no_fair_value(config):
    """No arbitrage forces them together — the rolling mean is the only
    anchor there is, and pretending otherwise would be a made-up
    number on the operator's screen."""
    asset = gold(config, pair_type='RELATED',
                 futures_expiry=NOW + timedelta(days=120))
    value, detail = fairvalue.fair_spread(asset, 70.0, 74.0, 1.0, now=NOW)
    assert value is None
    assert 'no arbitrage forces them together' in detail


# --- it refuses rather than guesses -------------------------------------

def test_a_rolling_contract_has_no_fair_value(config):
    asset = gold(config, futures_expiry=None)
    value, detail = fairvalue.fair_spread(asset, 4269.73, 4328.80, 1.0,
                                          now=NOW)
    assert value is None and 'no expiry in the future' in detail


def test_an_expired_contract_has_no_fair_value(config):
    asset = gold(config, futures_expiry=NOW - timedelta(days=5))
    value, _ = fairvalue.fair_spread(asset, 4269.73, 4328.80, 1.0, now=NOW)
    assert value is None


def test_a_missing_carry_rate_is_not_assumed_to_be_zero(config):
    asset = gold(config, futures_expiry=NOW + timedelta(days=140))
    asset['risk_free_rate'] = None
    value, detail = fairvalue.fair_spread(asset, 4269.73, 4328.80, 1.0,
                                          now=NOW)
    assert value is None and 'no carry rate' in detail


# --- the snapshot carries it, labelled ----------------------------------

def test_the_snapshot_carries_the_fair_value_and_the_gap(config):
    asset = gold(config, futures_expiry=datetime.now() + timedelta(days=140))
    data = compute_market_data(asset, *ticks(), 1.0)
    assert data['pair_type'] == 'SPOT_FUTURE'
    assert data['fair_value'] is not None
    assert data['fair_gap'] == pytest.approx(
        data['spread'] - data['fair_value'])


def test_the_snapshot_says_why_there_is_none(config):
    data = compute_market_data(gold(config, pair_type='RELATED'),
                               *ticks(), 1.0)
    assert data['fair_value'] is None and data['fair_gap'] is None
    assert data['fair_detail']


def test_the_ui_gets_it_flagged_as_reference():
    from statarb import webapi
    ui = webapi.status_to_ui({'assets': [{
        'asset': 'GOLD', 'z': 1.2, 'spread': 59.07, 'basis': 59.07,
        'pair_type': 'SPOT_FUTURE', 'fair_value': 71.2, 'fair_gap': -12.13,
        'fair_detail': 'spot compounded', 'samples': 80,
    }]}, {})
    assert ui['signal']['fair_value'] == 71.2
    assert ui['signal']['fair_gap'] == -12.13
    assert ui['signal']['pair_type'] == 'SPOT_FUTURE'


# --- NO INTERFERENCE WITH THE SIGNAL ------------------------------------
# The whole point. A previous build let a theoretical carry into the
# traded series; that is what made the spread impossible to reconcile
# against the two prices beside it.

def test_the_traded_spread_ignores_the_carry_rate(config):
    base = gold(config, futures_expiry=datetime.now() + timedelta(days=140))
    cheap = compute_market_data(dict(base, risk_free_rate=0.001),
                                *ticks(), 1.0)
    dear = compute_market_data(dict(base, risk_free_rate=0.40),
                               *ticks(), 1.0)
    assert cheap['spread'] == dear['spread'] == pytest.approx(59.07)
    assert cheap['fair_value'] != dear['fair_value']   # only THIS moved


def test_the_traded_spread_ignores_the_pair_type(config):
    base = gold(config, futures_expiry=datetime.now() + timedelta(days=140))
    spreads = [compute_market_data(dict(base, pair_type=kind),
                                   *ticks(), 1.0)['spread']
               for kind in fairvalue.PAIR_TYPES]
    assert spreads == [pytest.approx(59.07)] * len(fairvalue.PAIR_TYPES)


def test_no_signal_or_exit_module_imports_fair_value():
    """A structural guarantee, not a behavioural one: if fairvalue is
    never imported there, it can never leak into a decision."""
    import inspect
    from statarb import costs, exits, pair_executor, signals, spread
    for module in (signals, exits, spread, costs, pair_executor):
        source = inspect.getsource(module)
        assert 'fairvalue' not in source
        assert 'fair_value' not in source


def test_the_z_score_is_computed_on_the_traded_spread_only(config,
                                                           tmp_path,
                                                           monkeypatch):
    """The coordinator feeds SpreadStats market_data['spread'] — never
    the fair value or the gap."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(config, trading_mode='PAPER')
    fed = []

    class RecordingStats:
        samples, mu, sigma, z, half_life_sec = [], None, None, None, None
        degenerate, warm = False, False

        def update(self, value, quote_id=None):
            fed.append(value)

    coord.stats = {'GOLD': RecordingStats()}
    md = compute_market_data(
        gold(config, futures_expiry=datetime.now() + timedelta(days=140)),
        *ticks(), 1.0)
    coord.process_asset('GOLD', md)
    assert fed == [md['spread']]
    assert md['fair_value'] not in fed and md['fair_gap'] not in fed


# --- the arithmetic, not just the answer --------------------------------
# Operator, 2026-08-24: "Make the Fair Spread font Larger. Also, show a
# brief calculation." Catching a wrong contract month or multiplier is
# the whole reason fair value is on the screen, and a bare "fair 48.88"
# cannot be checked against anything.

def test_the_block_publishes_the_numbers_it_was_derived_from():
    asset = {'pair_type': 'SPOT_FUTURE', 'risk_free_rate': 0.0425,
             'futures_expiry': datetime(2026, 11, 25)}
    block = fairvalue.fair_value_block(asset, 4269.73, 4328.80, 59.07,
                                       1.0, now=NOW)
    inputs = block['fair_inputs']
    assert inputs['base_price'] == 4269.73
    assert inputs['rate_pct'] == pytest.approx(4.25)
    assert inputs['beta'] == 1.0
    assert inputs['days'] > 0


def test_the_published_inputs_reproduce_the_published_value():
    """The card renders these; if they did not multiply out to the same
    number the operator would be checking one figure against another."""
    import math
    asset = {'pair_type': 'SPOT_FUTURE', 'risk_free_rate': 0.0425,
             'futures_expiry': datetime(2026, 11, 25)}
    block = fairvalue.fair_value_block(asset, 4269.73, 4328.80, 59.07,
                                       0.98, now=NOW)
    i = block['fair_inputs']
    assert i['compounded'] == pytest.approx(
        i['base_price'] * math.exp(i['rate_pct'] / 100.0
                                   * i['days'] / 365.25))
    assert i['compounded'] - i['beta'] * i['base_price'] == \
        pytest.approx(block['fair_value'])


def test_no_fair_value_publishes_no_inputs():
    """A RELATED pair has no derivation to show, and half a derivation
    would render as arithmetic that does not reach the answer."""
    asset = {'pair_type': 'RELATED', 'risk_free_rate': 0.0425,
             'futures_expiry': datetime(2026, 11, 25)}
    block = fairvalue.fair_value_block(asset, 70.0, 74.0, 4.0, 1.0, now=NOW)
    assert block['fair_value'] is None
    assert block['fair_inputs'] is None


# --- is the gap a bad pair, or a stale rate? ----------------------------
# Operator, 2026-08-24: "Why is the Fair Spread in this calculating an
# incorrect Value" — fair 48.72 against a live 57.01. The arithmetic was
# right; risk_free_rate was a hand-typed 4.25% and the market was paying
# nearer 5%. The card could not tell that apart from a mislabelled pair,
# which is the thing it exists to catch.

def test_the_implied_rate_inverts_the_fair_value_formula():
    """Feed the fair spread back in and the configured rate comes out."""
    asset = {'pair_type': 'SPOT_FUTURE', 'risk_free_rate': 0.0425,
             'futures_expiry': datetime(2026, 11, 25)}
    block = fairvalue.fair_value_block(asset, 4269.73, 4328.80, 0.0,
                                       1.0, now=NOW)
    i = block['fair_inputs']
    assert fairvalue.implied_rate(i['base_price'], block['fair_value'],
                                  i['beta'], i['years']) == \
        pytest.approx(4.25, abs=1e-6)


def test_a_wider_live_spread_implies_a_higher_rate():
    """The operator's own screen: gold spot 4658.10, a live spread of
    57.01, and 90 days to a 2026-11-22 expiry. Fair value read 48.72 and
    looked wrong; the market was simply paying ~4.9% against a
    hand-typed 4.25%."""
    asset = {'pair_type': 'SPOT_FUTURE', 'risk_free_rate': 0.0425,
             'futures_expiry': datetime(2026, 11, 22)}
    block = fairvalue.fair_value_block(asset, 4658.10, 4714.22, 57.01, 1.0,
                                       now=datetime(2026, 8, 24, 14, 0))
    implied = block['fair_inputs']['implied_rate_pct']
    assert block['fair_value'] == pytest.approx(48.7, abs=0.5)
    assert implied > block['fair_inputs']['rate_pct']
    # A gold basis this size is a funding rate, not a broken pair.
    assert 4.5 < implied < 6.0


def test_an_impossible_spread_implies_no_rate():
    """A spread below -beta x S puts the far price at or under zero, and
    there is no rate that produces it."""
    assert fairvalue.implied_rate(100.0, -200.0, 1.0, 0.25) is None
    assert fairvalue.implied_rate(100.0, 5.0, 1.0, 0) is None
