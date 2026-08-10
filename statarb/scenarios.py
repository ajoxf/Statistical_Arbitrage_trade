"""Round-trip order scenarios: does this pair of accounts actually
trade the way the engine assumes?

Ported from the old app's Full Order Test Suite (feature_files/app.py),
adapted to this system's two-account architecture: the old one drove a
single MT5 connection, so it could place both legs itself. Here spot
and futures may live on different terminals, so every action goes
through a leg (LocalLeg or RemoteLeg) and the runner never imports MT5.

Each scenario is a complete round trip at MINIMUM volume — open, then
close — so nothing is left on the book. Spread scenarios roll back the
first leg if the second one fails: a test must never leave a naked
position behind.

The output is one text block per scenario, in the old app's format
(per-action prices with slippage vs target and mid, the spread and its
z at open and close, fees, gross/net), because that block is what the
operator reads to decide the plumbing is sound.
"""

import math
import time

from . import sizing

SCENARIO_TYPES = [
    ('BUY_SPOT', 'BUY_SPOT'),
    ('SELL_FUT', 'SELL_FUTURES'),
    ('BUY_FUT', 'BUY_FUTURES'),
    ('SELL_SPOT', 'SELL_SPOT'),
    ('LONG_SPR', 'LONG_SPREAD'),
    ('SHORT_SPR', 'SHORT_SPREAD'),
]

# Spacing the UI uses between scenarios in a full run (seconds).
RUN_SPACING_SEC = {'LIMIT': 20, 'MARKET': 5}

#: How long a scenario HOLDS an open position before closing it. The
#: default is a formality — long enough for the fill to settle, short
#: enough that a 40-scenario run is quick. An operator can raise it to
#: watch positions behave in the live book (margin, swap, the terminal's
#: own P&L), at the cost of real market exposure for that whole time
#: and a much longer run. `quick_close` variants ignore it on purpose:
#: testing the immediate close is the point of them.
DEFAULT_HOLD_SEC = 0.5
MAX_HOLD_SEC = 600.0

LIMIT_FILL_TIMEOUT_SEC = 15.0


def build_catalogue():
    """The 40 scenarios, in run order: 18 LIMIT (6 order types x 3
    attempts, the third a cancel), 18 MARKET (same, the third a
    quick-close), then 4 partial-fill recoveries."""
    out = []
    for kind, label in SCENARIO_TYPES:
        out.append({'type': kind, 'mode': 'LIMIT', 'variant': 'normal',
                    'name': f'{label} #1'})
        out.append({'type': kind, 'mode': 'LIMIT', 'variant': 'normal',
                    'name': f'{label} #2'})
        out.append({'type': kind, 'mode': 'LIMIT', 'variant': 'cancel',
                    'name': f'{label} #3 (cancel)'})
    for kind, label in SCENARIO_TYPES:
        out.append({'type': kind, 'mode': 'MARKET', 'variant': 'normal',
                    'name': f'MKT {label} #1'})
        out.append({'type': kind, 'mode': 'MARKET', 'variant': 'normal',
                    'name': f'MKT {label} #2'})
        out.append({'type': kind, 'mode': 'MARKET', 'variant': 'quick_close',
                    'name': f'MKT {label} #3 (quick-close)'})
    for spread, label in (('LONG_SPR', 'LONG_SPREAD'),
                          ('SHORT_SPR', 'SHORT_SPREAD')):
        out.append({'type': spread, 'mode': 'MARKET',
                    'variant': 'partial_spot',
                    'name': f'{label} partial: spot fills, futures fails '
                            f'→ market-close spot'})
        out.append({'type': spread, 'mode': 'MARKET',
                    'variant': 'partial_futures',
                    'name': f'{label} partial: futures fills, spot fails '
                            f'→ market-close futures'})
    for index, scenario in enumerate(out):
        scenario['id'] = index
    return out


CATALOGUE = build_catalogue()


def _digits(tick_size):
    """Decimals implied by the symbol's tick size, for the report."""
    text = f"{float(tick_size or 0.01):.10f}".rstrip('0')
    return max(2, len(text.split('.')[1]) if '.' in text else 2)


def _fmt(value, digits):
    return f"${value:.{digits}f}"


class Leg:
    """One side of a scenario: an account, a symbol and its specs."""

    def __init__(self, leg, symbol, role, contract_size=100.0,
                 commission_per_lot=0.0):
        self.leg = leg
        self.symbol = symbol
        self.role = role                 # 'SPOT' or 'FUTURES'
        self.contract_size = float(contract_size or 100.0)
        self.commission_per_lot = float(commission_per_lot or 0.0)
        self.meta = None

    @property
    def account(self):
        return getattr(self.leg, 'name', '?')

    def label(self, side):
        return f'{self.role} {side}'

    def specs(self):
        if self.meta is None:
            self.meta = self.leg.ensure_symbol(self.symbol) or {'ok': False}
        return self.meta

    def verify(self, ticket):
        """Read the ticket back OUT of MT5. A leg that cannot answer
        (paper fakes, older runners) returns None and verification is
        simply not claimed."""
        verifier = getattr(self.leg, 'verify_order', None)
        if verifier is None or ticket is None:
            return None
        try:
            return verifier(ticket)
        except Exception as e:                    # never break the test
            return {'ticket': ticket, 'confirmed': False, 'error': str(e)}

    def round_trip_fee(self, volume, price):
        """($ , bps) for open+close on this leg. Commission in config is
        quoted round-turn per lot, so one round trip is one charge."""
        fee = self.commission_per_lot * volume
        notional = price * self.contract_size * volume if price else 0.0
        return fee, ((fee / notional) * 10000 if notional > 0 else 0.0)

    def pnl(self, side, open_fill, close_fill, volume):
        if open_fill is None or close_fill is None:
            return None
        move = ((close_fill - open_fill) if side == 'BUY'
                else (open_fill - close_fill))
        return move * self.contract_size * volume


class ScenarioRunner:
    """Runs ONE scenario against the configured legs.

    `spread_stats` is a callable returning (spread, mu, sigma, z) or
    None — the coordinator hands over its live SpreadStats so the
    report carries the same z the strategy would have seen.
    """

    def __init__(self, spot, futures, spread_stats=None,
                 clock=time.time, sleep=time.sleep,
                 fill_timeout=LIMIT_FILL_TIMEOUT_SEC, hedge_ratio=1.0,
                 hold_sec=DEFAULT_HOLD_SEC):
        self.spot = spot
        self.futures = futures
        # Spread scenarios size Leg B against Leg A with this, so a
        # test pair carries the same exposure a real one would.
        self.hedge_ratio = float(hedge_ratio or 1.0)
        self.spread_stats = spread_stats or (lambda: None)
        self.clock = clock
        self.sleep = sleep
        self.fill_timeout = fill_timeout
        # Clamped: a typo of 6000 would strand a live position for
        # nearly two hours with the operator watching a frozen row.
        self.hold_sec = max(0.0, min(float(hold_sec or 0.0), MAX_HOLD_SEC))
        self.actions = []

    # -- one action at a time ------------------------------------------

    def _stamp(self):
        return time.strftime('%H:%M:%S', time.gmtime(self.clock()))

    def _record(self, action):
        self.actions.append(action)
        return action

    def _hold(self):
        """Keep the position open for the configured time, then return.

        Sliced rather than one long sleep. Scenarios run INLINE in the
        coordinator loop (a RemoteLeg has one socket, so a thread would
        interleave on it), which is fine at the half-second default and
        emphatically not fine at two minutes: nothing else in the engine
        runs while this blocks, so the price feed stops, the statistics
        window ages out and the Stop button cannot be read. Slicing lets
        the coordinator's own sleep function pump the feed and notice an
        abort between slices.
        """
        remaining = self.hold_sec
        if remaining <= 0:
            return
        if remaining > 1.0:
            self._record({'ok': True, 'kind': 'hold', 'leg_label': '',
                          'account': '', 'symbol': '', 'side': '',
                          'time': self._stamp(),
                          'note': f'holding the position {remaining:.0f}s '
                                  f'before closing'})
        slice_sec = 0.5
        while remaining > 0:
            self.sleep(min(slice_sec, remaining))
            remaining -= slice_sec

    def _verify(self, side_leg, action):
        """Confirm with MT5 that the ticket we think we created really
        exists there. `order_send` returning success is the broker's
        acknowledgement; this is the broker's RECORD."""
        if not action.get('ok') or not action.get('ticket'):
            return action
        result = side_leg.verify(action['ticket'])
        if result is not None:
            action['verification'] = result
            action['verified'] = bool(result.get('confirmed'))
        return action

    def _quote(self, side_leg, action):
        tick = side_leg.leg.tick(side_leg.symbol)
        if tick:
            action['bid'] = tick['bid']
            action['ask'] = tick['ask']
            action['mid'] = (tick['bid'] + tick['ask']) / 2
        return tick

    def _base(self, side_leg, kind, side):
        specs = side_leg.specs()
        return {'ok': False, 'kind': kind, 'leg_label': side_leg.label(side),
                'account': side_leg.account, 'symbol': side_leg.symbol,
                'side': side, 'digits': _digits(specs.get('tick_size')),
                'time': self._stamp()}

    def _volume(self, side_leg):
        return side_leg.specs().get('volume_min') or 0.01

    def pair_volumes(self):
        """(spot lots, futures lots) for a SPREAD scenario, matched.

        Each leg's own minimum is the right size for a single-leg test,
        but using it on both legs of a pair is not a hedge: on CFI the
        spot minimum is 0.01 (1 oz) and the futures minimum is 0.1
        (10 oz), so a "LONG_SPR" built that way is 9 oz net short, and
        its reported cost is ~94% one leg. Size up the smaller leg until
        both clear their minimum at the configured hedge ratio.

        The arithmetic lives in sizing.matched_minimum_lots, shared with
        the sizing plan the Full Order Test Suite DISPLAYS. Two copies
        would drift, and the copy on screen before a live run is the
        one that must not be wrong.
        """
        return sizing.matched_minimum_lots(
            self._volume(self.spot), self._volume(self.futures),
            self.spot.specs().get('volume_step'),
            self.futures.specs().get('volume_step'),
            self.hedge_ratio)

    def open_market(self, side_leg, side, comment='SCENARIO MKT',
                    volume=None):
        action = self._base(side_leg, 'open', side)
        specs = side_leg.specs()
        if not specs.get('ok'):
            action['error'] = specs.get('error', f'{side_leg.symbol} '
                                                 f'unavailable')
            return self._record(action)
        tick = self._quote(side_leg, action)
        if not tick:
            action['error'] = 'no tick'
            return self._record(action)
        volume = volume or self._volume(side_leg)
        action['tgt'] = tick['ask'] if side == 'BUY' else tick['bid']
        result = side_leg.leg.order(side_leg.symbol, side, volume,
                                    comment=comment)
        if not result.get('ok'):
            action['error'] = result.get('error') or 'order rejected'
            return self._record(action)
        action.update(ok=True, fill=result.get('price'),
                      volume=result.get('filled_volume') or volume,
                      ticket=(result.get('position_tickets') or
                              [result.get('ticket')])[0])
        return self._record(self._verify(side_leg, action))

    def place_limit(self, side_leg, side, marketable,
                    comment='SCENARIO LMT', volume=None):
        """Rest a limit order.

        `marketable` puts it AGGRESSIVELY inside the spread — one tick
        from the far touch — which is the closest a BUY_LIMIT can get to
        filling immediately. MT5 rejects a buy limit at or above the ask
        (10015 Invalid Price), so a true marketable limit is not
        expressible; one tick inside is.

        This used to park a BUY at the BID and a SELL at the ASK, which
        are PASSIVE prices — the order only fills if the market comes
        all the way to you. That is why the "should fill" scenarios
        never filled in 15s (live 2026-08-06) and the only fills the
        suite saw were orders the market ran through during a cancel.

        Without `marketable` it sits ~1% away, which is what the cancel
        scenarios need.
        """
        action = self._base(side_leg, 'place', side)
        specs = side_leg.specs()
        if not specs.get('ok'):
            action['error'] = specs.get('error', f'{side_leg.symbol} '
                                                 f'unavailable')
            return self._record(action)
        tick = self._quote(side_leg, action)
        if not tick:
            action['error'] = 'no tick'
            return self._record(action)
        step = specs.get('tick_size') or specs.get('point') or 0.01
        if side == 'BUY':
            # Strictly below the ask, and never below the bid.
            price = (max(tick['ask'] - step, tick['bid']) if marketable
                     else tick['bid'] * 0.99)
        else:
            # Strictly above the bid, and never above the ask.
            price = (min(tick['bid'] + step, tick['ask']) if marketable
                     else tick['ask'] * 1.01)
        price = round(price / step) * step
        action['tgt'] = price
        volume = volume or self._volume(side_leg)
        placed = side_leg.leg.place_limit(side_leg.symbol, side, volume,
                                          price, comment=comment)
        if not placed.get('ok'):
            action['error'] = placed.get('error') or 'limit rejected'
            return self._record(action)
        action.update(ok=True, order=placed.get('ticket'), volume=volume)
        return self._record(action)

    def wait_for_fill(self, side_leg, place_action):
        """Poll the resting order. Returns the open action on a fill,
        None if it never filled."""
        deadline = self.clock() + self.fill_timeout
        state = {}
        while self.clock() < deadline:
            state = side_leg.leg.order_state(place_action['order']) or {}
            if state.get('filled_volume'):
                break
            if not state.get('still_open'):
                break
            self.sleep(0.3)
        if not state.get('filled_volume'):
            return None
        action = self._base(side_leg, 'open', place_action['side'])
        self._quote(side_leg, action)
        tickets = state.get('position_tickets') or []
        action.update(ok=True, tgt=place_action['tgt'],
                      fill=state.get('price') or place_action['tgt'],
                      volume=state['filled_volume'],
                      ticket=tickets[0] if tickets
                      else place_action['order'])
        return self._record(self._verify(side_leg, action))

    def cancel(self, side_leg, place_action, reason=None):
        """Cancel a resting order, and flatten it if it filled first.

        Takes the whole PLACE action rather than just its ticket,
        because a leaked fill has to be closed on the side it was
        OPENED — and that side only exists here. Passing the leg's role
        instead ('SPOT') reached OrderSide('SPOT'), which raised inside
        the leg runner, so the leaked position stayed open and turned
        into an orphan (live 2026-08-06, ticket 102279299).
        """
        order_ticket = place_action['order']
        action = {'ok': False, 'kind': 'cancel', 'order': order_ticket,
                  'leg_label': side_leg.label(''), 'time': self._stamp(),
                  'account': side_leg.account}
        if reason:
            action['reason'] = reason
        state = side_leg.leg.cancel_order(order_ticket) or {}
        # Deal history lags a cancel: a fill that leaked through is the
        # thing we most need to see here, so report it rather than a
        # clean-looking pass.
        if not state.get('filled_volume'):
            # Ask the terminal directly — a filled pending order becomes
            # a POSITION with the same ticket, and that shows up before
            # the deal does.
            found = side_leg.verify(order_ticket)
            if found and found.get('position_open'):
                state = dict(state, filled_volume=found.get('volume') or 0.0,
                             price=found.get('price'),
                             position_tickets=[order_ticket],
                             leaked_fill=True)

        if state.get('filled_volume'):
            action['error'] = (f"order filled {state['filled_volume']} "
                               f"before the cancel landed")
            action['leaked'] = state
            self._record(action)
            # Never leave it on the book: flatten what leaked through.
            tickets = state.get('position_tickets') or [order_ticket]
            cleanup = self.close(side_leg, {
                'ok': True, 'ticket': tickets[0],
                'volume': state['filled_volume'],
                'side': place_action['side'],
                'kind': 'open'}, kind='leak cleanup')
            # A leak is the MARKET moving through a resting price, not a
            # defect: the order was valid and the broker filled it. What
            # matters is whether we noticed and flattened it. Cleaned up
            # and confirmed by MT5 -> the safety machinery did its job,
            # so the scenario passes WITH the leak on the record. Cleanup
            # that failed is a real failure and stays one.
            if cleanup.get('ok') and cleanup.get('verified') is not False:
                action['ok'] = True
                action['leak_handled'] = True
            return action
        if state.get('cancelled') or not state.get('still_open'):
            action['ok'] = True
        else:
            action['error'] = state.get('error') or 'still open after cancel'
        return self._record(action)

    def close(self, side_leg, open_action, kind='close'):
        # Label the close with the direction actually SENT, not the one
        # the position was opened on. Logging a close of a short as
        # "FUTURES SELL ... fill=$4302.02" when 4302.02 is the ASK reads
        # as a second sell at a price only a BUY could pay — the log has
        # to say what happened. close_ticket still receives the ENTRY
        # side, which is what MT5 needs to reverse the position.
        entry_side = open_action['side']
        closing_side = 'SELL' if entry_side == 'BUY' else 'BUY'
        action = self._base(side_leg, kind, closing_side)
        action['entry_side'] = entry_side
        tick = self._quote(side_leg, action)
        if tick:
            action['tgt'] = (tick['bid'] if entry_side == 'BUY'
                             else tick['ask'])
        action['ticket'] = open_action.get('ticket')
        action['volume'] = open_action.get('volume')
        result = side_leg.leg.close_ticket(
            side_leg.symbol, open_action.get('ticket'),
            open_action.get('volume'), entry_side,
            comment='SCENARIO close')
        if not result.get('ok'):
            action['error'] = result.get('error') or 'close rejected'
            return self._record(action)
        action.update(ok=True, fill=result.get('price'))
        return self._record(self._verify(side_leg, action))

    # -- scenarios ------------------------------------------------------

    def run(self, s_type, mode, variant='normal'):
        single = {'BUY_SPOT': (self.spot, 'BUY'),
                  'SELL_SPOT': (self.spot, 'SELL'),
                  'BUY_FUT': (self.futures, 'BUY'),
                  'SELL_FUT': (self.futures, 'SELL')}
        self.actions = []
        started = self.clock()
        if s_type in single:
            side_leg, side = single[s_type]
            outcome = self._single(side_leg, side, mode, variant)
        elif s_type == 'LONG_SPR':
            outcome = self._spread('BUY', 'SELL', mode, variant)
        elif s_type == 'SHORT_SPR':
            outcome = self._spread('SELL', 'BUY', mode, variant)
        else:
            return {'success': False, 'detail': f'Unknown scenario: {s_type}',
                    'actions': []}
        outcome['duration_sec'] = self.clock() - started
        outcome['detail'] = self._assemble(outcome)
        outcome['actions'] = self.actions
        return outcome

    def _result(self, success, open_stats=None, close_stats=None,
                legs=()):
        """legs: (side_leg, side, open_action, close_action) tuples."""
        return {'success': (success
                            and all(a.get('ok', True) for a in self.actions)
                            and all(a.get('verified', True)
                                    for a in self.actions)),
                'open_stats': open_stats, 'close_stats': close_stats,
                'legs': list(legs)}

    def _single(self, side_leg, side, mode, variant):
        if mode == 'MARKET':
            open_stats = self.spread_stats()
            opened = self.open_market(side_leg, side)
            if not opened['ok']:
                return self._result(False, open_stats)
            if variant != 'quick_close':
                self._hold()
            closed = self.close(side_leg, opened)
            return self._result(closed['ok'], open_stats, self.spread_stats(),
                                [(side_leg, side, opened, closed)])

        if variant == 'cancel':
            placed = self.place_limit(side_leg, side, marketable=False,
                                      comment='SCENARIO LMT cancel')
            if not placed['ok']:
                return self._result(False)
            self.sleep(0.3)
            cancelled = self.cancel(side_leg, placed)
            return self._result(cancelled['ok'])

        open_stats = self.spread_stats()
        placed = self.place_limit(side_leg, side, marketable=True)
        if not placed['ok']:
            return self._result(False, open_stats)
        opened = self.wait_for_fill(side_leg, placed)
        if not opened:
            cancelled = self.cancel(side_leg, placed,
                                    reason=f'no fill in '
                                           f'{self.fill_timeout:.0f}s')
            return self._result(cancelled['ok'], open_stats)
        self._hold()
        closed = self.close(side_leg, opened)
        return self._result(closed['ok'], open_stats, self.spread_stats(),
                            [(side_leg, side, opened, closed)])

    def _spread(self, spot_side, fut_side, mode, variant):
        if variant in ('partial_spot', 'partial_futures'):
            return self._partial(spot_side, fut_side, variant)
        if mode == 'MARKET':
            return self._spread_market(spot_side, fut_side, variant)
        if variant == 'cancel':
            return self._spread_cancel(spot_side, fut_side)
        return self._spread_limit(spot_side, fut_side)

    def _partial(self, spot_side, fut_side, variant):
        """One leg fills, the other is assumed to have failed: the
        engine's recovery is to market-close the filled leg at once.
        This proves that recovery works before it is needed for real."""
        if variant == 'partial_spot':
            side_leg, side = self.spot, spot_side
        else:
            side_leg, side = self.futures, fut_side
        open_stats = self.spread_stats()
        opened = self.open_market(side_leg, side, comment='SCENARIO partial')
        if not opened['ok']:
            return self._result(False, open_stats)
        self._hold()
        closed = self.close(side_leg, opened, kind='recovery close')
        return self._result(closed['ok'], open_stats, self.spread_stats(),
                            [(side_leg, side, opened, closed)])

    def _spread_market(self, spot_side, fut_side, variant):
        open_stats = self.spread_stats()
        spot_lots, fut_lots = self.pair_volumes()
        spot_open = self.open_market(self.spot, spot_side,
                                     comment='SCENARIO MKT spr/spot',
                                     volume=spot_lots)
        if not spot_open['ok']:
            return self._result(False, open_stats)
        fut_open = self.open_market(self.futures, fut_side,
                                    comment='SCENARIO MKT spr/fut',
                                    volume=fut_lots)
        if not fut_open['ok']:
            # Never leave the filled leg naked because the hedge failed.
            self.close(self.spot, spot_open, kind='rollback close')
            return self._result(False, open_stats, self.spread_stats())
        if variant != 'quick_close':
            self._hold()
        spot_close = self.close(self.spot, spot_open)
        fut_close = self.close(self.futures, fut_open)
        return self._result(
            spot_close['ok'] and fut_close['ok'], open_stats,
            self.spread_stats(),
            [(self.spot, spot_side, spot_open, spot_close),
             (self.futures, fut_side, fut_open, fut_close)])

    def _spread_cancel(self, spot_side, fut_side):
        spot_lots, fut_lots = self.pair_volumes()
        spot_place = self.place_limit(self.spot, spot_side, marketable=False,
                                      comment='SCENARIO LMT spr/spot cancel',
                                      volume=spot_lots)
        if not spot_place['ok']:
            return self._result(False)
        fut_place = self.place_limit(self.futures, fut_side, marketable=False,
                                     comment='SCENARIO LMT spr/fut cancel',
                                     volume=fut_lots)
        if not fut_place['ok']:
            self.cancel(self.spot, spot_place, reason='rollback')
            return self._result(False)
        self.sleep(0.3)
        self.cancel(self.spot, spot_place)
        self.cancel(self.futures, fut_place)
        return self._result(True)

    def _spread_limit(self, spot_side, fut_side):
        open_stats = self.spread_stats()
        spot_lots, fut_lots = self.pair_volumes()
        spot_place = self.place_limit(self.spot, spot_side, marketable=True,
                                      comment='SCENARIO LMT spr/spot',
                                      volume=spot_lots)
        if not spot_place['ok']:
            return self._result(False, open_stats)
        fut_place = self.place_limit(self.futures, fut_side, marketable=True,
                                     comment='SCENARIO LMT spr/fut',
                                     volume=fut_lots)
        if not fut_place['ok']:
            self.cancel(self.spot, spot_place, reason='rollback')
            return self._result(False, open_stats)

        spot_open = self.wait_for_fill(self.spot, spot_place)
        fut_open = self.wait_for_fill(self.futures, fut_place)
        if not spot_open:
            self.cancel(self.spot, spot_place,
                        reason=f'no fill in {self.fill_timeout:.0f}s')
        if not fut_open:
            self.cancel(self.futures, fut_place,
                        reason=f'no fill in {self.fill_timeout:.0f}s')

        # Hold BEFORE closing either leg, so the pair is open together
        # for the whole time rather than one leg sitting naked while the
        # other waits its turn.
        if spot_open or fut_open:
            self._hold()

        legs = []
        both = True
        for side_leg, side, opened in ((self.spot, spot_side, spot_open),
                                       (self.futures, fut_side, fut_open)):
            if not opened:
                both = False
                continue
            closed = self.close(side_leg, opened)
            legs.append((side_leg, side, opened, closed))
        return self._result(True, open_stats,
                            self.spread_stats() if legs else None,
                            legs if both else [])

    # -- the report the operator reads ----------------------------------

    def _fmt_action(self, action):
        # The hold is not an order, so it has no leg, price or ticket —
        # just the reason the row sat there.
        if action.get('kind') == 'hold':
            return f'... {action.get("note", "holding")}'
        label = action.get('leg_label', '?').strip() or '?'
        account = action.get('account')
        head = f'[{label} @ {account}]' if account else f'[{label}]'
        kind = action.get('kind', '?')
        when = action.get('time', '')
        digits = action.get('digits', 2)
        if not action.get('ok', True):
            return (f'{head} {kind} FAILED @ {when} UTC: '
                    f'{action.get("error", "?")}')
        if action.get('leak_handled'):
            # The scenario passes, but a real position really existed.
            # It must stay on the report, not be smoothed into
            # "cancelled" by having been cleaned up successfully.
            return (f'{head} {kind} RACED a fill @ {when} UTC: '
                    f'{action.get("error", "?")} — flattened and '
                    f'confirmed')
        if kind == 'cancel':
            line = f'{head} cancelled @ {when} UTC'
            if action.get('order'):
                line += f' order {action["order"]}'
            if action.get('reason'):
                line += f' ({action["reason"]})'
            return line
        parts = [head, f'{kind} @ {when} UTC']
        for key in ('bid', 'ask', 'tgt', 'fill'):
            if action.get(key) is not None:
                parts.append(f'{key}={_fmt(action[key], digits)}')
        deltas = []
        if action.get('fill') is not None and action.get('tgt') is not None:
            deltas.append(f'Δtgt={action["fill"] - action["tgt"]:+.{digits}f}')
        if action.get('fill') is not None and action.get('mid') is not None:
            deltas.append(f'Δmid={action["fill"] - action["mid"]:+.{digits}f}')
        line = ' '.join(parts)
        if deltas:
            line += f' ({", ".join(deltas)})'
        if action.get('ticket'):
            line += f' ticket {action["ticket"]}'
        if 'verified' in action:
            line += '\n    ' + self._fmt_verification(action)
        return line

    @staticmethod
    def _fmt_verification(action):
        """The line that answers "did this actually reach MT5?" —
        read back from the terminal, not from our own return value."""
        found = action.get('verification') or {}
        if not action.get('verified'):
            return (f'✗ NOT FOUND IN MT5: {found.get("error", "unknown")} '
                    f'(ticket {action.get("ticket")})')
        facts = []
        deals = found.get('deals') or []
        if deals:
            last = deals[-1]
            facts.append(f'deal {last["deal_id"]} order {last["order_id"]}')
            facts.append(f'{last["volume"]} @ {last["price"]}')
            if last.get('commission'):
                facts.append(f'commission {last["commission"]}')
        elif found.get('volume') is not None:
            facts.append(f'{found["volume"]} @ {found.get("price")}')
        if found.get('position_open'):
            facts.append('position OPEN')
        head = f'✓ MT5 confirms via {found.get("source", "terminal")}'
        return f'{head}: {", ".join(facts)}' if facts else head

    def _fmt_spread(self, label, stats):
        if not stats:
            return None
        spread, mu, sigma, z = stats
        if spread is None:
            return None
        parts = [f'{spread:+.4f}']
        if mu is not None:
            parts.append(f'μ={mu:+.4f}')
        if sigma is not None:
            parts.append(f'σ={sigma:.4f}')
        if z is not None:
            parts.append(f'z={z:+.2f}')
        return f'{label}: ' + ' '.join(parts)

    def _assemble(self, outcome):
        lines = [self._fmt_action(a) for a in self.actions if a]
        for label, stats in (('spread@open', outcome.get('open_stats')),
                             ('spread@close', outcome.get('close_stats'))):
            line = self._fmt_spread(label, stats)
            if line:
                lines.append(line)

        fees, gross = [], None
        for side_leg, side, opened, closed in outcome.get('legs') or ():
            if not (opened and closed and closed.get('ok')):
                continue
            volume = opened.get('volume') or 0.0
            fees.append(side_leg.round_trip_fee(volume, opened.get('fill')))
            leg_pnl = side_leg.pnl(side, opened.get('fill'),
                                   closed.get('fill'), volume)
            if leg_pnl is not None:
                gross = (gross or 0.0) + leg_pnl
        if fees:
            total = sum(f for f, _ in fees)
            lines.append(
                'fees: ' + '+'.join(f'${f:.3f}' for f, _ in fees)
                + f'=${total:.3f} ('
                + '+'.join(f'{b:.1f}' for _, b in fees) + ' bps round trip)')
        if gross is not None:
            net = gross - sum(f for f, _ in fees)
            lines.append(f'gross=${gross:+.2f} net=${net:+.2f} '
                         f'({outcome.get("duration_sec", 0):.1f}s)')
        return '\n'.join(lines)
