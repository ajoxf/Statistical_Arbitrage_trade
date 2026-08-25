"""The edge filter, audited end to end.

Operator, 2026-08-25: "Thoroughly check all the Edge Filter. Make sure
the Edge Filter is taking the right values for calculation. Example:
Make sure the Algo takes the Short Spread - the right Bid and Ask
values to make the relevant calculations. Confirm thoroughly."

The filter is `capture >= MIN_EDGE_MULTIPLE x round_trip`, and both
sides have to be measured on the right thing:

    capture     f x |z| x sigma x k        z and sigma on the MID series,
                                           k = L_B x C_B (leg B's units)
    round trip  each leg's FULL bid-ask    on ITS OWN units, plus
                                           commission per lot per leg

The subtle part is that these two are quoted on different things and
must NOT double-count. A short enters at the short spread and exits at
the long one, so a favourable MID move of d is worth
`d x k - (long - short) x k`. The second term IS the round trip. So
comparing a mid-measured capture against a full-bid-ask cost charges
the bid-ask exactly ONCE — which is what these tests pin.

Deliberately run at beta 2 with unequal contract sizes: at beta 1 with
equal contracts several wrong formulas give the right answer.
"""

from types import SimpleNamespace

import pytest

from statarb import costs, marketdata, sizing
from statarb.config import AlgoTradingConfig
from statarb.models import SignalType


ASSET = {'name': 'GOLD', 'enabled': True, 'lot_size': 100.0,
         'fut_lot_size': 50.0, 'multiplier': 1.0,
         'spot_symbols': ['XAUUSD'], 'futures_symbols': ['GCZ6']}
BETA = 2.0
SPOT = SimpleNamespace(bid=4635.90, ask=4636.10, last=0.0, time=1)   # 0.20
FUT = SimpleNamespace(bid=4690.80, ask=4691.20, last=0.0, time=1)    # 0.40
LOTS_A, C_A, C_B = 0.02, 100.0, 50.0


@pytest.fixture
def md():
    return marketdata.compute_market_data(ASSET, SPOT, FUT, BETA)


@pytest.fixture
def cfg():
    return {'SPREAD_COST_FACTOR': 1.0, 'MIN_EDGE_MULTIPLE': 1.2,
            'TARGET_FRACTION': 0.5, 'COMMISSION_PER_LOT_SPOT': 0.0,
            'COMMISSION_PER_LOT_FUT': 0.0}


def legs():
    lots_b = sizing.hedge_lots(LOTS_A, C_A, C_B, BETA)
    return lots_b, sizing.spread_units(lots_b, C_B)


# --- the ROUND TRIP reads the right bid and ask -----------------------

def test_the_cost_is_what_a_short_round_trip_actually_pays(md, cfg):
    """SELL fut at the BID and BUY spot at the ASK going in; BUY fut at
    the ASK and SELL spot at the BID coming out. Summed against the mid
    on each leg, that is what the model must charge."""
    lots_b, _ = legs()
    model = costs.round_trip_cost(md, LOTS_A, C_A, cfg,
                                  lots_b=lots_b, contract_b=C_B)
    units_a, units_b = LOTS_A * C_A, lots_b * C_B
    fut_mid, spot_mid = (FUT.bid + FUT.ask) / 2, (SPOT.bid + SPOT.ask) / 2
    real = (
        (abs(FUT.bid - fut_mid) + abs(FUT.ask - fut_mid)) * units_b    # leg B
        + (abs(SPOT.ask - spot_mid) + abs(SPOT.bid - spot_mid)) * units_a)
    assert model == pytest.approx(real)


def test_a_long_round_trip_pays_exactly_the_same(md, cfg):
    """You cross the same two books, just in the other order. A cost
    model that differed by direction would be wrong on one of them."""
    lots_b, _ = legs()
    units_a, units_b = LOTS_A * C_A, lots_b * C_B
    fut_mid, spot_mid = (FUT.bid + FUT.ask) / 2, (SPOT.bid + SPOT.ask) / 2
    long_trip = (
        (abs(FUT.ask - fut_mid) + abs(FUT.bid - fut_mid)) * units_b
        + (abs(SPOT.bid - spot_mid) + abs(SPOT.ask - spot_mid)) * units_a)
    assert costs.round_trip_cost(md, LOTS_A, C_A, cfg, lots_b=lots_b,
                                 contract_b=C_B) == pytest.approx(long_trip)


def test_the_cost_equals_the_gap_between_the_two_executable_spreads(md, cfg):
    """`(long - short) x k` is the same quantity in spread units. Two
    views of ONE cost — if these ever disagree the bid-ask is being
    charged twice somewhere, or not at all."""
    lots_b, k = legs()
    spread_only = costs.round_trip_cost(md, LOTS_A, C_A, cfg,
                                        lots_b=lots_b, contract_b=C_B)
    assert md['spread_cost'] * k == pytest.approx(spread_only)
    assert (md['long_spread'] - md['short_spread']) * k == pytest.approx(
        spread_only)


def test_the_bid_ask_is_charged_once_not_twice(md, cfg):
    """The whole point. Enter at the short spread, exit at the long,
    nothing moves: the pair is down exactly the modelled round trip.
    """
    lots_b, k = legs()
    cost = costs.round_trip_cost(md, LOTS_A, C_A, cfg,
                                 lots_b=lots_b, contract_b=C_B)
    entry = marketdata.executable_spread(md, SignalType.SELL_BASIS)
    exit_ = marketdata.executable_spread(md, SignalType.SELL_BASIS,
                                         closing=True)
    assert (entry - exit_) * k == pytest.approx(-cost)


def test_each_leg_is_priced_on_its_own_units(md, cfg):
    lots_b, _ = legs()
    cost = costs.round_trip_cost(md, LOTS_A, C_A, cfg,
                                 lots_b=lots_b, contract_b=C_B)
    leg_a = (SPOT.ask - SPOT.bid) * LOTS_A * C_A
    leg_b = (FUT.ask - FUT.bid) * lots_b * C_B
    assert cost == pytest.approx(leg_a + leg_b)
    # ...and NOT both legs on leg A's, the 2026-08-10 fault.
    wrong = ((SPOT.ask - SPOT.bid) + (FUT.ask - FUT.bid)) * LOTS_A * C_A
    assert cost != pytest.approx(wrong)


def test_it_reads_the_raw_touches_not_the_display_spread(md, cfg):
    """`spot_spread` / `futures_spread` in the snapshot are the bid-ask
    x100 for display. Reading those would overstate the cost 100-fold.
    """
    assert md['spot_spread'] == pytest.approx((SPOT.ask - SPOT.bid) * 100)
    lots_b, _ = legs()
    cost = costs.round_trip_cost(md, LOTS_A, C_A, cfg,
                                 lots_b=lots_b, contract_b=C_B)
    assert cost == pytest.approx(
        (SPOT.ask - SPOT.bid) * LOTS_A * C_A
        + (FUT.ask - FUT.bid) * lots_b * C_B)


def test_commission_is_per_lot_on_each_legs_own_lots(md):
    lots_b, _ = legs()
    cfg = {'SPREAD_COST_FACTOR': 0.0, 'MIN_EDGE_MULTIPLE': 1.2,
           'TARGET_FRACTION': 0.5, 'COMMISSION_PER_LOT_SPOT': 3.0,
           'COMMISSION_PER_LOT_FUT': 5.0}
    cost = costs.round_trip_cost(md, LOTS_A, C_A, cfg,
                                 lots_b=lots_b, contract_b=C_B)
    assert cost == pytest.approx(3.0 * LOTS_A + 5.0 * lots_b)


def test_the_spread_cost_factor_scales_only_the_crossing(md):
    lots_b, _ = legs()
    cfg = {'SPREAD_COST_FACTOR': 0.5, 'MIN_EDGE_MULTIPLE': 1.2,
           'TARGET_FRACTION': 0.5, 'COMMISSION_PER_LOT_SPOT': 3.0,
           'COMMISSION_PER_LOT_FUT': 5.0}
    crossing = ((SPOT.ask - SPOT.bid) * LOTS_A * C_A
                + (FUT.ask - FUT.bid) * lots_b * C_B)
    assert costs.round_trip_cost(md, LOTS_A, C_A, cfg, lots_b=lots_b,
                                 contract_b=C_B) == pytest.approx(
        crossing * 0.5 + 3.0 * LOTS_A + 5.0 * lots_b)


# --- the CAPTURE side -------------------------------------------------

def test_capture_is_priced_on_leg_b(md, cfg):
    lots_b, k = legs()
    cap = costs.expected_capture(3.5, 0.3752, LOTS_A, C_A, cfg,
                                 lots_b=lots_b, contract_b=C_B)
    assert cap == pytest.approx(0.5 * 3.5 * 0.3752 * k)
    assert cap != pytest.approx(0.5 * 3.5 * 0.3752 * LOTS_A * C_A)


def test_a_mid_move_of_the_capture_distance_nets_capture_minus_cost(md, cfg):
    """The two sides are quoted on different things, and this is why
    that is right. Capture is a MID move; a real short banks that move
    LESS one round turn, which is exactly the cost the filter charges.
    """
    lots_b, k = legs()
    cap = costs.expected_capture(3.5, 0.3752, LOTS_A, C_A, cfg,
                                 lots_b=lots_b, contract_b=C_B)
    cost = costs.round_trip_cost(md, LOTS_A, C_A, cfg,
                                 lots_b=lots_b, contract_b=C_B)
    move = cap / k                      # the mid move capture assumes
    entry = marketdata.executable_spread(md, SignalType.SELL_BASIS)
    # The mid falls by `move`; the book keeps its width.
    after = dict(md,
                 short_spread=md['short_spread'] - move,
                 long_spread=md['long_spread'] - move)
    exit_ = marketdata.executable_spread(after, SignalType.SELL_BASIS,
                                         closing=True)
    assert (entry - exit_) * k == pytest.approx(cap - cost)


def test_no_z_or_no_sigma_captures_nothing(md, cfg):
    lots_b, _ = legs()
    assert costs.expected_capture(None, 0.37, LOTS_A, C_A, cfg,
                                  lots_b=lots_b, contract_b=C_B) == 0.0
    assert costs.expected_capture(3.5, None, LOTS_A, C_A, cfg,
                                  lots_b=lots_b, contract_b=C_B) == 0.0


# --- the gate, and the numbers the card shows for it ------------------

def coordinator(monkeypatch, tmp_path):
    """A coordinator in a scratch cwd.

    `monkeypatch.chdir`, never a bare `os.chdir`: the latter leaks into
    every test that runs after this file and broke a sibling reading
    templates/ by a relative path.
    """
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    cfg = AlgoTradingConfig()
    cfg.TRADING.update({'HEDGE_RATIO': BETA, 'CLIP_LOTS': LOTS_A,
                        'SIZING_MODE': 'lots'})
    cfg.ASSETS.clear()
    cfg.ASSETS['GOLD'] = dict(ASSET)
    cfg.COSTS.update({'SPREAD_COST_FACTOR': 1.0, 'MIN_EDGE_MULTIPLE': 1.2,
                      'TARGET_FRACTION': 0.5,
                      'COMMISSION_PER_LOT_SPOT': 3.0,
                      'COMMISSION_PER_LOT_FUT': 5.0})
    c = Coordinator(cfg, trading_mode='PAPER')
    c.active_assets['GOLD'] = {'config': cfg.ASSETS['GOLD'],
                               'spot_symbol': 'XAUUSD',
                               'futures_symbol': 'GCZ6', 'last_data': None}
    return c


def test_the_cards_parts_add_up_to_the_number_the_gate_uses(md, monkeypatch, tmp_path):
    """The Filters card recomputes the per-leg breakdown locally while
    the total comes from `round_trip_cost` — two implementations of one
    sum, so they are pinned together."""
    c = coordinator(monkeypatch, tmp_path)
    stats = SimpleNamespace(z=3.5, sigma=0.3752, warm=True)
    b = c._sizing_and_cost('GOLD', md, stats)
    parts = (b['rt_leg_a_cost'] + b['rt_leg_b_cost']
             + b['rt_commission_a'] + b['rt_commission_b'])
    assert parts == pytest.approx(b['rt_cost_usd'])


def test_the_card_and_the_live_gate_agree(md, monkeypatch, tmp_path):
    """The card must report the decision the engine actually makes."""
    c = coordinator(monkeypatch, tmp_path)
    stats = SimpleNamespace(z=3.5, sigma=0.3752, warm=True)
    b = c._sizing_and_cost('GOLD', md, stats)
    plan = c._sizing_plan('GOLD', md)
    passes, capture, cost = costs.edge_ok(
        stats.z, stats.sigma, plan['leg_a_lots'], plan['leg_a_contract'],
        md, c.config.COSTS, plan['leg_b_lots'], plan['leg_b_contract'])
    assert capture == pytest.approx(b['capture_usd'])
    assert cost == pytest.approx(b['rt_cost_usd'])
    assert passes == b['edge_ok']


def test_the_z_it_says_you_need_is_exactly_the_boundary(md, monkeypatch, tmp_path):
    """"Needs |z| >= 3.8 to pass" has to be the real threshold, not an
    approximation of it."""
    c = coordinator(monkeypatch, tmp_path)
    stats = SimpleNamespace(z=3.5, sigma=0.3752, warm=True)
    b = c._sizing_and_cost('GOLD', md, stats)
    plan = c._sizing_plan('GOLD', md)
    needed = b['edge_z_needed']

    def passes(z):
        ok, *_ = costs.edge_ok(z, stats.sigma, plan['leg_a_lots'],
                               plan['leg_a_contract'], md, c.config.COSTS,
                               plan['leg_b_lots'], plan['leg_b_contract'])
        return ok

    assert not passes(needed * 0.999)
    assert passes(needed * 1.001)


def test_the_cost_in_sigmas_is_the_size_free_form_of_the_same_cost(md, monkeypatch, tmp_path):
    c = coordinator(monkeypatch, tmp_path)
    stats = SimpleNamespace(z=3.5, sigma=0.3752, warm=True)
    b = c._sizing_and_cost('GOLD', md, stats)
    k = sizing.spread_units(b['rt_lots_b'], b['rt_contract_b'])
    assert b['edge_cost_in_sigmas'] * stats.sigma * k == pytest.approx(
        b['rt_cost_usd'])


def test_the_gate_uses_leg_bs_size_not_leg_as(md, monkeypatch, tmp_path):
    """The 2026-08-11 fault: capture on leg A against cost on leg B
    understated the edge by 1/beta."""
    c = coordinator(monkeypatch, tmp_path)
    plan = c._sizing_plan('GOLD', md)
    assert plan['leg_b_contract'] == C_B
    assert plan['leg_b_lots'] == pytest.approx(
        sizing.hedge_lots(plan['leg_a_lots'], plan['leg_a_contract'],
                          C_B, BETA))


def test_a_bad_snapshot_says_so_instead_of_blanking_the_card(
        caplog, monkeypatch, tmp_path):
    """The cost model reads `futures_bid`/`futures_ask`; the
    coordinator's PUBLISHED asset block calls them `fut_bid`/`fut_ask`,
    and that rename has already caused one live fault elsewhere. Handing
    the wrong dict here used to be swallowed, leaving a Filters card
    with no numbers and no explanation."""
    import logging as _logging
    c = coordinator(monkeypatch, tmp_path)
    bad = {'spot_price': 4636.0, 'futures_price': 4691.0,
           'spot_bid': 4635.9, 'spot_ask': 4636.1,
           'fut_bid': 4690.8, 'fut_ask': 4691.2}          # wrong names
    stats = SimpleNamespace(z=3.5, sigma=0.3752, warm=True)
    with caplog.at_level(_logging.ERROR):
        block = c._sizing_and_cost('GOLD', bad, stats)
    assert 'rt_cost_usd' not in block
    assert 'futures_ask' in caplog.text
