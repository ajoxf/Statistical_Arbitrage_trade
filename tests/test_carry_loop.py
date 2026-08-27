"""The convergence loop: short while the basis is rich against carry,
re-enter after a win, stand down after a loss.

Operator, 2026-08-27. The loop places live orders by itself, so almost
everything here is about what it REFUSES to do — the refusals are the
feature. What it trades on is one comparison; what stops it is one
stop.
"""

import logging

import pytest

from statarb import carryloop
from statarb.models import SignalType


# ----------------------------------------------------------------------
# fair_spread: the refusals
# ----------------------------------------------------------------------

def test_the_fair_spread_is_the_one_on_the_carry_card():
    """Read the published block, never re-derive. Two places computing
    'fair' independently is how they end up disagreeing on screen while
    one of them places orders."""
    value, why = carryloop.fair_spread({'carry_spread': 52.4318})
    assert value == pytest.approx(52.4318)
    assert why is None


def test_no_carry_estimate_is_a_refusal_not_a_zero():
    """One unconvertible swap and the whole estimate is None. Trading
    'spread > 0' instead is a completely different trade."""
    value, why = carryloop.fair_spread(
        {'carry_spread': None, 'reason': 'XAUUSD swap mode 3 is a percent'})
    assert value is None
    assert 'percent' in why


def test_no_block_at_all_is_a_refusal():
    value, why = carryloop.fair_spread(None)
    assert value is None and why


def test_a_sanity_warning_stops_the_loop_dead():
    """`carry.sanity` fires when the broker's swap and the risk-free
    rate disagree about the SIGN of this basis — one input is provably
    wrong. The dashboard already refuses to print a verdict there; a
    loop placing real orders must refuse harder."""
    value, why = carryloop.fair_spread({
        'carry_spread': 52.0,
        'warning': {'text': 'the swap and the carry rate disagree in sign'}})
    assert value is None
    assert 'disagree' in why


# ----------------------------------------------------------------------
# evaluate: rich enough, and rich enough to pay for itself
# ----------------------------------------------------------------------

BLOCK = {'carry_spread': 52.00}


def test_a_rich_spread_opens_a_cycle():
    ok, detail = carryloop.evaluate(55.00, BLOCK)
    assert ok
    assert '55.0000' in detail and '52.0000' in detail


def test_a_spread_at_fair_does_not():
    ok, detail = carryloop.evaluate(52.00, BLOCK)
    assert not ok
    assert 'not rich' in detail


def test_a_cheap_spread_does_not():
    """The operator asked for the high-to-low trade only. A basis BELOW
    fair is the mirror trade and this loop does not take it."""
    ok, _ = carryloop.evaluate(48.00, BLOCK)
    assert not ok


def test_the_gap_has_to_clear_the_round_trip():
    """A 0.10 gap on a pair whose round trip is 0.30 of spread is not an
    edge, it is a fee. k = 20 units, $6 round trip -> 0.30 of spread."""
    ok, detail = carryloop.evaluate(52.10, BLOCK, cost_usd=6.0,
                                    spread_units=20.0)
    assert not ok
    assert '0.3000' in detail
    ok, _ = carryloop.evaluate(52.40, BLOCK, cost_usd=6.0, spread_units=20.0)
    assert ok


def test_the_edge_multiple_scales_what_it_demands():
    """1.0 = pay for itself. 2.0 = be worth crossing for."""
    assert carryloop.evaluate(52.40, BLOCK, cost_usd=6.0,
                              spread_units=20.0, edge_mult=1.0)[0]
    assert not carryloop.evaluate(52.40, BLOCK, cost_usd=6.0,
                                  spread_units=20.0, edge_mult=2.0)[0]


def test_no_spread_is_a_refusal():
    ok, detail = carryloop.evaluate(None, BLOCK)
    assert not ok and 'no executable spread' in detail


def test_a_refusal_always_explains_itself():
    """The panel shows this whether the answer is yes or no — 'nothing
    happened' is the failure mode this whole codebase keeps fixing."""
    for spread in (None, 48.0, 52.0, 55.0):
        _, detail = carryloop.evaluate(spread, BLOCK)
        assert detail and isinstance(detail, str)


# ----------------------------------------------------------------------
# levels: distances, because cycle 2 fills somewhere else
# ----------------------------------------------------------------------

def test_a_short_targets_below_the_fill_and_stops_above_it():
    target, stop = carryloop.levels_from_fill(55.00, 1.20, 0.80)
    assert target == pytest.approx(53.80)
    assert stop == pytest.approx(55.80)


def test_the_sign_the_operator_typed_is_ignored():
    """A distance is a distance. Typing -1.20 for 'down 1.20' must not
    invert the trade."""
    assert carryloop.levels_from_fill(55.0, -1.2, -0.8) == \
        carryloop.levels_from_fill(55.0, 1.2, 0.8)


def test_levels_move_with_the_fill():
    """The point of distances: an absolute level typed for cycle 1 is
    meaningless by cycle 2 — either unreachable or already passed."""
    first = carryloop.levels_from_fill(55.00, 1.0, 1.0)
    second = carryloop.levels_from_fill(53.00, 1.0, 1.0)
    assert first == (54.0, 56.0)
    assert second == (52.0, 54.0)


# ----------------------------------------------------------------------
# LoopState: a win re-arms, a loss stands it down
# ----------------------------------------------------------------------

def test_a_winning_cycle_re_arms():
    loop = carryloop.LoopState('GOLD', stop_loss=0.8, take_profit=1.2)
    loop.opened('POS_0001')
    assert loop.position_id == 'POS_0001' and loop.cycles == 1
    assert loop.closed(12.40, 'MANUAL_TARGET') is True
    assert loop.enabled
    assert loop.wins == 1
    assert loop.realized == pytest.approx(12.40)
    assert loop.position_id is None


def test_a_losing_cycle_switches_the_loop_off(caplog):
    """The operator chose the per-cycle stop as the loop's ONLY bound.
    This is where that bound is enforced — re-entering into a stop-out
    is how a bounded loop becomes an unbounded one."""
    loop = carryloop.LoopState('GOLD', stop_loss=0.8, take_profit=1.2)
    loop.opened('POS_0001')
    with caplog.at_level(logging.WARNING):
        assert loop.closed(-9.10, 'MANUAL_STOP') is False
    assert not loop.enabled
    assert 'losing cycle' in loop.stood_down
    assert any('STOOD DOWN' in r.message for r in caplog.records)


def test_a_scratch_is_a_loss_for_this_purpose():
    """Exactly zero banks nothing and has paid a round trip to find out.
    Repeating it is a fee schedule, not a strategy."""
    loop = carryloop.LoopState('GOLD', stop_loss=0.8, take_profit=1.2)
    loop.opened('POS_0001')
    assert loop.closed(0.0, 'MANUAL_CLOSE') is False
    assert not loop.enabled


def test_many_winning_cycles_accumulate():
    loop = carryloop.LoopState('GOLD', stop_loss=0.8, take_profit=1.2)
    for i in range(4):
        loop.opened(f'POS_000{i}')
        loop.closed(3.0, 'MANUAL_TARGET')
    assert loop.enabled and loop.cycles == 4 and loop.wins == 4
    assert loop.realized == pytest.approx(12.0)
    loop.opened('POS_0009')
    loop.closed(-20.0, 'MANUAL_STOP')
    assert not loop.enabled
    assert loop.realized == pytest.approx(-8.0)


# ----------------------------------------------------------------------
# Coordinator wiring
# ----------------------------------------------------------------------

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


def test_the_loop_refuses_to_start_without_a_stop(coord):
    """No stop means no bound at all: a manual trade has no engine stop,
    and a loop of stopless trades re-entering after every win is a
    machine for turning many small wins into one unlimited loss."""
    coord._carry_loop_start({'asset': 'GOLD', 'take_profit': 1.2})
    assert coord.carry_loop is None
    assert coord.manual_note['ok'] is False
    assert 'stop' in coord.manual_note['text']


def test_the_loop_refuses_to_start_without_a_target(coord):
    """A cycle has to be able to END in profit for there to be anything
    to repeat."""
    coord._carry_loop_start({'asset': 'GOLD', 'stop_loss': 0.8})
    assert coord.carry_loop is None
    assert coord.manual_note['ok'] is False


def test_the_loop_refuses_an_unknown_asset(coord):
    coord._carry_loop_start({'asset': 'PLATINUM', 'stop_loss': 0.8,
                             'take_profit': 1.2})
    assert coord.carry_loop is None


def test_a_started_loop_is_armed_and_published(coord):
    coord._carry_loop_start({'asset': 'GOLD', 'stop_loss': 0.8,
                             'take_profit': 1.2, 'lots': 0.05})
    assert coord.carry_loop is not None
    published = coord.carry_loop.to_dict()
    assert published['enabled'] and published['asset'] == 'GOLD'
    assert published['lots'] == 0.05
    assert published['cycles'] == 0


def test_stopping_the_loop_clears_it(coord):
    coord._carry_loop_start({'asset': 'GOLD', 'stop_loss': 0.8,
                             'take_profit': 1.2})
    coord._carry_loop_stop()
    assert coord.carry_loop is None


def test_the_loop_is_primed_so_a_restart_never_resumes_it(coord):
    """It PLACES ORDERS by itself. A loop left on at 17:00 must not
    resume when a crashed process comes back at 02:00 with nobody
    watching — that is the 2026-08-07 replay incident's shape."""
    assert ('carry_loop', '_last_carry_loop_ts') in coord._CONTROL_COMMANDS


def test_a_closed_cycle_that_is_not_the_loops_is_ignored(coord):
    coord._carry_loop_start({'asset': 'GOLD', 'stop_loss': 0.8,
                             'take_profit': 1.2})
    coord.carry_loop.opened('POS_0001')
    coord._carry_loop_closed('POS_0099', -50.0, 'DOLLAR_STOP')
    assert coord.carry_loop.enabled          # somebody else's trade
    coord._carry_loop_closed('POS_0001', -50.0, 'MANUAL_STOP')
    assert not coord.carry_loop.enabled


# ----------------------------------------------------------------------
# Structural: the loop reads carry, the STRATEGY still never does
# ----------------------------------------------------------------------

def test_no_signal_or_exit_module_imports_carry():
    """`carry` is reference for a MANUAL decision. The loop is a manual
    decision the operator automated — the SIGNAL path is still barred,
    exactly as it is for fairvalue."""
    import inspect
    from statarb import costs, exits, pair_executor, signals, spread
    for module in (signals, exits, spread, costs, pair_executor):
        source = inspect.getsource(module)
        assert 'carryloop' not in source
        assert 'import carry' not in source


# ----------------------------------------------------------------------
# End to end: the loop opening a cycle through the coordinator
# ----------------------------------------------------------------------

RICH = {'spot_price': 4292.61, 'futures_price': 4351.55, 'spread': 58.94,
        'spot_bid': 4292.55, 'spot_ask': 4292.68,
        'futures_bid': 4351.38, 'futures_ask': 4351.72,
        'short_spread': 58.70, 'long_spread': 59.17,
        'quote_id': 'q1'}


def armed(coord, monkeypatch, carry_spread=52.0, **over):
    """A loop armed on a pair whose fair value is a fixed number.

    `_carry_block` is stubbed because carry.py has its own tests — what
    is under test here is the WIRING: which spread is compared, which
    direction is taken, and where the levels land.
    """
    coord._carry_loop_start(dict({'asset': 'GOLD', 'stop_loss': 0.80,
                                  'take_profit': 1.20, 'lots': 0.05},
                                 **over))
    monkeypatch.setattr(coord, '_carry_block',
                        lambda *a, **k: {'carry_spread': carry_spread})
    monkeypatch.setattr(coord, '_sizing_and_cost',
                        lambda *a, **k: {'round_trip_cost': 0.0})
    return coord.carry_loop


def test_a_rich_basis_opens_a_short_and_the_loop_owns_it(coord, monkeypatch):
    opened = {}

    def fake_open(asset, direction, lots, **kw):
        opened.update(asset=asset, direction=direction, lots=lots, **kw)
        return type('P', (), {'position_id': 'POS_0001'})()

    loop = armed(coord, monkeypatch)
    monkeypatch.setattr(coord, '_manual_open', fake_open)
    coord._check_carry_loop('GOLD', dict(RICH))

    assert opened['direction'] == SignalType.SELL_BASIS.value
    assert opened['lots'] == 0.05
    assert loop.position_id == 'POS_0001' and loop.cycles == 1


def test_the_levels_are_anchored_on_the_executable_short_spread(
        coord, monkeypatch):
    """Not the mid. A short fills at 58.70 while the mid reads 58.94 —
    anchoring on the mid puts both levels 0.24 out, in the direction
    that flatters the trade."""
    opened = {}
    monkeypatch.setattr(coord, '_manual_open',
                        lambda a, d, l, **kw: (opened.update(kw) or
                                               type('P', (), {
                                                   'position_id': 'P1'})()))
    armed(coord, monkeypatch)
    coord._check_carry_loop('GOLD', dict(RICH))

    assert opened['exit_spread'] == pytest.approx(58.70 - 1.20)
    assert opened['stop_spread'] == pytest.approx(58.70 + 0.80)


def test_a_spread_at_fair_opens_nothing(coord, monkeypatch):
    calls = []
    monkeypatch.setattr(coord, '_manual_open',
                        lambda *a, **k: calls.append(1))
    loop = armed(coord, monkeypatch, carry_spread=58.70)
    coord._check_carry_loop('GOLD', dict(RICH))

    assert calls == [] and loop.cycles == 0
    assert 'not rich' in loop.last_note


def test_a_stale_quote_withholds_the_cycle(coord, monkeypatch):
    """The gap IS a level comparison, so a stale or desynced quote makes
    it fictitious in exactly the way a target is."""
    calls = []
    monkeypatch.setattr(coord, '_manual_open',
                        lambda *a, **k: calls.append(1))
    loop = armed(coord, monkeypatch)
    monkeypatch.setattr(coord, '_stale_quote',
                        lambda md: "Leg A's quote has not moved for 9s")
    coord._check_carry_loop('GOLD', dict(RICH))

    assert calls == []
    assert 'has not moved' in loop.last_note


def test_nothing_opens_while_a_cycle_is_running(coord, monkeypatch):
    calls = []
    monkeypatch.setattr(coord, '_manual_open',
                        lambda *a, **k: calls.append(1))
    armed(coord, monkeypatch)
    monkeypatch.setattr(coord.position_manager, 'get_positions_for_asset',
                        lambda key: {'POS_0001': object()})
    coord._check_carry_loop('GOLD', dict(RICH))
    assert calls == []


def test_a_refused_open_stands_the_loop_down(coord, monkeypatch):
    """Do NOT retry three times a second against a broker that just said
    no. Stand down and let the panel show the reason it already has."""
    monkeypatch.setattr(coord, '_manual_open', lambda *a, **k: None)
    armed(coord, monkeypatch)
    coord._check_carry_loop('GOLD', dict(RICH))
    assert coord.carry_loop is None


def test_a_disabled_loop_does_nothing(coord, monkeypatch):
    calls = []
    monkeypatch.setattr(coord, '_manual_open',
                        lambda *a, **k: calls.append(1))
    loop = armed(coord, monkeypatch)
    loop.enabled = False
    coord._check_carry_loop('GOLD', dict(RICH))
    assert calls == []


def test_the_loop_only_watches_its_own_pair(coord, monkeypatch):
    calls = []
    monkeypatch.setattr(coord, '_manual_open',
                        lambda *a, **k: calls.append(1))
    armed(coord, monkeypatch)
    coord._check_carry_loop('SILVER', dict(RICH))
    assert calls == []


# ----------------------------------------------------------------------
# The HTTP endpoint — refuses the same things the engine does
# ----------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import json
    from statarb.database import DataLogger
    from statarb.webapp import create_app

    db = tmp_path / 'a.db'
    DataLogger(db_path=str(db))
    (tmp_path / 'config.json').write_text('{"trading_mode": "paper"}')
    control = tmp_path / 'control.json'
    app = create_app(db_path=str(db),
                     status_path=str(tmp_path / 'runtime_status.json'),
                     config_path=str(tmp_path / 'config.json'),
                     control_path=str(control),
                     env_path=str(tmp_path / '.env'))
    app.testing = True
    c = app.test_client()
    c.control = lambda: json.loads(control.read_text())
    return c


def test_the_endpoint_refuses_a_loop_with_no_stop(client):
    """The browser can be bypassed, but it should not be the thing that
    lets an unbounded re-entering loop through."""
    r = client.post('/api/carry-loop',
                    json={'enabled': True, 'asset': 'GOLD',
                          'take_profit': 1.2})
    assert r.status_code == 400
    assert 'stop' in r.get_json()['error']


def test_the_endpoint_refuses_a_loop_with_no_target(client):
    r = client.post('/api/carry-loop',
                    json={'enabled': True, 'asset': 'GOLD',
                          'stop_loss': 0.8})
    assert r.status_code == 400
    assert 'take-profit' in r.get_json()['error']


def test_the_endpoint_refuses_a_loop_with_no_asset(client):
    r = client.post('/api/carry-loop',
                    json={'enabled': True, 'stop_loss': 0.8,
                          'take_profit': 1.2})
    assert r.status_code == 400


def test_a_started_loop_reaches_control_json(client):
    r = client.post('/api/carry-loop',
                    json={'enabled': True, 'asset': 'GOLD',
                          'take_profit': 1.2, 'stop_loss': 0.8,
                          'lots': 0.05})
    assert r.get_json()['success'] is True
    cmd = client.control()['carry_loop']
    assert cmd['enabled'] and cmd['asset'] == 'GOLD'
    assert cmd['take_profit'] == 1.2 and cmd['stop_loss'] == 0.8
    assert cmd['ts'] > 0


def test_distances_are_written_as_magnitudes(client):
    """A distance is a distance — a typed minus must not invert it."""
    client.post('/api/carry-loop',
                json={'enabled': True, 'asset': 'GOLD',
                      'take_profit': -1.2, 'stop_loss': -0.8})
    cmd = client.control()['carry_loop']
    assert cmd['take_profit'] == 1.2 and cmd['stop_loss'] == 0.8


def test_turning_it_off_needs_nothing_but_the_flag(client):
    """Off must work even when the fields have since been cleared or
    edited — a switch you cannot reach is not a switch."""
    r = client.post('/api/carry-loop', json={'enabled': False})
    assert r.get_json()['success'] is True
    assert client.control()['carry_loop']['enabled'] is False


def test_non_numeric_levels_are_refused_not_coerced(client):
    r = client.post('/api/carry-loop',
                    json={'enabled': True, 'asset': 'GOLD',
                          'take_profit': 'soon', 'stop_loss': 0.8})
    assert r.status_code == 400


# ----------------------------------------------------------------------
# The same parameters as the Manual Trade Card
# ----------------------------------------------------------------------
# Operator, 2026-08-27: "Need the same parameters for the loop."
# Direction, Lots, Take Profit (+% of margin), Stop Loss, Overnight —
# everything on that card except Entry, which is the one thing the loop
# works out for itself.

def test_a_long_loop_buys_the_spread_when_it_is_CHEAP():
    """The mirror trade. Same subtraction, opposite sign — positive
    always means 'in our favour' whichever way round it is."""
    ok, detail = carryloop.evaluate(48.00, BLOCK, direction=carryloop.LONG)
    assert ok and 'cheap' in detail
    assert not carryloop.evaluate(55.00, BLOCK,
                                  direction=carryloop.LONG)[0]


def test_short_and_long_are_exact_mirrors():
    for gap in (0.5, 2.0, 7.0):
        assert carryloop.evaluate(52.0 + gap, BLOCK,
                                  direction=carryloop.SHORT)[0]
        assert carryloop.evaluate(52.0 - gap, BLOCK,
                                  direction=carryloop.LONG)[0]
        assert not carryloop.evaluate(52.0 - gap, BLOCK,
                                      direction=carryloop.SHORT)[0]
        assert not carryloop.evaluate(52.0 + gap, BLOCK,
                                      direction=carryloop.LONG)[0]


def test_a_long_targets_above_the_fill_and_stops_below_it():
    target, stop = carryloop.levels_from_fill(48.00, 1.20, 0.80,
                                              carryloop.LONG)
    assert target == pytest.approx(49.20)
    assert stop == pytest.approx(47.20)


def test_the_round_trip_bar_applies_to_a_long_too():
    assert not carryloop.evaluate(51.90, BLOCK, direction=carryloop.LONG,
                                  cost_usd=6.0, spread_units=20.0)[0]
    assert carryloop.evaluate(51.60, BLOCK, direction=carryloop.LONG,
                              cost_usd=6.0, spread_units=20.0)[0]


def test_an_unset_direction_is_the_short(caplog):
    """The operator asked for the high-to-low trade; that stays the
    default so an omitted field cannot silently flip the trade."""
    assert carryloop.direction_sign(None) == 1.0
    assert carryloop.evaluate(55.0, BLOCK)[0]
    assert carryloop.LoopState('GOLD').direction == carryloop.SHORT


def test_the_loop_carries_direction_and_overnight(coord):
    coord._carry_loop_start({'asset': 'GOLD', 'stop_loss': 0.8,
                             'take_profit': 1.2,
                             'direction': 'BUY_BASIS',
                             'overnight': 'EXIT_ALWAYS'})
    published = coord.carry_loop.to_dict()
    assert published['direction'] == 'BUY_BASIS'
    assert published['overnight'] == 'EXIT_ALWAYS'


def test_the_loop_refuses_a_bad_direction(coord):
    coord._carry_loop_start({'asset': 'GOLD', 'stop_loss': 0.8,
                             'take_profit': 1.2, 'direction': 'sideways'})
    assert coord.carry_loop is None


def test_overnight_defaults_to_allow(coord):
    coord._carry_loop_start({'asset': 'GOLD', 'stop_loss': 0.8,
                             'take_profit': 1.2})
    assert coord.carry_loop.overnight == 'ALLOW'


def test_a_long_cycle_reads_the_long_spread_and_mirrors_its_levels(
        coord, monkeypatch):
    """A long fills where the spread is BOUGHT (59.17), not where it is
    sold — and its target sits above that, its stop below."""
    opened = {}
    monkeypatch.setattr(coord, '_manual_open',
                        lambda a, d, l, **kw: (opened.update(dict(kw,
                                               direction=d)) or
                                               type('P', (), {
                                                   'position_id': 'P1'})()))
    armed(coord, monkeypatch, carry_spread=70.0, direction='BUY_BASIS',
          overnight='EXIT_IF_PROFIT')
    coord._check_carry_loop('GOLD', dict(RICH))

    assert opened['direction'] == 'BUY_BASIS'
    assert opened['overnight'] == 'EXIT_IF_PROFIT'
    assert opened['exit_spread'] == pytest.approx(59.17 + 1.20)
    assert opened['stop_spread'] == pytest.approx(59.17 - 0.80)


def test_the_endpoint_carries_direction_and_overnight(client):
    client.post('/api/carry-loop',
                json={'enabled': True, 'asset': 'GOLD',
                      'take_profit': 1.2, 'stop_loss': 0.8,
                      'direction': 'BUY_BASIS',
                      'overnight': 'EXIT_ALWAYS'})
    cmd = client.control()['carry_loop']
    assert cmd['direction'] == 'BUY_BASIS'
    assert cmd['overnight'] == 'EXIT_ALWAYS'


def test_the_endpoint_refuses_a_bad_direction(client):
    r = client.post('/api/carry-loop',
                    json={'enabled': True, 'asset': 'GOLD',
                          'take_profit': 1.2, 'stop_loss': 0.8,
                          'direction': 'UP'})
    assert r.status_code == 400


def test_the_endpoint_defaults_to_the_short(client):
    client.post('/api/carry-loop',
                json={'enabled': True, 'asset': 'GOLD',
                      'take_profit': 1.2, 'stop_loss': 0.8})
    cmd = client.control()['carry_loop']
    assert cmd['direction'] == 'SELL_BASIS'
    assert cmd['overnight'] == 'ALLOW'
