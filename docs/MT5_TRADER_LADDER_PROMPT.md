# Prompt for the new project — `ajoxf/MT5-Trader`

Paste everything below the line into a fresh session that has access to
`https://github.com/ajoxf/MT5-Trader`.

---

## What you are building

A **spread price-ladder trading terminal for MetaTrader 5**, in the repo
`https://github.com/ajoxf/MT5-Trader`.

It is a **manual trading tool**. There is **no strategy, no signal engine, no
z-score, no automatic entries or exits**. A human looks at a ladder of spread
prices, clicks a price, and an order exists at that price. That is the whole
product.

Each ladder trades **one pair of instruments across two MetaTrader 5 accounts** —
Leg A on account 1, Leg B on account 2 — and displays the **spread** between
them as a single instrument, exactly as a CQG or TT inter-product spread ladder
does. Several ladders run at once (Spot Gold vs Gold Future; Spot Silver vs
Silver Future; WTI vs Brent; UKOIL Oct vs UKOIL Nov), all routing through the
**same two configured accounts**.

## Read the existing system first — it is the specification

There is a production stat-arb system for the same two brokers, the same two
accounts, the same instruments, built and debugged live over months:

- Repo: `https://github.com/ajoxf/Statistical_Arbitrage_trade`
- Branch: `claude/mt5-multi-account-streaming-4pmun0`

**Read it thoroughly before writing any code.** Start with `CLAUDE.md` at the
repo root: it is a long incident log, and nearly every paragraph in it is a bug
that cost real money on a live account. Then read, in this order:

| File | Why it matters to you |
|---|---|
| `CLAUDE.md` | Every hard-won rule. Read it twice. |
| `statarb/broker.py` | The **only** module that imports MetaTrader5. Every MT5 quirk is encoded here. |
| `statarb/leg_runner.py`, `statarb/ipc.py`, `statarb/legs.py` | The two-account architecture and its wire protocol. |
| `statarb/marketdata.py` | Spread definition, executable spreads, staleness and jump guards. |
| `statarb/sizing.py` | Hedge ratio arithmetic. Getting this wrong inverts your hedge. |
| `statarb/pair_executor.py` | Limit-first execution, re-peg, cancel-and-verify, unwind, ticket-based closes. |
| `statarb/reconcile.py` | Orphans, ghosts, 3-strike cleanup. |
| `statarb/costs.py` | What a round trip actually costs, per leg. |
| `statarb/config.py` | File-based config + `.env` secrets. |
| `statarb/webapp.py`, `statarb/webapi.py`, `templates/` | The read-only-ish Flask/file-bridge UI pattern. |
| `statarb/positions.py`, `statarb/models.py` | Position lifecycle, crash-safe state, restart recovery. |
| `statarb/database.py` | SQLite logging, WAL, busy timeout. |
| `tests/` | ~1,470 tests, all fakes, no MT5 required. Copy this discipline. |

You may **port code directly** where it fits. Prefer porting the modules above
over rewriting them — they encode failures you have not had yet.

**Do not port**: `signals.py`, `spread.py` (the z-score statistics),
`exits.py`, `expectancy.py`, `shadow.py`, `risk.py`'s breakers,
`carryloop.py`, `system.py`, `main.py`. That is all strategy machinery for
managing a trade the strategy chose. This product has no strategy.

---

## 1. Architecture — the constraint that decides everything

**The MetaTrader5 Python package holds ONE global connection per process.** A
second `mt5.initialize()` silently replaces the first. There is no way around
this in-process.

So the process topology is fixed:

```
  leg runner A  (own process, own MT5 terminal via path=, account A)  :9101
  leg runner B  (own process, own MT5 terminal via path=, account B)  :9102
       ^                                                        ^
       |  JSON-lines over localhost TCP (statarb/ipc.py)        |
       +----------------- coordinator process ------------------+
                    fuses both feeds into spreads,
                    holds the synthetic order book,
                    routes each leg's order to its own runner
                                    |
                          web UI process (Flask)
                        serves the ladders to the browser
```

Port `statarb/leg_runner.py` almost verbatim. Its `LegServer` already:

- serialises all MT5 work behind a `threading.Lock` (one connection, many clients),
- `listen(8)` so the UI and coordinator can both attach,
- serves this command set — reuse it, extend it, do not redesign it:

  `ping`, `account_info`, `ensure_symbol`, `tick`, `order`, `place_limit`,
  `pending_orders`, `modify_order`, `cancel_order`, `order_state`, `positions`,
  `order_log`, `terminal_report`, `symbol_report`, `verify_order`,
  `find_symbols`, `close_ticket`.

`statarb/legs.py` gives you `LocalLeg` (in-process, for the one-account case)
and `RemoteLeg` (socket client) behind **one interface**, so the rest of the
code never knows which it is talking to. Keep that.

### Rules already paid for, at startup

- **One account = one port, one login, one terminal installation.** Two accounts
  pointing at the same terminal folder are one account whatever the config says
  — a terminal holds a single login. Refuse all three clashes **at save time**,
  not only at startup (`_endpoint_clash`, `_login_clash`, `_terminal_clash` in
  `webapp.py`). A refusal at save is a corrected field; a refusal at startup is
  five restart attempts with the reason scrolling past.
- Endpoints: accept `127.0.0.1:9101`, the mistyped `127.0.0.1.9101`, and a bare
  port. Never crash with a traceback on a typo — name the account and say what
  to type.
- Symbol setup must work **with the coordinator down**. The UI opens a
  short-lived `RemoteLeg` straight to the account's runner for symbol search,
  test and diagnose. Otherwise it deadlocks: the coordinator will not start
  until the symbols are right, and those are the tools for finding out.

---

## 2. The spread — get these definitions exactly right

For a pair with Leg A (e.g. spot) and Leg B (e.g. the future), and a hedge
ratio β (`HEDGE_RATIO`):

```
mid_spread   = fut_mid - β × spot_mid          the display / series price
short_spread = fut_bid - β × spot_ask          SELL the spread: sell B, buy A
long_spread  = fut_ask - β × spot_bid          BUY  the spread: buy B, sell A
spread_cost  = long_spread - short_spread      exactly one round turn of both
                                               legs' bid-ask, in spread units
```

By construction `short_spread ≤ mid_spread ≤ long_spread`.

Three rules that came out of live losses:

1. **Build the spread from the mid of the BOOK, never from `tick.last`.** A
   trade print above the ask puts the "mid" above the long spread — above the
   best price anyone can buy the spread at.
2. **A price someone NAMED is compared against what the market OFFERS.** Every
   ladder level, every working order, every trigger reads the **executable**
   spread for its own direction, never the mid. Arming at 59.00 and firing when
   the mid touched 59.00 fires on a level the market never offered, and fills
   half a round turn worse.
3. **A position reads a DIFFERENT touch at each end of its life.** A short
   enters on `short_spread` and exits on `long_spread`. Reading the favourable
   side at both ends makes every trade look like it cleared its costs — worse
   than using the mid, not better. Port
   `marketdata.executable_spread(md, side, closing=False)` and use it as the
   single rule.
4. **The bid-ask is charged ONCE.** `spread_cost × k` and `costs.round_trip_cost`
   are two views of the same quantity. Use the executable spreads to decide
   *whether a level has been reached*; never add them on top of the cost model.

### Sizing — the arithmetic that inverts if you guess

From `spread = P_B − β·P_A`, matching the pair's P&L to a spread move requires

```
    L_A · C_A  =  β · L_B · C_B          so      L_B = L_A · C_A / (β · C_B)
```

where `C` is each leg's **contract size** (read from MT5, do not type it in).

- **`L_B = L_A × β` is WRONG.** It is identical only at β = 1 with equal
  contract sizes — which is the only configuration this has ever run in, so it
  hid for months. At β = 2 the correct hedge is *half* the leg-A lots; the naive
  rule trades double, turning a should-be-zero move into a loss three times the
  intended size.
- **`k = L_B × C_B`** (`sizing.spread_units`) is the dollars-per-1.00-of-spread
  multiplier. Every conversion from spread to money uses **this one `k`** — the
  ladder's per-tick value, the working order's notional, the position's P&L, the
  fees. Four places each deriving their own multiplier is exactly how they drift
  apart.
- The hedge rounds **DOWN** to a tradable step (short is the recoverable error);
  leg A's own lots round to the **NEAREST** step when sized from a notional.
- A pair has a **minimum tradable notional** derived from both legs' minimum
  volumes and both contract sizes. On CFI, spot min is 0.01 lots and futures min
  is 0.1 — ten times larger. State the floor in the UI *before* the operator
  tries to trade under it.
- `HEDGE_RATIO` belongs to the **pair**. Stamp it with the pair it was computed
  for (`HEDGE_RATIO_FOR = "USOIL|UKOIL"`) so a stale β from the previous
  instrument cannot silently define the spread. Port `statarb/hedgeratio.py`:
  same underlying → β = 1; different instruments on the **same price scale**
  (WTI 83 vs Brent 86) → β = 1 and the spread is the differential; different
  scale (silver 65 vs gold 4,352) → the price ratio.

---

## 3. The ladder — UI specification

Model the screenshot the operator supplied (a CQG/TT inter-product ladder).
Each ladder is one card/panel and owns one pair.

### Header

Pair name (`CL Oct26 − BZ Oct26`), the account pair it routes to, the
**Increment** (spread ticks per row), quantity presets, and the order-type
controls: **Limit / Market**, **Day / GTC**, and a default quantity.

### Columns, left to right

| Column | Contents |
|---|---|
| **Work (sell)** | Your resting SELL-the-spread orders at that level, as a quantity. |
| **Bid qty** (blue) | Depth or a marker on the buy side. **Clicking here places a BUY-the-spread order at that price.** |
| **Price** | The spread level. This is the centre column and the whole point. |
| **Ask qty** (red) | **Clicking here places a SELL-the-spread order at that price.** |
| **Work (buy)** | Your resting BUY-the-spread orders at that level. |
| **LTQ / last** | Last traded quantity at that level (your own fills — MT5 gives you no spread tape). |

### Behaviour

- **Every click adds one order of the current default quantity at that level.**
  Click three times at 58.40 → three separate working orders of 1 each, not one
  order of 3. They must be individually cancellable (right-click / click the
  Work cell to pull one).
- The ladder **scrolls with the market** but must have a **lock/centre** control.
  A ladder that re-centres under a click is how a trader clicks the wrong price.
- Rows are generated from the current spread ± N increments. `Increment` is per
  pair and configurable — for a gold basis around 59 with σ 0.3, an increment of
  0.01 is right; for WTI/Brent it is not.
- **Prices update on every coordinator poll** (the existing system polls at
  0.3 s and the browser refreshes at ~300 ms). Do not let the ladder lag the
  price card beside it — that mistake was made once already, and the panel that
  lagged was the one that places orders.
- **Cancel controls**: `CXL B` (all buys on this ladder), `CXL S` (all sells),
  `CXL All` (everything on this ladder), and a global cancel-all across every
  ladder. The global one confirms; the per-ladder ones do not need to.
- Show, per ladder: **net position in spreads**, average entry spread, live
  P&L in dollars (see §5), and both legs' fills.
- **Colour convention is fixed and global**: **bid green, ask red**, everywhere
  they appear. A price must not change colour depending on which table it sits
  in.
- Define your own CSS for anything load-bearing (colours, grid, borders). A
  blocked CDN has already taken this UI down once; the dialog that reports
  "could not save" must work when the network is what failed.

---

## 4. The hard part: a resting spread order is SYNTHETIC

**MT5 has no spread instrument, and certainly not one spanning two accounts.**
There is nothing to rest an order on. A working order on the ladder is a
**synthetic order held by the coordinator**, and how it becomes two real orders
is the central engineering problem of the whole product.

### The fire rule — DECIDED: quote one leg, cross the other

> **A synthetic working order is backed by a REAL pending limit on ONE leg (the
> "quoting" leg). When that limit fills, the other leg is crossed at market
> immediately.**

This earns one leg's bid-ask and pays the other's, instead of paying both. The
operator chose it knowingly, and it buys that edge by taking on legging risk.
Build it properly.

**The quoting leg's price is NOT fixed.** This is the part that is easy to get
wrong. The trader clicked a *spread* level, but the pending order lives on a
*leg*. The price that produces the clicked spread depends on where the other leg
is right now — so it moves whenever the other leg moves:

```
quoting leg = B, SELL the spread at S :   P_B = S + β × spot_ask
quoting leg = B, BUY  the spread at S :   P_B = S + β × spot_bid
quoting leg = A, SELL the spread at S :   P_A = (fut_bid − S) / β      (buy A)
quoting leg = A, BUY  the spread at S :   P_A = (fut_ask − S) / β      (sell A)
```

So the pending order must be **continuously re-priced by
`TRADE_ACTION_MODIFY`** to hold the implied spread at the level the trader
clicked. Place it once, then chase the *other* leg for the order's whole life.
That is the mechanism; everything below is what it costs.

- **Re-peg on a dead band, not on every tick.** Every MODIFY loses queue
  position, so re-pricing three times a second guarantees you are never at the
  front of a queue — which defeats the entire point of quoting. Only re-peg when
  the implied spread has drifted more than a threshold (default: one ladder
  increment). Make it a setting and log how often it fires.
- **Re-peg by MODIFY, never cancel-and-replace.** MODIFY keeps one ticket for
  the order's life. Cancel-and-replace needs ticket-history tracking across
  replacements and opens a window where the order does not exist.
- **Every MODIFY can race a fill.** MT5 can fill the pending between your read
  and your modify, and `positions_get` shows the fill (carrying the *order's*
  ticket) before deal history does. Check for that on every re-peg, or you will
  re-price an order that has already become a naked position.
- **`legal_limit_price` governs where the peg may go**, and the broker's
  `trade_stops_level` can make the required price **unreachable**. When it does,
  say so on the ladder in words — "this level needs a peg 3 points inside the
  broker's stops level; it cannot rest here" — not in a log file.
- **Which leg quotes is a per-pair setting.** Default it to the leg with the
  **wider bid-ask** (that is the spread you are earning), and show both legs'
  measured widths on the ladder so the choice is made from measurement. Note the
  tension honestly: the wider leg is usually the less liquid one, where you queue
  longest and fill least.
- **Aggregate the real pendings, track the synthetics separately.** Three clicks
  at 58.40 are three synthetic orders, individually cancellable — but they should
  become **one** real pending at the summed size, not three. Cancelling one
  synthetic re-sizes the real pending; cancelling the last one pulls it.
- **A pending can fill PARTIALLY.** Hedge to what actually filled, not to what
  rested.

### The naked window is now real — bound it hard

Between the quoting leg filling and the crossing leg landing, **you hold one leg
of a spread**. That is an outright position in gold or oil, not a basis trade.
It is the price of the fire rule, and it must be bounded by construction:

1. **The crossing order goes IMMEDIATELY on fill notification — not after a
   wait.** The deadline below is a *failure-escalation* window, not a patience
   window. There is nothing to be patient for: the hedge must go on.
2. **DECIDED — the deadline is 2.0 seconds, and it CROSSES; it does not
   unwind.** Measured in the existing system, a market order round-trips in
   **24 ms**, so 2 s is ~80× headroom and only fires on a real fault. Within it,
   retry the market order across the allowed filling modes (10030) and re-read.
3. **Only a REJECTED crossing leg triggers an unwind** — `10027 AutoTrading
   disabled`, insufficient margin, symbol not tradable. Slow is not rejected. On
   a genuine rejection: unwind the quoting leg **by ticket** (see below), log
   CRITICAL, name the broker's own words on the ladder, and **pull every other
   working order on that pair** — if the crossing account cannot trade, none of
   the remaining synthetics can complete either.
4. **Log the elapsed time from fill to hedge-on, on every single fill**, and
   surface it in the UI. It is the number that tells the operator whether 2.0 s
   is right, and it is exactly what was missing when the 13.96 s entry happened.

### The worked example — why this section exists

In the existing system, one live manual entry showed **+0.4700 of slippage
(−$9.40)** on a 0.02-lot gold pair:

- The executor rested a limit on leg A, re-pegging every 2 s, `LIMIT_TIMEOUT_SEC
  = 15.0`, and only sent leg B **after leg A filled**.
- Spot was falling (4623.91 → 4620.19). The peg chased the market *away from
  itself* — it was pegged to leg A's own book, not to the spread — never filled,
  timed out at 15 s, and crossed.
- **13.96 seconds** from click to pair-on. The comparable market entry: **24 ms**.

Two distinct faults, and the new design must not reproduce either. The peg was
anchored on the wrong thing (§4's re-pricing formula fixes that), and the
timeout was a patience window with no ceiling on the drift it permitted (the
2 s escalation fixes that).

### Two more rules, ported

- **Pre-check BOTH legs before resting EITHER.** `PairExecutor._precheck_pair`
  verifies both symbols' minimum volumes and steps up front. A pair whose child
  order is under either leg's minimum must be refused before any money moves —
  and on a quoting design, before anything rests at the broker.
- **Partial-fill policy.** Keep the matched piece only if it is at least
  `MIN_MATCHED_FRACTION` (0.4) of the clip; otherwise unwind all of it. Ported
  from `pair_executor.py`, where it is tested.

### The most dangerous consequence of quoting: a pending outlives the process

A synthetic order lives in our memory. **The real pending backing it lives at
the broker**, and it does not care whether our process is running. If we die
with pendings resting, they can fill — unhedged, unwatched, with nobody to cross
the other leg.

- **On shutdown: sweep and cancel every one of our pendings on both accounts,
  before anything else.** Verify the cancels. A pending we failed to cancel is a
  CRITICAL line, not a warning.
- **On startup: sweep before doing anything at all.** Any of our pendings still
  resting are from a previous life and must be cancelled, and any that *filled*
  while we were down is an orphan position — hand it to the reconciler (§7) and
  say so loudly on the ladder.
- **Magic-number scope every sweep**, so the trader's own terminal orders are
  never touched.

### The unwind must CLOSE, not offset

**These accounts are HEDGING mode.** A plain opposite market order does **not**
close a position — it opens a second, offsetting one. This has already happened
live: a failed hedge triggered an "unwind" that left *two* live positions on the
spot account, which the reconciler then found as orphans 60 seconds later and
cleared at the cost of two more round trips.

So, on **every** path out of the executor — rejected limit, no tick to peg on,
timeout, unwind, close button, cancel-with-a-leaked-fill:

- close by **position ticket** (`position=<ticket>` on the request),
- take the volume from **the broker's book**, not from what you sent (a ticket
  may be partly closed already, and asking to close more than is there fails the
  whole request),
- skip a ticket MT5 no longer lists — it is already gone, not an error,
- when unwinding only *part* of a leg, walk the tickets **newest first**,
- keep the plain opposite order only as an explicit **fallback** for a netting
  account or a ticket the broker refuses, and say so in the log.

---

## 5. Positions, marks and money

- **Mark every position at the touches it would actually CLOSE at.**
  `marketdata.closing_prices(md, side)` gives them: a long spot leg at the bid,
  a short futures leg at the ask, and the mirror. A mid mark shows a profit that
  cannot be taken.
- Consequence, and it is correct: **a position shows a loss the instant it
  opens**, equal to one round turn of both legs' bid-ask. That is what closing
  immediately would cost. Say so in the UI rather than hiding it.
- **Do not charge the crossing twice.** A mark taken at the closing touch on a
  position entered at a real fill has already paid both crossings — they are in
  the two prices. The only fee still to subtract is **commission**
  (`costs.cost_parts` / `exits.mark_fees`). Subtracting the full round trip
  again is the bid-ask twice.
- Price each leg in **its own contract size**. `update_position_pnl`,
  `realized_pnl_from_fills` and `close_position` all take `contract_b`.
- **P&L must agree with MT5's own.** It does, once marked at the closing touch.
- Per-position, per-ladder and per-account P&L, all reconciling to the same
  total.

### Slippage, measured properly

Port `statarb/slippage.py`. Score every fill against **the executable touch at
the moment the order was decided**, not the mid:

```
EXPECTED 55.9300   FILLED 55.8000   SLIPPAGE +0.1300
```

Positive is a cost, always, including on exits (a short *buys* the spread back,
so the sign flips between entry and exit — getting that backwards reports every
exit's cost as a gain). **Unmeasured is not zero** — render "—". Add the
**elapsed time from click to both legs on**; on a ladder that is the number that
explains a bad fill.

---

## 6. MT5 order mechanics — the lessons that cost money

All of these are already handled in `statarb/broker.py`. Port them.

- **10015 Invalid price.** A limit price must (a) come from a *fresh* tick,
  (b) be rounded to `trade_tick_size`, (c) sit strictly inside the book
  (BUY_LIMIT < ask, SELL_LIMIT > bid), **and** (d) be at least
  `trade_stops_level` **points** away from the market. CFI sets a stops level on
  the energy symbols and leaves it 0 on gold, so this bug is invisible until the
  instrument changes. `legal_limit_price` enforces both rules **and returns what
  it had to move and why**.
- **Filling modes.** Choose from the `symbol_info.filling_mode` bitmask
  (FOK = 1, IOC = 2, else RETURN). Hardcoding IOC means every close comes back
  `10030 Unsupported filling mode` on a FOK-only broker — the engine could open
  and never exit. Retry across the allowed modes on 10030 only.
- **`10027 AutoTrading disabled by client`** — the Algo Trading button is off in
  *that* terminal. Detect it per account and say it in words on the screen.
- **Deal history lags.** A zero-fill read immediately after a cancel must be
  re-read before it is believed. `positions_get` shows a filled pending order
  (carrying the *order's* ticket) before deal history does — so a cancel that
  actually filled must be caught by checking positions, or you believe the leg
  is flat while a naked position sits there.
- **Re-peg by `TRADE_ACTION_MODIFY`, not cancel-and-replace.** MODIFY keeps one
  ticket for the order's life; cancel-and-replace needs ticket-history tracking
  across replacements. Do not regress to it.
- **Sweep orphan pending orders.** Timeouts and failed cancels leave pendings
  that eventually fill as untracked naked positions. Sweep **all** of our
  pendings on both symbols before every new execution.
- **Verify every ticket out of MT5.** `verify_ticket` reads it back from
  positions → deal history → order history, retried because history lags. Log
  `[MT5 CONFIRMED]` / `[MT5 NOT CONFIRMED]` with deal and order ids. On a
  ladder, the trader must be able to see that the click reached the exchange.
- **Stamp the MT5 `comment`** with the source (`LADDER`, `MANUAL`, `CXL`, …) and
  a short uuid, and read it back in the order log. It is how you tell your own
  orders from the trader's terminal clicks.
- **Magic number** scopes every position/pending query to this application.

---

## 7. Reconciliation — assume you will lose track

Port `statarb/reconcile.py`, running every ~20 s in LIVE:

- **Orphans** (at the broker, not in our book) → 3 strikes, then auto-close by
  ticket, booked to an `untracked_closes` ledger the operator can read.
- **Ghosts** (in our book, not at the broker) → 3 strikes, then force-clear.
- **P&L on an orphan close must multiply by CONTRACT SIZE.** This was wrong for
  months and booked four closes at **1%** of what they cost ($0.81 where the
  answer was $81.10). An unknown symbol gets 1.0 **and says so**.
- After `CLOSE_ATTEMPTS` (3) failed closes, escalate **once** with "CLOSE IT BY
  HAND" and stop hammering the broker.
- **A close that did not go through leaves the position OPEN and ACTIVE.**
  Setting an ERROR/CLOSING status removes it from every ACTIVE-filtered lookup —
  the exit loop, the health block, the ladder, *and the reconciler's known-ticket
  set* — so the money sits at the broker while the UI reads flat. Recovered
  positions come back **ACTIVE**, and that promotion is persisted.
- Restart: recover open positions from the state table and **reconcile before
  trading**.

---

## 8. Guards on the price itself

Two independent ways a level can be a lie. Both already exist in
`marketdata.py`; port both.

- **`QuoteAgeTracker` / `stale_quote`** — a leg that has stopped ticking. Measure
  on `time.monotonic()` and the quote's own identity, **never** on
  `tick.time` (that conflates the broker's clock offset with staleness).
  Unknown is neither fresh nor stale. A pair is only as good as its **worse**
  leg — a combined "108 quotes/min" reads healthy while one leg is frozen.
- **`SpreadJumpTracker`** — both legs ticking hard but momentarily out of step,
  printing a spread level neither book is offering. Measure the change *between
  quotes* against the spread's own **level** sigma. A disturbance jumps twice —
  out and back — so a jump makes the level unusable until the series has been
  quiet for a settle period, not for one quote. This one cost a trade **$20.40**:
  a target fired on a print 8σ away that was gone within seconds.

**On a manual ladder the asymmetry is different from the algo's** — decide it
deliberately and say so in the UI:

- A **synthetic working order** must not fire on a stale or jumped print.
  Withhold it; the level is still there when the quote refreshes, and if it is
  not then it was never offered.
- A **click** is the trader looking at the screen. Do not silently swallow it —
  but **do** show the guard's state on the ladder (a badge: feed OK / oldest leg
  2.3 s / desynced) so they know what they are clicking into.
- Never let a guard prevent a **close**. A trade must always be closable.

---

## 9. Multiple ladders, two accounts

- Config holds a **list of pairs**, each with: key, Leg A symbol + account, Leg B
  symbol + account, β (stamped), contract sizes (read from MT5), tick increment,
  default quantity, enabled flag.
- Every ladder shares the **same two leg runners**. The coordinator loops over
  enabled pairs each poll. One MT5 connection per account, serialised behind the
  runner's lock — that is exactly what it is for.
- **Symbol resolution failures must list what the account actually offers**
  (`find_symbols`), and say plainly when nothing matches — that account is
  probably the wrong leg. Brokers spell gold `XAUUSD`, `GOLD`, `XAUUSD.r`.
- **Watch the poll budget.** Each pair costs two `tick` round-trips per poll;
  `account_info` is an IPC round trip and must be **cached** (~5 s), not fetched
  three times a second. Measure and publish the achieved loop interval so "is
  the engine or the browser slow?" is answerable rather than guessed at.
- Pairs can be **created, renamed, disabled and deleted** from the UI. The
  existing system shipped for months where a pair could only ever be *created* —
  and a leftover row is one resolving symbol away from a second live position on
  the same underlying. Refuse rename/disable/delete with a **409** while that
  pair has an open position. Route on `<path:key>`, not `<string:key>` — the
  pair that most needs deleting has a slash in its key.
- Per-ladder risk is the trader's, but keep a **global kill**: cancel every
  working order and (on confirm) flatten every position, across every ladder.

---

## 10. Config, secrets, persistence

- **Credentials live in `.env`, gitignored, referenced from `config.json` via
  `password_env`. Never in code, never in config, never in chat, never in a log
  line.** Sanitise generated env keys — an account named `Ut 2` produced
  `MT5_PASSWORD_UT 2`, a key with a space, which dotenv cannot parse, so the
  password silently never loaded. Quote values so passwords with spaces or `#`
  survive.
- Secrets are **UI-managed**. The operator never edits files.
- **Config writes go through a tmp file and `os.replace`.** A plain
  `open(path, 'w')` truncates, and a reader in that window sees half a config —
  which, in front of a read-modify-write save, wrote an **empty** config back and
  deleted every account. A tolerant reader is precisely wrong there: distinguish
  *missing* (first run) from *present-but-broken* (fall back to `.bak`, and
  **raise** if there is none). Refuse a save that would drop a non-empty
  `accounts`, `leg_accounts` or `pairs` unless the caller explicitly means it.
- Structural fields (accounts, symbols, β, contract sizes) require a **restart**
  and must say so. Display and comfort settings hot-apply. Do not cry "restart"
  on every save — compare only what the launcher actually reads at startup, or
  the operator learns to ignore the line that matters.
- SQLite with **WAL** and a **30 s busy timeout**, through one `_connect()`. The
  web process reads the same file the coordinator writes, and the default raises
  `database is locked` immediately — which once stopped the exit loop for 30
  seconds with a live position open.
- One-shot commands passed between processes through a file must have their
  timestamps **primed at startup**. In the existing system every watermark
  initialised to 0, so on restart the engine replayed the *entire history* of
  commands in half a second — opening an unintended live position and placing
  real test orders. Persistent *state* (which ladders are open) is different from
  a *command* (place this order) and must be handled differently.
- A launcher (`start.py` equivalent) that spawns both runners, the coordinator
  and the web app, and restarts a crashed child with backoff. It must **not**
  `terminate()` a child that is running its own shutdown — wait for it.

---

## 11. UI principles that are not negotiable

These are all rules the existing system learned by getting them wrong.

- **Never send the operator to a log for a decision the engine already made.**
  If an order was refused, the panel says the broker's own words
  (`10027 AutoTrading disabled by client`), not "check the log". This
  anti-pattern was fixed **twice** in that repo.
- **A warning nobody can act on is not a fix.** When the system can name the
  wrong field and the right value, offer a one-click correction — but keep it a
  click. Never silently correct an input.
- **Unmeasured is not zero.** Render "—". A zero reads as a measurement.
- **A number with no unit is not checkable.** `× 2` meant "2 ounces" and was read
  as "doubled". Show the derivation: `56.5400 × 2 units (0.02 lots × 100)`.
- **Internal consistency is not correctness.** Four rows agreeing to the cent
  proves they share an anchor, not that the anchor is right. Anchor spread levels
  on the **executed fill**, not on the mid the decision was taken at.
- **Log on state CHANGE, not on a clock** — and make a change wait until it
  *holds* before printing, counting the ones that did not hold. A gate sitting on
  its own threshold turns an event-driven log back into a flood. Keep a slow
  heartbeat purely to prove the engine is alive, and have it state the **live**
  verdict. Drop 2xx from the web access log and keep 4xx/5xx.
- Errors in toasts **do not auto-hide**. A failure that vanishes in 3 s is one
  the operator misses.
- No native `confirm()` / `alert()` / `prompt()`. One shared modal, and a test
  that fails the build if they come back.
- **Verify the UI in a real browser.** Python tests cannot see a
  temporal-dead-zone `ReferenceError` that aborts an entire script block and
  silently unregisters the Save handler — which is exactly what happened, and
  clicking Save did a native form submit that reset the page. Drive Chromium
  under Playwright and read `pageerror`. Cross-check every number input's
  `min`/`max`/`step` against the shipped default: two of them rejected the
  engine's own defaults, so Chrome refused to submit and fired no event at all.

---

## 12. Shutdown

If positions are open, **ask before closing anything**:

```
  SHUTTING DOWN with 2 OPEN POSITIONS
  ...
  y  close them now, at market
  N  leave them open at the broker — no engine, until you start up again
```

An unanswered prompt means **NO**. Closing at market is irreversible; a position
left open is recovered on the next start. No tty, a timeout, or a second Ctrl+C
all resolve the same way, and the reader runs on a daemon thread so an
unanswered prompt cannot hold the process open. Make it a setting
(`ask` / `always` / `never`), defaulting to `ask`, and put it in the UI.

---

## 13. Testing

- **`pytest tests/ -q` must pass before any commit, and LIVE mode must never be
  run without it.** The existing repo has ~1,470 tests and needs no MT5 to run
  them.
- Everything is faked: a `FakeBroker` / `FakeLeg` with a real hedging-mode
  `book` dict, recorded orders, recorded comments, and injectable failures.
  MetaTrader5 is Windows-only; the whole system must be testable on Linux.
- Write the **regression test as the live sequence**, not as a restatement of the
  formula. And give each guard test a **control** that turns the guard off and
  asserts the opposite outcome — otherwise the test can pass for an unrelated
  reason.
- Tests to write on day one:
  - a pure β move nets to zero, parameterised over several contract-size and β
    combinations (this fails under the naive `L_B = L_A × β`);
  - enter at `short_spread`, exit at `long_spread`, nothing moves → the pair is
    down **exactly** the modelled round trip, no more;
  - a failed hedge leaves the leg-A book **empty**, not "flat by offsetting",
    and no opposite-side order was ever sent;
  - one leg frozen while the other ticks the spread through a level → the
    synthetic does **not** fire; the leg quotes again → it fires at once;
  - orphan close P&L includes the contract size;
  - three clicks at one price produce three individually cancellable orders.

---

## 14. Suggested build order

1. Config + `.env` + accounts/pairs model; the clash refusals; the launcher.
2. Leg runners + IPC + `LocalLeg`/`RemoteLeg` (port).
3. Coordinator loop: fuse two feeds → `mid/short/long` spread per pair, publish
   a status file. Verify with a read-only ladder that just prints prices.
4. Market orders both legs, ticket-based closes, verify-out-of-MT5, order log.
   **Get flat-and-back-to-flat exactly right before anything rests.**
5. The ladder UI: rows, clicks, working-order model, cancels.
6. Synthetic working orders: the fire rule, the legging deadline, the unwind.
7. Limit-first / passive mode, `legal_limit_price`, re-peg by MODIFY, sweep.
8. Reconciliation, restart recovery, shutdown prompt.
9. Staleness + jump guards, slippage reporting, per-ladder P&L.
10. Multiple concurrent ladders; poll budget; caching.

Ship 1–4 as a working, tested slice before starting 5. A ladder that can place
an order it cannot reliably close is worse than no ladder.

---

## 15. Ask the operator these before building

1. **Fire rule default** — should a synthetic working order cross both legs at
   market when the spread trades through (fast, pays both bid-asks), or rest a
   real limit on one leg and cross the other on fill (earns one spread, owns the
   legging risk)?
2. **Legging deadline** — if leg B is not on within N seconds of leg A filling,
   cross B at market or unwind A? And what is N?
3. **Increment per pair** — what spread tick should each ladder step by?
4. **Default click quantity** — in spreads, or in leg-A lots?
5. **Does a click ever cross immediately** (a market order at the touch), or is
   every click a working order?
6. **Should working orders survive a restart?** (The existing system's answer for
   anything that places orders by itself was *no* — a loop left on at 17:00 must
   not resume at 02:00 with nobody watching. A ladder may want the opposite.
   It is their call, and it must be a deliberate one.)
7. **Day / GTC** — does a working order die at the session end?
8. **Overnight** — is holding a spread over the swap charge allowed per ladder?

---

## Hard rules — repeat back before you start

- **Never attach broker-side stops to individual legs.** One leg stopping alone
  converts the hedge into a naked position.
- **Closes target position tickets.** These accounts are hedging mode; an
  opposite order opens a second position.
- **Credentials live only in `.env`.** Never in code, config, chat, or logs.
- **Never run LIVE without `pytest tests/` passing.**
- **The spread is `Leg B − β × Leg A`**, built from the **mid of the book**.
  Levels and triggers read the **executable** side; a position reads the opposite
  executable side to close.
- **`L_B = L_A · C_A / (β · C_B)`**, and `k = L_B · C_B` is the one multiplier.
- **No strategy.** No z-score, no signals, no automatic entries, no automatic
  exits. The trader decides; the system executes and keeps the books honest.
