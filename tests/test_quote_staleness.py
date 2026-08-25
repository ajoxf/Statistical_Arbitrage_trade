"""Never act on a price level that only exists in our snapshot.

Live 2026-08-25, POS_0002. A manual short filled at 56.70 with a
take-profit at 55.76 (worth +$9.40 at k = 10):

    trigger   executable closing spread 55.67  -> MANUAL_TARGET fired
    mark      (56.70 - 55.67) x 10 = +$10.30   -> the recorded peak
    fill      4711.04 - 4653.86  =  57.18
    realised  (56.70 - 57.18) x 10 = -$4.80    -> and MT5 agreed:
                                                  spot +$71.50,
                                                  futures -$76.30

The futures leg filled 1.29 ABOVE the ask we were holding, which a
market order cannot do unless the ask has moved. The heartbeats show
why: over the two minutes before, spot ran up 2.60 while the futures
quote moved 1.29, so the spread "fell" ~2.9 sigma; three minutes later
it was back at 56.71, having gone nowhere. The engine reached for a
profit that only existed on a stale price.

The feed RATE was healthy throughout — 108 quotes/min — because it
counts both legs together and one leg was carrying the count. A pair
trade is only as good as its WORSE leg.
"""

import pytest

from statarb import marketdata
from statarb.coordinator import Coordinator
from statarb.marketdata import QuoteAgeTracker, stale_quote
# The wired-up Coordinator, with fake legs and a paper executor.
from tests.test_manual_trade import coordinator      # noqa: F401


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt
        return self.t


def snap(spot_q='s1', fut_q='f1'):
    return {'spot_quote_id': spot_q, 'fut_quote_id': fut_q}


# --- measuring the age ------------------------------------------------

def test_the_first_sighting_has_no_opinion():
    """Unknown is not fresh, and it is not stale either — a guard that
    fired on the first poll of every start would be unusable."""
    t = QuoteAgeTracker(clock=Clock())
    md = snap()
    assert t.observe('GOLD', md) == (None, None)
    assert stale_quote(md, 2.0) is None


def test_a_leg_ages_only_while_its_own_quote_is_unchanged():
    clock = Clock()
    t = QuoteAgeTracker(clock=clock)
    t.observe('GOLD', snap())
    clock.tick(5.0)
    # Leg A moved, leg B did not.
    md = snap(spot_q='s2')
    assert t.observe('GOLD', md) == (0.0, 5.0)
    assert md['spot_quote_age_sec'] == 0.0
    assert md['fut_quote_age_sec'] == 5.0


def test_the_rate_can_look_healthy_while_one_leg_is_dead():
    """The exact shape of the live failure: leg A ticking every poll
    keeps the quote rate up while leg B has not moved for a minute."""
    clock = Clock()
    t = QuoteAgeTracker(clock=clock)
    t.observe('GOLD', snap())
    md = None
    for i in range(60):
        clock.tick(1.0)
        md = snap(spot_q=f's{i}')
        t.observe('GOLD', md)
    assert md['spot_quote_age_sec'] == 0.0
    assert md['fut_quote_age_sec'] == pytest.approx(60.0)
    assert 'Leg B' in stale_quote(md, 2.0)


def test_it_is_measured_on_the_local_clock_not_the_brokers():
    """`tick.time - time.time()` conflates the broker's clock offset
    with staleness — the conflation that made the broker-clock line flap
    for weeks. A guard that gates real orders cannot inherit it, so the
    tracker never reads the tick's own timestamp: a quote stamped in
    1970 is fresh if it just arrived."""
    clock = Clock()
    t = QuoteAgeTracker(clock=clock)
    t.observe('GOLD', {'spot_quote_id': '0:1/2', 'fut_quote_id': '0:3/4'})
    clock.tick(0.5)
    md = {'spot_quote_id': '0:1.5/2.5', 'fut_quote_id': '0:3.5/4.5'}
    assert t.observe('GOLD', md) == (0.0, 0.0)
    assert stale_quote(md, 2.0) is None


def test_the_worse_leg_names_itself():
    md = {'spot_quote_age_sec': 0.0, 'fut_quote_age_sec': 12.4}
    note = stale_quote(md, 2.0)
    assert 'Leg B' in note and '12.4s' in note and '2s' in note
    assert stale_quote({'spot_quote_age_sec': 9.0,
                        'fut_quote_age_sec': 0.0}, 2.0).startswith('Leg A')


@pytest.mark.parametrize('limit', [0, 0.0, None])
def test_zero_turns_it_off(limit):
    assert stale_quote({'spot_quote_age_sec': 999.0,
                        'fut_quote_age_sec': 999.0}, limit) is None


def test_compute_market_data_publishes_a_quote_id_per_leg():
    from types import SimpleNamespace
    md = marketdata.compute_market_data(
        {'name': 'GOLD'},
        SimpleNamespace(bid=100.0, ask=100.2, last=0, time=1),
        SimpleNamespace(bid=110.0, ask=110.4, last=0, time=2))
    assert md['spot_quote_id'] != md['fut_quote_id']
    # The combined id the sigma dedup uses is still there and still
    # changes when either leg does.
    assert md['quote_id'] == f"{md['spot_quote_id']}|{md['fut_quote_id']}"


# --- what the guard withholds ----------------------------------------

class Coord:
    """Just enough Coordinator to exercise the gate."""

    def __init__(self, max_age=2.0, grace=10.0, clock=None):
        from types import SimpleNamespace
        self.config = SimpleNamespace(EXECUTION={
            'MAX_QUOTE_AGE_SEC': max_age, 'STALE_STOP_GRACE_SEC': grace})
        self._stale_since = {}
        self._last_stale_note = {}

    _stale_quote = Coordinator._stale_quote
    _gate_on_stale_quote = Coordinator._gate_on_stale_quote
    STALE_BLOCKED_PROFIT = Coordinator.STALE_BLOCKED_PROFIT
    STALE_DEFERRED_STOPS = Coordinator.STALE_DEFERRED_STOPS


FRESH = {'spot_quote_age_sec': 0.1, 'fut_quote_age_sec': 0.2}
STALE = {'spot_quote_age_sec': 0.1, 'fut_quote_age_sec': 40.0}


@pytest.mark.parametrize('reason', ['MANUAL_TARGET', 'TAKE_PROFIT',
                                    'REVERSION_EXIT'])
def test_a_profit_is_never_taken_off_a_stale_price(reason):
    """The live failure. Waiting costs nothing: a target that existed
    only on a stale quote was never available to take."""
    assert Coord()._gate_on_stale_quote('GOLD', reason, STALE) is None


@pytest.mark.parametrize('reason', ['MANUAL_TARGET', 'DOLLAR_STOP',
                                    'MAX_HOLD', 'OVERNIGHT_CLOSE'])
def test_nothing_is_withheld_on_a_fresh_quote(reason):
    assert Coord()._gate_on_stale_quote('GOLD', reason, FRESH) == reason


@pytest.mark.parametrize('reason', ['MAX_HOLD', 'TIME_STOP',
                                    'OVERNIGHT_CLOSE', 'MANUAL_CLOSE',
                                    'SYSTEM_SHUTDOWN'])
def test_an_exit_that_reads_no_price_is_untouched(reason):
    """The clock, the session cutoff and the operator's own button are
    not reading a quote, so a stale one tells them nothing."""
    assert Coord()._gate_on_stale_quote('GOLD', reason, STALE) == reason


@pytest.mark.parametrize('reason', ['DOLLAR_STOP', 'MANUAL_STOP', 'Z_STOP'])
def test_a_stop_is_deferred_but_never_abandoned(reason, monkeypatch):
    """A trade must always have a stop, so an unrefreshed feed must not
    become a reason to hold a loser for ever."""
    import statarb.coordinator as coord_mod
    now = [5000.0]
    monkeypatch.setattr(coord_mod.time, 'time', lambda: now[0])

    c = Coord(grace=10.0)
    assert c._gate_on_stale_quote('GOLD', reason, STALE) is None
    now[0] += 9.0
    assert c._gate_on_stale_quote('GOLD', reason, STALE) is None
    now[0] += 2.0                      # past the grace
    assert c._gate_on_stale_quote('GOLD', reason, STALE) == reason


def test_the_grace_clock_starts_at_the_first_DEFERRAL(monkeypatch):
    """Not at the first stale tick and not at entry — otherwise a feed
    that was briefly stale hours ago would let the next stop straight
    through."""
    import statarb.coordinator as coord_mod
    now = [5000.0]
    monkeypatch.setattr(coord_mod.time, 'time', lambda: now[0])

    c = Coord(grace=10.0)
    # Stale for a minute, but nothing wanted to exit.
    now[0] += 60.0
    assert c._gate_on_stale_quote('GOLD', None, STALE) is None
    now[0] += 60.0
    assert c._gate_on_stale_quote('GOLD', 'DOLLAR_STOP', STALE) is None


def test_a_refreshed_quote_resets_the_grace(monkeypatch):
    import statarb.coordinator as coord_mod
    now = [5000.0]
    monkeypatch.setattr(coord_mod.time, 'time', lambda: now[0])

    c = Coord(grace=10.0)
    c._gate_on_stale_quote('GOLD', 'DOLLAR_STOP', STALE)
    now[0] += 9.0
    assert c._gate_on_stale_quote('GOLD', 'DOLLAR_STOP', FRESH) == 'DOLLAR_STOP'
    assert 'GOLD' not in c._stale_since
    now[0] += 5.0
    # Stale again — the clock starts over, so the stop waits again.
    assert c._gate_on_stale_quote('GOLD', 'DOLLAR_STOP', STALE) is None


def test_the_guard_is_off_when_the_limit_is_zero():
    c = Coord(max_age=0)
    assert c._gate_on_stale_quote('GOLD', 'MANUAL_TARGET', STALE) \
        == 'MANUAL_TARGET'


# --- end to end, through the coordinator -----------------------------

def _book(coordinator, mid, spot_q, fut_q):
    """A snapshot with the touches, stamped with per-leg quote ids so
    the tracker can age them."""
    from tests.test_manual_trade import market
    md = dict(market(mid))
    md['short_spread'] = mid - 0.30
    md['long_spread'] = mid + 0.30
    md['spot_quote_id'], md['fut_quote_id'] = spot_q, fut_q
    coordinator.quote_ages.observe('GOLD', md)
    return md


def test_a_target_does_not_fire_on_a_leg_that_stopped_quoting(coordinator):
    """POS_0002, reproduced: leg A keeps ticking (so the quote RATE
    stays healthy) while leg B's quote is frozen, and the spread walks
    down through the operator's target. Nothing may close."""
    from tests.test_manual_trade import arm

    clock = Clock()
    coordinator.quote_ages = QuoteAgeTracker(clock=clock)
    coordinator.config.EXECUTION['MAX_QUOTE_AGE_SEC'] = 2.0

    coordinator.active_assets['GOLD']['last_data'] = _book(
        coordinator, 22.0, 's0', 'f0')
    arm(coordinator, direction='SELL_BASIS', lots=1.0, exit_spread=19.0)
    assert coordinator.position_manager.get_active_positions()

    # Leg B frozen at f0; leg A ticks the spread down past the target.
    for i, mid in enumerate((21.0, 20.0, 18.5)):
        clock.tick(5.0)
        coordinator.process_asset('GOLD', _book(coordinator, mid, f's{i}',
                                                'f0'))
    assert coordinator.position_manager.get_active_positions(), \
        "closed on a spread built from a frozen leg"

    # Leg B quotes again — and the target fires immediately.
    clock.tick(0.5)
    coordinator.process_asset('GOLD', _book(coordinator, 18.5, 's9', 'f9'))
    assert not coordinator.position_manager.get_active_positions()


def test_an_armed_entry_waits_for_both_legs(coordinator):
    from tests.test_manual_trade import arm

    clock = Clock()
    coordinator.quote_ages = QuoteAgeTracker(clock=clock)
    coordinator.config.EXECUTION['MAX_QUOTE_AGE_SEC'] = 2.0

    coordinator.active_assets['GOLD']['last_data'] = _book(
        coordinator, 20.0, 's0', 'f0')
    arm(coordinator, direction='SELL_BASIS', entry_spread=22.0,
        exit_spread=19.0, lots=1.0)
    assert not coordinator.position_manager.get_active_positions()

    clock.tick(30.0)
    coordinator._check_manual_arm('GOLD', _book(coordinator, 22.3,
                                                's1', 'f0'))
    assert not coordinator.position_manager.get_active_positions(), \
        "armed on a level built from a frozen leg"
    assert coordinator.manual_order is not None, "must stay armed"

    clock.tick(0.5)
    coordinator._check_manual_arm('GOLD', _book(coordinator, 22.3,
                                                's2', 'f2'))
    assert coordinator.position_manager.get_active_positions()


def test_with_the_guard_OFF_the_same_sequence_closes_the_trade(coordinator):
    """The control. Without this the test above could be passing for
    some unrelated reason — it has to be the guard doing the work, and
    turning the guard off has to reproduce the live loss."""
    from tests.test_manual_trade import arm

    clock = Clock()
    coordinator.quote_ages = QuoteAgeTracker(clock=clock)
    coordinator.config.EXECUTION['MAX_QUOTE_AGE_SEC'] = 0      # off

    coordinator.active_assets['GOLD']['last_data'] = _book(
        coordinator, 22.0, 's0', 'f0')
    arm(coordinator, direction='SELL_BASIS', lots=1.0, exit_spread=19.0)

    for i, mid in enumerate((21.0, 20.0, 18.5)):
        clock.tick(5.0)
        coordinator.process_asset('GOLD', _book(coordinator, mid, f's{i}',
                                                'f0'))
    assert not coordinator.position_manager.get_active_positions()
