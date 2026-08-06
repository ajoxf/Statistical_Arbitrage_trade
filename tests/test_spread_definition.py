"""What the number labelled "Spread" actually is, and where the carry
cost behind it comes from.

Operator, 2026-08-06: "The Spread seems incorrect". The card showed a
carry-detrended spread with no indication that a carry had been
subtracted — and the carry itself was read off the WRONG LEG and never
adopted, so it was whatever happened to be typed in config.
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


# --- "carry-adjusted" must mean a carry was actually subtracted ----------

def test_no_carry_cost_means_the_spread_is_the_raw_basis(config):
    """swap_charge defaults to 0, so swap_basis is identically zero and
    swap_diff IS futures minus spot. Claiming 'carry-detrended' there
    sent the operator hunting for a difference that did not exist."""
    asset = gold(config, swap_charge=0.0,
                 futures_expiry=datetime.now() + timedelta(days=140))
    data = compute_market_data(asset, *ticks())
    assert data['swap_diff'] == pytest.approx(59.07)
    assert data['carry_adjusted'] is False
    assert data['spread_formula'] == 'swap_diff = futures - spot'


def test_a_real_carry_cost_detrends_the_basis(config):
    asset = gold(config, swap_charge=35.0,
                 futures_expiry=datetime.now() + timedelta(days=140))
    data = compute_market_data(asset, *ticks())
    assert data['carry_adjusted'] is True
    assert data['swap_basis'] > 0
    assert data['swap_diff'] < data['actual_basis']
    assert data['spread_formula'] == 'swap_diff = (futures - spot) - carry'


def test_the_snapshot_spells_out_its_own_derivation(config):
    """The dashboard shows one number next to two prices; without the
    pieces there is no way to tell a detrended spread from a broken."""
    asset = gold(config, swap_charge=35.0,
                 futures_expiry=datetime.now() + timedelta(days=140))
    data = compute_market_data(asset, *ticks())
    assert data['actual_basis'] == pytest.approx(59.07)
    assert data['swap_diff'] == pytest.approx(
        data['actual_basis'] - data['swap_basis'])
    assert data['swap_charge'] == 35.0


# --- the carry comes from the SPOT leg, in the right units and sign ------

class SpecLeg:
    def __init__(self, name, report):
        self.name = name
        self._report = report

    def symbol_report(self, symbol):
        return dict(self._report, symbol=symbol, found=True)

    def close(self):
        pass


@pytest.fixture
def coord(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    return Coordinator(config, trading_mode='PAPER')


def swap_of(coord, asset, **report):
    coord._adopt_swap_charge('GOLD', asset, dict(report))
    return asset.get('swap_charge')


def test_a_currency_swap_is_taken_as_the_daily_cost(coord, config):
    """swap_mode 2/3/4: already an amount per lot per day. The broker
    reports a DEBIT as negative; the carry model wants a cost."""
    asset = gold(config, swap_charge=0.0)
    assert swap_of(coord, asset, swap_long=-35.0, swap_mode=2) == 35.0


def test_a_points_swap_is_converted_not_taken_literally(coord, config):
    """swap_mode 1 is in POINTS. Taking -35 points as -$35 on gold is
    wrong by the point size times the contract size — here 100x."""
    asset = gold(config, swap_charge=0.0, lot_size=100)
    charge = swap_of(coord, asset, swap_long=-3.5, swap_mode=1, point=0.01)
    assert charge == pytest.approx(3.5)          # 3.5 x 0.01 x 100


def test_an_interest_rate_swap_is_refused_rather_than_guessed(coord,
                                                              config):
    """swap_mode 5/6 is an annual percentage. A wrong carry is worse
    than none — it shifts the whole spread series."""
    asset = gold(config, swap_charge=0.0)
    assert not swap_of(coord, asset, swap_long=-2.5, swap_mode=5)


def test_an_unknown_swap_mode_is_refused(coord, config):
    asset = gold(config, swap_charge=0.0)
    assert not swap_of(coord, asset, swap_long=-35.0, swap_mode=None)


def test_a_configured_swap_charge_is_never_overwritten(coord, config):
    asset = gold(config, swap_charge=12.0)
    assert swap_of(coord, asset, swap_long=-35.0, swap_mode=2) == 12.0


def test_the_carry_is_read_from_the_spot_leg(coord, config, caplog):
    """It used to read the FUTURES symbol's swap — the wrong leg, and
    typically zero on a dated contract — and only logged it."""
    asset = gold(config, swap_charge=0.0, lot_size=100)
    coord.spot_leg = SpecLeg('account_a', {
        'contract_size': 100, 'swap_long': -35.0, 'swap_mode': 2,
        'point': 0.01})
    coord.futures_leg = SpecLeg('account_b', {
        'contract_size': 100, 'swap_long': 0.0, 'swap_mode': 2,
        'point': 0.01, 'expiry': 0})
    coord._adopt_broker_specs('GOLD', asset, 'XAUUSD_', 'GC1226')
    assert asset['swap_charge'] == 35.0


def test_the_log_says_what_the_spread_is(coord, config, caplog):
    """'The spread seems incorrect' is usually the operator comparing a
    detrended spread against futures minus spot."""
    asset = gold(config, swap_charge=35.0,
                 futures_expiry=datetime.now() + timedelta(days=140))
    with caplog.at_level('INFO'):
        coord._log_spread_definition('GOLD', asset)
    assert 'NOT equal futures minus spot' in caplog.text


def test_the_log_says_so_when_there_is_no_carry(coord, config, caplog):
    asset = gold(config, swap_charge=0.0, futures_expiry=None)
    with caplog.at_level('INFO'):
        coord._log_spread_definition('GOLD', asset)
    assert 'raw basis' in caplog.text


# --- what reaches the dashboard -----------------------------------------

def test_the_ui_gets_the_pieces_of_the_spread():
    from statarb import webapi
    ui = webapi.status_to_ui({'assets': [{
        'asset': 'GOLD', 'z': 1.2, 'swap_diff': 9.13, 'basis': 59.07,
        'swap_basis': 49.94, 'carry_adjusted': True,
        'spread_formula': 'swap_diff = (futures - spot) - carry',
        'samples': 80, 'min_samples': 50, 'lookback': 7200,
    }]}, {})
    signal = ui['signal']
    assert signal['spread'] == 9.13
    assert signal['raw_basis'] == 59.07
    assert signal['carry'] == 49.94
    assert signal['carry_adjusted'] is True
