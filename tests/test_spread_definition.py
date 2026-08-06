"""The spread the strategy trades.

Owner's definition (2026-08-06):

    spread = Leg B - hedge_ratio * Leg A
           = futures - HEDGE_RATIO * spot

Leg B minus the hedge ratio times Leg A, and nothing else. No carry
term, no swap cost, no dependence on the futures expiry — the previous
build subtracted a swap-implied carry, which made the number on the
dashboard impossible to reconcile against the two prices beside it and
made the whole series depend on a swap figure nobody could verify.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from statarb.marketdata import compute_market_data


def ticks(spot=4269.73, futures=4328.80):
    return (SimpleNamespace(bid=spot - 0.1, ask=spot + 0.1, last=spot,
                            time=1000),
            SimpleNamespace(bid=futures - 0.1, ask=futures + 0.1,
                            last=futures, time=1000))


def gold(config, **overrides):
    asset = dict(config.ASSETS['GOLD'])
    asset.update(overrides)
    return asset


# --- the formula --------------------------------------------------------

def test_the_spread_is_leg_b_minus_ratio_times_leg_a(config):
    data = compute_market_data(gold(config), *ticks(), 1.0)
    assert data['spread'] == pytest.approx(59.07)      # 4328.80 - 4269.73


def test_the_hedge_ratio_scales_the_spot_leg(config):
    data = compute_market_data(gold(config), *ticks(), 0.5)
    assert data['spread'] == pytest.approx(4328.80 - 0.5 * 4269.73)
    assert data['hedge_ratio'] == 0.5


def test_the_raw_basis_is_still_reported_alongside(config):
    """The hedge-ratio spread is what is traded; futures minus spot is
    still worth seeing, and they differ once the ratio is not 1."""
    data = compute_market_data(gold(config), *ticks(), 0.5)
    assert data['actual_basis'] == pytest.approx(59.07)
    assert data['spread'] != data['actual_basis']


def test_a_missing_hedge_ratio_defaults_to_one(config):
    assert compute_market_data(gold(config), *ticks())['spread'] == \
        pytest.approx(59.07)
    assert compute_market_data(gold(config), *ticks(), None)['spread'] == \
        pytest.approx(59.07)


# --- nothing about the carry may touch it -------------------------------

def test_the_swap_charge_does_not_move_the_spread(config):
    """This is the change: a swap figure used to shift the entire
    series, and the operator had no way to check it."""
    without = compute_market_data(gold(config, swap_charge=0.0), *ticks(), 1.0)
    with_swap = compute_market_data(gold(config, swap_charge=45.0),
                                    *ticks(), 1.0)
    assert with_swap['spread'] == without['spread']


def test_the_futures_expiry_does_not_move_the_spread(config):
    near = compute_market_data(
        gold(config, futures_expiry=datetime.now() + timedelta(days=3)),
        *ticks(), 1.0)
    far = compute_market_data(
        gold(config, futures_expiry=datetime.now() + timedelta(days=400)),
        *ticks(), 1.0)
    none = compute_market_data(gold(config, futures_expiry=None),
                               *ticks(), 1.0)
    assert near['spread'] == far['spread'] == none['spread']


def test_a_past_expiry_no_longer_changes_anything(config):
    """An expired contract used to zero the swap basis and silently
    change the spread definition underneath the operator."""
    stale = compute_market_data(
        gold(config, futures_expiry=datetime.now() - timedelta(days=5)),
        *ticks(), 1.0)
    assert stale['spread'] == pytest.approx(59.07)


def test_the_snapshot_carries_no_carry_fields(config):
    data = compute_market_data(gold(config), *ticks(), 1.0)
    for gone in ('swap_diff', 'swap_basis', 'swap_premium_pct',
                 'carry_adjusted', 'swap_futures_price', 'annual_swap_rate'):
        assert gone not in data


# --- it says what it is -------------------------------------------------

def test_the_snapshot_spells_out_the_formula(config):
    assert compute_market_data(gold(config), *ticks(),
                               1.0)['spread_formula'] == \
        'spread = futures - 1 x spot'
    assert compute_market_data(gold(config), *ticks(),
                               0.5)['spread_formula'] == \
        'spread = futures - 0.5 x spot'


def test_the_log_states_the_definition_at_startup(tmp_path, monkeypatch,
                                                  config, caplog):
    """"The spread seems incorrect" is usually the operator checking it
    against the two prices beside it."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(config, trading_mode='PAPER')
    coord.config.TRADING['HEDGE_RATIO'] = 1.0
    with caplog.at_level('INFO'):
        coord._log_spread_definition('GOLD')
    assert 'spread = futures - 1 x spot' in caplog.text


def test_the_ui_gets_the_pieces_of_the_spread():
    from statarb import webapi
    ui = webapi.status_to_ui({'assets': [{
        'asset': 'GOLD', 'z': 1.2, 'spread': 59.07, 'basis': 59.07,
        'hedge_ratio': 1.0,
        'spread_formula': 'spread = futures - 1 x spot',
        'samples': 80, 'min_samples': 50, 'lookback': 7200,
    }]}, {})
    signal = ui['signal']
    assert signal['spread'] == 59.07
    assert signal['raw_basis'] == 59.07
    assert signal['spread_hedge_ratio'] == 1.0
    assert signal['spread_formula'] == 'spread = futures - 1 x spot'


# --- the coordinator feeds the configured ratio in ----------------------

def test_the_coordinator_uses_the_configured_hedge_ratio(tmp_path,
                                                         monkeypatch,
                                                         config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coord = Coordinator(config, trading_mode='PAPER')
    coord.config.TRADING['HEDGE_RATIO'] = 0.5
    coord.active_assets = {'GOLD': {
        'config': gold(config), 'spot_symbol': 'XAUUSD_',
        'futures_symbol': 'GC1226', 'last_data': None}}

    class TickLeg:
        def __init__(self, price):
            self.price = price

        def tick(self, symbol):
            return {'bid': self.price - 0.1, 'ask': self.price + 0.1,
                    'last': self.price, 'time': 1000}

    coord.spot_leg = TickLeg(4269.73)
    coord.futures_leg = TickLeg(4328.80)
    data = coord.get_market_data('GOLD')
    assert data['spread'] == pytest.approx(4328.80 - 0.5 * 4269.73)
