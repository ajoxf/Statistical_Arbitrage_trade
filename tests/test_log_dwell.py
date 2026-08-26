"""A verdict must HOLD before the health block is reprinted
(operator, 2026-08-26: "do both" — raise the staleness threshold and
stop the log flooding).

The status log is event-driven, which is right: a fixed cadence wrote
the same sentence 360 times an hour. But the event is "a verdict
changed", and the block is SEVEN lines, so any gate sitting on top of
its own threshold turns the fix back into the flood. Live 2026-08-26
the staleness guard was set at 2.0s against a feed whose legs
routinely gapped 2.0-2.5s: entries went OK -> BLOCKED -> OK several
times a second and each flip cost seven lines.

Dwell costs a few seconds of latency on a genuine change, which a log
can afford. What it must NOT do is hide the flapping: a threshold
sitting on the live figure is exactly the thing the operator needs to
go and widen, so the changes that were withheld are counted and
reported beside whatever finally settles.
"""

import logging
import time

import pytest

from statarb.coordinator import Coordinator


def market_data(spot=3300.0, futures=3320.0):
    from datetime import datetime
    return {'GOLD': {
        'asset_name': 'GOLD', 'timestamp': datetime.now(),
        'quote_id': f'{spot}/{futures}',
        'spot_price': spot, 'spot_bid': spot - 0.1, 'spot_ask': spot + 0.1,
        'futures_price': futures, 'futures_bid': futures - 0.1,
        'futures_ask': futures + 0.1,
        'actual_basis': futures - spot, 'spread': futures - spot,
        'basis_pct': 0.0, 'hedge_ratio': 1.0,
        'spread_formula': 'spread = futures - 1 x spot',
    }}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def rig(tmp_path, monkeypatch, config, caplog):
    """A coordinator whose health verdict is whatever the test says it
    is, on a clock the test drives."""
    monkeypatch.chdir(tmp_path)
    coordinator = Coordinator(config, trading_mode='PAPER')
    clock = Clock()
    monkeypatch.setattr(time, 'monotonic', clock)

    state = {'value': ('OK',)}
    coordinator._status_state = lambda key, md: state['value']
    coordinator._health = lambda key, md: [
        ('entries', state['value'][0], 'because')]

    caplog.set_level(logging.INFO)

    def poll(verdict=None, advance=0.0, heartbeat=False):
        if verdict is not None:
            state['value'] = (verdict,)
        clock.advance(advance)
        caplog.clear()
        coordinator.log_status(market_data(), heartbeat=heartbeat)
        return [r.getMessage() for r in caplog.records]

    return coordinator, poll


# --- the flood ------------------------------------------------------------

def test_a_verdict_that_reverts_inside_the_dwell_is_never_printed(rig):
    """The live case: OK -> BLOCKED -> OK, several times a second,
    seven lines each way."""
    coordinator, poll = rig
    coordinator.config.TRADING['LOG_STATE_DWELL_SEC'] = 5.0
    poll('OK')                                  # settle on a baseline
    poll(advance=10.0)
    printed = []
    for _ in range(40):
        printed += poll('BLOCKED', advance=0.3)
        printed += poll('OK', advance=0.3)
    assert printed == [], f"the flood came back: {printed[:3]}"


def test_a_change_that_holds_is_printed_once(rig):
    coordinator, poll = rig
    coordinator.config.TRADING['LOG_STATE_DWELL_SEC'] = 5.0
    poll('OK')
    poll(advance=10.0)
    assert poll('BLOCKED', advance=0.3) == [], 'printed before it settled'
    assert poll(advance=2.0) == [], 'printed before it settled'
    lines = poll(advance=4.0)
    assert any('BLOCKED' in line for line in lines)
    # ...and not again on the next poll, because it is no longer news.
    assert poll(advance=0.3) == []


def test_the_withheld_changes_are_COUNTED_not_lost(rig):
    """A block that arrives quietly, having flapped thirty times to get
    there, reads as a stable engine. The count is the operator's cue
    that the threshold is sitting on the live figure."""
    coordinator, poll = rig
    coordinator.config.TRADING['LOG_STATE_DWELL_SEC'] = 5.0
    poll('OK')
    poll(advance=10.0)
    for _ in range(15):
        poll('BLOCKED', advance=0.3)
        poll('OK', advance=0.3)
    poll('BLOCKED', advance=0.3)
    lines = poll(advance=6.0)
    headline = lines[0]
    assert '15 earlier changes did not hold' in headline, headline


def test_a_clean_single_change_says_nothing_about_flapping(rig):
    coordinator, poll = rig
    coordinator.config.TRADING['LOG_STATE_DWELL_SEC'] = 5.0
    poll('OK')
    poll(advance=10.0)
    poll('BLOCKED', advance=0.3)
    lines = poll(advance=6.0)
    assert 'did not hold' not in lines[0], lines[0]


def test_the_count_resets_after_it_is_reported(rig):
    coordinator, poll = rig
    coordinator.config.TRADING['LOG_STATE_DWELL_SEC'] = 5.0
    poll('OK')
    poll(advance=10.0)
    for _ in range(4):
        poll('BLOCKED', advance=0.3)
        poll('OK', advance=0.3)
    poll('BLOCKED', advance=0.3)
    poll(advance=6.0)                            # reported, with a count
    poll('OK', advance=0.3)
    lines = poll(advance=6.0)
    assert 'did not hold' not in lines[0], lines[0]


# --- what dwell must not break -------------------------------------------

def test_zero_restores_print_every_change(rig):
    """The old behaviour, verbatim, for anyone who wants it."""
    coordinator, poll = rig
    coordinator.config.TRADING['LOG_STATE_DWELL_SEC'] = 0
    poll('OK')
    assert any('BLOCKED' in line for line in poll('BLOCKED'))
    assert any('OK' in line for line in poll('OK'))


def test_the_heartbeat_still_prints_and_states_the_LIVE_verdict(rig):
    """The heartbeat exists to prove the engine is alive, so it cannot
    be withheld — and it must describe the feed as it is right now,
    not the last verdict that happened to settle."""
    coordinator, poll = rig
    coordinator.config.TRADING['LOG_STATE_DWELL_SEC'] = 5.0
    poll('OK')
    poll(advance=10.0)
    lines = poll('BLOCKED', advance=0.3, heartbeat=True)
    assert lines, 'the heartbeat was swallowed'
    assert '[heartbeat]' in lines[0]
    assert any('BLOCKED' in line for line in lines)


def test_a_genuine_change_is_still_reported_promptly(rig):
    """Dwell buys quiet at the price of a few seconds. Anything longer
    than that and the log stops being a record of what happened."""
    coordinator, poll = rig
    coordinator.config.TRADING['LOG_STATE_DWELL_SEC'] = 5.0
    poll('OK')
    poll(advance=10.0)
    poll('BLOCKED', advance=0.3)
    assert poll(advance=5.0), 'a settled change was still not printed'


def test_the_dwell_hot_applies():
    """A knob with no live effect is how COMMISSION_PER_LOT sat at zero
    for months. Every setting on that page belongs in the hot tuple."""
    from statarb.config import AlgoTradingConfig
    assert 'LOG_STATE_DWELL_SEC' in AlgoTradingConfig.HOT_TRADING_KEYS


def test_the_default_threshold_clears_a_real_feeds_gaps():
    """2.0s was the first guess and it was under the gaps a healthy
    retail feed actually shows (live 2026-08-26: routine 2.0-2.5s on
    one leg or the other at 102 quotes/min)."""
    from statarb.config import AlgoTradingConfig
    assert AlgoTradingConfig().EXECUTION['MAX_QUOTE_AGE_SEC'] >= 5.0
