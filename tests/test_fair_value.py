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
