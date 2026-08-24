"""Cross-account pair execution for large clips.

Execution ladder per child order (limit-first — a limit fill saves
paying the spread, worth ~hundreds of dollars per 10-lot child):
1. Rest a limit at the peg (buy at bid / sell at ask, RETURN filling
   so partials keep working).
2. Re-peg via order-MODIFY when the market drifts — one round trip,
   no cancel/replace window.
3. On timeout: cancel and ALWAYS re-read fills (a "cancelled" order
   can carry partials), then cross the remainder at market
   (ON_TIMEOUT='cross') or give up on the remainder ('abort').
Stops and unwinds never rest — straight to market.

Hedge policy (unchanged from the market-only version):
- futures hedge is sized to the actual spot FILL;
- hedge fills nothing -> unwind all spot;
- hedge partial -> unwind unmatched spot excess, keep matched size —
  but only if matched >= MIN_MATCHED_FRACTION of the intended clip
  (a runt position pays full costs for a fraction of the edge);
- failed unwind logs CRITICAL (unhedged exposure).

Hedging-mode accounts: fills record their MT5 position tickets and
closes target those tickets (a plain opposite order would OPEN a
second position instead of closing).
"""

import logging
import math
import time as time_mod
import uuid

from .models import OrderSide, SignalType, Trade
from . import sizing
from . import slippage

EPS = 1e-9
URGENT_REASONS = {'STOP_LOSS', 'DOLLAR_STOP', 'Z_STOP', 'SYSTEM_SHUTDOWN',
                  'MANUAL_CLOSE'}


class PairExecutor:
    def __init__(self, config, spot_leg, futures_leg,
                 clock=time_mod.time, sleep=time_mod.sleep):
        self.config = config
        self.spot_leg = spot_leg
        self.futures_leg = futures_leg
        self.clock = clock
        self.sleep = sleep
        self._meta_cache = {}

    # ------------------------------------------------------------------
    # Volume helpers
    # ------------------------------------------------------------------

    def _meta(self, leg, symbol):
        key = (leg.name, symbol)
        if key not in self._meta_cache:
            meta = leg.ensure_symbol(symbol)
            if not meta or not meta.get('ok'):
                meta = {'ok': False, 'volume_min': 0.01,
                        'volume_max': 1000.0, 'volume_step': 0.01,
                        'point': 0.01}
            self._meta_cache[key] = meta
        return self._meta_cache[key]

    @staticmethod
    def _round_step(volume, step):
        if step <= 0:
            return volume
        return math.floor(volume / step + EPS) * step

    # ------------------------------------------------------------------
    # Child-order execution (one slice)
    # ------------------------------------------------------------------

    def _peg_price(self, leg, symbol, side, meta):
        """Peg price from a FRESH tick, rounded to tick size and kept
        strictly inside the book — brokers reject a BUY_LIMIT at/above
        the ask with Invalid Price 10015 (live-tested 2026-06)."""
        tick = leg.tick(symbol)
        if not tick:
            return None
        offset = self.config.EXECUTION.get('PEG_OFFSET_POINTS', 0.0) \
            * meta.get('point', 0.01)
        if side is OrderSide.BUY:
            price = tick['bid'] + offset      # passive side for a buyer
            if price >= tick['ask']:
                price = tick['bid']           # never cross into the ask
        else:
            price = tick['ask'] - offset      # passive side for a seller
            if price <= tick['bid']:
                price = tick['ask']
        tick_size = meta.get('tick_size') or meta.get('point', 0.01) or 0.01
        return round(round(price / tick_size) * tick_size, 10)

    def _market_child(self, leg, symbol, side, volume, comment):
        result = leg.order(
            symbol, side.value, volume,
            slippage_points=self.config.EXECUTION['SLIPPAGE_TOLERANCE'],
            comment=comment)
        return {
            'filled': float(result.get('filled_volume') or 0.0),
            'price': result.get('price'),
            'tickets': list(result.get('position_tickets') or []),
            'ok': bool(result.get('ok')),
            'error': result.get('error'),
        }

    def _limit_child(self, leg, symbol, side, volume, comment, timeout,
                     position_ticket=None, escalate=None):
        """Rest at peg, re-peg via modify, escalate on timeout.

        position_ticket makes the limit a CLOSING order for that
        position (hedging mode). escalate overrides the timeout
        fallback (default: cross at market)."""
        execution = self.config.EXECUTION
        meta = self._meta(leg, symbol)
        poll = execution.get('ORDER_POLL_SEC', 0.5)
        repeg_every = execution.get('REPEG_INTERVAL_SEC', 2.0)
        point = meta.get('point', 0.01)

        price = self._peg_price(leg, symbol, side, meta)
        if price is None:
            return self._market_child(leg, symbol, side, volume, comment)

        placed = leg.place_limit(symbol, side.value, volume, price,
                                 comment=comment,
                                 position_ticket=position_ticket)
        if not placed.get('ok'):
            logging.warning("[%s] limit rejected (%s) — falling back to "
                            "market", leg.name, placed.get('error'))
            return self._market_child(leg, symbol, side, volume, comment)

        ticket = placed['ticket']
        deadline = self.clock() + timeout
        last_repeg = self.clock()

        while self.clock() < deadline:
            self.sleep(poll)
            state = leg.order_state(ticket)
            filled = float(state.get('filled_volume') or 0.0)

            if filled >= volume - EPS or not state.get('still_open', True):
                return {'filled': filled, 'price': state.get('price'),
                        'tickets': list(state.get('position_tickets') or []),
                        'ok': filled > EPS, 'error': state.get('error')}

            if self.clock() - last_repeg >= repeg_every:
                new_price = self._peg_price(leg, symbol, side, meta)
                if new_price is not None and abs(new_price - price) > point / 2:
                    modified = leg.modify_order(ticket, new_price)
                    if modified.get('ok'):
                        price = new_price
                    # modify can fail because the order just filled —
                    # the next order_state poll picks that up
                last_repeg = self.clock()

        # Timeout: cancel, then trust only the re-read fill state
        cancelled = leg.cancel_order(ticket)
        filled = float(cancelled.get('filled_volume') or 0.0)
        vwap = cancelled.get('price')
        tickets = list(cancelled.get('position_tickets') or [])
        remaining = volume - filled

        if remaining > EPS and execution.get('ON_TIMEOUT', 'cross') == 'cross':
            if escalate is None:
                crossed = self._market_child(leg, symbol, side, remaining,
                                             comment)
            else:
                crossed = escalate(remaining)
            got = crossed['filled']
            if got > EPS:
                total = filled + got
                notional = ((vwap or 0.0) * filled
                            + (crossed['price'] or 0.0) * got)
                vwap = notional / total
                filled = total
                tickets.extend(crossed['tickets'])

        return {'filled': filled, 'price': vwap, 'tickets': tickets,
                'ok': filled > EPS,
                'error': None if filled > EPS else 'no fill before timeout'}

    # ------------------------------------------------------------------
    # Stale-order sweep
    # ------------------------------------------------------------------

    def sweep_stale_orders(self, targets):
        """Cancel ALL of our leftover pending orders on these symbols
        before a new execution. Orphan pendings accumulate after
        timeouts and failed cancels; left alone they eventually fill
        and become untracked naked positions (live-tested 2026-06).

        targets: iterable of (leg, symbol)."""
        seen = set()
        for leg, symbol in targets:
            key = (leg.name, symbol)
            if key in seen:
                continue
            seen.add(key)
            orders = leg.pending_orders(symbol)
            if not orders:
                continue
            logging.warning("[%s] %d stale pending order(s) on %s — "
                            "cancelling before new execution",
                            leg.name, len(orders), symbol)
            for order in orders:
                state = leg.cancel_order(order['ticket'])
                leaked = float(state.get('filled_volume') or 0.0)
                if leaked > EPS:
                    logging.critical(
                        "[%s] stale order %s on %s had FILLED %.2f lots — "
                        "reconciler will pick the position up as an orphan",
                        leg.name, order['ticket'], symbol, leaked)

    # ------------------------------------------------------------------
    # Sliced sending
    # ------------------------------------------------------------------

    def _send_sliced(self, leg, symbol, side, total_lots, comment,
                     style='market', timeout=None):
        meta = self._meta(leg, symbol)
        step = meta.get('volume_step') or 0.01
        vmax = meta.get('volume_max') or total_lots

        slice_lots = self.config.TRADING.get('SLICE_LOTS') or total_lots
        slice_lots = min(slice_lots, vmax)
        timeout = timeout or self.config.EXECUTION.get('LIMIT_TIMEOUT_SEC', 15)

        remaining = total_lots
        filled = 0.0
        notional = 0.0
        tickets = []

        while remaining > EPS:
            volume = self._round_step(min(slice_lots, remaining), step)
            if volume <= 0:
                break

            if style == 'limit':
                result = self._limit_child(leg, symbol, side, volume,
                                           comment, timeout)
            else:
                result = self._market_child(leg, symbol, side, volume,
                                            comment)

            got = result['filled']
            if got > EPS:
                filled += got
                notional += got * float(result['price'] or 0.0)
                tickets.extend(result['tickets'])

            if not result['ok']:
                logging.warning("[%s] %s %s %.2f lots failed: %s",
                                leg.name, side.value, symbol, volume,
                                result.get('error'))
                break
            if got < volume - EPS:
                logging.warning(
                    "[%s] %s %s partial fill %.2f/%.2f — stopping slices, "
                    "not chasing liquidity", leg.name, side.value, symbol,
                    got, volume)
                break

            remaining -= got

        vwap = notional / filled if filled > EPS else None
        return filled, vwap, tickets

    def _unwind(self, leg, symbol, entry_side, lots, comment, tickets=None):
        """Reverse an entry fill AT MARKET; CRITICAL on failure.

        Closes by TICKET, because these accounts are HEDGING mode. A
        plain opposite order there does not close anything — it OPENS a
        second, offsetting position, and the book then holds two rows
        that net to nothing. Live 2026-08-24, a manual gold pair whose
        futures leg was refused (10027, algo trading off in that
        terminal):

            Futures hedge filled nothing — unwinding 0.05 spot lots
            [Account_Spot] unwound 0.05 lots of XAUUSD
            Reconcile: orphan ticket 862 BUY  0.05 XAUUSD (strike 1/3)
            Reconcile: orphan ticket 863 SELL 0.05 XAUUSD (strike 1/3)

        862 was the entry and 863 was the "unwind". Economically flat,
        so the exposure was contained, but the engine believed it had
        reversed a position it had in fact doubled into, and the two
        rows sat live for the 60s the reconciler's three strikes take
        before it closed them — at the cost of two more round trips.
        This is the exact failure CLAUDE.md's hedging-mode rule names.

        The opposite-market order survives as a FALLBACK for whatever
        the ticket route cannot reach: a netting account (where it is
        the correct instrument), or a ticket the broker will not close.
        Offsetting is worse than closing, and far better than staying
        naked — but it is now the exception and it says so in the log.
        """
        if lots <= EPS:
            return True
        remaining = lots

        for ticket, volume in self._closable(leg, symbol, tickets):
            if remaining <= EPS:
                break
            volume = min(volume, remaining)
            result = leg.close_ticket(
                symbol, ticket, volume, entry_side.value,
                slippage_points=self.config.EXECUTION['SLIPPAGE_TOLERANCE'],
                comment=comment)
            if result.get('ok'):
                remaining -= float(result.get('filled_volume') or volume)
            else:
                logging.error("[%s] unwind close of ticket %s failed: %s",
                              leg.name, ticket, result.get('error'))

        if remaining > EPS:
            if tickets:
                logging.warning(
                    "[%s] %.2f lots of %s could not be unwound by ticket — "
                    "sending an opposite market order. On a hedging account "
                    "that OFFSETS rather than closes and leaves a second "
                    "position for the reconciler.",
                    leg.name, remaining, symbol)
            filled, _, _ = self._send_sliced(
                leg, symbol, entry_side.opposite, remaining, comment,
                style='market')
            remaining -= filled

        if remaining > EPS:
            logging.critical(
                "UNHEDGED EXPOSURE on [%s]: tried to unwind %.2f lots of "
                "%s, %.2f still open — MANUAL INTERVENTION REQUIRED",
                leg.name, lots, symbol, remaining)
            return False
        logging.info("[%s] unwound %.2f lots of %s", leg.name, lots, symbol)
        return True

    def _closable(self, leg, symbol, tickets):
        """(ticket, live volume) for the entry tickets still open.

        The BROKER's volume, not the one we sent: a ticket can have been
        partly closed already, and closing more than is there fails the
        whole request. A ticket it no longer lists is already gone and
        is skipped rather than treated as an error."""
        if not tickets:
            return []
        try:
            live = {p.get('ticket'): float(p.get('volume') or 0.0)
                    for p in (leg.positions(symbol) or [])}
        except Exception as exc:                       # broker/IPC down
            logging.error("[%s] cannot read positions to unwind by ticket "
                          "(%s) — falling back to an opposite order",
                          leg.name, exc)
            return []
        return [(t, live[t]) for t in tickets
                if live.get(t, 0.0) > EPS]

    # ------------------------------------------------------------------
    # Atomic pre-checks
    # ------------------------------------------------------------------

    def _contract_size(self, asset_key, role):
        """Units per lot for one leg.

        Read from the terminal at startup (_adopt_broker_specs) into
        the asset config. `lot_size` is leg A's; leg B carries its own
        when the two differ, and falls back to leg A's when it does
        not — which is the common case and the only one this engine has
        been run in."""
        asset = self.config.ASSETS.get(asset_key) or {}
        if role == 'futures':
            return float(asset.get('fut_lot_size')
                         or asset.get('lot_size') or 1.0)
        return float(asset.get('lot_size') or 1.0)

    def _precheck_pair(self, asset_key, lot_size, spot_symbol,
                       futures_symbol):
        """Validate both legs up front. Returns an error string or None."""
        spot_meta = self._meta(self.spot_leg, spot_symbol)
        fut_meta = self._meta(self.futures_leg, futures_symbol)
        if not spot_meta.get('ok'):
            return f"spot symbol {spot_symbol} unavailable on " \
                   f"[{self.spot_leg.name}]"
        if not fut_meta.get('ok'):
            return f"futures symbol {futures_symbol} unavailable on " \
                   f"[{self.futures_leg.name}]"

        hedge_ratio = self.config.TRADING.get('HEDGE_RATIO', 1.0)
        slice_lots = self.config.TRADING.get('SLICE_LOTS') or lot_size
        spot_child = min(slice_lots, lot_size)
        fut_child = sizing.hedge_lots(
            spot_child, self._contract_size(asset_key, 'spot'),
            self._contract_size(asset_key, 'futures'), hedge_ratio)

        if spot_child < spot_meta.get('volume_min', 0) - EPS:
            return (f"spot child order {spot_child:.2f} below minimum "
                    f"{spot_meta['volume_min']:.2f} on {spot_symbol}")
        if fut_child < fut_meta.get('volume_min', 0) - EPS:
            return (f"futures child order {fut_child:.2f} below minimum "
                    f"{fut_meta['volume_min']:.2f} on {futures_symbol}")
        return None

    # ------------------------------------------------------------------
    # Pair entry (PositionManager-compatible interface)
    # ------------------------------------------------------------------

    def _slippage(self, asset_key, signal_type, closing, spot_side,
                  futures_side, reference, spot_trade, futures_trade):
        """Decision-to-fill account for the pair. `reference` is the
        market_data snapshot the DECISION was made on — not a fresh
        tick, which would measure only the last few milliseconds and
        miss the poll interval entirely."""
        contract = float((self.config.ASSETS.get(asset_key) or {})
                         .get('lot_size', 0.0) or 0.0)
        report = slippage.build(
            signal_type, closing,
            self.config.TRADING.get('HEDGE_RATIO', 1.0),
            (spot_trade.lot_size or 0.0) * contract,
            spot_side, futures_side, reference,
            spot_trade.executed_price, futures_trade.executed_price,
            spot_trade.symbol, futures_trade.symbol)
        if report:
            logging.info("[SLIPPAGE] %s %s: %s",
                         'exit' if closing else 'entry',
                         signal_type.value, slippage.summarise(report))
        # The quoted touch is 'what we wanted', which is exactly what
        # the trades table's requested_price column is for; it has been
        # written as NULL since the pair executor was built.
        legs = (report or {}).get('legs') or {}
        spot_trade.requested_price = (legs.get('spot') or {}).get('quote')
        futures_trade.requested_price = (legs.get('futures') or {}).get('quote')
        return report

    def execute_trade_pair(self, asset, signal_type, lot_size,
                           spot_symbol, futures_symbol, tag='BASIS_ARB',
                           reference=None):
        if signal_type == SignalType.SELL_BASIS:
            spot_side, futures_side = OrderSide.BUY, OrderSide.SELL
        elif signal_type == SignalType.BUY_BASIS:
            spot_side, futures_side = OrderSide.SELL, OrderSide.BUY
        else:
            raise ValueError(f"Invalid signal type for opening: {signal_type}")

        execution = self.config.EXECUTION
        entry_style = execution.get('ENTRY_STYLE', 'market')
        # The comment travels to MT5 and comes back in the
        # order log, so the operator can tell an algo entry
        # from a manual one in the terminal itself.
        comment = f"{tag}_{uuid.uuid4().hex[:8]}"
        spot_trade = Trade(spot_symbol, spot_side, 0.0)
        futures_trade = Trade(futures_symbol, futures_side, 0.0)
        # Units per lot, from the broker, recorded on the trade. Volume
        # in lots cannot be added across two instruments; volume in
        # money can, and this is the only place both figures are known.
        spot_trade.contract_size = self._contract_size(asset, 'spot')
        futures_trade.contract_size = self._contract_size(asset,
                                                          'futures')

        self.sweep_stale_orders([(self.spot_leg, spot_symbol),
                                 (self.futures_leg, futures_symbol)])

        # Atomic pre-checks: BOTH legs validated before placing EITHER
        # order — a leg that fails minimums after the other filled is
        # an instant naked position
        precheck_error = self._precheck_pair(asset, lot_size, spot_symbol,
                                             futures_symbol)
        if precheck_error:
            logging.error("Entry refused (pre-check): %s", precheck_error)
            spot_trade.status = futures_trade.status = "ERROR"
            spot_trade.error_message = precheck_error
            return False, spot_trade, futures_trade

        # Leg 1: spot clip — patient (no position at risk while resting)
        spot_filled, spot_vwap, spot_tickets = self._send_sliced(
            self.spot_leg, spot_symbol, spot_side, lot_size, comment,
            style=entry_style,
            timeout=execution.get('LIMIT_TIMEOUT_SEC', 15))

        if spot_filled <= EPS:
            spot_trade.status = futures_trade.status = "ERROR"
            spot_trade.error_message = "Spot leg filled nothing"
            return False, spot_trade, futures_trade

        # Leg 2: futures hedge — short patience, every second unhedged
        # is naked exposure; timeout crosses the spread
        hedge_ratio = self.config.TRADING.get('HEDGE_RATIO', 1.0)
        fut_meta = self._meta(self.futures_leg, futures_symbol)
        fut_step = fut_meta.get('volume_step') or 0.01
        # Sized off the actual spot FILL, and off both CONTRACT SIZES —
        # a hedge of `spot_lots x beta` is only right when beta is 1 and
        # the two contracts are equal. See statarb/sizing.py.
        hedge_target = sizing.hedge_lots(
            spot_filled, self._contract_size(asset, 'spot'),
            self._contract_size(asset, 'futures'), hedge_ratio, fut_step)

        fut_filled, fut_vwap, fut_tickets = self._send_sliced(
            self.futures_leg, futures_symbol, futures_side, hedge_target,
            comment, style=entry_style,
            timeout=execution.get('HEDGE_TIMEOUT_SEC', 4))

        if fut_filled <= EPS:
            logging.error("Futures hedge filled nothing — unwinding %.2f "
                          "spot lots", spot_filled)
            self._unwind(self.spot_leg, spot_symbol, spot_side,
                         spot_filled, comment, tickets=spot_tickets)
            spot_trade.status = futures_trade.status = "ERROR"
            futures_trade.error_message = "Futures hedge filled nothing"
            return False, spot_trade, futures_trade

        if fut_filled < hedge_target - EPS:
            spot_step = self._meta(self.spot_leg,
                                   spot_symbol).get('volume_step') or 0.01
            # The inverse of hedge_lots: how much spot this partial
            # futures fill actually covers. Must use the same contract
            # sizes, or a partial fill unwinds the wrong amount.
            matched_spot = sizing.hedge_lots(
                fut_filled, self._contract_size(asset, 'futures'),
                self._contract_size(asset, 'spot'), 1.0 / hedge_ratio
                if hedge_ratio else 1.0, spot_step)
            min_fraction = execution.get('MIN_MATCHED_FRACTION', 0.0)

            if matched_spot < lot_size * min_fraction - EPS:
                # Matched piece too small to be worth its costs —
                # unwind EVERYTHING on both legs and fail the entry
                logging.warning(
                    "Matched size %.2f < %.0f%% of %.2f clip — unwinding "
                    "both legs", matched_spot, min_fraction * 100, lot_size)
                self._unwind(self.spot_leg, spot_symbol, spot_side,
                             spot_filled, comment, tickets=spot_tickets)
                self._unwind(self.futures_leg, futures_symbol, futures_side,
                             fut_filled, comment, tickets=fut_tickets)
                spot_trade.status = futures_trade.status = "ERROR"
                futures_trade.error_message = (
                    f"Matched {matched_spot:.2f} below "
                    f"{min_fraction:.0%} of clip")
                return False, spot_trade, futures_trade

            excess = spot_filled - matched_spot
            logging.warning(
                "Hedge partial: futures %.2f/%.2f — unwinding %.2f excess "
                "spot lots, keeping matched position",
                fut_filled, hedge_target, excess)
            self._unwind(self.spot_leg, spot_symbol, spot_side, excess,
                         comment, tickets=list(reversed(spot_tickets)))
            spot_filled = matched_spot

        spot_trade.lot_size = spot_filled
        spot_trade.executed_price = spot_vwap
        spot_trade.order_ticket = spot_tickets[0] if spot_tickets else None
        spot_trade.position_tickets = spot_tickets
        spot_trade.status = "EXECUTED"

        futures_trade.lot_size = fut_filled
        futures_trade.executed_price = fut_vwap
        futures_trade.order_ticket = fut_tickets[0] if fut_tickets else None
        futures_trade.position_tickets = fut_tickets
        futures_trade.status = "EXECUTED"

        logging.info("Pair executed: %s %s — spot %.2f @ %.2f [%s], "
                     "futures %.2f @ %.2f [%s]",
                     asset, signal_type.value, spot_filled, spot_vwap or 0,
                     self.spot_leg.name, fut_filled, fut_vwap or 0,
                     self.futures_leg.name)
        spot_trade.slippage = self._slippage(
            asset, signal_type, False, spot_side, futures_side,
            reference, spot_trade, futures_trade)
        return True, spot_trade, futures_trade

    # ------------------------------------------------------------------
    # Pair close
    # ------------------------------------------------------------------

    def _market_close_ticket(self, leg, trade, ticket, volume, comment):
        result = leg.close_ticket(
            trade.symbol, ticket, volume, trade.side.value,
            slippage_points=self.config.EXECUTION['SLIPPAGE_TOLERANCE'],
            comment=comment)
        if not result.get('ok'):
            logging.error("Close ticket %s on [%s] failed: %s",
                          ticket, leg.name, result.get('error'))
            # A retcode alone does not say WHY. Ask MT5 what it has for
            # this ticket: still open (and at what volume), or already
            # gone. Live 2026-08-07 two closes came back "10013 Invalid
            # request" with no way to tell whether the position had
            # vanished underneath us or the request was malformed —
            # and the same code path had closed a scenario position
            # successfully seconds earlier.
            self._explain_close_failure(leg, trade, ticket, volume)
            return {'filled': 0.0, 'price': None, 'tickets': [],
                    'ok': False, 'error': result.get('error')}
        got = float(result.get('filled_volume') or volume)
        return {'filled': got, 'price': result.get('price'), 'tickets': [],
                'ok': True, 'error': None}

    def _explain_close_failure(self, leg, trade, ticket, volume):
        """Log the broker's own view of a ticket we failed to close."""
        verify = getattr(leg, 'verify_order', None)
        if callable(verify):
            try:
                record = verify(ticket) or {}
            except Exception as e:
                record = {'error': str(e)}
            if record.get('position_open'):
                logging.error(
                    "  MT5 still holds ticket %s: %s lots of %s — the "
                    "close request was rejected, not the position",
                    ticket, record.get('volume'), trade.symbol)
            elif record.get('found'):
                logging.error(
                    "  MT5 has no OPEN position for ticket %s (it shows "
                    "%s) — it may already be closed; reconciliation will "
                    "settle the engine's view",
                    ticket, record.get('state') or 'history only')
            else:
                logging.error(
                    "  MT5 has no record of ticket %s at all — the "
                    "ticket the engine stored is not one the broker "
                    "recognises, so the close could never work", ticket)
        try:
            live = leg.positions(trade.symbol) or []
        except Exception:
            return
        logging.error("  [%s] currently holds %d %s position(s): %s",
                      leg.name, len(live), trade.symbol,
                      ', '.join(f"#{p.get('ticket')} {p.get('volume')}"
                                for p in live) or 'none')

    def _close_leg(self, leg, trade, comment, urgent):
        """Close one leg. Tickets recorded -> close each by ticket
        (hedging-mode correct); none recorded -> opposite market order
        (netting fallback).

        Non-urgent ticket closes go limit-first: a pending limit with
        position=ticket closes the position when it executes, saving
        the spread on every exit; timeout escalates to a market close
        of the remainder. Urgent closes (stops) never rest."""
        if trade.position_tickets:
            style = 'market' if urgent else \
                self.config.EXECUTION.get('ENTRY_STYLE', 'market')
            timeout = self.config.EXECUTION.get('EXIT_TIMEOUT_SEC', 15)
            close_side = trade.side.opposite
            filled = 0.0
            notional = 0.0
            per_ticket = trade.lot_size / len(trade.position_tickets)

            for ticket in trade.position_tickets:
                if style == 'limit':
                    result = self._limit_child(
                        leg, trade.symbol, close_side, per_ticket, comment,
                        timeout, position_ticket=ticket,
                        escalate=lambda remaining, t=ticket:
                            self._market_close_ticket(leg, trade, t,
                                                      remaining, comment))
                else:
                    result = self._market_close_ticket(leg, trade, ticket,
                                                       per_ticket, comment)
                got = result['filled']
                if got > EPS:
                    filled += got
                    notional += got * float(result['price'] or 0.0)
            vwap = notional / filled if filled > EPS else None
            return filled, vwap

        style = 'market' if urgent else \
            self.config.EXECUTION.get('ENTRY_STYLE', 'market')
        filled, vwap, _ = self._send_sliced(
            leg, trade.symbol, trade.side.opposite, trade.lot_size,
            comment, style=style,
            timeout=self.config.EXECUTION.get('EXIT_TIMEOUT_SEC', 15))
        return filled, vwap

    def execute_close_pair(self, position, reason=None, reference=None):
        urgent = (reason or '').upper() in URGENT_REASONS
        comment = f"BASIS_ARB_CX_{uuid.uuid4().hex[:6]}"

        close_spot = Trade(position.spot_trade.symbol,
                           position.spot_trade.side.opposite,
                           position.spot_trade.lot_size)
        close_futures = Trade(position.futures_trade.symbol,
                              position.futures_trade.side.opposite,
                              position.futures_trade.lot_size)
        # The closing leg trades the same instrument as the opening one.
        close_spot.contract_size = position.spot_trade.contract_size
        close_futures.contract_size = position.futures_trade.contract_size

        spot_filled, spot_vwap = self._close_leg(
            self.spot_leg, position.spot_trade, comment, urgent)
        fut_filled, fut_vwap = self._close_leg(
            self.futures_leg, position.futures_trade, comment, urgent)

        spot_ok = spot_filled >= close_spot.lot_size - EPS
        fut_ok = fut_filled >= close_futures.lot_size - EPS

        close_spot.executed_price = spot_vwap
        close_spot.status = "EXECUTED" if spot_ok else "ERROR"
        close_futures.executed_price = fut_vwap
        close_futures.status = "EXECUTED" if fut_ok else "ERROR"

        if not (spot_ok and fut_ok):
            logging.critical(
                "INCOMPLETE CLOSE for %s: spot %.2f/%.2f, futures %.2f/%.2f "
                "— residual exposure, MANUAL INTERVENTION REQUIRED",
                position.position_id, spot_filled, close_spot.lot_size,
                fut_filled, close_futures.lot_size)
            return False, close_spot, close_futures

        close_spot.slippage = self._slippage(
            position.asset, position.signal_type, True,
            close_spot.side, close_futures.side, reference,
            close_spot, close_futures)
        return True, close_spot, close_futures
