"""A position saved MID-CLOSE comes back under management
(live 2026-08-26).

`close_position` sets CLOSING before it calls the broker. If the
process dies in that window the row stays CLOSING, and
`load_open_position_states` deliberately loads it — someone knew it
mattered. But `Position.from_dict` restores the status verbatim and
EVERY lookup in PositionManager filters on ACTIVE, so the position came
back invisible:

- not in `get_active_positions`, so the exit ladder never sees it;
- not counted by the health block, which read "1 position(s) being
  managed" beside two recovered positions;
- not in the reconciler's known-ticket set, which is also built from
  ACTIVE positions.

Meanwhile the money may still be at the broker. POS_0001 sat like that
across at least two restarts on the live box.

The call is the same one `_close_failed` makes for the in-process
version: a close that was not seen to complete leaves the position
OPEN. If it actually closed, the reconciler's 3-strike ghost clear
resolves it — and that only looks at ACTIVE positions too.
"""

import pytest

from statarb.coordinator import Coordinator
from statarb.models import (OrderSide, Position, PositionStatus, SignalType,
                            Trade)


def make_position(pid, status):
    spot = Trade('XAUUSD', OrderSide.BUY, 0.02)
    fut = Trade('GCZ6', OrderSide.SELL, 0.02)
    spot.executed_price, fut.executed_price = 4629.23, 4684.32
    position = Position(pid, 'GOLD', SignalType.SELL_BASIS, spot, fut)
    position.status = status
    return position


@pytest.fixture
def coord(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)
    return Coordinator(config, trading_mode='LIVE')


def recover(coord, *positions):
    for position in positions:
        coord.data_logger.save_position_state(position)
    coord._recover_positions()
    return coord.position_manager


def test_a_mid_close_position_comes_back_ACTIVE(coord):
    manager = recover(coord, make_position('POS_0001', PositionStatus.CLOSING))
    assert 'POS_0001' in manager.get_active_positions(), \
        'recovered but managed by nothing — the live fault'


def test_it_is_counted_by_the_health_block(coord):
    """"1 position(s) being managed" beside two recovered positions is
    how this stayed hidden."""
    recover(coord,
            make_position('POS_0001', PositionStatus.CLOSING),
            make_position('POS_0004', PositionStatus.ACTIVE))
    assert len(coord.position_manager
               .get_positions_for_asset('GOLD')) == 2


def test_the_reconciler_now_knows_its_tickets(coord):
    """The known-ticket set is built from ACTIVE positions, so a
    CLOSING one's tickets read as orphans at the broker."""
    position = make_position('POS_0001', PositionStatus.CLOSING)
    position.spot_trade.position_tickets = [8801]
    position.futures_trade.position_tickets = [8802]
    recover(coord, position)
    known = set()
    for p in coord.position_manager.get_active_positions().values():
        for trade in (p.spot_trade, p.futures_trade):
            known.update(trade.position_tickets or [])
    assert known == {8801, 8802}


def test_an_ACTIVE_row_is_untouched(coord):
    manager = recover(coord, make_position('POS_0004',
                                           PositionStatus.ACTIVE))
    assert manager.positions['POS_0004'].status is PositionStatus.ACTIVE


def test_the_promotion_is_PERSISTED(coord):
    """Otherwise the next restart re-reads CLOSING and the warning
    repeats forever — which is what the live box was doing."""
    recover(coord, make_position('POS_0001', PositionStatus.CLOSING))
    rows = coord.data_logger.load_open_position_states()
    assert [r['status'] for r in rows] == ['ACTIVE']


def test_it_says_so(coord, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        recover(coord, make_position('POS_0001', PositionStatus.CLOSING))
    assert any('saved mid-close' in r.getMessage() for r in caplog.records)
