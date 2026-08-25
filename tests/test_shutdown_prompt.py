"""Shutting down with a position open ASKS first (operator,
2026-08-25).

`Coordinator.stop()` used to close every active LIVE position
unconditionally, tagged SYSTEM_SHUTDOWN. So restarting to change a
setting liquidated a live trade at market and paid the round trip for
it, with nothing asked and nothing to decline.

The rules pinned here:

- with a console, the operator is asked and their answer decides;
- with NOBODY there (no tty, or the prompt times out) the position is
  LEFT OPEN, because closing is irreversible while a position left
  open is recovered from `position_state` on the next start;
- CLOSE_ON_SHUTDOWN=always restores the old behaviour verbatim, and
  =never skips the question;
- stop() is idempotent, so the operator is never asked twice.
"""

import subprocess
import sys

import pytest

from statarb.models import OrderSide, Position, SignalType, Trade


def make_position(pid='POS_0001', lots=0.05, pnl=-1.06):
    spot = Trade('XAUUSD', OrderSide.BUY, lots)
    fut = Trade('GC1225', OrderSide.SELL, lots)
    p = Position(pid, 'GOLD', SignalType.SELL_BASIS, spot, fut)
    p.lots = lots
    p.unrealized_pnl = pnl
    return p


@pytest.fixture
def live(tmp_path, monkeypatch, config):
    """A LIVE coordinator holding one open position, with the closes
    recorded rather than sent."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coordinator = Coordinator(config, trading_mode='LIVE')
    position = make_position()
    coordinator.position_manager.positions[position.position_id] = position

    closed = []
    coordinator._close = lambda pid, pos, reason, contract, md: (
        closed.append((pid, reason)))
    coordinator._each_leg = lambda: []
    coordinator.notifier.notify_shutdown = lambda m: None
    coordinator.notifier.stop = lambda: None
    return coordinator, closed


# --- the question ---------------------------------------------------------

def test_yes_closes_the_position(live):
    coordinator, closed = live
    coordinator.stop(close_positions=True)
    assert closed == [('POS_0001', 'SYSTEM_SHUTDOWN')]


def test_no_leaves_it_open(live):
    """The whole point of asking. Nothing is sent to the broker."""
    coordinator, closed = live
    coordinator.stop(close_positions=False)
    assert closed == []
    # ...and it is still on the books, so a restart recovers it.
    assert coordinator.position_manager.get_active_positions()


def test_the_operator_is_asked_and_y_means_close(live, monkeypatch):
    coordinator, closed = live
    asked = []
    monkeypatch.setattr(type(coordinator), '_prompt_line',
                        staticmethod(lambda prompt, timeout: (
                            asked.append(prompt) or 'y\n')))
    coordinator.stop()
    assert asked, 'the operator was never asked'
    assert closed == [('POS_0001', 'SYSTEM_SHUTDOWN')]


@pytest.mark.parametrize('answer', ['n\n', '\n', 'no\n', 'later\n'])
def test_anything_but_yes_keeps_the_position(live, monkeypatch, answer):
    """[y/N]: the default is the safe one, and only an explicit yes
    reaches the broker."""
    coordinator, closed = live
    monkeypatch.setattr(type(coordinator), '_prompt_line',
                        staticmethod(lambda prompt, timeout: answer))
    coordinator.stop()
    assert closed == []


def test_no_console_means_keep_it(live, monkeypatch):
    """No tty (the launcher, a service, a test) or a prompt that times
    out -> None. Closing at market must never be what happens because
    nobody was there to answer."""
    coordinator, closed = live
    monkeypatch.setattr(type(coordinator), '_prompt_line',
                        staticmethod(lambda prompt, timeout: None))
    coordinator.stop()
    assert closed == []


def test_the_prompt_names_the_position(live):
    """An operator answering this cannot go and look it up — the
    process is already shutting down."""
    coordinator, _ = live
    position = coordinator.position_manager.positions['POS_0001']
    line = coordinator.describe_open_position('POS_0001', position)
    assert 'POS_0001' in line
    assert 'GOLD' in line
    assert 'SELL_BASIS' in line
    assert '0.05 lots' in line
    assert '$-1.06' in line


# --- the config knob ------------------------------------------------------

def test_always_restores_the_old_behaviour(live, monkeypatch):
    coordinator, closed = live
    coordinator.config.TRADING['CLOSE_ON_SHUTDOWN'] = 'always'
    monkeypatch.setattr(type(coordinator), '_prompt_line',
                        staticmethod(lambda prompt, timeout: pytest.fail(
                            'must not ask when the answer is configured')))
    coordinator.stop()
    assert closed == [('POS_0001', 'SYSTEM_SHUTDOWN')]


def test_never_skips_the_question(live, monkeypatch):
    coordinator, closed = live
    coordinator.config.TRADING['CLOSE_ON_SHUTDOWN'] = 'never'
    monkeypatch.setattr(type(coordinator), '_prompt_line',
                        staticmethod(lambda prompt, timeout: pytest.fail(
                            'must not ask when the answer is configured')))
    coordinator.stop()
    assert closed == []


def test_a_typo_falls_back_to_asking(live, monkeypatch):
    """The failure mode of a mistyped setting must not be silently
    liquidating the book."""
    coordinator, closed = live
    coordinator.config.TRADING['CLOSE_ON_SHUTDOWN'] = 'yes please'
    monkeypatch.setattr(type(coordinator), '_prompt_line',
                        staticmethod(lambda prompt, timeout: None))
    coordinator.stop()
    assert closed == []


def test_ask_is_the_default():
    from statarb.config import AlgoTradingConfig
    assert AlgoTradingConfig().TRADING['CLOSE_ON_SHUTDOWN'] == 'ask'


def test_the_shutdown_keys_hot_apply():
    """Any key the operator can change belongs in HOT_TRADING_KEYS —
    otherwise it is written and then ignored until a restart, which for
    a SHUTDOWN setting is a particularly silly place to need one."""
    from statarb.config import AlgoTradingConfig
    for key in ('CLOSE_ON_SHUTDOWN', 'SHUTDOWN_PROMPT_SEC'):
        assert key in AlgoTradingConfig.HOT_TRADING_KEYS


# --- paper, and being asked twice -----------------------------------------

def test_paper_is_never_asked(tmp_path, monkeypatch, config):
    """Paper positions are not at a broker; there is nothing to close
    and nothing to ask about."""
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coordinator = Coordinator(config, trading_mode='PAPER')
    position = make_position()
    coordinator.position_manager.positions[position.position_id] = position
    monkeypatch.setattr(type(coordinator), '_prompt_line',
                        staticmethod(lambda prompt, timeout: pytest.fail(
                            'paper must not prompt')))
    coordinator._each_leg = lambda: []
    coordinator.notifier.notify_shutdown = lambda m: None
    coordinator.notifier.stop = lambda: None
    coordinator.stop()


def test_stop_is_idempotent(live, monkeypatch):
    """run() calls stop() on KeyboardInterrupt and main() catches one
    too. Asking twice — and acting on the second answer — is not a
    thing this should ever do."""
    coordinator, closed = live
    asks = []
    monkeypatch.setattr(type(coordinator), '_prompt_line',
                        staticmethod(lambda prompt, timeout: (
                            asks.append(1) or 'n\n')))
    coordinator.stop()
    coordinator.stop()
    assert len(asks) == 1
    assert closed == []


def test_a_flat_book_asks_nothing(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    from statarb.coordinator import Coordinator
    coordinator = Coordinator(config, trading_mode='LIVE')
    monkeypatch.setattr(type(coordinator), '_prompt_line',
                        staticmethod(lambda prompt, timeout: pytest.fail(
                            'nothing is open — nothing to ask')))
    coordinator._each_leg = lambda: []
    coordinator.notifier.notify_shutdown = lambda m: None
    coordinator.notifier.stop = lambda: None
    coordinator.stop()


# --- the prompt itself ----------------------------------------------------

def test_prompt_returns_none_without_a_tty(monkeypatch):
    from statarb.coordinator import Coordinator

    class NotATty:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, 'stdin', NotATty())
    assert Coordinator._prompt_line('go? ', 5.0) is None


def test_prompt_gives_up_rather_than_hanging(monkeypatch):
    """A daemon reader with a join timeout: an unanswered prompt must
    not hold the process open."""
    import threading
    from statarb.coordinator import Coordinator

    never = threading.Event()

    class SilentTty:
        def isatty(self):
            return True

        def readline(self):
            never.wait()      # nobody types anything, ever
            return ''

    monkeypatch.setattr(sys, 'stdin', SilentTty())
    assert Coordinator._prompt_line('go? ', 0.2) is None
    never.set()


# --- the launcher has to leave room for the answer ------------------------

def test_the_launcher_waits_for_the_child_before_killing_it():
    """Children share this console, so Ctrl+C already reached them and
    they are running their own shutdown. terminate() used to land on
    top of that — on Windows TerminateProcess, which nothing can catch
    — so the prompt would be killed before it could be answered."""
    from start import Child
    child = Child('coordinator', ['true'], grace=7)

    events = []

    class FakeProc:
        returncode = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            events.append(('wait', timeout))
            return 0

        def terminate(self):
            events.append(('terminate', None))

        def kill(self):
            events.append(('kill', None))

    child.proc = FakeProc()
    child.stop()
    assert events == [('wait', 7)], events


def test_the_launcher_still_kills_a_child_that_will_not_go():
    from start import Child
    child = Child('leg runner', ['true'], grace=1)

    events = []

    class Stubborn:
        returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            events.append(('wait', timeout))
            if len(events) == 1:
                raise subprocess.TimeoutExpired('x', timeout)
            return 0

        def terminate(self):
            events.append(('terminate', None))

        def kill(self):
            events.append(('kill', None))

    child.proc = Stubborn()
    child.stop()
    assert ('terminate', None) in events


def test_the_setting_round_trips_through_the_ui():
    """A knob with no control is how COMMISSION_PER_LOT_* sat at zero
    for months. Posted, stored, and read back."""
    from statarb import webapi
    raw, _, _ = webapi.apply_ui_config(
        {}, {'close_on_shutdown': 'NEVER', 'shutdown_prompt_sec': 45})
    assert raw['trading']['CLOSE_ON_SHUTDOWN'] == 'never'
    assert raw['trading']['SHUTDOWN_PROMPT_SEC'] == 45
    ui = webapi.to_ui_config(raw)
    assert ui['close_on_shutdown'] == 'never'
    assert ui['shutdown_prompt_sec'] == 45


def test_the_launcher_grace_covers_the_prompt():
    """The window the launcher gives the coordinator must be longer
    than the window the coordinator waits for an answer, or the
    question is asked and then killed mid-answer."""
    from start import shutdown_grace
    assert shutdown_grace({'trading': {'SHUTDOWN_PROMPT_SEC': 30.0}}) > 30.0
    assert shutdown_grace({'trading': {'SHUTDOWN_PROMPT_SEC': 120.0}}) > 120.0
    # A missing or unreadable value must still leave room for the
    # default prompt rather than collapsing to zero.
    assert shutdown_grace({}) > 30.0
    assert shutdown_grace({'trading': {'SHUTDOWN_PROMPT_SEC': 'soon'}}) > 30.0
