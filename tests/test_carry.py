"""Carry to expiry: is the spread wider than the cost of waiting?

Operator, 2026-08-19: "swap can be entered manually and the number of
days can be calculated to identify if the Spread is higher or not."

A different question from the z-score. z asks whether the spread is
unusual against its own history; this asks whether today's spread
beats what it costs to hold the pair until the contract expires — a
date on which the trade is decided whether or not anything reverts.
"""

from datetime import datetime, timedelta

import pytest

from statarb import carry


# --- days ----------------------------------------------------------------

def test_days_counts_to_the_expiry():
    now = datetime(2026, 8, 19, 12, 0)
    assert carry.days_to_expiry(datetime(2026, 11, 25), now) == \
        pytest.approx(97.5, abs=0.1)


def test_a_rolling_contract_has_no_convergence_date():
    """No expiry is not zero days — it means this trade has no date."""
    assert carry.days_to_expiry(None) is None


def test_an_expired_contract_is_not_a_negative_hold():
    now = datetime(2026, 8, 19)
    assert carry.days_to_expiry(datetime(2026, 8, 1), now) is None


# --- swap units ----------------------------------------------------------
# The same "-4.5" is points on one symbol, account currency on another
# and percent a year on a third. Reading it as money regardless is how
# the old carry-detrended spread produced a basis nobody could
# reconcile against the two prices beside it.

def test_money_modes_are_taken_as_quoted():
    value, note = carry.swap_per_lot_night(-4.5, carry.SWAP_CURRENCY_DEPOSIT)
    assert value == pytest.approx(-4.5)
    assert 'as quoted' in note


def test_points_are_priced_by_tick_value():
    value, note = carry.swap_per_lot_night(
        -4.5, carry.SWAP_POINTS, tick_size=0.01, tick_value=10.0)
    assert value == pytest.approx(-4.5 * 1000)
    assert 'tick value' in note


def test_points_fall_back_to_contract_size_and_say_so():
    value, note = carry.swap_per_lot_night(
        -2.0, carry.SWAP_POINTS, contract_size=1000.0)
    assert value == pytest.approx(-2000.0)
    assert 'no tick value' in note


def test_annual_percent_needs_a_notional():
    value, _ = carry.swap_per_lot_night(
        -3.0, carry.SWAP_INTEREST_CURRENT, contract_size=1000.0, price=85.0)
    assert value == pytest.approx(85000.0 * -0.03 / 360.0)
    missing, why = carry.swap_per_lot_night(-3.0,
                                            carry.SWAP_INTEREST_CURRENT)
    assert missing is None and 'contract size' in why


def test_an_unknown_mode_is_none_not_zero():
    """A swap that cannot be converted is not a swap of zero."""
    value, why = carry.swap_per_lot_night(-4.5, carry.SWAP_REOPEN_BID)
    assert value is None and 'not one this can convert' in why
    value, why = carry.swap_per_lot_night(-4.5, None)
    assert value is None and 'swap_mode' in why


def test_a_positive_swap_keeps_its_sign():
    """A pair is long one leg and short the other, so the two often
    pull opposite ways — and being PAID to wait is the case worth
    finding."""
    value, _ = carry.swap_per_lot_night(+1.25, carry.SWAP_CURRENCY_DEPOSIT)
    assert value == pytest.approx(1.25)


# --- the decision --------------------------------------------------------

def test_a_spread_wider_than_the_carry_is_an_edge():
    plan = carry.convergence_plan(
        spread=-7.22, days=90.0, spread_units=110.0,
        legs=[(-0.40, 0.11, 'leg A'), (-0.30, 0.11, 'leg B')],
        cost_usd=39.0)
    assert plan['gross_usd'] == pytest.approx(7.22 * 110)
    # (-0.40 - 0.30) x 0.11 lots x 90 nights
    assert plan['carry_usd'] == pytest.approx(-0.70 * 0.11 * 90)
    assert plan['net_usd'] == pytest.approx(794.2 - 6.93 - 39.0, abs=0.5)
    assert plan['net_usd'] > 0


def test_carry_can_be_a_credit_and_adds_to_the_edge():
    plan = carry.convergence_plan(
        spread=1.0, days=30.0, spread_units=100.0,
        legs=[(+0.50, 1.0, 'leg A'), (-0.10, 1.0, 'leg B')], cost_usd=10.0)
    assert plan['carry_usd'] == pytest.approx(0.40 * 30)
    assert plan['net_usd'] == pytest.approx(100.0 + 12.0 - 10.0)


def test_carry_can_swallow_the_whole_spread():
    plan = carry.convergence_plan(
        spread=0.20, days=120.0, spread_units=110.0,
        legs=[(-0.40, 0.11, 'a'), (-0.30, 0.11, 'b')], cost_usd=39.0)
    assert plan['net_usd'] < 0


def test_one_unconvertible_leg_makes_the_whole_estimate_none():
    """Half a carry estimate is not a smaller estimate."""
    plan = carry.convergence_plan(
        spread=-7.22, days=90.0, spread_units=110.0,
        legs=[(-0.40, 0.11, 'leg A'),
              (None, 0.11, 'leg B swap mode 8 is not convertible')],
        cost_usd=39.0)
    assert plan['net_usd'] is None
    assert plan['carry_usd'] is None
    assert 'mode 8' in plan['reason']


def test_no_expiry_says_there_is_no_date_rather_than_no_carry():
    plan = carry.convergence_plan(spread=-7.22, days=None,
                                  spread_units=110.0, legs=[])
    assert plan['net_usd'] is None
    assert 'never converges' in plan['reason']


def test_the_direction_does_not_change_what_convergence_pays():
    """At expiry the future IS the spot, so the whole spread is the
    prize whichever side of the mean it sits."""
    up = carry.convergence_plan(spread=+2.0, days=30.0, spread_units=100.0,
                                legs=[(0.0, 1.0, 'a')], cost_usd=0.0)
    down = carry.convergence_plan(spread=-2.0, days=30.0, spread_units=100.0,
                                  legs=[(0.0, 1.0, 'a')], cost_usd=0.0)
    assert up['gross_usd'] == down['gross_usd'] == pytest.approx(200.0)


# --- how the coordinator prices the two legs ----------------------------
# The module arithmetic is above; these cover the wiring, which is where
# the two mistakes that matter live: reading the wrong side of the swap,
# and letting an override that cannot be cleared outlive its pair.

@pytest.fixture
def coord(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from statarb.config import AlgoTradingConfig
    from statarb.coordinator import Coordinator, PaperExecutor

    class Leg:
        name = 'broker'

        def ensure_symbol(self, sym):
            spot = 'XAU' in sym
            return {'ok': True,
                    'volume_step': 0.01 if spot else 0.1,
                    'volume_min': 0.01 if spot else 0.1,
                    'volume_max': 100.0, 'point': 0.01}

        def tick(self, s):
            return None

        def account_info(self):
            return {}

        def order_log(self, hours=24):
            return []

        def ping(self):
            return True

    config = AlgoTradingConfig()
    config.TRADING.update({'SIZING_MODE': 'notional',
                           'NOTIONAL_PER_LEG_USD': 500_000.0,
                           'HEDGE_RATIO': 1.0})
    c = Coordinator(config, trading_mode='PAPER')
    c.spot_leg = c.futures_leg = Leg()
    c.executor = PaperExecutor(c.spot_leg, c.futures_leg, config)
    c.active_assets['GOLD'] = {'config': config.ASSETS['GOLD'],
                               'spot_symbol': 'XAUUSD_',
                               'futures_symbol': 'GC1226',
                               'last_data': None}
    return c


GOLD_MD = {'spot_price': 4292.61, 'futures_price': 4351.55, 'spread': 58.94,
           'spot_bid': 4292.55, 'spot_ask': 4292.68,
           'futures_bid': 4351.38, 'futures_ask': 4351.72}


def test_no_expiry_hides_the_whole_thing(coord):
    coord.config.ASSETS['GOLD'].pop('futures_expiry', None)
    block = coord._carry_block('GOLD', GOLD_MD, 59.0)
    assert block['days'] is None and block['net_usd'] is None


def test_a_hand_entered_swap_beats_what_mt5_reported(coord):
    """MT5's units cannot always be converted, and the operator can see
    what the broker actually charges. Their number wins."""
    asset = coord.config.ASSETS['GOLD']
    asset['futures_expiry'] = datetime.now() + timedelta(days=30)
    coord._swap_specs[('GOLD', 'spot')] = {
        'symbol': 'XAUUSD_', 'swap_mode': 8, 'swap_long': -4.0,
        'swap_short': -4.0}
    coord._swap_specs[('GOLD', 'futures')] = {
        'symbol': 'GC1226', 'swap_mode': 2, 'swap_long': 0.0,
        'swap_short': 0.0}
    # Mode 8 is unconvertible, so without an override there is no number.
    assert coord._carry_block('GOLD', GOLD_MD, 59.0)['net_usd'] is None
    asset['swap_spot_long_per_lot'] = -1.25
    block = coord._carry_block('GOLD', GOLD_MD, 59.0)
    assert block['net_usd'] is not None
    assert 'entered by hand' in block['per_leg'][0]['note']


def test_each_leg_is_charged_the_side_it_will_be_traded_on(coord):
    """A positive spread is SHORT the spread: long leg A, short leg B.
    Reading swap_long on both legs would price a trade nobody places."""
    asset = coord.config.ASSETS['GOLD']
    asset['futures_expiry'] = datetime.now() + timedelta(days=30)
    for role, sym in (('spot', 'XAUUSD_'), ('futures', 'GC1226')):
        coord._swap_specs[('GOLD', role)] = {
            'symbol': sym, 'swap_mode': carry.SWAP_CURRENCY_SYMBOL,
            'swap_long': -1.0, 'swap_short': +2.0}

    short = coord._carry_block('GOLD', dict(GOLD_MD, spread=58.94), 0.0)
    assert short['per_leg'][0]['per_lot_night'] == -1.0   # leg A long
    assert short['per_leg'][1]['per_lot_night'] == +2.0   # leg B short

    long_ = coord._carry_block('GOLD', dict(GOLD_MD, spread=-58.94), 0.0)
    assert long_['per_leg'][0]['per_lot_night'] == +2.0
    assert long_['per_leg'][1]['per_lot_night'] == -1.0


def test_the_net_subtracts_the_round_trip(coord):
    asset = coord.config.ASSETS['GOLD']
    asset['futures_expiry'] = datetime.now() + timedelta(days=30)
    asset['swap_spot_long_per_lot'] = 0.0
    asset['swap_futures_short_per_lot'] = 0.0
    free = coord._carry_block('GOLD', GOLD_MD, 0.0)
    charged = coord._carry_block('GOLD', GOLD_MD, 59.0)
    assert charged['net_usd'] == pytest.approx(free['net_usd'] - 59.0)


# --- the override has to be removable -----------------------------------

def test_a_blank_swap_field_clears_the_override():
    """Blank means "use MT5's". An override the operator cannot delete
    would outlive the pair it was typed for."""
    from statarb import webapi
    raw = {'assets': {'GOLD': {'name': 'GOLD',
                               'swap_spot_long_per_lot': -1.25}}}
    out, _, _ = webapi.apply_ui_config(
        raw, {'asset': 'GOLD', 'swap_spot_long_per_lot': ''})
    assert 'swap_spot_long_per_lot' not in out['assets']['GOLD']


def test_a_zero_swap_is_kept_as_an_override():
    """0 is a real statement — this symbol is not charged — and must not
    be read as "unset"."""
    from statarb import webapi
    out, _, _ = webapi.apply_ui_config(
        {'assets': {}}, {'asset': 'GOLD', 'swap_futures_short_per_lot': '0'})
    assert out['assets']['GOLD']['swap_futures_short_per_lot'] == 0.0


def test_the_override_survives_a_round_trip_through_the_ui():
    from statarb import webapi
    raw, _, _ = webapi.apply_ui_config(
        {'assets': {}}, {'asset': 'GOLD', 'swap_spot_long_per_lot': '-1.25',
                        'swap_futures_short_per_lot': '2.5'})
    ui = webapi.to_ui_config(raw)
    assert ui['swap_spot_long_per_lot'] == -1.25
    assert ui['swap_futures_short_per_lot'] == 2.5


def test_the_gross_is_shown_as_a_multiplication_not_just_a_total():
    """Every money figure on this dashboard is checkable against the two
    numbers beside it. A bare total gets believed for months."""
    plan = carry.convergence_plan(spread=-7.22, days=90.0, spread_units=110.0,
                                  legs=[(0.0, 1.0, 'a')], cost_usd=0.0)
    assert plan['spread'] == -7.22
    assert plan['spread_units'] == 110.0
    assert plan['gross_usd'] == pytest.approx(abs(plan['spread'])
                                              * plan['spread_units'])


# --- the same answer in SPREAD ------------------------------------------
# Operator, 2026-08-24: "where can I see what the actual spread should be
# - depending on the number of days left from the expiry". Dollars move
# with lots and leverage; the spread the pair has to beat does not.

def test_carry_says_what_the_spread_should_be():
    """On financing alone, the theoretical basis for the days left."""
    plan = carry.convergence_plan(
        spread=-7.22, days=90.0, spread_units=110.0,
        legs=[(-0.40, 0.11, 'a'), (-0.30, 0.11, 'b')], cost_usd=0.0)
    # -$6.93 of carry over 110 units
    assert plan['carry_spread'] == pytest.approx(6.93 / 110.0)


def test_break_even_spread_is_where_the_net_turns_over():
    plan = carry.convergence_plan(
        spread=-7.22, days=90.0, spread_units=110.0,
        legs=[(-0.40, 0.11, 'a'), (-0.30, 0.11, 'b')], cost_usd=39.0)
    at_be = carry.convergence_plan(
        spread=plan['breakeven_spread'], days=90.0, spread_units=110.0,
        legs=[(-0.40, 0.11, 'a'), (-0.30, 0.11, 'b')], cost_usd=39.0)
    assert at_be['net_usd'] == pytest.approx(0.0, abs=1e-9)


def test_the_gap_agrees_with_the_net():
    """Two readings of one fact — they must never disagree on screen."""
    plan = carry.convergence_plan(
        spread=-7.22, days=90.0, spread_units=110.0,
        legs=[(-0.40, 0.11, 'a'), (-0.30, 0.11, 'b')], cost_usd=39.0)
    assert plan['spread_gap'] * plan['spread_units'] == \
        pytest.approx(plan['net_usd'])
    assert (plan['spread_gap'] > 0) == (plan['net_usd'] > 0)


def test_a_credit_larger_than_the_round_trip_needs_no_spread_at_all():
    """Being PAID to wait is the case worth finding, and it shows up as
    a break-even BELOW zero rather than as a small positive."""
    plan = carry.convergence_plan(
        spread=0.10, days=90.0, spread_units=110.0,
        legs=[(+1.00, 1.0, 'a')], cost_usd=39.0)
    assert plan['breakeven_spread'] < 0


def test_the_schedule_decays_to_the_round_trip_not_to_zero():
    """Carry shrinks with the days left; the round trip does not. A
    table that ran to zero would promise a free trade at expiry."""
    plan = carry.convergence_plan(
        spread=-7.22, days=90.0, spread_units=110.0,
        legs=[(-0.40, 0.11, 'a'), (-0.30, 0.11, 'b')], cost_usd=39.0)
    needed = [pt['breakeven_spread'] for pt in plan['schedule']]
    assert needed == sorted(needed, reverse=True)   # falls as expiry nears
    assert plan['schedule'][-1]['days'] == 0
    assert plan['schedule'][-1]['breakeven_spread'] == \
        pytest.approx(39.0 / 110.0)


def test_the_schedule_never_looks_past_the_expiry():
    plan = carry.convergence_plan(
        spread=1.0, days=21.0, spread_units=100.0,
        legs=[(-0.10, 1.0, 'a')], cost_usd=5.0)
    assert all(pt['days'] <= 21.0 for pt in plan['schedule'])
    assert plan['schedule'][0]['days'] == 21.0


# --- the operator can set the expiry ------------------------------------
# There was no field for it anywhere: expiry came from MT5 or not at all,
# and MT5 does not always report one.

def test_an_expiry_typed_by_hand_is_stored():
    from statarb import webapi
    out, _, _ = webapi.apply_ui_config(
        {'assets': {}}, {'asset': 'GOLD', 'futures_expiry': '2026-11-25'})
    assert out['assets']['GOLD']['futures_expiry'] == '2026-11-25'


def test_a_blank_expiry_clears_it_back_to_rolling():
    """A rolling contract has no convergence date. If the date could not
    be removed, the card would price a trade that has no deadline."""
    from statarb import webapi
    raw = {'assets': {'GOLD': {'futures_expiry': '2026-11-25'}}}
    out, _, _ = webapi.apply_ui_config(
        raw, {'asset': 'GOLD', 'futures_expiry': ''})
    assert 'futures_expiry' not in out['assets']['GOLD']


def test_an_unreadable_date_is_reported_not_swallowed():
    from statarb import webapi
    raw = {'assets': {'GOLD': {'futures_expiry': '2026-11-25'}}}
    out, _, notes = webapi.apply_ui_config(
        raw, {'asset': 'GOLD', 'futures_expiry': 'Dec 26'})
    assert out['assets']['GOLD']['futures_expiry'] == '2026-11-25'
    assert any('not a date' in n for n in notes)


def test_a_past_expiry_says_the_card_will_stay_hidden():
    from statarb import webapi
    _, _, notes = webapi.apply_ui_config(
        {'assets': {}}, {'asset': 'GOLD', 'futures_expiry': '2020-01-01'})
    assert any('already' in n and 'passed' in n for n in notes)


def test_the_expiry_survives_a_round_trip_through_the_ui():
    from statarb import webapi
    raw, _, _ = webapi.apply_ui_config(
        {'assets': {}}, {'asset': 'GOLD', 'futures_expiry': '2026-11-25',
                         'spot_expiry': '2026-09-25'})
    ui = webapi.to_ui_config(raw)
    assert ui['futures_expiry'] == '2026-11-25'
    assert ui['spot_expiry'] == '2026-09-25'


def test_a_typed_expiry_beats_what_the_terminal_reports(coord):
    """_adopt_broker_specs only fills a BLANK expiry, so the operator's
    date is not overwritten at the next startup."""
    from datetime import datetime as _dt
    asset = coord.config.ASSETS['GOLD']
    asset['futures_expiry'] = _dt(2026, 11, 25)
    block = coord._carry_block('GOLD', GOLD_MD, 39.0)
    assert block['expiry'] == '2026-11-25'


# --- the swap and the carry rate price the same basis --------------------
# Live 2026-08-24 they sat side by side on the dashboard disagreeing in
# SIGN, and nothing said so. The operator had entered +58.00 per lot per
# night on the spot leg: the right magnitude (58 x 365 / (100 x 4,646) =
# 4.56% a year, exactly what gold funds at) with the sign inverted. A
# long spot position is CHARGED that.

def test_opposite_signs_are_called_out():
    msg = carry.sanity(carry_spread=-51.82, fair_value=48.55)
    assert msg and 'OPPOSITE' in msg
    assert 'normally negative' in msg


def test_the_same_swap_with_the_right_sign_agrees_with_fair_value():
    """Flip it and the two independent estimates agree to within 7%."""
    assert carry.sanity(carry_spread=+51.82, fair_value=48.55) is None


def test_a_wildly_different_magnitude_is_called_out():
    assert carry.sanity(carry_spread=500.0, fair_value=48.55)
    assert carry.sanity(carry_spread=1.0, fair_value=48.55)


def test_no_fair_value_means_no_opinion():
    """A RELATED pair has no fair value, and that is not a disagreement."""
    assert carry.sanity(carry_spread=-51.82, fair_value=None) is None
    assert carry.sanity(carry_spread=None, fair_value=48.55) is None
    assert carry.sanity(carry_spread=-51.82, fair_value=0.0) is None


def test_the_plan_carries_the_warning():
    plan = carry.convergence_plan(
        spread=56.545, days=89.34, spread_units=2.0,
        legs=[(+58.0, 0.02, 'XAUUSD long'), (0.0, 0.02, 'GCZ6 short')],
        cost_usd=1.22, fair_value=48.55)
    assert plan['carry_spread'] == pytest.approx(-51.82, abs=0.01)
    assert plan['warning'] and 'OPPOSITE' in plan['warning']


def test_the_charged_version_of_the_same_trade_is_clean():
    plan = carry.convergence_plan(
        spread=56.545, days=89.34, spread_units=2.0,
        legs=[(-58.0, 0.02, 'XAUUSD long'), (0.0, 0.02, 'GCZ6 short')],
        cost_usd=1.22, fair_value=48.55)
    assert plan['warning'] is None
    # And the edge all but vanishes, which is the honest answer: the
    # basis IS the financing, so capturing it barely beats paying it.
    assert plan['net_usd'] == pytest.approx(8.24, abs=0.5)


# --- one box per leg per SIDE -------------------------------------------
# MT5 quotes swap_long and swap_short separately and they routinely
# differ in sign. A single box per leg silently changed meaning whenever
# the spread crossed zero, because the side follows the spread's sign.

def test_each_side_gets_its_own_override(coord):
    asset = coord.config.ASSETS['GOLD']
    asset['futures_expiry'] = datetime.now() + timedelta(days=30)
    asset['swap_spot_long_per_lot'] = -1.25
    asset['swap_spot_short_per_lot'] = +0.40
    asset['swap_futures_long_per_lot'] = 0.0
    asset['swap_futures_short_per_lot'] = 0.0

    # A POSITIVE spread is shorted: long leg A, so leg A reads its LONG box.
    short = coord._carry_block('GOLD', dict(GOLD_MD, spread=58.94), 0.0)
    assert short['per_leg'][0]['per_lot_night'] == -1.25

    # A NEGATIVE spread is bought: leg A is now short, so the other box.
    long_ = coord._carry_block('GOLD', dict(GOLD_MD, spread=-58.94), 0.0)
    assert long_['per_leg'][0]['per_lot_night'] == +0.40


def test_a_pre_split_config_still_works_and_says_so(coord):
    """One value written before the split cannot quietly start meaning
    one side only."""
    asset = coord.config.ASSETS['GOLD']
    asset['futures_expiry'] = datetime.now() + timedelta(days=30)
    asset['swap_spot_per_lot'] = -1.25
    asset['swap_futures_per_lot'] = 0.0
    block = coord._carry_block('GOLD', GOLD_MD, 0.0)
    assert block['per_leg'][0]['per_lot_night'] == -1.25
    assert 'both sides' in block['per_leg'][0]['note']


def test_a_side_specific_box_beats_the_legacy_one(coord):
    asset = coord.config.ASSETS['GOLD']
    asset['futures_expiry'] = datetime.now() + timedelta(days=30)
    asset['swap_spot_per_lot'] = -1.25
    asset['swap_spot_long_per_lot'] = -2.00
    asset['swap_futures_per_lot'] = 0.0
    block = coord._carry_block('GOLD', dict(GOLD_MD, spread=58.94), 0.0)
    assert block['per_leg'][0]['per_lot_night'] == -2.00
    assert 'both sides' not in block['per_leg'][0]['note']


def test_saving_a_side_retires_that_legs_legacy_value():
    """Leaving it behind would keep a number meaning "both sides"
    underneath two that mean one side each."""
    from statarb import webapi
    raw = {'assets': {'GOLD': {'name': 'GOLD',
                               'swap_spot_per_lot': -1.25,
                               'swap_futures_per_lot': -0.50}}}
    out, _, _ = webapi.apply_ui_config(
        raw, {'asset': 'GOLD', 'swap_spot_long_per_lot': '-2.0'})
    gold = out['assets']['GOLD']
    assert 'swap_spot_per_lot' not in gold
    assert gold['swap_spot_long_per_lot'] == -2.0
    # The OTHER leg was not touched, so its legacy value survives.
    assert gold['swap_futures_per_lot'] == -0.50


def test_a_pre_split_value_shows_in_both_boxes():
    """So the operator can see what is in force and fix whichever side
    is wrong."""
    from statarb import webapi
    ui = webapi.to_ui_config(
        {'assets': {'GOLD': {'name': 'GOLD', 'enabled': True,
                             'swap_spot_per_lot': -1.25}}})
    assert ui['swap_spot_long_per_lot'] == -1.25
    assert ui['swap_spot_short_per_lot'] == -1.25
