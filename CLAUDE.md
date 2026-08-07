# Statistical Arbitrage Trade — Project Memory

Basis/stat-arb trading system on MetaTrader 5 (Gold & Silver, spot vs
futures, z-score on the spread futures - HEDGE_RATIO * spot).

## Goal

Run Leg A (spot) on Account A and Leg B (futures) on Account B —
different brokers or same broker — streaming prices from both and
routing each leg's order to its own account.

## Hard constraints — do not re-litigate

- **The MetaTrader5 Python package holds ONE global connection per
  process.** A second `mt5.initialize()` replaces the first. True
  simultaneous two-account streaming uses **one leg runner process
  per account** (`run_leg.py`, own terminal via `path=`) plus the
  coordinator (`run_coordinator.py`) that fuses prices and routes
  orders over localhost TCP (built 2026-07). Same-account setups run
  the coordinator alone (in-process LocalLeg).
- **Scale requirements (owner, 2026-07):** ~500 lots of gold notional
  per day (`DAILY_LOT_TARGET` — a throughput target, NOT a cap; never
  reject on it). Each trade = 50-lot spot clip + 50-lot futures hedge
  (`CLIP_LOTS`), sliced into 10-lot child orders (`SLICE_LOTS`).
  Futures hedge is sized to actual spot FILL; partial-fill policies
  live in `pair_executor.py` and are covered by tests.
- **Never run LIVE mode without the tests passing** (`pytest tests/`).
  The money paths (entry pair, close pair, hedge unwind, stop loss)
  are covered by tests using a FakeBroker — no MT5 needed.
- Credentials live in `.env` (gitignored), referenced from
  config.json via `password_env`. Never in code, config, or chat.

## Architecture (as of 2026-07, z-score strategy build)

```
start.py / start.bat    ONE-command launcher: spawns leg runners per
                        topology + coordinator (+ --yes for live) +
                        dashboard, watchdog-restarts children.
                        Non-technical operator entry point.
main.py                 single-account entry point (legacy flow)
run_leg.py              leg runner: one process per MT5 account
run_coordinator.py      coordinator: fuses legs, signals, routes orders
run_watchdog.py         relaunches coordinator on crash (backoff)
check_mt5.py            STANDALONE connection checker (no leg runners,
                        no coordinator): connects each account in turn,
                        decodes MT5's init errors (-10003 IPC, -6 auth)
                        and order retcodes (10027 algo trading off) into
                        fixes; --order places a min-lot round trip and
                        confirms it in the terminal's own history
statarb/
  config.py             file-based config: accounts/.env, TRADING,
                        SIGNALS, COSTS, EXITS, RECONCILE, EXECUTION
  broker.py             BrokerSession — ONLY module importing MetaTrader5;
                        market+limit orders, modify/cancel, fill state via
                        deal history, close-by-ticket, magic-scoped positions
  legs.py               LocalLeg / RemoteLeg — one interface for both
  ipc.py                JSON-lines over localhost TCP
  leg_runner.py         LegServer — serves ticks/orders for one account
  pair_executor.py      limit-first clip execution: peg -> MODIFY repeg ->
                        cancel+verify -> cross; hedge match; matched floor;
                        ticket-based closes (hedging mode)
  coordinator.py        Coordinator + PaperExecutor (paper = same lifecycle)
  spread.py             SpreadStats: frozen mu/sigma/z on the spread,
                        AR(1) half-life, trend slope
  signals.py            SignalGenerator (legacy) + ZSignalGenerator (gates:
                        ceiling, trend, cooldowns, z-reset, edge filter)
  costs.py              round-trip cost model + edge filter
  fairvalue.py          theoretical spread by pair type —
                        DISPLAY ONLY, never a signal input
  exits.py              ExitLadder — dollar levels frozen at entry
  sizing.py             lots per leg: notional-anchored or clip-
                        anchored, and the hedge derived from BOTH
                        contract sizes (NOT lots x beta)
  slippage.py           decision-to-fill: what the signal saw (mid),
                        what it was executable at (touch), what MT5
                        filled — crossing and slippage kept APART
  reconcile.py          orphan close / ghost clear, 3-strike, ledger
  notify.py             Telegram alerts + /status /positions /pnl
                        (background thread, never blocks the loop;
                        token/chat from .env only)
  webapp.py             read-only Flask dashboard (own process, reads
                        SQLite + runtime_status.json)
  marketdata.py         the spread (futures - HEDGE_RATIO * spot),
                        shared by both loops
  models.py             enums, Trade (+position_tickets), Position (+to/from_dict)
  execution.py          OrderManager — single-account entry/close pairs
  positions.py          PositionManager — lifecycle, per-leg realized P&L
                        to the cent, crash-safe state, restart recovery
  risk.py               RiskManager — hard limits, circuit breakers,
                        streak reducer, lots-today tracking
  performance.py        PerformanceTracker
  database.py           DataLogger (SQLite): trades, positions, market data,
                        position_state, untracked_closes, trade_review
  system.py             AlgorithmicTradingSystem — single-account loop
tests/                  437 tests, all fakes, no MT5 (runs anywhere)
legacy/                 original monolith, superseded — do not extend
```

## Strategy (decided 2026-07; spec v2 applied 2026-07)

- **The spread is `futures - HEDGE_RATIO * spot`** (owner, 2026-08-06:
  "Leg B - (Hedge Ratio) * Leg A"). Nothing else: no carry term, no
  swap cost, no dependence on the futures expiry. An earlier build
  subtracted a swap-implied carry; that made the dashboard number
  impossible to reconcile against the two prices beside it and tied the
  whole series to a swap figure nobody could verify. The drift the
  carry was meant to remove is real but months-long, while the window
  is hours — ~0.03 across a 2h window on a 59-point gold basis, far
  below the noise z measures. Because HEDGE_RATIO now defines the
  series, it stays structural: blocked while a position is open,
  restart to change.
- **Pair type + reference fair value** (owner, 2026-08-06). An asset
  declares `pair_type`: SPOT_FUTURE / FUTURE_FUTURE (basis pairs, carry
  ties the legs) or RELATED (WTI vs Brent — no arbitrage between them).
  For basis pairs `statarb/fairvalue.py` computes the THEORETICAL
  spread (spot compounded at `risk_free_rate` to expiry; for a calendar
  spread, over the gap between the two expiries) and the dashboard
  shows it under the spread card badged "ref only". It answers "is the
  mean z is anchored on anywhere near what carry says?" — a big gap
  usually means the contract month/multiplier/contract size is wrong,
  NOT that the trade is good. **It is reference only and MUST stay
  that way**: signals.py / exits.py / spread.py / costs.py /
  pair_executor.py must never read it (structurally asserted in
  tests/test_fair_value.py), and it returns None rather than guessing
  when inputs are missing. Leg A's expiry (`spot_expiry`, for calendar
  spreads) is read from the terminal like Leg B's.
- Entries: z-score on that spread. ALL gates must pass: warm stats, |z| >=
  ENTRY_Z, |z| < MAX_ENTRY_Z (entry ceiling — its OWN knob, always
  active, keep the band >= 1 sigma wide; a z at 5+ is a momentum
  spike, their extreme entries went 0-for-3), trend filter, cooldowns,
  z-reset after stops, edge filter (capture >= 1.5x round-trip cost).
- Atomic pre-checks: BOTH legs' symbol minimums verified before
  placing EITHER order (PairExecutor._precheck_pair).
- Exits: DOLLAR levels frozen at entry from actual fills. Priority:
  DOLLAR_STOP (ungated; tighter of TP/RR, %-capital when LEVERAGE
  set, per-lot) > TAKE_PROFIT (sigma-fraction > %-capital > fixed $,
  cost floor) > gated reversion exit — the gate DEFERS to max-hold:
  floor decays to break-even past 1x max-hold, releases entirely past
  2x (deadlock fix: gate+max-hold+unreachable TP once held a fully
  reverted trade at +$1.19 over 2 cents, then bled to -$4.46) >
  MAX_HOLD (4x half-life, profit only, suppressed while z-progress
  >= 50% toward an EXISTING TP) > TIME_STOP hard clock at 3x max-hold
  regardless of P&L (the sideways loser's only exit) > Z_STOP,
  DEMOTED: off by default while a dollar stop is armed, auto-re-
  enabled when none is (a trade must always have a stop), would-have-
  fired occasions LOGGED for design scoring.
- Exit-path completeness rule: every (P&L, z, time) state must have a
  reachable exit — regression-tested in tests/test_exits.py.
- Tuning is data-driven: per-trade peak/trough net P&L with minutes-
  after-entry persist to trade_review; set the TP near the 60-70th
  percentile of peaks, max-hold near the median peak-minute of
  winners; verify measured win rate clears stop/(target+stop).
- Every close gets a deterministic outcome tag: TARGET_HIT /
  REVERSION_BANKED / TIME_EXIT / STOPPED_IN_TREND /
  STOPPED_AFTER_FULL_REVERSION (z came home, price never paid).
- Execution: limit-first (rest at peg, re-peg via TRADE_ACTION_MODIFY,
  timeout -> cancel+verify fills -> cross). Stops/unwinds ALWAYS
  market. Hedge leg gets short patience (HEDGE_TIMEOUT_SEC).
- Accounts are HEDGING mode (owner, 2026-07): closes MUST target
  position tickets; plain opposite orders would OPEN positions.
- Partial-fill policy: hedge sized to spot FILL; keep matched piece
  only if >= MIN_MATCHED_FRACTION (0.4) of clip, else unwind all.
- Reconciliation every 20s in LIVE: 3-strike orphan auto-close by
  ticket (booked to untracked_closes ledger, charged to breakers),
  3-strike ghost force-clear. Restart: recover open positions from
  position_state table, reconcile BEFORE trading.

## Hard rules (owner's spec, ported 2026-07)

- NEVER attach broker-side stops to individual legs — one leg stopping
  alone converts the hedge into a naked position.
- z is for ENTRIES; exits act on MONEY (in-trade z "reverts" as the
  rolling mean chases the spread without the price paying you).
- Audit modeled vs realized costs monthly (query trades vs COSTS
  config); alarm if modeled >= 2x realized — an inflated cost model
  silently blocks every good trade.
- Parameters (ENTRY_Z 3.0, STOP_Z 4.5, etc.) are STRUCTURE defaults —
  recalibrate against recorded gold basis data before LIVE.

## Live-tested MT5 lessons (ported 2026-07 from the June
## `claude/limit-orders-trade-exits-q0quM` branch) — don't reintroduce

That branch ran REAL orders; its commit log paid for these rules:

- Orphan pending orders accumulate after timeouts/failed cancels and
  eventually fill as untracked naked positions. Sweep ALL of our
  pending orders on both symbols before every new pair execution
  (`PairExecutor.sweep_stale_orders`).
- Invalid Price (10015): limit prices MUST come from a fresh tick,
  be rounded to `trade_tick_size`, and sit strictly inside the book
  (BUY_LIMIT < ask, SELL_LIMIT > bid) — clamp to bid/ask when an
  offset would cross.
- Some brokers reject RETURN filling on pending orders — choose the
  filling mode from `symbol_info.filling_mode` bitmask (FOK=1, IOC=2,
  else RETURN).
- Deal history lags briefly after a cancel: a zero-fill read right
  after TRADE_ACTION_REMOVE must be re-read before being believed.
- A pending limit with `position=<ticket>` CLOSES that position when
  it executes — non-urgent hedging-mode exits go limit-first (spread
  saved on every exit), escalating to a market ticket-close on
  timeout. Stops still never rest.
- Their cancel-and-replace re-peg needed order_ticket_history to
  track fills across replaced tickets; our MODIFY-based re-peg keeps
  one ticket for the order's life — do not regress to cancel/replace.

## W3 feature port (2026-08, from ajoxf/Stat_Arb_W3_Wsckt
## branch claude/busy-tesla-41ja8f) — status

Applied to the engine:
- Exits are BREAK-EVEN aware: TP/gate/max-hold act on NET = gross -
  round-trip cost ("profit % on top of BE"); dollar stop stays GROSS
  (stop = spread distance, not fees).
- Spread LEVELS frozen at entry for the in-position card: BE/EX/TP/SL
  as absolute spread values (ExitLadder.spread_levels).
- EXIT_MODE zscore|spread|hybrid (spread = crosses the mean frozen at
  entry). HARD_MAX_HOLD_MIN fixed-minutes clock (owner: ~90min, exit
  before the spread starts drifting) alongside HARD_TIME_STOP_MULT.
Web control panel (statarb/webapp.py, own process, file-bridge only):
- Dashboard: in-position card (BE/EX/TP/SL + level $ + gross/net P&L
  + peak/trough + max-hold bar + P&L progress bar), algo start/stop,
  manual market-close button, canvas spread/z charts.
- Settings: ALL config sections + MT5 broker/leg topology editor
  (3 topologies: 2 brokers / 1 broker 2 accounts / 1 account).
  Saves merge into config.json (back-fill — partial saves can't zero
  fields); coordinator hot-reloads safe sections every ~10s via
  AlgoTradingConfig.hot_apply; STRUCTURAL fields (accounts, legs,
  symbols, HEDGE_RATIO) are blocked -> restart; beta change with a
  position open returns 409 (W3 rule).
- Connectivity checklist (Exchanges page, statarb/diagnostics.py):
  per-account [Test] + [Diagnose] and a page-level Run Full
  Connectivity Check, ported from app.py's broker diagnose panel and
  widened for two accounts. Per leg: terminal attached, broker
  connected, logged in as the CONFIGURED login, algo trading on,
  trading permitted (investor password caught), hedging vs netting,
  leverage vs Settings, symbol found/visible/priced/tradable, order
  sizes against volume min-step-max, contract size vs configured
  lot_size, futures expiry. Pair: topology, account currency, both
  symbols distinct, HEDGE_RATIO vs the two contract sizes (this is
  CLAUDE.md open item 2, now automated), live basis, quote freshness.
  Read-only, so it runs with the algo trading; request via
  control.json {'diagnose': {...}} -> runtime_status.diagnostics.
- Symbols (Exchanges page): per-leg symbol with Find — searches THAT
  account's symbol list by name or description (brokers spell gold
  XAUUSD / GOLD / XAUUSD.r) via control.json {'diagnose':
  {'find_symbols': ...}} -> runtime_status.symbol_search. Saving
  writes config assets and says a launcher restart is needed
  (symbols are structural).
- Per-leg leverage: EXITS SPOT_LEVERAGE / FUT_LEVERAGE (Settings' Leg
  A/Leg B selectors, now up to 500x and actually persisted — they were
  dead controls). ExitLadder._capital_at_risk divides EACH leg's
  notional by ITS account's leverage; both fall back to LEVERAGE.
  MT5 leverage is broker-side — the config only mirrors it, and the
  checklist warns when they disagree.
- Full Order Test Suite (Exchanges page, statarb/scenarios.py): the
  40-scenario round-trip matrix ported from the old app.py — 18 LIMIT
  (6 order types x 3, third a cancel) + 18 MARKET (third a
  quick-close) + 4 partial-fill recoveries, every one a complete round
  trip at MINIMUM lot. Spot goes to the spot account and futures to
  the futures account (app.py had one connection and could place both
  itself). Spread scenarios ROLL BACK the first leg if the second
  fails — a test must never leave a naked position. A fill that leaks
  through a cancel FAILS the scenario. Runs INLINE in the coordinator
  loop (a RemoteLeg has one socket; a thread would interleave on it),
  requested via control.json {'scenario': {...}} and answered in
  runtime_status.scenario_result; /api/scenario-test writes the
  request and waits for that answer, /api/scenario-catalogue serves
  the table. Refused unless the algo is stopped and the book flat.
- Order proof (owner asked "how do we know orders reach MT5?"):
  BrokerSession.verify_ticket reads the ticket back OUT of MT5
  (positions -> deal history -> order history, retried because history
  lags a fill). Scenarios verify every open/close and FAIL when the
  terminal has no record; LIVE entries/exits log [MT5 CONFIRMED] /
  [MT5 NOT CONFIRMED] with deal+order ids; the order test adds an
  'MT5 record' check row. After any order the coordinator polls the
  order log immediately (interval=0) so the rows appear at once. The
  MT5 comment carries the source (BASIS_ARB / MANUAL / SCENARIO /
  ORDER_TEST) and the log's Source column reads it back.
- Exchange Order Log (dashboard): BOTH accounts' raw MT5 activity in
  one table, Account column first. Coordinator polls every leg's
  `order_log()` every 30s into the `broker_orders` table (webapp is a
  separate process and never touches MT5); filled deals + cancelled/
  rejected history orders + resting orders; manual terminal trades are
  included on purpose, `is_bot` marks ours. Resting rows are re-stated
  per poll (deleted for the accounts read that pass) so a filled order
  leaves no stale 'working' row; key is (account, order_id, deal_id)
  because ticket numbers are only unique per terminal. CSV export at
  /api/exchange-orders/csv.
- Analysis: outcome + edge-quality tiles (win rate, PF, expectancy,
  break-even WR, max DD, P70 peak, median peak-minute), outcome-tag
  counts, excursions table (peak/trough $ @ minutes, capture %),
  trade journal, untracked-close ledger.
- Coordinator control.json: {'algo_enabled', 'close': {...},
  'open': {...} (Manual Spread Trade — bypasses signal gates only,
  never risk/breakers), 'test': {...} (MT5 self-tests: connectivity,
  and order round-trips with REAL min-volume orders — requires algo
  off + flat book)} — algo off stops ENTRIES only; exits always run.
  MANUAL_CLOSE is urgent (market, by ticket).
- Secrets are UI-managed (owner: operator never edits files): the
  settings page writes passwords/Telegram token to .env via
  update_env_file; config.json NEVER holds a password. trading_mode
  (paper|live) is a config.json top-level read by start.py.
- Telegram is at W3 detail (owner: "exactly as in W3"): entry with
  full exit geometry rows, exit with per-leg prices, spread change,
  gross/fees/net, ANALYSIS block (outcome sentence, stop type,
  peak/trough@min, capture %, hold xmax, z path with range); commands
  /dashboard /status /positions /trades /balance /pnl /stats /shadow
  /eod /settings /set /pause /resume /closeall (+menu registration).
- SD-touch distribution (z crossings of ±1/2/3 sigma -> sd_touches
  table, chart+table on Analysis) and the shadow "what-if-held"
  tracker (statarb/shadow.py: after every close, marks the position
  virtually until TP/horizon; verdicts REVERTED_TO_TARGET /
  REVERTED_TO_BREAK_EVEN / KEPT_BLEEDING; aggregates suppressed
  below 5 completed) — both ported 2026-08.
Reversibility: tag v1-engine-baseline = pre-port engine; main stays
there until the owner approves this work. NOT yet ported from W3:
Hurst/velocity/trailing optional exits, auto-tuner, AI monitor,
per-leg maker/taker fee split, backtest suite.

## The window counter fell for two hours after every start (2026-08-07)

Operator: "What is this number? It keeps reducing and cant understand
it" — the "quotes in 2h window" tile sliding down from ~24,000.

Not just a labelling problem. `log_market_data` was called on EVERY
poll (3/sec) with no dedup, so market_data stored the same quote
hundreds of times. `_warm_start` then replayed those rows raw:

- The window filled with POLL duplicates instead of quote events, so
  the count started far too high and decayed for a full LOOKBACK_SEC
  as the duplicates aged out. That decay is what was visible.
- Worse, it silently undid the quote_id fix ACROSS RESTARTS. A window
  of repeats has a deflated variance — the same collapsed sigma that
  produced z = +53,026 on 2026-08-06. Sigma was poll-rate invariant
  live and NOT invariant through a restart.
- It also wrote ~288k rows per asset per day for no information.

Fixed on both sides: `Coordinator._log_quote` persists only when
quote_id changes, and `SpreadStats._collapse_repeats` drops CONSECUTIVE
identical spreads from seeded history (defence for databases already
full of poll rows; only consecutive, because a spread genuinely
revisiting a level is a real observation). Regression-tested: the same
quotes stored once each vs twenty times each give the same sigma.

**The counter is now GONE** (operator, 2026-08-07: "10,516 quotes in
window - not require"). It was a rolling OCCUPANCY, not a total, so it
legitimately fell whenever the market was quieter than a window ago and
read as data being lost — three attempts to word it and no decision
ever depended on it. What replaced it:

- the **readiness gates** line answers the question it stood in for
  (enough data to trust mu/sigma?) against the thresholds that actually
  gate trading, each capped so a met gate reads as met;
- the **feed rate** tile (`SpreadStats.quote_rate_per_min`) keeps the
  one genuinely diagnostic part, amber below ~6/min, because a thin
  feed is what collapses sigma;
- the progress bar shows a plain percentage, since the gate numbers
  live on the readiness line and a second running total up there only
  duplicated or contradicted it.

## Log volume (2026-08-07, operator: "Too many messages")

Three separate floods, all fixed:

- `ZSignalGenerator` logged every gate rejection on EVERY poll (3/sec).
  With the edge filter failing persistently — correct behaviour when
  sigma does not cover costs — that alone was ~10,000 identical lines
  an hour. Now one line when a gate STARTS blocking, one when the
  gates clear.
- The coordinator's status line ran on a fixed 10s clock. Now
  event-driven (warm / no-usable-z / signal change / halt), with a
  heartbeat at `TRADING.LOG_HEARTBEAT_SEC` (5 min) purely to prove the
  engine is alive.
- **werkzeug's access log**, which was the loudest: the dashboard
  polls half a dozen endpoints continuously, so the webapp wrote ~5
  lines a second, every one a 200 for a timer-driven request.
  `webapp._QuietAccessLog` (installed by `run_app`) drops 2xx/3xx and
  KEEPS 4xx/5xx — a request that failed is the reason to read the log.
  `/api/active-orders` was also polled at 1Hz against an endpoint that
  returns a constant `[]` (resting orders are not wired to it); now
  15s.

## A restart replayed every command ever sent (2026-08-07)

LIVE. The operator restarted the launcher and, inside half a second,
the engine re-ran the whole history of `control.json`:

```
18:00:14,089 - Manual trade ARMED: GOLD SELL_BASIS at spread 59.0000
18:00:14,091 - [TEST] connection ping: PASS
18:00:14,164 - [DIAGNOSE] PASS: 28 pass, 0 warn, 0 fail
18:00:14,208 - [SCENARIO] BUY_SPOT LIMIT normal: FAIL
18:00:14,493 - Reconciliation requested via web UI
18:00:14,516 - Manual trade TRIGGERED: spread 59.5400 reached 59.0000
18:00:14,524 - MANUAL SPREAD TRADE via web UI: GOLD SELL_BASIS 1.00 lots
```

That opened a **second unintended LIVE position** (POS_0001, tickets
102322325 / 102322326) and placed real min-lot SCENARIO orders on the
account. The armed order triggered instantly because the spread had
already travelled through its entry level while the process was down.

`control.json` is a PERSISTENT file and every command in it carries a
`ts`. All six watermarks (`_last_close_ts`, `_last_open_ts`,
`_last_test_ts`, `_last_diag_ts`, `_last_scenario_ts`,
`_last_recon_ts`) initialised to **0**, so the first `_read_control()`
of a fresh process saw every historical command as newer than "never
seen" and executed the lot in one pass.

`Coordinator._prime_control()` now reads the file at construction and
ADOPTS its timestamps without running anything, so only commands
written after the process starts are acted on. `algo_enabled` is
deliberately excluded — it is persistent STATE (an algo the operator
stopped must stay stopped across a restart), not a command. The
distinction is the whole fix: `_CONTROL_COMMANDS` lists exactly the
keys that EXECUTE something, and everything on that list is primed.

## A failed close made the engine believe it was flat (2026-08-07)

LIVE, the worst state this system can reach. A manual 1-lot gold pair
opened, then 37 seconds later:

```
Close ticket 102320968 failed: 10013 - Invalid request
Close ticket 102320969 failed: 10013 - Invalid request
CRITICAL INCOMPLETE CLOSE for POS_0001 — MANUAL INTERVENTION REQUIRED
Failed to close position: POS_0001
    exits    --      flat          <-- while 1 lot sat open at the broker
```

`close_position` set `PositionStatus.ERROR` on failure, and EVERY
lookup in PositionManager filters on ACTIVE. So the position vanished
from the exit loop, the health block, the position snapshot and the
dashboard, while the money was still on the account. Nothing would
ever have retried it; only the reconciler's orphan sweep would have
eventually force-closed it.

A close that did not go through leaves the position OPEN, so it now
stays ACTIVE and under management (`_close_failed`). The ladder
re-evaluates it next tick; `Coordinator._close_is_due` rate-limits
retries to CLOSE_RETRY_SEC (5s) so a refusing broker is not hammered
three times a second, and escalates CRITICAL once past
CLOSE_ESCALATE_AFTER (5). The health block's `exits` row goes FAILED
and names the broker error. Two tests that asserted the ERROR status
were encoding the bug and now assert the opposite.

## MAX_HOLD had no floor and a 3-second half-life closed a live trade

Same trade: `Exit plan (RESOLVED): TP=$215 STOP=$717 max_hold=0min
time_stop=1min`. The AR(1) fit runs on consecutive QUOTES, ~0.6s apart
at 104 quotes/min, so a spread that is mostly tick noise fits a
half-life of a few SECONDS: 4 x 3s = 12s max hold, 3x that = a 36s
hard time stop. The trade was force-exited 37 seconds after entry,
paying the full round trip with no chance of reaching its $215 target.

`EXITS.MIN_MAX_HOLD_SEC` (default 300s) floors it, and the binding
case logs a warning saying the half-life is measuring tick noise
rather than the spread. A genuine half-life is left alone.

## The log says what is working (2026-08-07, operator)

"I am more interested to get details on what is working and what is
not working." A repeated price tick never answered that. The status
line is now a HEALTH BLOCK — every subsystem, its verdict, and the
reason:

```
GOLD spot 4292.61 | fut 4351.55 | spread +58.94 — held up by: stats, sizing
    feed     OK      118 quotes/min
    stats    BLOCKED still collecting — 45 more minutes needed
    sizing   BLOCKED the hedge for 0.05 lots on leg A is under leg B's
                     0.1-lot minimum — needs at least $42,926 per leg
    entries  BLOCKED edge gate
    exits    --      flat
    risk     OK      no breaker, 0/500 lots today
```

`Coordinator._health` returns `[(subsystem, state, detail)]`; only the
STATE column decides whether to reprint, so a drifting sigma is not
news and the flood does not come back. Also published to
runtime_status as `health` for the UI.

Writing it exposed a real bug: `_sizing_plan` read symbol limits from
`executor._meta`, which **PaperExecutor does not have** — so in PAPER
the plan was computed with no volume step and no minimum at all
(fractional lots, and the minimum-notional guard never fired). It now
reads `leg.ensure_symbol` directly, cached per (leg, symbol) because a
RemoteLeg answers over IPC and this runs every poll.

## A manual trade's own target is what gets measured (2026-08-07)

Operator armed SHORT at 59.00 with TP 57 / SL 69, it triggered at
59.12, and then: `Exit plan not viable: cost floor $59 exceeds
plausible full reversion $15 — blocking entry`. Badge back to IDLE,
panel said "order was not filled".

`ExitLadder.build_plan` vetoes an entry whose target cannot clear the
round trip, measuring `plausible = |z| x sigma x oz` — a FULL reversion
of the current z. That is the edge filter's last line and it is right
for a SIGNAL entry. It is wrong for a hand-placed one: the operator's
target was 2.12 spread units away, worth $212 against $59 of cost. The
engine refused a trade nobody had proposed.

`build_plan(..., manual_target_usd=)` now measures the operator's own
distance when they have named a level. A manual target BELOW cost is
placed anyway with a loud warning — manual means manual, and they may
be hedging something the engine cannot see. Signal entries are
unchanged (regression-tested).

Also: `build_plan` sets `last_refusal` and the coordinator puts it in
`manual_note`, so the panel states the reason instead of "see the log
for the broker/engine reason" — which sent the operator to a file to
find a decision the engine had already made.

## Position sizing: notional per leg (2026-08-07, owner)

Owner: "the way we had it before in the W3 project is — User fixes the
notional value of the leg and the lots are calculated by itself and
after considering the leverage. The User saves the leg Notional Value
in the Settings page." Also: "if I am doing WTI and BRENT the
quantities after Hedge Ratio should be balanced."

`TRADING.SIZING_MODE` is `lots` (CLIP_LOTS, the original) or `notional`
(`NOTIONAL_PER_LEG_USD`, W3's model). Notional mode is the only one in
which "balanced" means anything across two instruments — a lot is a
different amount of money on each, so one CLIP_LOTS is $21m of gold and
$80k of oil. Lots = notional / (contract size x price), rounded DOWN to
a tradable step; margin = notional / that leg's leverage.

**HEDGE_RATIO is the price coefficient, NOT the lot ratio.** From
`spread = P_B - beta * P_A`, matching the pair's P&L to the spread move
requires `L_A*C_A = beta * L_B*C_B`, i.e.

    L_B = L_A * C_A / (beta * C_B)

The engine used to size the hedge as `L_B = L_A * beta`. Identical only
at beta 1 with equal contract sizes — which is the ONLY configuration
it has ever been run in (gold, 100 oz both legs), so it never showed.
Away from there it was wrong twice over: different contract sizes left
the pair unbalanced by their ratio, and **beta != 1 INVERTED it** (at
beta 2 the correct hedge is HALF the spot lots; the old rule traded
double, turning a should-be-zero move into a loss three times the
intended size). Regression test: a pure beta move must net to zero,
parameterised over four contract pairs x four betas — it fails under
the old rule for every case except the gold one.

The same `k = L_B * C_B` is what turns a spread move into dollars.
ExitLadder and slippage.py still use leg A's units (`spot_lots x
contract`); equal at beta 1 with equal contracts, off by beta otherwise
— NOT yet corrected, flagged here.

The dashboard's Position Sizing card states the lots per leg and the
residual imbalance (green within 2%, red beyond — rounding to a
tradable lot makes exact balance impossible, so the residual is stated
rather than implied away).

**Hedge balance is a CHOICE** (owner, 2026-08-07: "Want dollar-neutral
rather than unit-neutral - Yes"). `TRADING.HEDGE_MODE`:

- `units` (default) — `L_B*C_B = L_A*C_A / beta`. Equal quantity on
  both legs, so the pair's P&L IS the spread move the z-score is
  measured on. The legs' dollar values then differ by the basis, which
  is the thing being traded, not an imbalance. Right for spot/futures.
- `notional` — `L_B*C_B*P_B = L_A*C_A*P_A`. Equal money, so the pair
  trades the RETURN spread (`P&L = notional x (ret_A - ret_B)`). Right
  for two related instruments with no arbitrage tying them.

They coincide only when beta equals the live price ratio `P_B/P_A`.
Away from that, dollar-neutral sizing and a fixed HEDGE_RATIO disagree
and the position stops tracking the series the signal measures — the
plan publishes `dollar_neutral_beta` and `beta_gap_pct` and the
dashboard warns past 2%. Regression-tested both ways: a common
percentage move nets to zero under `notional` and does NOT under
`units`; a pure beta move nets to zero under `units`.

**Lots snap to the NEAREST tradable step, not down** (operator,
2026-08-07: "Why is Leg A notional being calculated incorrectly" —
$20,000 requested, $17,170 shown). The notional is a TARGET, not a
ceiling. One 0.01 gold lot is $4,293, so $20,000 is 0.0466 lots exactly
and flooring gave 0.04 = $17,170, 14% short; nearest gives 0.05 =
$21,463, 7% over. The rule is invisible at size (one step is 0.2% of
$2m) and dominant when small, which is why it went unnoticed.

The HEDGE still rounds DOWN, deliberately: leg B's step is 10x leg A's,
so nearest would turn a wanted 0.05 into 0.1 — a hedge twice the
position it hedges, net short the difference, and it would slip past
the minimum-notional guard below. Short is the recoverable error; the
executor already trims leg A to the matched size.

The card now states the target beside the result ("Asked for $20,000
per leg; the nearest tradable lot gives $21,463 (+7.3%, one lot =
$4,293)"), because showing only the achieved figure reads as a bug.

**The pair has a minimum tradable notional.** Live on CFI the spot
minimum is 0.01 lots and the futures minimum is 0.1 — ten times larger
— so $20,000 per leg gives 0.04 spot lots and a hedge of 0.04 against a
0.1 floor: leg B sizes to nothing and the pair reads 100% unbalanced.
`sizing.minimum_notional` computes the floor from both legs' minimums
and both contract sizes ($42,926 for gold at 4292/4351), the refusal
names it, and the Settings notional field shows it BEFORE saving.
Note leg B's STEP is also 10x leg A's, so above the floor the hedge
stays coarse: 0.23 spot vs 0.2 futures at $100k is 12% under-hedged
(flagged red), washing out to ~1% at $2m.

Follow-ups the operator found the same day:
- **"Cannot change clip sizing."** The inactive sizing field was hidden
  outright, so an operator who wanted to change the clip could not find
  it. Both fields are now always visible and editable; the mode only
  decides which one the engine USES, and each carries an IN USE /
  "not in use" badge. MAX_LOT_SIZE stays in lots whichever mode is
  active, so the notional preview now states the derived lots against
  the cap and goes red when the cap would block every entry.
- **Saving notional sizing did nothing.** `SIZING_MODE` and
  `NOTIONAL_PER_LEG_USD` were not in `HOT_TRADING_KEYS`, so they were
  written to config.json and then IGNORED until a restart — silently,
  because `hot_apply` only reports what it DID apply. The dashboard
  kept reading "Sized from 50 lots per leg". Any key the operator can
  change on the Settings page belongs in that tuple.
- **Leverage always read 100x.** The card read `leg_a_leverage` /
  `leg_b_leverage`; nothing ever published them, so it kept the value
  Jinja baked into the page at load time and never followed a config
  change. Now published from the engine's own sizing plan, along with
  each leg's margin, so a leverage change reaches the card without a
  browser reload.

Also fixed: `_isDerivative` / `_settingsIsDerivative` were W3's
hyphen-counting symbol test for a crypto venue. No MT5 symbol matches
it, so BOTH pages rendered every leg as unlevered cash and the
configured leverage never reached the margin figures — the capital
preview quoted the full notional as the requirement. They now take the
engine's per-leg margin.

## Slippage tracking (2026-08-07, owner asked for it)

Owner: "what your signal wanted to enter at and what the orders got
placed at on MT5". Nothing measured it before — `Trade.requested_price`
was written as NULL by the pair executor from the day it was built.

`statarb/slippage.py` scores every fill against THREE prices, because
the signal and the fill are not quoted on the same thing:

- **mid** — `compute_market_data` builds the spread from mids, so this
  is what the z-score, the edge filter and the exit ladder all saw.
- **quote** — the executable touch at that same instant (ask to buy,
  bid to sell). What the decision was actually worth.
- **fill** — what came back from MT5.

Split into `crossing` (mid->quote: quoted on the screen, known before
you trade, already charged by COSTS) and `slippage` (quote->fill: the
surprise). **Do not merge these into one number.** One figure would
make a wide spread look like bad execution and a fast market look like
a wide spread, and the fix for each is a different fix.

- Sign convention: POSITIVE IS A COST, everywhere. Negative slippage is
  price improvement and it does happen on limit fills, so the sign is
  kept rather than absolute-valued.
- `selling_the_spread(signal, closing)`: a short-spread position SELLS
  the spread to get in and BUYS it back to get out, so the same signal
  type flips sign between entry and exit. Getting this backwards
  reports every exit's cost as a gain.
- The reference is the market_data snapshot the DECISION was made on,
  not a fresh tick — a fresh tick would measure the last few
  milliseconds and miss the poll interval entirely.
- Invariant, regression-tested over both directions x entry/exit x
  three betas: `spread_slip == fut_slip + beta * spot_slip`. The spread
  is `futures - beta * spot`, so each leg enters with its own weight.
  If it ever fails, one of the two numbers on the operator's screen is
  wrong and they cannot tell which.
- **Unmeasured is not zero.** No snapshot or an unfilled leg reports
  None all the way to the UI, which renders "—". A zero would read as
  flawless execution.
- Paper measures it identically to LIVE (PaperExecutor takes `config`
  now), so the number is readable before any money is at risk.

Where it surfaces: a `[SLIPPAGE]` log line per pair; the in-position
card's "Entry cost" row; Telegram's EXECUTION block on entry and exit;
`trade_review` columns (entry/exit x cross/slip, spread AND usd, plus
a round-trip `slip_usd`); the Analysis page's Execution Quality tiles
and the journal's "Slip $" column.

This finally answers CLAUDE.md's own audit rule ("alarm if modeled >=
2x realized") — the **Modelled / realised cost** tile computes it
continuously. Realised is crossing PLUS slippage, because `cost_est`
covers the whole round trip; comparing it against slippage alone would
flatter the model enormously.

Both `trade_review` and `broker_orders` INSERTs now NAME their columns.
Both tables gain columns by ALTER on upgrade, which appends them after
the last column, so a bare `VALUES (...)` writes every field one slot
off the moment the ordering drifts.

## Repo branch map (2026-07)

- `main` = this system (fast-forwarded 2026-07).
- `claude/limit-orders-trade-exits-q0quM` (June): older parallel
  system (adapters/, feature_files/ web app, Telegram bot, OKX).
  Fully superseded 2026-07: MT5 lessons, Telegram notifications and
  the web dashboard are all ported to this system. Only the OKX
  adapter remains unported — the sole reason to keep that branch.
- Other `claude/*` branches: session artifacts, superseded.

## Measured execution costs (2026-08-06, CFI7-Demo, full order suite)

The 40-scenario suite run against the real account. Every MARKET
scenario passed, both legs MT5-confirmed, zero slippage (`Δtgt=0.000`
every fill — market orders filled exactly at the quoted touch).

- Spot XAUUSD_: **0.25/oz** spread. 0.01 lot round trip = -$0.25.
- Futures GC1226: **0.34/oz** spread. 0.1 lot round trip = -$3.40.
- Pair round trip (LONG_SPR/SHORT_SPR MARKET): **-$3.64 to -$3.68**
  at 0.01 spot + 0.1 futures.
- **Per 1 lot: spot $25 + futures $34 = $59 round trip**, before
  commissions (COMMISSION_PER_LOT_* are still 0.0 and MUST be set).
- Contract size confirmed 100 oz/lot on BOTH legs by arithmetic
  (0.01 lot moved $0.25 at 0.25/oz = 1 oz), so **HEDGE_RATIO = 1.0 is
  correct** — this closes open item 2 for this broker.
- Minimum volumes DIFFER: spot 0.01, futures 0.1. The futures min is
  10x the spot min, which constrains SLICE_LOTS and any partial fill.
  LIVE trading was already correct here (`PairExecutor._precheck_pair`
  rejects a pair whose CHILD order is under either leg's minimum, and
  the hedge is `round_step(spot_filled x HEDGE_RATIO, fut_step)`), but
  the SCENARIO suite took each leg's own minimum, making "LONG_SPR"
  1 oz long vs 10 oz short — 9 oz net directional, with a reported
  cost ~94% one leg. `ScenarioRunner.pair_volumes()` now sizes the
  smaller leg up until BOTH clear their minimum at HEDGE_RATIO;
  single-leg scenarios still use that leg's own minimum.

**The edge does not currently cover this.** Measured sigma on the
spread was 0.063-0.073. At sigma 0.07 and z=3, expected capture is
`TARGET_FRACTION(0.5) x 3 x 0.07 x 100 = $10.50/lot` against $59 of
cost — the edge filter needs 1.5x cost ($88.50), so it is short by
~8x and NO z inside [ENTRY_Z, MAX_ENTRY_Z=4.5] can clear it. Even a
FULL 3-sigma reversion ($21/lot) is a third of the cost. The engine
will therefore sit at NO_SIGNAL forever on this broker/window, which
is the edge filter working correctly, not a bug. Break-even needs
either sigma >= ~0.20/oz (3x today) or total spreads down from
0.59/oz to ~0.20/oz (a raw/ECN account). Re-measure sigma over a full
2h window before concluding — these readings came from a warm-up
window.

**Passive limits do not fill here** — but the suite was also asking
the wrong question. `place_limit(marketable=True)` put a BUY at the
BID and a SELL at the ASK: those are PASSIVE prices, so the "should
fill" scenarios could only fill by luck, and all four LIMIT spread
scenarios failed on a leg the market happened to run through. MT5
rejects a buy limit at/above the ask (10015), so a true marketable
limit is not expressible; it now places ONE TICK inside the far touch,
which is as aggressive as BUY_LIMIT gets. Re-run before drawing
conclusions about fill rates.
A leaked fill is now scored precisely: the market moving through a
resting price is not a defect, so a leak that is flattened AND
confirmed by MT5 PASSES, with the line rendered "RACED a fill ... —
flattened and confirmed" so it never disappears from the report. A
leak whose cleanup fails is still a FAIL. (Before: any leak = FAIL,
which buried genuine faults in red; before that: any leak = silent
PASS, which left orphans.)
SPREAD_COST_FACTOR still cannot be lowered on the strength of limit
fills on this account — assume 1.0 until measured.

## Live LIVE-run bugs (2026-08-06, CFI7-Demo, engine streaming)

- **z of +53,026 on a spread of 9.13.** Sigma collapsed (a quiet
  window / polling faster than the feed ticks) and z is a division by
  it. SpreadStats.degenerate now refuses a window whose implied |z|
  exceeds SIGNALS.MAX_ABS_Z (25) or whose sigma is below the OPT-IN
  SIGNALS.MIN_SIGMA floor; degenerate windows report warm-up, not a
  number. Note the residual risk the guards cannot remove: while the
  spread sits still |z| stays ~1, but a SMALL-not-absurd sigma can put
  z inside [ENTRY_Z, MAX_ENTRY_Z) on noise — MIN_SIGMA is the only
  defence and must be set once the spread's real sigma is known.
- **Root cause of that collapsed sigma: the window was a series of
  POLLS, not of quotes.** The coordinator polls every 0.5s, faster
  than either broker ticks, so the same quote was appended over and
  over and the window held almost no variation. compute_market_data
  now stamps each snapshot with a `quote_id` (both tick times AND both
  bid/asks — some brokers stamp only to the second) and
  `SpreadStats.update(value, quote_id)` ignores a repeat. Sigma is now
  invariant to poll rate (regression-tested: same quotes polled 1x and
  20x give the same sigma). Ageing still runs on EVERY call, so a feed
  that stops ticking drains out of the window and goes cold rather
  than freezing a stale z. This also shrinks — but does not remove —
  the MIN_SIGMA residual risk above.
- **The dashboard price only moved every 10s** (operator: "not
  refreshing every 0.3 secs"). Nothing in the browser was at fault —
  the chain is coordinator poll -> runtime_status.json -> webapp
  socket bridge -> page, and the slowest link sets the rate. Three
  links were slow: `_write_runtime_status` was called only from
  `log_status` (every 20th loop = 10s), the `updated` stamp the socket
  bridge diffs on had whole-SECOND resolution (a 1Hz cap on its own),
  and that bridge slept 1.0s. Now: status written EVERY poll, stamp
  carries milliseconds, bridge sleeps `BROADCAST_INTERVAL_SEC` (0.2s),
  POLL_INTERVAL_SEC default 0.5 -> 0.3 to match the page's
  UPDATE_INTERVAL — safe only because quote_id dedup (above) made
  sigma poll-rate invariant. account_info() is an IPC round-trip per
  account, so it is CACHED (`ACCOUNT_REFRESH_SEC` 5s) rather than
  re-fetched 3x/second; the margin breaker updates on each refresh.
  Housekeeping (status log, config hot-reload) moved from loop counts
  to the clock, so changing the poll rate no longer changes how often
  the log prints. `run()` also re-reads POLL_INTERVAL_SEC each pass —
  it is in HOT_TRADING_KEYS but was captured once before the loop, so
  hot-reloading it did nothing.
- **"The Spread seems incorrect."** First pass fixed the presentation
  (the card showed a detrended spread with no derivation, and its
  tooltip still carried W3's `futures - beta*spot`) and the carry
  inputs (it read the FUTURES symbol's swap, ignored MT5's `swap_mode`
  units, and never adopted what it read). Then the OWNER SETTLED IT:
  **the spread is `futures - HEDGE_RATIO * spot`, full stop** — see
  Strategy above. All the carry machinery is gone: `calculate_swap_basis`,
  `_adopt_swap_charge`, `swap_diff`/`swap_basis`/`swap_premium_pct`/
  `carry_adjusted`, main.py's swap prompt. `swap_charge` survives only
  as an inert config key. market_data now carries `spread`, `basis_pct`
  and `spread_formula` (shown under the card and logged at startup);
  the DB's market_data table gained `spread`/`basis_pct` columns via
  ALTER-if-missing, replacing `swap_basis`/`swap_premium_pct`.
- **"The lookback period setting seems incorrect and not working."**
  The Settings field was labelled "Number of ticks to collect before
  trading" (W3's meaning) but wrote `SIGNALS.LOOKBACK_SEC` — a window
  DURATION in seconds. `MIN_SAMPLES`, which is what actually gates
  trading and what the warm-up bar counts, had NO control at all, so
  changing "Lookback Period" appeared to do nothing to warm-up while
  silently narrowing the statistical window (and a too-short window is
  exactly what collapses sigma). Now two honest controls: "Lookback
  Window (seconds)" and "Minimum Samples". Both hot-apply live —
  SpreadStats holds a reference to config.SIGNALS and hot_apply
  updates that dict IN PLACE; regression-tested, because swapping it
  for a new dict would silently freeze the live window at old values.
  FOLLOW-UP: three more places still spoke W3's "lookback = tick count"
  vocabulary. The banner's initial line said "Waiting for lookback
  period"; the signal-reason line repeated the same broken ratio worded
  as "N / 300 ticks"; and the "Suggested Lookback" tile showed
  `suggested_lookback` in "pts" — a value this engine NEVER published,
  so it had read "—" since the port. It is now a real suggestion in the
  same unit as the setting: `SpreadStats.suggested_lookback_sec` =
  measured half-life x SIGNALS.LOOKBACK_HALF_LIVES (6), None without a
  half-life, and it never changes LOOKBACK_SEC by itself. The tile goes
  RED when the configured window is shorter than the suggestion —
  that is the setting that collapses sigma.
- **A restart used to throw the whole window away** (operator: "every
  time we start the program the Data Collection goes to 0"). Every
  quote was already being written to `market_data`; nothing read it
  back, so MIN_HISTORY_SEC 7200 meant two hours before trading after
  any crash or config change. `DataLogger.recent_spreads` +
  `SpreadStats.seed` now refill the window at startup
  (`Coordinator._warm_start`), crediting `collecting_since` from the
  OLDEST recovered sample so history reflects when collection really
  began. Same approach app.py took, whose comment names the identical
  symptom. SAFETY: rows carry a `series_key` (spot|futures|beta) and
  only an exact match is reused — seeding a beta=2 window with beta=1
  history would give a mean the live spread never visits, which the
  engine would read as an enormous z. Rows predating the column are
  NULL and never reused. Note a warm start cannot credit MORE than
  LOOKBACK_SEC (older samples are gone), so with MIN_HISTORY_SEC ==
  LOOKBACK_SEC expect a short top-up of minutes, not hours.
- **Warm-up has TWO gates now** (owner, 2026-08-06: "I would the system
  to take 120 minutes (not hard coded) of data - calculate mean and
  standard deviation before going ahead"). MIN_SAMPLES alone was not
  that: 300 quotes arrive in ~3 minutes on a live gold feed, so the
  Lookback Window looked "not changeable" — it sets the window width
  but never gated when trading STARTS. `SIGNALS.MIN_HISTORY_SEC`
  (default 7200 = 120 min, 0 = off) requires that much elapsed
  collection before `warm`. It is measured from when collecting
  STARTED, not as the window's span: samples older than LOOKBACK_SEC
  are dropped so the span can approach but never reach the window
  width, and a gate written against the span would deadlock. Capped at
  LOOKBACK_SEC for the same reason. Resets if the feed dies and the
  window empties. The banner names WHICHEVER gate is binding
  ("45 of 120 minutes collected" vs "118 more quotes").
- The warm-up bar read "181 / 7,200": it compared a SAMPLE COUNT to
  LOOKBACK_SEC, a duration, so it could never fill. It now counts
  against MIN_SAMPLES, which is what actually gates trading. FOLLOW-UP
  (same day, operator: "10,894 / 300 — why is it stuck at 300?"):
  MIN_SAMPLES is a threshold to CLEAR, not a target to sit at, so once
  cleared the ratio reads like a broken denominator. The counter now
  drops the ratio when warm and shows "10,894 quotes in 2h window"
  instead. And when there are enough quotes but still no z (a collapsed
  sigma — `degenerate`, now published to the UI) the banner says "No
  usable Z-score / the spread has barely moved" rather than "Collecting
  Data" while the counter sits past its target.
- **Filling mode**: `close_position_ticket` and `send_market_order`
  hardcoded ORDER_FILLING_IOC, so on a FOK-only broker EVERY close came
  back `10030 Unsupported filling mode` — the engine could open and
  never exit. Both now read symbol_info.filling_mode and RETRY across
  the allowed modes (`_send_market`); only 10030 is retried, and the
  error names what the broker allows. The pending path already did
  this; the market paths did not.
- An orphan the broker refuses to close was retried every 20s forever.
  After RECONCILE.CLOSE_ATTEMPTS (3) failures the reconciler escalates
  ONCE with "CLOSE IT BY HAND" and stops hammering it; a later
  successful close clears the escalation.
- **A leaked fill's cleanup passed the leg ROLE where a SIDE belonged.**
  Live: `cancel FAILED: order filled 0.01 before the cancel landed` ->
  `leak cleanup FAILED: 'SPOT' is not a valid OrderSide` -> orphan
  ticket 102279299. `ScenarioRunner.cancel` built the close from
  `side_leg.label('').strip()`, which is the ROLE ('SPOT'/'FUT'), so
  `OrderSide('SPOT')` raised inside the leg runner and the leaked
  position stayed open. cancel() now takes the whole PLACE action and
  closes on `place_action['side']`. The test fakes had DROPPED
  entry_side when recording closes, which is why nothing caught it —
  they now record it and a test runs `OrderSide()` over every close.
- **A cancel that actually FILLED reported a clean cancel.** A
  scenario logged "cancelled (no fill in 15s)" and PASS; 11s later the
  reconciler found a live position carrying that same ticket. MT5
  turns a filled pending order into a POSITION with the ORDER's
  ticket, and positions_get shows it before deal history does.
  `cancel_pending` now checks positions_get when the fill reads zero
  (flagging `leaked_fill`), and a scenario that leaks FAILS and
  flattens the position instead of passing. This protects the live
  path too: PairExecutor's cancel+verify would have believed the leg
  was flat and left a naked position.
- Scenario preconditions check the BROKER's book, not just the
  engine's: leaked fills left positions the engine never knew about,
  the flat-book test passed, and every further run piled on (eleven
  orphans in one session, all reporting PASS). An unreadable book is
  refused too — never place test orders blind.
- LegServer served ONE client at a time, so the UI's direct leg-runner
  calls timed out whenever the coordinator was attached. It now
  accepts several clients, each in a thread, with the MT5 work
  serialised behind a lock.
- The Settings suite table rendered "undefined" rows: it was fed the
  raw {leg, check, ok, detail} rows while the vendored template reads
  label/mode/order_type/status/detail. _test_state now maps them.

## Web UI faults found by driving Chromium (2026-08-06)

Operator: "changes are made on the Settings Page and clicked Save - The
settings are not getting saved - ... but no error shows up". Found by
running the page in the bundled Chromium under Playwright and reading
`pageerror` — none of it is visible from Python tests alone.

- **The Save handler was never registered.** settings.html called
  `updatePairLabel(); checkPairWarning(); fetchBetaSuggestion();` at
  template line ~1170, ~160 lines ABOVE the `const instrumentCategories`
  / `let _betaSuggestionTimer` they read. A `const` read before its
  initialiser is a temporal dead zone ReferenceError, which aborts the
  WHOLE script block — and the submit listener sat further down, so it
  never attached. Clicking Save did a native form submit (page reload,
  values reset) with the error only in the dev console. Those three
  calls now run LAST, in a "Boot" section at the end of the block.
- **Two number inputs rejected the engine's own defaults**, so Chrome
  refused to submit and fired no submit event at all — silently, since
  the offending field is scrolled off a page this long.
  `profit_target_min_cost_mult` had `step="0.25"` against a default of
  1.2; `min_fill_ratio` had `min="0.5"` against MIN_MATCHED_FRACTION
  0.4. Fixed, AND the submit handler now checks `form.checkValidity()`
  itself and names the offending fields in a dialog, scrolling to the
  first. tests/test_nexus_ui.py cross-checks every number input's
  min/max/step against the shipped default.
- **Saving fragmented the asset config.** `updatePairLabel()` wrote its
  display label into the hidden `#asset` field, which is the asset KEY,
  so every save created a second enabled asset ("XAUUSD_/GC1226") beside
  the real "GOLD" — the new one missing lot_size. The label is now
  display-only.
- **A blocked CDN disabled the whole app.** `const socket = io();` in
  base.html threw when cdn.socket.io was unreachable (locked-down box,
  offline, corporate proxy), killing base's script block — and with it
  showToast, showDialog and every save handler defined below. `io` is
  now probed before use with a stub that degrades to polling, the
  dialog/toast helpers are defined ABOVE it, and showDialog shows/hides
  the modal itself instead of via `bootstrap.Modal` (the dialog that
  reports "could not save" must work when the network is what failed).

- **Black bands with unreadable text in striped tables** (operator).
  The app is a LIGHT theme, but several tables carry Bootstrap's
  `.table-dark`. base.html overrode `--bs-table-bg`/`-color` but NOT
  the STRIPED/HOVER/ACTIVE colours, which `.table-dark` ships as
  near-black — and Bootstrap 5.3 paints those over the cell with an
  `inset 0 0 0 9999px` box-shadow, so a plain `background-color`
  override is invisible. Stripe #2c3034 under base.html's forced dark
  `.table-dark td` colour measured **1.16:1**. Every state variable is
  now mapped into the light theme AND the odd-row rule sets
  `--bs-table-bg-type`/`--bs-table-color-type`, the hooks the box-shadow
  actually reads: **13.01:1**, verified in Chromium with Bootstrap
  5.3's real table rules injected.

- **Cards showing only dashes** (operator, 2026-08-07: Filters cost
  row, Position Sizing notionals). Not a runtime fault — W3 field names
  the vendored templates read (`std_ratio`, `round_trip_cost_bps`,
  `fee_bps_used`, `order_mode`, leg notionals) that `status_to_ui`
  never published. The engine computes all of it every tick inside
  `costs.edge_ok`; `Coordinator._sizing_and_cost` now publishes it.
  Notionals come from lots x contract size x live mid, because this
  engine's sizing anchor is CLIP_LOTS and W3's `position_size_usd` is
  never set.
- **Statistics & Regime showed Mean/Std 0.00** against a live mu of
  58.8: the card reads `data.spread_mean`/`spread_std` at the TOP
  level, but those only existed inside the `signal` block. Now
  published, at 4 dp — 2 dp rounds sigma 0.0631 to "0.06" and hides
  the whole sigma-vs-cost question. `regime` is a direct reading of the
  AR(1) fit (half-life present = MEAN_REVERTING, absent while warm =
  TRENDING), and Hurst is published as None: this engine does not
  compute it, and the card used to default to a fabricated 0.5000 that
  reads as a measurement. Half-Life is labelled MINUTES, not "periods".
- **Margin Details**: IMR/MMR/Margin Ratio/liquidation ARE per-position
  and correctly blank while flat — the card says so. But `Capital req`
  is knowable flat and was hidden because `capital_required` was never
  published; it now comes from `_sizing_and_cost` (each leg's notional
  over ITS OWN leverage, plus the M2M buffer) via /api/account-info,
  with a util badge against available. That is the pre-trade
  affordability check for the configured CLIP_LOTS.
- **Prices refreshing slower than 0.3s**: `config.example.json` pinned
  `POLL_INTERVAL_SEC: 0.5`, and `start.py` copies it to config.json on
  first run, so the 0.3 default never applied on an existing install —
  and the key had NO control on the Settings page. Example fixed, and
  "Poll Interval (seconds)" is now editable (hot-applies). The status
  file also publishes a MEASURED `write_interval_ms`, so "is the engine
  or the browser slow?" is answerable rather than guessed at.
- The warm-up counter shows the history gate in SECONDS (`7,200 /
  7,200s`), the same unit as the setting, capped once met so it never
  reads like a broken denominator.

- **Position Sizing was labelled in USD but holds LOTS** (operator,
  2026-08-07: "can't seem to find CLIP_LOTS / SLICE_LOTS /
  MAX_LOT_SIZE"). Two of the three were on the Settings page the whole
  time as "Position Size (USD)" (`clip_lots`) and "Max Position Size
  (USD)" (`max_lot_size`) — the same units mislabel as Lookback Period.
  SLICE_LOTS had no control at all, despite gating whether a child
  order clears each leg's minimum. All three are now labelled LOTS with
  the notional consequence spelled out. The inline "Capital required
  (live)" preview also read the lot count as dollars ("$100" for a clip
  whose real notional is $42m); it now converts lots x contract size x
  live mid and shows that conversion in the breakdown.
- **config.example.json ships PRODUCTION scale** (CLIP_LOTS 50,
  SLICE_LOTS 10, MAX_LOT_SIZE 50, DAILY_LOT_TARGET 500) and start.py
  copies it verbatim on first run, so a fresh install is ~$43m of gold
  notional out of the box. The code defaults in config.py are 1.0, but
  they only apply when a key is ABSENT. Note MAX_LOT_SIZE 50 does NOT
  block a 50-lot clip, and there is no pre-trade margin guard — only
  the edge filter and MT5's own margin rejection stand in the way.

## Dialogs (2026-08-06)

Owner: "Make all the Dialog Boxes Professional and also Everytime new
settings are saved - a dialog box that confirms or gives an error."
- One shared modal (`#appDialogModal` in base.html) behind
  `showDialog()` / `showConfirm()` / `showResult()` / `reportSave()`.
  All 14 native `confirm()` calls are gone; destructive actions get the
  danger variant. A test fails the build if `confirm(`/`alert(`/
  `prompt(` reappears in any template.
- Toasts stack in one container with an icon, a title and a dismiss
  button; ERRORS DO NOT AUTO-HIDE (ms: 0) because a failure that
  vanishes in 3s is one the operator misses. Message text goes in via
  textContent — it carries broker and server strings.
- Every save answers with a dialog: `/api/config` and the Exchanges
  account save both route through it, carrying the server's note
  (hot-reload timing, restart-required, the 409 beta rejection).

## Live startup bugs found on the operator's box (2026-08-06)

- Endpoint typed `127.0.0.1.9101` (dot, not colon) crashed BOTH the
  coordinator and the leg runner at startup in a restart loop, with an
  int() traceback. `ipc.parse_endpoint` now accepts the dot form and a
  bare port, raises a message that says what to type, the UI REJECTS a
  bad endpoint on save (and normalises the dot form), and both
  processes print "Cannot start: ..." naming the account instead of a
  traceback.
- An account named `Ut 2` produced the .env key `MT5_PASSWORD_UT 2` —
  a key with a space, which dotenv cannot parse, so the password never
  loaded and MT5 could not log in ("could not parse statement starting
  at line 2"). `env_var_name` now sanitises the key, values are
  QUOTED (passwords with spaces/# survive), and update_env_file drops
  lines dotenv cannot read. check_mt5.py reports both faults.
- Symbols follow app.py's model: the symbol, contract size, swap and
  expiry live on the BROKER ROW with its leg, not in a separate panel.
  Saving a row writes leg_accounts[role] and assets[..]_symbols.
  Settings' Pair Selection therefore shows the two symbols READ-ONLY
  (with a link to Exchanges) and dropped the Quick Pick dropdown —
  editing a symbol there set no leg mapping, so the two places could
  silently disagree. HEDGE_RATIO stays editable in Settings: it is a
  strategy parameter, and the connectivity check computes the right
  value from the two contract sizes.
- Setup must work with the coordinator DOWN. Symbol search, [Test] and
  [Diagnose] now talk STRAIGHT to the account's leg runner over its
  endpoint (webapp opens a short-lived RemoteLeg — a socket client, no
  MT5 in-process), falling back to the coordinator bridge. Otherwise
  it is a deadlock: the coordinator will not start until the symbols
  and legs are right, and those were the tools for finding out. An
  account therefore NEEDS an endpoint; the Add form pre-fills the next
  free port.
- leg_accounts must map BOTH legs. An empty/stale mapping used to
  KeyError('default') in _resolve_legs — a raw crash loop that also
  bypassed main()'s clean ValueError exit. Now it names the missing
  leg, the accounts that DO exist, and where to fix it; the Exchanges
  page shows a red banner before the operator restarts, and
  check_mt5.py reports the mapping. Deleting an account clears its
  leg role — that is how the mapping goes missing.
- Contract size, expiry and swap are READ FROM THE TERMINAL at startup
  (_adopt_broker_specs), not typed in — the owner asked why they were
  on the form at all, and MT5 knows all three. The broker's number
  wins and a contradiction with config is logged; legs with different
  contract sizes log the implied HEDGE_RATIO. A real futures contract
  brings its own expiry; it is informational only — the spread does
  not depend on it.
- Symbol resolution failures now LIST WHAT THE ACCOUNT OFFERS
  (_suggest_symbols via find_symbols) instead of just "missing
  symbols", and say plainly when an account has nothing matching —
  that account is probably the wrong leg.
- futures_expiry is OPTIONAL (owner: "just pick up the symbols, don't
  get into expiry"). Missing expiry used to KeyError in
  validate_expiries and kill startup. Now: no expiry -> the engine
  still trades (the spread never depended on it once the carry was
  removed); a PAST expiry -> warns, because the contract has rolled and
  its quotes go stale.
  SUPERSEDED 2026-08-06: expiry used to switch a carry adjustment on
  and off, so an expired one silently changed the spread definition. Two accounts at the SAME broker need two separate MT5
  INSTALLATIONS; sharing one is refused at startup and by the
  checklist, since one terminal holds one login.

## Bugs already found and fixed (2026-07) — don't reintroduce

- Legacy `close_position` routed closes through the entry path with
  `SignalType.NO_SIGNAL` → ValueError → LIVE positions could never be
  closed. Fixed: closes use `OrderManager.execute_close_pair`, which
  reverses each leg's recorded side. Regression tests in
  `tests/test_execution.py`.
- Legacy P&L multiplied price change by lots only, ignoring contract
  size (100 oz/lot gold, 5000 oz/lot silver) → understated ~100x.
  Fixed via `contract_size` in `update_position_pnl`.
- Legacy `mt5.initialize()` took no path/login/server — could only
  attach to whatever terminal happened to be open. Fixed:
  `BrokerSession` passes them from config.

## Decisions made (2026-07)

- Architecture: leg runners + coordinator over localhost TCP (owner
  described the scenario; sequential switching rejected).
- Leg mapping: spot on Broker 1 (e.g. FxPro), futures on Broker 2
  (e.g. CFI); must also work when both legs share one terminal.
- 500 lots/day is a target to aim for, NOT a risk cap.

## Open items (waiting on owner / real-broker validation)

1. Owner's real "working config" (symbols, brokers) was never pushed.
2. HEDGE_RATIO must be verified against both brokers' actual contract
   specs (spot lot = 100 oz vs futures contract size) before LIVE.
   The Exchanges page's connectivity check now computes the implied
   ratio from both symbols and FAILS on a mismatch — run it against
   the real brokers.
3. Futures expiry dates in config must be set to the live contract
   (the same check flags an expired or disagreeing expiry).
4. Latency budget: coordinator polls at 0.5s; if the basis edge decays
   faster, consider push-streaming ticks from leg runners.

## Conventions

- Develop on the designated `claude/...` branch; commit + push every
  session. No more versioning by filename (`15_..._f.py` era is over).
- Run `pytest tests/ -q` before any commit touching statarb/.
- Futures expiry dates in config must be kept current — an expired
  contract stops trading and its quotes go stale (a warning is logged
  at startup). It no longer affects the spread.
- MT5 package is Windows-only; on Linux/dev machines everything but
  the live connection works (tests, config, imports).
