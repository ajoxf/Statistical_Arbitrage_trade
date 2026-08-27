"""The convergence loop — re-enter the same short while the basis is
rich against what it costs to carry.

Operator, 2026-08-27: "If Current Spread > fair spread — open a
position, after profit position is closed, open again the same high to
low trade (as long as current spread > fair), this loop keeps going on
as long as the button is kept on."

This is the CARRY trade, not the z-score one. z asks whether the spread
is unusual against its own recent history; this asks whether it is wide
against the financing needed to hold the pair to expiry — a question
that is decided by a date rather than by mean reversion happening. The
two disagree often, which is the point of having both.

WHICH FAIR VALUE. The broker's swap (`carry.convergence_plan`'s
`carry_spread`), chosen by the operator over `fairvalue`'s risk-free
rate. It is the one tied to money actually charged, and it is
size-free — dividing by k cancels the lots — so the same threshold
holds whatever the clip is.

WHAT IT REFUSES TO TRADE ON, and why each refusal is a refusal rather
than a fallback:

- no `carry_spread` — one unconvertible swap or no expiry. A carry
  estimate missing a leg is not a smaller estimate; entering on half of
  one is entering on a number nobody computed.
- `carry.sanity` warning — the swap and the risk-free rate disagree
  about the SIGN of this basis, so one of the two inputs is provably
  wrong. The dashboard already refuses to print a verdict here; a loop
  that places real orders must refuse harder.
- a stale or desynced quote — the same guard every other level-reading
  decision uses. A gap measured against a phantom print is a phantom
  gap.

WHAT STOPS IT. The operator picked one bound: the stop loss on each
cycle. A cycle that stops out switches the whole loop OFF and says so.
Anything else — a target reached, the gap closing, the button — leaves
it armed. The loop therefore cannot bleed indefinitely: it takes one
loss and stands down, and re-arming is a human decision.

Levels are DISTANCES, not the absolute spread levels the Manual card
takes. Cycle 2 fills somewhere else, so an absolute level typed for
cycle 1 is meaningless by then — it would be either unreachable or
already passed.
"""

import logging


# A short is entered when the spread is rich; the gap has to clear this
# much of the round trip before it is worth crossing for. 1.0 = the
# trade must at least pay its own fees, which is the weakest bound that
# is not simply wrong.
DEFAULT_EDGE_MULT = 1.0


def fair_spread(carry_block):
    """The spread the basis SHOULD be on financing alone, or (None, why).

    Reads the block `Coordinator._carry_block` already publishes, so the
    number the loop trades on is the one printed on the Carry to Expiry
    card. Two places deriving 'fair' independently is how they end up
    disagreeing on screen while one of them places orders.
    """
    block = carry_block or {}
    warning = block.get('warning')
    if warning:
        # sanity() fires when the broker's swap and the risk-free rate
        # disagree about which way this basis points. Trading through
        # that is trading on an input the engine can prove is wrong.
        return None, f"the carry inputs disagree — {warning.get('text', warning)}"
    value = block.get('carry_spread')
    if value is None:
        return None, (block.get('reason')
                      or 'no carry estimate for this pair')
    return float(value), None


def evaluate(spread, carry_block, cost_usd=0.0, spread_units=None,
             edge_mult=DEFAULT_EDGE_MULT):
    """Should the loop open a short right now?

    `spread` is the EXECUTABLE short spread — the price a seller of the
    spread actually fills at — not the mid. The whole trade is 'sell it
    rich', so comparing the mid against fair would claim an edge half a
    round turn of which is not there.

    Returns (ok, detail). `detail` always says something, because the
    panel shows it whether the answer is yes or no.
    """
    fair, why = fair_spread(carry_block)
    if fair is None:
        return False, why
    if spread is None:
        return False, 'no executable spread yet'

    gap = float(spread) - fair
    # The gap has to cover the round trip before it is an edge at all.
    # Expressed in SPREAD, so it is comparable to the two numbers beside
    # it rather than needing the reader to convert.
    need = 0.0
    if cost_usd and spread_units:
        need = (float(cost_usd) / float(spread_units)) * float(edge_mult)
    if gap <= need:
        if need:
            return False, (f"spread {spread:.4f} is {gap:+.4f} vs fair "
                           f"{fair:.4f} — needs {need:.4f} to clear the "
                           f"round trip")
        return False, (f"spread {spread:.4f} is {gap:+.4f} vs fair "
                       f"{fair:.4f} — not rich")
    return True, (f"spread {spread:.4f} is {gap:+.4f} above fair "
                  f"{fair:.4f}, clearing {need:.4f} of cost")


def levels_from_fill(fill_spread, take_profit, stop_loss):
    """A short's absolute TP/SL for THIS cycle, from distances.

    A short profits as the spread FALLS, so the target sits below the
    fill and the stop above it. Returning None for an unset distance is
    deliberate: no take-profit means the cycle is closed by the operator
    or by the stop, and no stop means the loop refuses to start at all
    (see `Coordinator._carry_loop_start`) rather than running unbounded.
    """
    if fill_spread is None:
        return None, None
    fill = float(fill_spread)
    target = fill - abs(float(take_profit)) if take_profit else None
    stop = fill + abs(float(stop_loss)) if stop_loss else None
    return target, stop


class LoopState:
    """What the loop is doing, and what it has done today.

    Kept out of the coordinator so the whole lifecycle — armed, in a
    cycle, stood down — can be exercised without a broker, a socket or
    a clock.
    """

    def __init__(self, asset, lots=None, take_profit=None, stop_loss=None,
                 edge_mult=DEFAULT_EDGE_MULT):
        self.asset = asset
        self.lots = lots
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.edge_mult = edge_mult
        self.enabled = True
        self.cycles = 0
        self.wins = 0
        self.realized = 0.0
        self.position_id = None      # the cycle currently open
        self.last_note = 'armed'
        self.stood_down = None       # why the loop switched itself off

    # -- lifecycle ---------------------------------------------------
    def opened(self, position_id):
        self.position_id = position_id
        self.cycles += 1
        self.last_note = f"cycle {self.cycles} open ({position_id})"

    def closed(self, pnl, reason):
        """One cycle finished. A LOSS stands the loop down.

        The operator chose the stop as the loop's only bound, so this is
        where that bound is actually enforced: a stopped-out cycle is
        the market saying the gap was not an edge, and re-entering into
        it is how a bounded loop becomes an unbounded one.
        """
        self.position_id = None
        pnl = float(pnl or 0.0)
        self.realized += pnl
        if pnl > 0:
            self.wins += 1
            self.last_note = (f"cycle {self.cycles} banked ${pnl:+,.2f} "
                              f"({reason}) — re-arming")
            return True
        self.enabled = False
        self.stood_down = (f"cycle {self.cycles} closed ${pnl:+,.2f} "
                           f"({reason}) — the loop stops on a losing "
                           f"cycle. Turn it back on when you want it.")
        self.last_note = self.stood_down
        logging.warning("Carry loop STOOD DOWN on %s: %s",
                        self.asset, self.stood_down)
        return False

    def to_dict(self):
        return {'asset': self.asset, 'enabled': self.enabled,
                'lots': self.lots, 'take_profit': self.take_profit,
                'stop_loss': self.stop_loss, 'edge_mult': self.edge_mult,
                'cycles': self.cycles, 'wins': self.wins,
                'realized': round(self.realized, 2),
                'position_id': self.position_id, 'note': self.last_note,
                'stood_down': self.stood_down}
