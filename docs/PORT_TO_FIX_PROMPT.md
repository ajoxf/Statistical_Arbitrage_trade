# Porting prompt — stat-arb enhancements, MT5 → FIX

Paste everything below the line into a Claude Code session opened on the
FIX-connected repository. It is self-contained: it states the arithmetic
rather than pointing at our files, so the other agent does not need this
repo to work from.

---

You are working on a two-leg statistical-arbitrage / basis trading system
that connects to its broker over **FIX**, not MetaTrader 5. A sibling
system with the same strategy runs on MT5, and over the last four days it
was debugged hard against a live account. This prompt is the distilled
result: the rules that were proved wrong the expensive way, and the fixes.

**Do not start by writing code.** First audit this repository against the
list below and produce a gap report: for each numbered item, say whether
it is already correct here, partly present, absent, or not applicable to a
FIX venue, with the file and line you checked. Show me that report and
wait. Then port in the order given — arithmetic first, reporting second,
UI last — committing each group separately with tests that fail against
the current code.

Ground rules while you work:

- **Every test must run with no venue connection.** Fake the FIX session.
  If a test needs a live socket it is the wrong test.
- **A test that fails with `AttributeError` or `TypeError` on the old code
  proves nothing.** Route assertions through APIs that exist on both
  sides, so a failure means a wrong *answer*.
- **Internal consistency is not correctness.** Most of the bugs below
  survived for months because every figure on the screen agreed with every
  other figure on the screen. They shared an anchor; the anchor was wrong.
- When you find one of these bugs, state the arithmetic of the error, not
  just the fix.

---

## A. The prices — which quote each decision reads

**1. The spread is the mid of the book, always.**
`spread = leg_B − β × leg_A`, with each leg's price the `(bid + ask) / 2`.
Never a last-traded price. A print outside the book makes the mid sit
above the best offer, and it breaks the series: μ/σ/z need one continuous
definition, and a spread that switches from a midpoint to a trade print
whenever a trade crosses carries that jump as noise σ then absorbs.

**2. Publish the two spreads that can actually be traded.**

```
short_spread = B_bid − β × A_ask      (sell B, buy A)
long_spread  = B_ask − β × A_bid      (buy B, sell A)
spread_cost  = long_spread − short_spread
```

By construction `short ≤ mid ≤ long`, and `spread_cost` is **exactly one
round turn of both legs' bid-ask, in spread units** — the same quantity
the cost model charges in dollars. Two views of one cost, never two costs.

**3. One function decides which side any given action reads.**

```
executable_spread(md, direction, closing=False)
    SELL_BASIS  entering → short_spread    exiting → long_spread
    BUY_BASIS   entering → long_spread     exiting → short_spread
```

A position comes out the opposite way it went in. Reading the favourable
side at both ends makes every trade look like it cleared its costs —
worse than using the mid, not better. Fall back to the mid when the
touches are missing so replayed rows still price.

**4. The per-leg version, for marking a position.**

```
closing_prices(md, direction)
    SELL_BASIS (long A, short B) → A_bid, B_ask
    BUY_BASIS  (short A, long B) → A_ask, B_bid
```

Assert in a test that `B_mark − β × A_mark == executable_spread(closing=True)`.
The mark and the levels it is compared against must never be quoted on
different bases.

**5. The rule that settles every one of these questions:**

> A price someone **named** is compared against what the market offers.
> A **statistic** is compared against the series it was measured on.

So: the operator's stop and target read the executable closing side; an
armed entry trigger reads the executable entry side; the z-score, σ, the
entry mean and the warm-start window all read the **mid**.

**6. The statistical window is a series of quote events, not of polls.**
Stamp each snapshot with a quote identity (both legs' timestamps *and*
both bid/asks — some feeds stamp only to the second) and ignore a repeat.
Without this, polling faster than the feed ticks collapses σ and
manufactures enormous z-scores. Also drop *consecutive* identical spreads
when seeding the window from stored history, or a database full of poll
rows reintroduces the same collapse across a restart. Regression test:
the same quotes recorded once each and twenty times each give the same σ.

## B. Sizing — one multiplier, everywhere

**7. The hedge follows from the spread definition, and it is not β.**
From `spread = P_B − β·P_A`, matching the pair's P&L to a spread move
requires

```
L_A × C_A = β × L_B × C_B        →    L_B = L_A × C_A / (β × C_B)
```

Sizing the hedge as `L_B = L_A × β` is *inverted* for β ≠ 1: at β = 2 the
correct hedge is half the leg-A lots and the naive rule trades double.
It is only ever right at β = 1 with equal contract sizes.

**8. `k = L_B × C_B` is the number of dollars in 1.00 of spread.** Every
single spread↔dollar conversion uses it — the take-profit, the displayed
levels, the manual target, the slippage report, the expected value, the
carry card. Four places each deriving their own multiplier is how they
drift apart. Publish `k` on the plan and have everything read it.

**9. Each leg is priced in its own contract size.** Search for anywhere
leg B's quantity is computed with leg A's `contract_size` — cost model,
P&L marking, realised P&L, capital-at-risk. It is exact only when the two
legs are the same instrument spec, which is the one configuration these
systems get tested in.

**10. Hedge balance is a choice, and the two disagree.**
`units` mode (`L_B·C_B = L_A·C_A/β`) makes the pair's P&L equal the spread
move the z-score is measured on — right for a basis pair. `notional` mode
(equal money both legs) trades the *return* spread — right for two related
instruments with no arbitrage tying them. They coincide only when β equals
the live price ratio; publish the gap and warn past a couple of percent.

**11. Notional sizing rounds leg A to the NEAREST tradable step and the
hedge DOWN.** The notional is a target, not a ceiling: flooring a small
order loses a large fraction of it. But the hedge must round down —
rounding a wanted 0.05 up to leg B's coarser 0.1 step is a hedge twice
the size of the position it hedges. Short is the recoverable error.
Compute and display the pair's **minimum tradable notional** from both
legs' minimum sizes and both contract sizes, and refuse below it naming
the figure.

**12. β belongs to the pair.** Stamp the configured hedge ratio with the
instrument pair it was derived for. On startup, if the stamp names a
*different* pair, re-derive it; if it matches, leave the operator's tuned
value alone; if there is no stamp, keep the value and just stamp it unless
it is implausible against live prices. Never re-derive while a position is
open — β defines the series the position was entered on. Block entries (do
not merely warn) when the spread is implausible against the two prices
quoting it.

## C. Costs, and charging the bid-ask exactly once

**13. Split the cost model.**

```
cost_parts(...) → (crossing, commission)
round_trip_cost = crossing + commission
```

`crossing` = both legs' bid-ask, each on its own units, over the round
trip. `commission` = per lot per leg.

**14. `mark_fees(plan)` — what is still outstanding against a mark.**
A position entered at a real fill and marked at the price it would close
at has **already paid both crossings**; they are in the two prices.
Subtracting the whole round trip again is the bid-ask twice. So:

```
NET = gross(exit-marked) − commissions          ← the only fees left
```

Keep the full round trip for anything pricing a trade that **has not
happened yet**: the edge filter, the expected value, the pre-trade cost
card, the entry notification, the modelled-vs-realised audit.

This is subtle enough to have been half-wrong even before the mark moved:
a mid-marked gross measured from a real fill already carries the *entry*
crossing, so `gross − round_trip` over-charged by the exit half. It never
showed as an error because the card was self-consistent.

**15. Regression-test the invariant directly:** enter at the executable
spread, move nothing, close — the pair must be down exactly one modelled
round trip, no more.

**16. The edge filter, and the leg it measures on.**
`capture = TARGET_FRACTION × |z| × σ × k` against
`MIN_EDGE_MULTIPLE × round_trip_cost`. Capture is a **mid** move and cost
is the whole round turn, which is what makes the comparison sound.
Capture computed on leg A's units while cost is computed per leg
understates the edge by exactly 1/β. Publish `edge_z_needed` (the exact
boundary) and the cost expressed in σ, and probe both sides of the
boundary in a test.

## D. Marking the position

**17. Mark each leg at the price it would actually be closed at**, using
`closing_prices` — never the mids. Consequences, all correct and all worth
stating in the UI:

- The stop, the take-profit, the peak/trough distribution and any
  net-based gate act on the true figure. A stop fires when the position
  really is that far down, not half a round turn later.
- **A position shows a loss the instant it opens**, equal to one round
  turn of bid-ask. That is what closing immediately costs. It was always
  there; the mid was hiding half of it and the fee model was charging that
  half a second time.
- Your P&L now agrees with the venue's own, which marks at closing touches.

**18. Levels are anchored on the FILL, not on the decision mid.** The
dollar ladder fires off P&L, and P&L is measured from executed prices, so
a level drawn from the mid names a price the stop will not fire at — out
by the entry crossing plus slippage. Keep the decision mid separately as
the *statistical* anchor for the entry mean.

**19. Break-even, with the new fee basis, is `fill ∓ commission / k`,**
read against the **closing** side of the book. That is the comparison an
operator actually makes: for a short, "is the long spread above my
break-even?" Both numbers must be on one basis or the answer inverts.

## E. The exit ladder

**20. Priority, and completeness.** Hard stop (ungated, gross) → take
profit (net) → gated reversion exit → max hold (profit only) → hard time
stop → z-stop. **Every reachable (P&L, z, time) state must have a
reachable exit**, and that must be a regression test, not a belief.

**21. Floor the max-hold.** A mean-reversion half-life fitted on
sub-second quotes measures tick noise: an 8-second half-life gave a
32-second max hold and a 96-second hard time stop, and force-closed a live
trade 37 seconds in. Apply a minimum (5 minutes is the value in use) and
log loudly when it binds, saying the fit is measuring noise.

**22. A reversion gate that was already satisfied at entry must be
skipped entirely.** A signal entry cannot start inside the band (the gates
guarantee |z| ≥ entry threshold > exit threshold) but a manual entry
routinely does, at z ≈ 0. The gate then never measures anything — and the
"release past 2× max hold" branch below it degrades into an unconditional
timed exit at whatever loss the trade happens to carry. Freeze
`entry_home` at entry and skip the whole reversion block when it is set.

**23. Re-price the target once the fill is known, and re-derive the stop
with it.** The plan is built before the order exists, so a hand-entered
target can only be measured from the mid, while the dollar target is
compared against P&L measured from the fill. With a reward-ratio stop
(`stop = target / RR`) an overstated target widens the risk by
`overstatement / RR` — at RR 0.3, more than three times. Make the stop
*selection* a single shared function so it can be re-run, rather than
existing once inline.

**24. Name the binding knob.** Three stop knobs in three units collapse to
one figure through `min()`; publish which one bound (`stop_source`) and
the **break-even win rate** `stop / (target + stop)`, amber past a coin
flip. RR < 1 is the trap: it reads like a risk setting and it is a
*reward* ratio.

**25. Expected value at entry**, if you want it: `EV = p·TP − (1−p)·(STOP
+ cost)` — the loss leg carries the cost, because the target fires on net
and the stop on gross. Get `p` from the OU two-barrier probability with
scale function `S'(z) = exp(z²/2)`; the reversion speed cancels, which
matters when the half-life is unreliable. Compute the integrals factored
by `exp(peak²/2)` or they overflow. Sanity property: the no-reversion
baseline is exactly `−cost` for every combination of levels. Any veto on
it must be opt-in and default off.

## F. Manual trades are a separate regime

This was the largest behavioural change and it is worth porting whole.

**26. A hand-placed trade is governed by its own card and nothing else.**
Stamp `source = MANUAL` **inside** the plan builder, not by the caller
afterwards — the ladder reads it to decide who governs the trade, and an
unstamped plan must fail safe to SIGNAL (managed) rather than unmanaged.
For a manual plan, short-circuit the ladder to exactly: the operator's
stop, the operator's target, the overnight rule, the close button. Off:
the dollar stop, the sigma take-profit, the reversion gate, max hold, the
hard time stop, the z-stop.

**27. Risk limits are off for a manual trade too** — circuit breaker,
maximum size, positions-per-instrument, loss-streak and margin size
reducers. They are the *strategy's* governor, there to stop the algo
trading itself into trouble unattended. Log every bypass at WARNING beside
the trade that used it. Still standing, because none is a risk policy: the
level-geometry check (a stop on the winning side is a typo, not an
appetite), both legs' minimum-size pre-check, and the venue's own
rejection.

**28. Never veto a manual entry** on the viability test that asks whether
a *signal-derived* target clears the round trip. Measure the operator's
own target instead; below cost, place it with a loud warning.

**29. State the price of all this.** A manual trade with the stop box
empty has **no stop**. It runs until the target, the overnight rule, or the
operator. Log a WARNING at entry, turn the hint under the field amber,
and never draw a stop on the card that cannot fire.

**30. Order of operations bug, found twice.** Restating the manual risk
(replacing the engine's stop with the operator's, zeroing the gate
release, recomputing the break-even win rate **and the EV**) must run
**after** the target is re-priced — because re-pricing re-runs the stop
selection and puts the RR-derived stop straight back. Symptom: a card
reading "SL 58.53 / −$6.30 / stop from target $1.89 / RR 0.3 / needs 77% of
trades to win" on a trade whose stop box was empty, beside the engine's own
log line saying it had no stop.

**31. Only show what can fire.** With the engine's clock off, the max-hold
and remaining-time rows must not render on a manual position either. A
countdown next to a hand-placed order reads as a considered decision.

**32. Two books.** A manual trade must not feed the algo's circuit
breaker, loss streak, size reducer, daily trade count or entry cooldown —
book its P&L separately and add the two together for the account total.
Record everything else about it identically (peak/trough with minutes,
outcome tag, frozen levels, slippage split) and report statistics as
`{all, algo, manual}`. Note: profit factor with no losses is **undefined**,
not 0.0 — 0.0 is the worst possible value and a small all-winners book
renders it in red.

**33. Overnight rule:** `ALLOW` / `EXIT_IF_PROFIT` / `EXIT_ALWAYS` against
a session cutoff, checked before the ladder. Two gotchas: it stays armed
for the rest of the day (a trade opened after the cutoff with
`EXIT_ALWAYS` closes on its next tick), and the cutoff time needs a
control on the settings page, not just a config key.

## G. Recording and reporting

**34. Record the direction; never infer it.** Store the signal type on the
closed-trade row. Inferring it from the sign of z is valid *only* for a
signal entry (the gates guarantee |z| ≥ threshold and the sign decides).
A manual entry has no z requirement — the operator picks — so a hand-placed
long at z = +2.0 renders as SHORT. And `(None or 0) > 0` is False, so every
row without a recorded z rendered LONG whatever it was. With neither
available, render a **dash**: an unknown direction must not render as a
confident one.

**35. Record the exit on the same basis as the entry.** The journal shows
"entry spread → exit spread"; if entry is the fill and exit is the mid,
the difference between the two columns is not the trade's move and will
not reconcile against the P&L beside it. Record the exit at the executable
closing spread.

**36. Direction sign in every "was this favourable?" calculation.**
`d = −1` for a short spread (sold it, profits as it *falls*), `+1` for a
long. This was inverted independently in two places — a journal column and
a live delta cell — each of which rendered a profitable short as a loss.
Derive it once, publish it, and have the UI read the published value
rather than keeping its own copy of the rule.

**37. Slippage, scored against the price you expected.** Three prices,
kept apart: the **mid** the decision was measured on, the **executable
touch** at that instant (what the decision was actually worth), and the
**fill**. Report `expected → filled → slippage` where *expected* is the
executable touch — that is the number on the screen when the operator
clicked. Do not show the mid in that table; labelling a midpoint "wanted"
promises a goal nobody could ever have been filled at. Positive is always
a cost; keep the sign, because limit fills do improve. Flip the sign
convention between entry and exit — the same direction sells the spread
going in and buys it back coming out. Invariant to test across both
directions × entry/exit × several βs:
`spread_slip == B_slip + β × A_slip`. **Unmeasured must report None, not
zero** — a zero reads as flawless execution.

**38. Ask before closing an open position on shutdown.** Print the
position (instrument, direction, size, live P&L) and wait. **An unanswered
prompt means "leave it open"** — closing at market is irreversible, and a
position left open is recovered on the next start. No tty, a timeout, or a
second interrupt all resolve the same way, and the reader must be on a
daemon thread so it cannot hold the process open. Make it configurable
(`ask` / `always` / `never`) with an unrecognised value falling back to
`ask`. If a launcher supervises the process, it must **wait** for the child
rather than killing it, or the prompt dies before it can be answered.

**39. Config writes must be atomic.** Write to a temp file and rename. A
half-written config read by a tolerant loader that returns `{}` on parse
failure, in front of a read-modify-write settings page, silently deletes
every account. Also: keep a `.bak`, distinguish "missing" from
"present but broken" (fall back to the backup, and *raise* if there is
none — refusing the save is the only safe answer), and refuse a save that
drops a previously non-empty accounts/instruments section unless the
caller explicitly means it.

**40. Any key the operator can change needs a control**, and any key with
a control must be in the hot-reload set. A setting that is saved and then
silently ignored until restart is worse than no setting; hot-reload
reporting only what it *did* apply hides it. Commission per lot sat at
zero for months with no way to set it, behind four decorative fee fields
that were posted and dropped by the server.

## H. UI, if this repo has one

Lower priority than everything above, but these were all real operator
complaints:

- The two **executable** spreads are the headline, each over the two
  touches it is built from, at a fixed 2dp; the mid is not shown at all.
- The open-position card is a real CSS **grid** (`max-content 1fr
  max-content 1fr`) so values align down the card; optional groups use
  `display: contents`, not a nested grid, or they land a few pixels off.
- Frozen levels as a **table**: one column per level (BE / TP / SL), one
  row per reading (spread, P&L), driven by a single column list so the two
  rows cannot disagree.
- The P&L progress bar must survive a trade with **only one** level — the
  level that exists scales both sides; say in the tooltip that the
  mirrored side is not a real stop.
- Local CSS for anything load-bearing: a blocked CDN must not take the
  colours, the borders or the save handlers with it. Probe any CDN global
  before using it — one `const socket = io()` at the top of a shared
  template killed every handler defined below it.
- **Event-driven logging.** Log a gate when it *starts* blocking and when
  it clears, not on every poll; drop 2xx/3xx from the web access log and
  keep 4xx/5xx. Three separate floods, each thousands of lines an hour.
- Never send the operator to a log for a decision the engine already made.
  Carry the venue's own rejection text up to the panel that asked.
- Every multiplier printed with its **unit and derivation**. A bare "× 2"
  reads as a factor of two when it means two ounces.

---

## MT5 → FIX: what does not translate

Work through these deliberately; several of the MT5 rules above encode
platform behaviour that FIX handles differently, and a couple invert.

| MT5 concept | FIX equivalent | What changes |
|---|---|---|
| One global connection per process; two accounts need two processes and a socket bridge | One FIX session per venue/account, usually many per process | **Re-examine the whole multi-process split.** If the FIX engine supports concurrent sessions in one process, collapse the leg-runner/coordinator architecture and keep only the `Leg` interface. Do not port the port/login/terminal clash checks. |
| Hedging vs netting accounts; closes MUST target position tickets | No tickets. Positions are your own bookkeeping, or `RequestForPositions` (35=AN → 35=AP) | **Establish first whether an offsetting order closes or opens.** The MT5 rule exists because an opposite order on a hedging account opens a second position — a bug that cost a live unwind. Whatever the answer is here, pin it with a test. |
| `order_send` retcodes (10015 invalid price, 10027 algo off, 10030 filling mode) | `ExecutionReport` (35=8) with `ExecType`/`OrdStatus`, plus `OrdRejReason` (103) and `Text` (58) | Keep the principle: **surface the venue's own words** all the way to the operator, never a summary that describes the symptom. |
| Filling modes FOK/IOC/RETURN chosen from a bitmask, retried on rejection | `TimeInForce` (59) and `ExecInst` (18) | Same shape: read what the venue supports, retry across the allowed values, and let the error name what is allowed. |
| `trade_stops_level`, `trade_tick_size` | `SecurityDefinition` (35=d): `MinPriceIncrement` (969), tick rules, price bands | The limit-price legality rule stands: fresh quote, rounded to the increment, strictly inside the book, clamped when an offset would cross. |
| Deal history lags a fill; re-read before believing a zero | `ExecutionReport`s are ordered and authoritative | **This one gets simpler.** But add the FIX-specific version: reports missed while disconnected arrive on `ResendRequest`/gap-fill, so reconciliation must handle a session that was down. |
| Re-peg via `TRADE_ACTION_MODIFY`, one ticket for the order's life — explicitly *not* cancel/replace | `OrderCancelReplaceRequest` (35=G) issues a **new** `ClOrdID` | **This rule inverts.** You cannot keep one identifier across a re-peg. Maintain the `OrigClOrdID → ClOrdID` chain and accumulate fills across it, which is exactly what the MT5 rule was avoiding having to do. Get this right before building the limit-first executor. |
| `verify_ticket`: read the order back out of the terminal | `OrderStatusRequest` (35=H), drop-copy session, `RequestForPositions` | Keep the discipline: after any order, **prove it exists at the venue** and log confirmed / not confirmed with the identifiers. |
| `swap_long` / `swap_short` and `swap_mode` units | No FIX equivalent | Carry-to-expiry inputs must be hand-entered per leg per side, clearable (blank means "unknown"), and an unconvertible input must return None rather than being read as zero. |
| Symbol resolution, `ensure_symbol`, visibility | `SecurityListRequest` (35=x) | Keep the behaviour of *listing what the venue offers* when a symbol does not resolve, rather than reporting "missing symbols". |
| Account balance / margin | `CollateralInquiry` (35=BB), `RequestForPositions`, or the venue's REST | Cache it — do not fetch per poll. |
| Broker clock offset from tick timestamps | `SendingTime` (52), `TransactTime` (60), UTC by specification | Largely goes away. If you keep a check, remember the offset conflates clock skew with quote staleness — take a running **maximum** and quantise to the half hour. |
| — | **Session layer: `Logon`(A) / `Logout`(5) / `Heartbeat`(0) / `TestRequest`(1) / sequence numbers / `ResendRequest`(2) / `SequenceReset`(4)** | **Entirely new failure surface with no MT5 analogue.** A dropped session with orders resting is the FIX version of the orphan problem. The reconciler must run on reconnect, before trading, and must assume it missed reports. Sequence-number mismatch must be an operator-visible state, not a silent reconnect loop. |

Finally: the MT5 system's connectivity checklist (terminal attached, algo
trading enabled, logged in as the configured account, investor password,
hedging vs netting, leverage, symbol tradable, volume min/step/max,
contract size, expiry) should be **rebuilt for FIX**, not ported: session
logged on and sequence-synced, credentials/`SenderCompID`/`TargetCompID`
accepted, security definitions resolved for both legs, both legs' price
increments and size limits, and a live-basis plus quote-freshness check.
Read-only, so it can run while trading.
