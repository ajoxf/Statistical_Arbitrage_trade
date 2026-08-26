"""A leg LAGGING is not a leg STOPPED (live 2026-08-26, POS_0004).

The staleness guard shipped the day before catches a leg that has
stopped quoting. It cannot catch a leg that is a moment behind its
partner during a fast move, because both legs are ticking hard and the
feed reads perfectly healthy — `oldest leg 0.0s` in the live log.

The trade:

    13:14:54   spread +54.96      heartbeat
    13:16:03   spread  53.26      the operator's 54.18 target fired here
               filled  55.30      2.04 away, $20.40 at k=10
    13:16:08   spread +55.26      heartbeat, five seconds later

Sigma was 0.29, so 53.26 is eight sigma below the mean and gone within
seconds. The recorded extremes give it away completely: peak +$18.30
and trough -$35.40 were BOTH stamped at minute 280 — a 5.3-point swing
in the spread inside one minute, on a series whose sigma is 0.29. The
target was worth +$9.14 and the trade booked -$2.10.
"""

import pytest

from statarb.marketdata import SpreadJumpTracker

SIGMA = 0.29
MAX_SIGMAS = 5.0
SETTLE = 2.0


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def rig():
    clock = Clock()
    tracker = SpreadJumpTracker(clock=clock)
    seq = {'n': 0}

    def quote(spread, sigma=SIGMA, max_sigmas=MAX_SIGMAS, repeat=False):
        """One QUOTE. `repeat` re-polls the same one, which is what the
        engine does three times a second between broker ticks."""
        if not repeat:
            seq['n'] += 1
        md = {'spread': spread, 'quote_id': f'q{seq["n"]}'}
        note = tracker.observe('GOLD', md, sigma, max_sigmas, SETTLE)
        return note, md

    return clock, quote


# --- the live sequence ----------------------------------------------------

def test_the_desync_print_is_refused(rig):
    _, quote = rig
    for spread in (55.50, 55.46, 55.52, 54.96):
        note, _ = quote(spread)
        assert note is None, f"a normal tick was refused: {note}"

    note, md = quote(53.26)              # the print the target fired on
    assert note is not None
    assert 'lagging' in note
    assert md['spread_jump_sigmas'] > 5


def test_the_control_lets_it_through_with_the_guard_off(rig):
    """Otherwise the test above could be passing for any reason at
    all."""
    _, quote = rig
    quote(54.96, max_sigmas=0)
    note, _ = quote(53.26, max_sigmas=0)
    assert note is None


def test_the_level_stays_refused_while_the_series_is_disturbed(rig):
    """A desync jumps twice — out and back — and BOTH prints are
    untradeable. Blocking only the quote that jumped would have let the
    trade fire on the very next one."""
    clock, quote = rig
    quote(54.96)
    assert quote(53.26)[0] is not None
    clock.advance(0.5)
    assert quote(53.30)[0] is not None, 'the second bad print got through'
    clock.advance(0.5)
    assert quote(55.26)[0] is not None, 'the jump BACK is a jump too'


def test_it_clears_once_the_series_settles(rig):
    """Waiting costs nothing on a target, but a guard that never
    releases is a position that can never be closed."""
    clock, quote = rig
    quote(54.96)
    assert quote(53.26)[0] is not None
    clock.advance(SETTLE + 0.1)
    assert quote(55.26)[0] is not None, 'the jump back re-arms it'
    clock.advance(SETTLE + 0.1)
    assert quote(55.24)[0] is None, 'still blocked on a quiet series'


# --- what it must not do --------------------------------------------------

def test_a_normal_fast_market_is_not_blocked(rig):
    """Gold moving is not the fault. The legs moving APART is."""
    _, quote = rig
    for spread in (55.50, 55.61, 55.49, 55.72, 55.38, 55.60, 55.44):
        note, _ = quote(spread)
        assert note is None, f"{spread} was refused: {note}"


def test_a_repeated_poll_is_not_a_jump(rig):
    """The engine polls 2-3x a second and the brokers tick far slower,
    so most calls carry the SAME quote. Measuring those as movement
    would report a jump of zero forever and, worse, would let the
    settle window expire against duplicates."""
    _, quote = rig
    quote(54.96)
    note, md = quote(53.26)
    assert note is not None
    for _ in range(10):
        note, md = quote(53.26, repeat=True)
        assert note is not None, 'a duplicate poll cleared the block'


def test_no_sigma_means_no_opinion(rig):
    """Cold start. The engine must not refuse to trade because it
    cannot yet judge — the warm-up gates are what hold it back there."""
    _, quote = rig
    assert quote(55.50, sigma=None)[0] is None
    assert quote(40.00, sigma=None)[0] is None


def test_the_series_is_tracked_through_the_cold_start(rig):
    """The first quote after warm-up needs something to be measured
    against, or the guard is blind for exactly one tick — and one tick
    is all this fault takes."""
    _, quote = rig
    quote(54.96, sigma=None)             # cold: tracked, no opinion
    note, _ = quote(53.26)               # sigma arrives; measured at once
    assert note is not None


def test_the_reason_names_the_numbers(rig):
    """A block the operator cannot check is a block they will switch
    off."""
    _, quote = rig
    quote(54.96)
    note, _ = quote(53.26)
    assert '1.70' in note                # the jump
    assert '5.9 sigma' in note           # ...and what it is in sigmas


# --- end to end, through the coordinator ---------------------------------

from statarb.marketdata import QuoteAgeTracker             # noqa: E402
from tests.test_manual_trade import coordinator            # noqa: F401,E402


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


def _book(coordinator, mid, quote='q'):
    from tests.test_manual_trade import market
    md = dict(market(mid))
    md['short_spread'] = mid - 0.30
    md['long_spread'] = mid + 0.30
    md['quote_id'] = quote
    md['spot_quote_id'], md['fut_quote_id'] = quote, quote
    coordinator.quote_ages.observe('GOLD', md)
    return md


def _seed_sigma(coordinator):
    """A REAL SpreadStats carrying a real sigma.

    Not a stub: the jump guard reads `sigma` and the health block reads
    half a dozen other attributes, and a stub that satisfies one and
    not the others proves nothing about either. (A fixture inventing an
    attribute the engine never sets is what hid the shutdown-prompt
    crash earlier today.)
    """
    from statarb.spread import SpreadStats
    stats = SpreadStats(coordinator.config.SIGNALS)
    for i, value in enumerate(_JITTER):
        stats.update(22.0 + value, f'seed{i}')
    coordinator.stats['GOLD'] = stats
    return stats


# +/- one sigma-ish around 22.0, so the window has a real spread of
# values rather than a contrived one.
_JITTER = [0.29, -0.29, 0.15, -0.44, 0.30, -0.15, 0.44, -0.30,
           0.20, -0.20, 0.35, -0.35]


def _rig(coordinator, jump_sigma=5.0):
    """Both legs quoting perfectly — only the JUMP guard can fire."""
    _seed_sigma(coordinator)
    clock = _Clock()
    coordinator.quote_ages = QuoteAgeTracker(clock=clock)
    coordinator.spread_jumps = SpreadJumpTracker(clock=clock)
    coordinator.config.EXECUTION.update({
        'MAX_QUOTE_AGE_SEC': 5.0,
        'MAX_SPREAD_JUMP_SIGMA': jump_sigma,
        'JUMP_SETTLE_SEC': SETTLE,
    })
    return clock


def _walk(coordinator, clock, mids):
    md = None
    for i, mid in enumerate(mids):
        clock.tick(0.5)
        md = _book(coordinator, mid, f'q{i}')
        coordinator.process_asset('GOLD', md)
    return md


def test_a_target_does_not_fire_on_a_desynced_print(coordinator):
    """POS_0004 reproduced. Both legs quote on every tick, so the
    staleness guard reads a perfectly healthy feed — and the spread
    still prints 1.7 below anything the market is offering."""
    from tests.test_manual_trade import arm

    clock = _rig(coordinator)
    coordinator.active_assets['GOLD']['last_data'] = _book(
        coordinator, 22.0, 'q_seed')
    arm(coordinator, direction='SELL_BASIS', lots=1.0, exit_spread=19.0)
    assert coordinator.position_manager.get_active_positions()

    # A quiet series, then one print far below it — through the target
    # — which no book was offering.
    _walk(coordinator, clock, (22.0, 21.9, 22.1, 18.5))
    assert coordinator.position_manager.get_active_positions(), \
        "closed on a print neither book was offering"

    # The series settles somewhere the target is NOT reached, and the
    # position is still open — which is the whole point: the operator
    # keeps the trade instead of paying 1.7 of slippage for it.
    clock.tick(SETTLE + 1.0)
    coordinator.process_asset('GOLD', _book(coordinator, 22.05, 'qz'))
    assert coordinator.position_manager.get_active_positions()


def test_with_the_guard_OFF_the_same_sequence_loses_the_trade(coordinator):
    """The control — otherwise the test above could pass for any
    reason. With the guard off, the desync print closes the position,
    which is exactly what happened live."""
    from tests.test_manual_trade import arm

    clock = _rig(coordinator, jump_sigma=0)
    coordinator.active_assets['GOLD']['last_data'] = _book(
        coordinator, 22.0, 'q_seed')
    arm(coordinator, direction='SELL_BASIS', lots=1.0, exit_spread=19.0)

    _walk(coordinator, clock, (22.0, 21.9, 22.1, 18.5))
    assert not coordinator.position_manager.get_active_positions(), \
        "the guard was supposed to be off"


def test_a_real_move_to_the_target_still_closes(coordinator):
    """The guard must not become a reason a target never fires. Walk
    the spread down in ordinary steps and it closes as it always did."""
    from tests.test_manual_trade import arm

    clock = _rig(coordinator)
    coordinator.active_assets['GOLD']['last_data'] = _book(
        coordinator, 22.0, 'q_seed')
    arm(coordinator, direction='SELL_BASIS', lots=1.0, exit_spread=19.0)

    _walk(coordinator, clock, (21.7, 21.4, 21.1, 20.8, 20.5, 20.2,
                               19.9, 19.6, 19.3, 19.0, 18.7, 18.5))
    assert not coordinator.position_manager.get_active_positions(), \
        "a real walk to the target was blocked"


def test_the_health_line_names_it(coordinator):
    """The feed row read OK through the whole live incident."""
    clock = _rig(coordinator)
    coordinator.active_assets['GOLD']['last_data'] = _book(
        coordinator, 22.0, 'q_seed')
    md = _walk(coordinator, clock, (22.0, 21.9, 18.5))
    feed = {name: (state, detail)
            for name, state, detail in coordinator._health('GOLD', md)}['feed']
    assert feed[0] == coordinator.BLOCKED
    assert 'lagging' in feed[1]


# --- the exit style that had a control and no wiring ---------------------
# Item 1 of the operator's list — "make the target a resting limit" —
# was advice they could not follow: the Settings page has had an "Exit
# Execution Mode" selector since the vendored UI landed, and it was
# never in webapi.FIELD_MAP, so the server dropped it on every save.
# The maker/taker-fee fault of 2026-08-10 exactly. Worse than dead: the
# knob that DID govern exit style was labelled "Entry".

def test_exit_execution_mode_reaches_the_engine():
    from statarb.webapi import FIELD_MAP, apply_ui_config
    from statarb.config import AlgoTradingConfig
    assert FIELD_MAP['exit_execution_mode'] == ('EXECUTION', 'EXIT_STYLE')
    raw = {}
    apply_ui_config(raw, {'exit_execution_mode': 'LIMIT'})
    assert raw['execution']['EXIT_STYLE'] == 'limit', \
        'saved and silently dropped, like the maker/taker boxes'


def test_a_non_urgent_close_obeys_EXIT_STYLE():
    from statarb.config import AlgoTradingConfig
    from statarb.pair_executor import PairExecutor
    from statarb.models import OrderSide, Trade

    config = AlgoTradingConfig()
    config.EXECUTION.update({'ENTRY_STYLE': 'market', 'EXIT_STYLE': 'limit'})
    styles = []

    executor = PairExecutor(config, None, None)
    executor._limit_child = lambda *a, **k: (
        styles.append('limit') or {'filled': 1.0, 'price': 1.0,
                                   'tickets': []})
    executor._market_close_ticket = lambda *a, **k: (
        styles.append('market') or {'filled': 1.0, 'price': 1.0})

    trade = Trade('XAUUSD', OrderSide.BUY, 1.0)
    trade.position_tickets = [1]
    executor._close_leg(None, trade, 'c', urgent=False)
    assert styles == ['limit']

    # A STOP still crosses, whatever the setting says.
    styles.clear()
    executor._close_leg(None, trade, 'c', urgent=True)
    assert styles == ['market']


def test_it_falls_back_to_ENTRY_STYLE_when_unset():
    """A config written before EXIT_STYLE existed must behave exactly
    as it did — that is what the exit path read."""
    from statarb.config import AlgoTradingConfig
    from statarb.pair_executor import PairExecutor
    from statarb.models import OrderSide, Trade

    config = AlgoTradingConfig()
    config.EXECUTION.update({'ENTRY_STYLE': 'limit', 'EXIT_STYLE': ''})
    styles = []
    executor = PairExecutor(config, None, None)
    executor._limit_child = lambda *a, **k: (
        styles.append('limit') or {'filled': 1.0, 'price': 1.0,
                                   'tickets': []})
    executor._market_close_ticket = lambda *a, **k: (
        styles.append('market') or {'filled': 1.0, 'price': 1.0})
    trade = Trade('XAUUSD', OrderSide.BUY, 1.0)
    trade.position_tickets = [1]
    executor._close_leg(None, trade, 'c', urgent=False)
    assert styles == ['limit']
