"""Cross-account pair execution for large clips.

Executes each basis trade as: spot clip on the spot leg's account,
then a futures hedge on the futures leg's account sized to what the
spot actually FILLED (IOC orders can partially fill at 50-lot size).

Policies:
- Clips are sliced into child orders (TRADING.SLICE_LOTS) so a single
  IOC sweep doesn't punch through the book.
- If a slice partially fills, slicing stops — don't chase liquidity.
- If the futures hedge fills nothing, the spot fill is fully unwound.
- If the hedge partially fills, the unmatched spot excess is unwound
  and the position is kept at the matched size.
- Any unwind failure logs CRITICAL — that is unhedged exposure and
  needs manual intervention.

Presents the same interface PositionManager expects
(execute_trade_pair / execute_close_pair), so position lifecycle code
is shared with the single-account path.
"""

import logging
import math
import uuid

from .models import OrderSide, SignalType, Trade

EPS = 1e-9


class PairExecutor:
    def __init__(self, config, spot_leg, futures_leg):
        self.config = config
        self.spot_leg = spot_leg
        self.futures_leg = futures_leg
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
                        'volume_max': 1000.0, 'volume_step': 0.01}
            self._meta_cache[key] = meta
        return self._meta_cache[key]

    @staticmethod
    def _round_step(volume, step):
        if step <= 0:
            return volume
        return math.floor(volume / step + EPS) * step

    # ------------------------------------------------------------------
    # Sliced sending
    # ------------------------------------------------------------------

    def _send_sliced(self, leg, symbol, side, total_lots, comment):
        """Send total_lots as child orders; returns (filled, vwap, tickets)."""
        meta = self._meta(leg, symbol)
        step = meta.get('volume_step') or 0.01
        vmax = meta.get('volume_max') or total_lots

        slice_lots = self.config.TRADING.get('SLICE_LOTS') or total_lots
        slice_lots = min(slice_lots, vmax)
        slippage = self.config.EXECUTION['SLIPPAGE_TOLERANCE']

        remaining = total_lots
        filled = 0.0
        notional = 0.0
        tickets = []

        while remaining > EPS:
            volume = self._round_step(min(slice_lots, remaining), step)
            if volume <= 0:
                break

            result = leg.order(symbol, side.value, volume,
                               slippage_points=slippage, comment=comment)
            got = float(result.get('filled_volume') or 0.0)

            if got > 0:
                filled += got
                notional += got * float(result.get('price') or 0.0)
                if result.get('ticket') is not None:
                    tickets.append(result['ticket'])

            if not result.get('ok'):
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

    def _unwind(self, leg, symbol, entry_side, lots, comment):
        """Reverse an entry fill; CRITICAL on failure (unhedged exposure)."""
        if lots <= EPS:
            return True
        filled, _, _ = self._send_sliced(
            leg, symbol, entry_side.opposite, lots, comment)
        if filled < lots - EPS:
            logging.critical(
                "UNHEDGED EXPOSURE on [%s]: tried to unwind %.2f lots of "
                "%s, only %.2f reversed — MANUAL INTERVENTION REQUIRED",
                leg.name, lots, symbol, filled)
            return False
        logging.info("[%s] unwound %.2f lots of %s", leg.name, lots, symbol)
        return True

    # ------------------------------------------------------------------
    # Pair entry / close (PositionManager-compatible interface)
    # ------------------------------------------------------------------

    def execute_trade_pair(self, asset, signal_type, lot_size,
                           spot_symbol, futures_symbol):
        if signal_type == SignalType.SELL_BASIS:
            spot_side, futures_side = OrderSide.BUY, OrderSide.SELL
        elif signal_type == SignalType.BUY_BASIS:
            spot_side, futures_side = OrderSide.SELL, OrderSide.BUY
        else:
            raise ValueError(f"Invalid signal type for opening: {signal_type}")

        comment = f"BASIS_ARB_{uuid.uuid4().hex[:8]}"
        spot_trade = Trade(spot_symbol, spot_side, 0.0)
        futures_trade = Trade(futures_symbol, futures_side, 0.0)

        # Leg 1: spot clip
        spot_filled, spot_vwap, spot_tickets = self._send_sliced(
            self.spot_leg, spot_symbol, spot_side, lot_size, comment)

        if spot_filled <= EPS:
            spot_trade.status = futures_trade.status = "ERROR"
            spot_trade.error_message = "Spot leg filled nothing"
            return False, spot_trade, futures_trade

        # Leg 2: futures hedge sized to the actual spot fill
        hedge_ratio = self.config.TRADING.get('HEDGE_RATIO', 1.0)
        fut_step = self._meta(self.futures_leg,
                              futures_symbol).get('volume_step') or 0.01
        hedge_target = self._round_step(spot_filled * hedge_ratio, fut_step)

        fut_filled, fut_vwap, fut_tickets = self._send_sliced(
            self.futures_leg, futures_symbol, futures_side,
            hedge_target, comment)

        if fut_filled <= EPS:
            logging.error("Futures hedge filled nothing — unwinding %.2f "
                          "spot lots", spot_filled)
            self._unwind(self.spot_leg, spot_symbol, spot_side,
                         spot_filled, comment)
            spot_trade.status = futures_trade.status = "ERROR"
            futures_trade.error_message = "Futures hedge filled nothing"
            return False, spot_trade, futures_trade

        if fut_filled < hedge_target - EPS:
            spot_step = self._meta(self.spot_leg,
                                   spot_symbol).get('volume_step') or 0.01
            matched_spot = self._round_step(fut_filled / hedge_ratio,
                                            spot_step)
            excess = spot_filled - matched_spot
            logging.warning(
                "Hedge partial: futures %.2f/%.2f — unwinding %.2f excess "
                "spot lots, keeping matched position",
                fut_filled, hedge_target, excess)
            self._unwind(self.spot_leg, spot_symbol, spot_side, excess,
                         comment)
            spot_filled = matched_spot

        spot_trade.lot_size = spot_filled
        spot_trade.executed_price = spot_vwap
        spot_trade.order_ticket = spot_tickets[0] if spot_tickets else None
        spot_trade.status = "EXECUTED"

        futures_trade.lot_size = fut_filled
        futures_trade.executed_price = fut_vwap
        futures_trade.order_ticket = fut_tickets[0] if fut_tickets else None
        futures_trade.status = "EXECUTED"

        logging.info("Pair executed: %s %s — spot %.2f @ %.2f [%s], "
                     "futures %.2f @ %.2f [%s]",
                     asset, signal_type.value, spot_filled, spot_vwap or 0,
                     self.spot_leg.name, fut_filled, fut_vwap or 0,
                     self.futures_leg.name)
        return True, spot_trade, futures_trade

    def execute_close_pair(self, position):
        comment = f"BASIS_ARB_CX_{uuid.uuid4().hex[:6]}"

        close_spot = Trade(position.spot_trade.symbol,
                           position.spot_trade.side.opposite,
                           position.spot_trade.lot_size)
        close_futures = Trade(position.futures_trade.symbol,
                              position.futures_trade.side.opposite,
                              position.futures_trade.lot_size)

        spot_filled, spot_vwap, spot_tickets = self._send_sliced(
            self.spot_leg, close_spot.symbol, close_spot.side,
            close_spot.lot_size, comment)
        fut_filled, fut_vwap, fut_tickets = self._send_sliced(
            self.futures_leg, close_futures.symbol, close_futures.side,
            close_futures.lot_size, comment)

        spot_ok = spot_filled >= close_spot.lot_size - EPS
        fut_ok = fut_filled >= close_futures.lot_size - EPS

        for trade, filled, vwap, tickets, ok in [
                (close_spot, spot_filled, spot_vwap, spot_tickets, spot_ok),
                (close_futures, fut_filled, fut_vwap, fut_tickets, fut_ok)]:
            trade.executed_price = vwap
            trade.order_ticket = tickets[0] if tickets else None
            trade.status = "EXECUTED" if ok else "ERROR"
            if not ok:
                trade.error_message = (
                    f"Close incomplete: {filled:.2f}/{trade.lot_size:.2f}")

        if not (spot_ok and fut_ok):
            logging.critical(
                "INCOMPLETE CLOSE for %s: spot %.2f/%.2f, futures %.2f/%.2f "
                "— residual exposure, MANUAL INTERVENTION REQUIRED",
                position.position_id, spot_filled, close_spot.lot_size,
                fut_filled, close_futures.lot_size)
            return False, close_spot, close_futures

        return True, close_spot, close_futures
