"""Reconciliation & self-healing — engine state vs live broker state.

This is where accounts die: a crashed close, a leaked partial, a
manual intervention, and the engine's book no longer matches the
broker's. Every SYNC_INTERVAL:

- ORPHANS (broker has bot positions the engine doesn't know): after
  STRIKES consecutive sightings, close them by position ticket, book
  the cost to the untracked-close ledger, and charge the daily-loss
  breaker. Magic-scoped — non-bot positions are never touched.
- GHOSTS (engine holds a position the broker no longer has): after
  STRIKES consecutive misses, force-clear the engine position so it
  stops acting on a phantom.

Strike counters make one flaky snapshot harmless; only persistent
mismatches trigger action.
"""

import logging
import time as time_mod

from .models import OrderSide, PositionStatus


class Reconciler:
    def __init__(self, config, position_manager, data_logger,
                 risk_manager, leg_map, clock=time_mod.time):
        """leg_map: {'spot': leg, 'futures': leg} (may be the same leg)."""
        self.config = config
        self.position_manager = position_manager
        self.data_logger = data_logger
        self.risk_manager = risk_manager
        self.leg_map = leg_map
        self.clock = clock
        self.orphan_strikes = {}    # (leg_name, ticket) -> count
        self.ghost_strikes = {}     # position_id -> count
        self.close_failures = {}    # (leg_name, ticket) -> failed closes
        self.unclosable = {}        # (leg_name, ticket) -> last error
        self.last_sync = 0.0

    # ------------------------------------------------------------------

    def _expected_tickets(self):
        """{leg_role: {ticket: (position, trade)}} from active positions."""
        expected = {'spot': {}, 'futures': {}}
        for position in self.position_manager.get_active_positions().values():
            for role, trade in (('spot', position.spot_trade),
                                ('futures', position.futures_trade)):
                for ticket in trade.position_tickets:
                    expected[role][ticket] = (position, trade)
        return expected

    def due(self):
        return (self.clock() - self.last_sync
                >= self.config.RECONCILE['SYNC_INTERVAL_SEC'])

    def check(self):
        """One reconciliation pass. Returns list of actions taken."""
        self.last_sync = self.clock()
        strikes_needed = self.config.RECONCILE['STRIKES']
        actions = []

        legs = {}
        for leg in self.leg_map.values():
            legs[id(leg)] = leg

        snapshots = {}
        for leg_id, leg in legs.items():
            snapshots[leg_id] = leg.positions()
            if snapshots[leg_id] is None:
                logging.warning("Reconcile: no position snapshot from leg "
                                "'%s' — skipping its checks this pass",
                                leg.name)

        expected = self._expected_tickets()
        expected_by_leg = {leg_id: {} for leg_id in legs}
        for role, tickets in expected.items():
            expected_by_leg[id(self.leg_map[role])].update(tickets)

        # --- orphans: on broker, unknown to engine (per readable leg) ---
        for leg_id, leg in legs.items():
            snapshot = snapshots[leg_id]
            if snapshot is None:
                continue
            for pos_info in snapshot:
                ticket = pos_info['ticket']
                key = (leg.name, ticket)
                if ticket in expected_by_leg[leg_id]:
                    self.orphan_strikes.pop(key, None)
                    self.close_failures.pop(key, None)
                    self.unclosable.pop(key, None)
                    continue
                if key in self.unclosable:
                    continue        # already escalated; do not spam
                self.orphan_strikes[key] = self.orphan_strikes.get(key, 0) + 1
                logging.warning(
                    "Reconcile: orphan position on [%s]: ticket %s %s "
                    "%.2f %s (strike %d/%d)", leg.name, ticket,
                    pos_info['side'], pos_info['volume'], pos_info['symbol'],
                    self.orphan_strikes[key], strikes_needed)
                if self.orphan_strikes[key] >= strikes_needed:
                    self._close_orphan(leg, pos_info)
                    self.orphan_strikes.pop(key, None)
                    actions.append(('orphan_closed', leg.name, ticket))

        # --- ghosts: engine expects tickets the broker lacks. One strike
        # per position per PASS (not per ticket), and only when every
        # snapshot was readable — a flaky leg must not accuse anyone ---
        if all(s is not None for s in snapshots.values()):
            missing = {}
            for leg_id, leg in legs.items():
                live = {p['ticket'] for p in snapshots[leg_id]}
                for ticket, (position, _t) in expected_by_leg[leg_id].items():
                    if ticket not in live:
                        missing[position.position_id] = (position, leg.name,
                                                         ticket)
            for pid, (position, leg_name, ticket) in missing.items():
                self.ghost_strikes[pid] = self.ghost_strikes.get(pid, 0) + 1
                logging.warning(
                    "Reconcile: engine position %s expects ticket %s on "
                    "[%s] but broker is flat (strike %d/%d)", pid, ticket,
                    leg_name, self.ghost_strikes[pid], strikes_needed)
                if self.ghost_strikes[pid] >= strikes_needed:
                    self._force_clear(position)
                    self.ghost_strikes.pop(pid, None)
                    actions.append(('ghost_cleared', leg_name, pid))
            for position in \
                    self.position_manager.get_active_positions().values():
                if position.position_id not in missing:
                    self.ghost_strikes.pop(position.position_id, None)

        return actions

    # ------------------------------------------------------------------

    def _close_orphan(self, leg, pos_info):
        """Close a bot-tagged position the engine doesn't know, by
        ticket — never hand-compute sizes, use the broker's own volume."""
        result = leg.close_ticket(
            pos_info['symbol'], pos_info['ticket'], pos_info['volume'],
            pos_info['side'],
            slippage_points=self.config.EXECUTION['SLIPPAGE_TOLERANCE'],
            comment="BASIS_ARB_ORPHAN")
        if result.get('ok'):
            price = result.get('price')
            entry = pos_info.get('price_open') or 0.0
            side = OrderSide(pos_info['side'])
            cost = 0.0
            if price and entry:
                signed = 1.0 if side is OrderSide.BUY else -1.0
                cost = signed * (price - entry) * pos_info['volume']
            self.data_logger.log_untracked_close(
                leg.name, pos_info['symbol'], pos_info['ticket'],
                pos_info['volume'], price,
                f"orphan auto-close, est P&L ${cost:.2f}")
            self.risk_manager.on_position_closed(cost)
            logging.critical(
                "Reconcile: ORPHAN CLOSED on [%s]: %s ticket %s %.2f lots "
                "— booked to untracked ledger (est $%.2f)", leg.name,
                pos_info['symbol'], pos_info['ticket'],
                pos_info['volume'], cost)
        else:
            # Failed close books NOTHING — it strikes again next pass.
            # After enough failures the retry loop is just noise hiding
            # a position that needs a human: say so once, loudly, and
            # stop re-trying that ticket.
            key = (leg.name, pos_info['ticket'])
            self.close_failures[key] = self.close_failures.get(key, 0) + 1
            attempts = self.close_failures[key]
            logging.critical(
                "Reconcile: orphan close FAILED on [%s] ticket %s "
                "(attempt %d): %s", leg.name, pos_info['ticket'], attempts,
                result.get('error'))
            if attempts >= self.config.RECONCILE.get('CLOSE_ATTEMPTS', 3):
                self.unclosable[key] = result.get('error')
                logging.critical(
                    "Reconcile: GIVING UP on [%s] ticket %s %s %.2f lots "
                    "after %d attempts — CLOSE IT BY HAND in the terminal. "
                    "Last error: %s", leg.name, pos_info['ticket'],
                    pos_info['symbol'], pos_info['volume'], attempts,
                    result.get('error'))

    def _force_clear(self, position):
        position.status = PositionStatus.CLOSED
        position.close_reason = "RECONCILE_FORCE_CLEAR"
        self.data_logger.log_position(position)
        self.data_logger.clear_position_state(position.position_id)
        logging.critical(
            "Reconcile: FORCE-CLEARED engine position %s — broker shows "
            "flat; verify the account statement", position.position_id)
