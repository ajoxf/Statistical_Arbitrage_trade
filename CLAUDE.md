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

## Expected value at entry (2026-08-08, owner asked for it)

Owner: "When we enter a trade and have a Break even and Take Profit
calculated. Can we calculate the Expected Value as well rather than a
BE + % Exit". The ladder already froze a target and a stop; nothing
stated the arithmetic they imply. `statarb/expectancy.py` does.

    EV = p x TP - (1 - p) x (STOP + cost)

- **The loss leg carries the cost.** TAKE_PROFIT fires on NET so a win
  banks exactly `tp_usd`; DOLLAR_STOP fires on GROSS so a loss books
  `stop_usd + rt_cost` net. An EV that forgets this flatters every
  trade by the price of the trade.
- **p comes from the OU two-barrier probability**, not from a guess.
  In z-units (the stationary sd IS the unit a z-score is measured in)
  the scale function is `S'(z) = exp(z^2/2)` and
  `p = integral(z_entry..z_stop) / integral(z_target..z_stop)`.
  **The reversion speed cancels** — theta appears nowhere — which
  matters because this engine's AR(1) half-life is fitted on 0.6s
  quotes and is often measuring tick noise. Replace `exp(z^2/2)` with
  1 and it reduces exactly to the driftless `b/(a+b)`, so the gap
  between the two IS the mean-reversion edge, measured not asserted.
  Integrals are computed factored by `exp(peak^2/2)` because the raw
  integrand overflows a float near z=38 and MAX_ABS_Z defaults to 25.
- **The no-reversion baseline is always exactly `-cost`**, for every
  combination of levels — it falls out of the algebra
  (`(S*T - (T+c)(S+c))/(T+S+c) = -c`). Printed beside the real EV, it
  proves the cost accounting is self-consistent and makes the point
  that every dollar of edge comes from reversion and nothing else.
- **`needs_overshoot`** flags a target sitting PAST the mean, which
  needs the spread to overshoot rather than come home — a different
  bet from the one z measured. build_plan's full-reversion veto keeps
  SIGNAL entries out of it, but a manual target lands there easily.
  The barrier ratio can read reassuringly while BOTH barriers are out
  of reach and the clock is what actually closes the trade.
- **What it does NOT model, stated everywhere it is shown:** the clock
  (MAX_HOLD / TIME_STOP / the reversion gate can close a trade that
  touched neither barrier), sigma being real, and a stationary mean.
  It is the run-to-a-barrier EV. The Analysis page's `expectancy` is
  the realised counterpart that actually scores the strategy.
- `EXITS.EV_MIN_USD` is an OPT-IN veto, default 0 = off. A negative-EV
  trade is worth refusing, but that changes what the engine trades and
  must not switch itself on. Manual entries are never vetoed by it.

Surfaced in: the `Exit plan (VALUE)` log line, an EXPECTED VALUE block
in the Telegram entry message, and an "Expected value" row on the
in-position card (hidden, never zeroed, when unmeasurable).

Writing it made the edge problem legible in the unit that matters. At
sigma 0.2247 on 110 oz, one sigma of spread is **$24.72**, so a $47
round trip is **1.9 sigma of travel before a cent of profit**. From
z = 3 that leaves 1.1 sigma of reachable target — any target above
~$27 already needs an overshoot. That is the same conclusion as the
edge filter's, arrived at independently and readable at a glance.

## Carry to expiry — the convergence trade (2026-08-24, operator)

Operator: "Did we include a provision - where swap can be entered
manually and the number of days can be calculated to identify if the
Spread is higher or not. We can place a manual trade according to it."
No, and nothing in the repo did: `swap_charge` was an inert config key
nothing read, and `fairvalue.py` prices carry from an annual risk-free
rate, not from what the broker actually charges, and is barred from
every trading path.

`statarb/carry.py` answers a DIFFERENT question from the z-score. z
asks whether the spread is unusual against its own recent history.
This asks whether today's spread beats what it costs to hold the pair
until the contract expires — a date on which the trade is decided
whether or not anything reverts:

    net = |spread| x k  -  carry to expiry  -  round trip

`k` is leg B's units (`sizing.spread_units`), the same multiplier that
turns any spread move into dollars. This matters on a pair whose edge
filter can never pass: the Brent/WTI investigation the day before found
a z of 11.1 needed against a ceiling of 4.5, so no mean-reversion trade
existed — but convergence does not need reversion, only expiry.

- **Swap units are the whole difficulty.** MT5 reports `swap_long` /
  `swap_short` in whatever `swap_mode` says, and the same "-4.5" is
  4.5 points on one symbol, 4.5 units of account currency on another
  and 4.5 percent a year on a third. Each mode is converted
  EXPLICITLY, and a mode this cannot convert returns None with the
  reason. **An unconvertible swap is not a zero swap** — reading it as
  money regardless is exactly how the old carry-detrended spread
  produced a basis nobody could reconcile against the two prices
  beside it.
- **One unpriced leg makes the whole estimate None.** Half a carry
  estimate is not a smaller estimate; a net that quietly dropped one
  leg's financing reads as an edge that is not there.
- **The sign is kept.** A pair is long one leg and short the other, so
  the two frequently pull opposite ways, and being PAID to wait is the
  case worth finding. Which side each leg is charged on follows the
  spread's sign — reading `swap_long` on both would price a trade
  nobody places.
- **Hand-entered overrides win** (`swap_spot_per_lot` /
  `swap_futures_per_lot`, per asset, Settings → Pair Selection). MT5's
  units cannot always be converted and the operator can see what the
  broker actually charges. They are CLEARABLE — blank means "use MT5's"
  — so they cannot ride webapi's skip-if-blank field loop, which can
  only ever set a value. An override that cannot be deleted outlives
  the pair it was typed for. `?? ''` and not `|| ''` on the reload
  path: a swap of 0 is a real statement.
- **No expiry hides the card only when the pair could not have one.**
  Originally it hid on ANY missing expiry. Wrong, found by use
  (operator, 2026-08-24: "cannot see - What the spread should be based
  on the Swap and Expiry Date Calculation?"): WTI vs Brent with no date
  is simply what that pair IS, but a BASIS pair with no date is an
  unset field, and hiding both looked identical from the outside — an
  operator who had just typed an expiry could not tell whether it had
  been rejected, ignored, or was waiting on a restart. A basis pair now
  shows the card with a one-line prompt and a link. `expects_expiry`
  carries the distinction.
- **The carry inputs HOT-APPLY** (`config.CARRY_ASSET_KEYS`). The whole
  `assets` section was blocked from hot-reload, which is right for
  symbols, contract sizes and beta — they define the series and the
  orders — and wrong for four reference-only fields whose only consumer
  is a dashboard card. The same operator report was caused by this: the
  values were saved and correct, and their own log said "Config reload:
  assets change requires a restart" ten lines above the trade. The
  structural comparison now excludes those four keys, so a symbol
  change still demands a restart (regression-tested).
- **The answer is given in SPREAD, not only in dollars** (operator,
  2026-08-24: "where can I see what the actual spread should be -
  depending on the number of days left from the expiry"). Dollars move
  with lots and leverage; the spread the pair has to beat does not, so
  that is the row to read. `carry_spread = -carry / k` is what the
  basis SHOULD be on financing alone — the theoretical spread for the
  days remaining, priced from the broker's own swap rather than
  fairvalue's risk-free rate. `breakeven_spread = (cost - carry) / k`
  adds the round trip: net = 0 rearranged, so the two readings can
  never disagree on screen (regression-tested:
  `spread_gap x k == net_usd`). A break-even BELOW zero is the
  paid-to-wait case and says so in words.
- **The decay table flattens onto the round trip, not onto zero.**
  Carry shrinks with the days left; commission does not. A schedule
  running to zero would promise a free trade at expiry, so the 0-day
  row always survives the row cap — it is the one that shows the floor.
- **The operator can now SET the expiry** (same request: "The user
  should set Expiry dates for Futures Leg"). There was no field for it
  in any template — it came from MT5 at startup or not at all, and MT5
  does not always report one. Both legs' dates are on Settings → Pair
  Selection beside the swap boxes, since that is the whole carry input
  set. `_adopt_broker_specs` already only filled a BLANK expiry, so a
  typed date survives every restart. Clearable, like the swaps: blank
  means "read MT5, or a rolling contract". A date that will not parse
  is REPORTED and the old value kept; a date already gone is accepted
  but flagged, because from the engine's side a passed expiry and a
  rolling contract are the same thing.
- **REFERENCE for a MANUAL decision**, like fairvalue: signals.py,
  exits.py, spread.py, costs.py and pair_executor.py never read it.
  The dashboard card states its assumptions — held to expiry, full
  convergence, no model of the spread widening in the meantime, margin
  calls or a roll — and shows the gross as `|spread| x k` rather than
  only its answer, because a bare total gets believed for months.

## Three things that hid a broker error (2026-08-24, LIVE)

The futures terminal had its Algo Trading button off, so every manual
pair died on `10027 - AutoTrading disabled by client`. One toggle, and
it took two log reads to find, because each layer dropped the reason:

- **The manual panel said "the pair did not execute — check the log for
  the broker error".** The engine was holding that error in a variable.
  `PairExecutor._send_sliced` logged the child's failure and discarded
  it, and the hedge branch overwrote `error_message` with its own
  summary ("Futures hedge filled nothing"), which describes the
  SYMPTOM. `_last_child_error` keeps the broker's words, the branch
  quotes them, and `Coordinator._exec_error` carries them to
  `manual_note`. This is the second time this exact anti-pattern has
  been fixed here — see the manual-target refusal (2026-08-07) — and
  the rule is the same: never send the operator to a log for a decision
  the engine already made.
- **The leg runner logged every client connect and disconnect at INFO.**
  The webapp opens a short-lived RemoteLeg each time the Exchanges page
  polls (15s, per leg), so that is four lines a quarter-minute saying
  nothing, burying the coordinator's own output. DEBUG now; a
  connection that DROPS is still a warning.
- **The launcher shouted "restart" at every save.** It watched
  config.json's MTIME, so saving anything printed the notice —
  including the carry fields that now hot-apply. Crying restart at
  every save trains the operator to ignore the line that matters.
  `_launch_signature` compares only what the LAUNCHER itself reads at
  startup: accounts and leg_accounts. A parse failure returns None and
  announces nothing, so a half-written file is not a change.

Also from this session: the fair-value row was 0.6rem and showed only
its answer. It now shows the derivation as two lines of arithmetic
(`4,292.61 × e^(4.25% × 90/365) = 4,341.49` / `− 1 × 4,292.61 = 48.88`)
rendered from `fair_inputs`, which `fairvalue._derive` publishes
alongside the value. Catching a wrong contract month or multiplier is
the entire reason fair value is on the screen, and a bare number cannot
be checked against anything. `fair_spread` keeps its two-tuple
signature — 14 call sites unpack it — so `_derive` is the shared
implementation rather than a second copy of the carry maths.

## The swap and the carry rate must agree (2026-08-24, operator)

Operator, on a Carry to Expiry card reading "you are paid to hold this
to expiry at any spread": "Check this better. This seems like an error."

Every line of that card was internally consistent — gross, carry, net,
the two spread readings and the decay table all reconciled to the cent.
The INPUT was wrong. The operator had entered `+58.00` per lot per night
on the spot leg, and the magnitude was right: 58 x 365 / (100 oz x
4,646) is **4.56% a year**, exactly what gold funds at. The SIGN was
inverted. A long spot position is CHARGED that, not paid it.

With the sign flipped the same trade reads net **+$8.24** on a $113
convergence — the basis IS the financing, so capturing it barely beats
paying it, which is the honest answer and the opposite of the $215.50
the card showed.

`carry.sanity` catches it, and the check costs nothing because the
answer was already on the screen. **`fairvalue` prices the basis from an
annual rate and `carry` prices it from the broker's swap — two
independent estimates of ONE physical quantity, from different inputs,
in different code.** They sat side by side disagreeing in SIGN (-51.82
against +48.55) and nothing said so. Now:

- opposite signs -> named as such, with the usual cause ("a leg you are
  LONG is normally charged, so its figure is normally negative");
- more than `SANITY_MULT` (3x) apart either way -> flagged as a units
  problem, since both are pricing the same thing;
- no fair value (a RELATED pair) -> no opinion, not a warning.

The warning REPLACES the verdict line rather than sitting under it. A
conclusion drawn from an input the engine can prove is wrong should not
be displayed at all, least of all this one — "you are paid to hold this
at any spread" is a licence to print money and must never render
unchallenged.

**FIXED the same day** (operator: "add two boxes per leg for long and
short swap"). MT5 quotes `swap_long` and `swap_short` separately and
they routinely differ in sign; one box per leg silently changed meaning
whenever the spread crossed zero, because the side follows the spread's
sign. Now four keys —
`swap_{spot,futures}_{long,short}_per_lot` — and the pair reads
whichever side it actually holds that leg on. The pre-split single value
is still honoured and SAYS SO in the note ("one value for both sides"),
shows in BOTH boxes so the operator can see what is in force, and is
RETIRED the moment either side of that leg is saved: a number meaning
"both sides" must not survive underneath two that mean one side each.

## The two spreads you can actually trade (2026-08-24, operator)

Operator: "Short Spread - Sell Future and Buy Spot - The spread should
be calculated using (Bid - Future) and (Ask - Spot). Long Spread - Buy
Future and Sell Spot ... The relevant spread prices should also be used
in the Algo."

The headline spread is a midpoint of two midpoints and nobody fills
there. `marketdata` now also publishes:

    short_spread = fut_bid - beta x spot_ask     sell fut, buy spot
    long_spread  = fut_ask - beta x spot_bid     buy fut, sell spot

By construction `short <= mid <= long`, and the gap between them is
`spread_cost` — exactly one round turn of both legs' bid-ask, in spread
units.

- **`marketdata.executable_spread(md, signal_type, closing=False)` is
  the single rule.** A position reads a DIFFERENT touch at each end of
  its life, because it comes out the opposite way it went in: SELL_BASIS
  enters on the short spread and exits on the long. Reading the
  favourable side at both ends would make every trade look like it
  cleared its costs — worse than using the mid, not better.
- **Wired into the two places that compare a spread to a LEVEL:** the
  armed manual trigger (`_check_manual_arm`) and the exit ladder's
  manual stop/target (`Coordinator._exit_reason` passes `closing=True`).
  Arming a short at 59.00 and firing when the MID touched 59.00 fired
  early, on a level the market never offered, and filled lower by the
  full bid-ask.
- **The z-score series stays on MIDS and must.** mu/sigma/z need one
  continuous series; a series that flips definition with the direction
  under consideration is discontinuous, and its sigma is inflated by the
  bid-ask. `series_key` and every warm-start row assume the mid too.
- **The bid-ask is charged ONCE.** `costs.round_trip_cost` already
  charges both legs' spreads in dollars, and `long - short` is the same
  quantity in spread units — two views of one cost, not two costs. So
  the executable spreads are used to decide WHETHER a level has been
  reached, never added on top of the cost model. Regression-tested: a
  round trip at the executable prices with nothing moving loses exactly
  `spread_cost`, no more.
- A snapshot with no touches falls back to the mid, so replayed rows and
  older callers keep working.

Dashboard: both are shown under the spread, labelled by the trade each
one is, with the gap stated as the round turn it is.

## "Why is the fair spread wrong?" and "why is the stop $4.77?"

Two readings on one card (operator, 2026-08-24), neither of them a
miscalculation, and neither answerable from the number shown.

**Fair 48.72 against a live 57.01.** The arithmetic was right. Fair
value is only ever as good as `risk_free_rate`, a hand-typed number
defaulting to 4.25% that nothing keeps current, and gold was funding
nearer 4.9%. But a gap has two quite different causes — a stale rate, or
a pair carry does not describe — and the card could not tell them apart,
which is the ONE thing it exists to do. `fairvalue.implied_rate`
inverts the same formula (`r = ln((spread + beta x S) / S) / T`) so the
two rates sit side by side: "live 57.01 implies 4.94% a year vs 4.25%
configured" reads as a stale input; 300% reads as a mislabelled pair.
It is a READING, never a correction — fitting the rate to the price
would make fair value agree with the spread by construction and it
would then catch nothing.

**A $1.43 target against a $4.77 stop.** Three knobs in three units
(`STOP_USD_PER_LOT` x lots, `STOP_CAPITAL_PCT` of capital, `TP / RR`)
collapse to one dollar figure via `min()`, and the figure does not say
which bound. Here it was `RR`: 1.43 / 0.3 = 4.77. The operator had
hand-set the target; the engine then derived a stop 3.3x wider from it,
so a level they chose silently produced a risk they did not.

- `plan['stop_source']` names the binding knob, in the log
  (`Exit plan (RISK)`) and under Target/Stop on the card.
- `plan['breakeven_win_rate']` = `stop / (target + stop)` — the win rate
  the geometry needs before any edge. **CLAUDE.md has carried the rule
  "verify measured win rate clears stop/(target+stop)" since the cost
  measurements and nothing ever computed it.** Unlike the EV block it
  needs no sigma, so it is there on the first trade of a cold start —
  exactly when a hand-set target and an RR-derived stop can quietly ask
  for 77%. Amber past a coin flip: a stop wider than the target is a bet
  on frequency, which is legitimate and must be a choice.

RR < 1 is the trap. It reads like a risk setting and it is a REWARD
ratio: at 0.3 the stop is 3.3x the target.

## The card had the answer and buried it under arithmetic (2026-08-24)

Operator: "The screenshot details is not required. The 2 spreads need to
be displayed clearly."

Three lines of fair-value derivation sat above the two spreads that can
actually be traded. The derivation was added the same day to make fair
value checkable, and it was right to add — but it is REFERENCE, and it
was outranking the prices the operator fills at.

- The derivation moved into the row's TOOLTIP. Still there to catch a
  wrong contract month or multiplier; no longer occupying the card.
- The implied-rate line stays VISIBLE, but only when the two rates
  differ by more than 0.5pp. A rate that already agrees is not news, and
  a line that is always there stops being read — the same event-driven
  rule the status log follows.
- `short_spread` and `long_spread` are now the HEADLINE, 1.5rem with
  their own labels, and the mid steps down beneath them. The mid is a
  midpoint of two midpoints: it is the series the z-score is measured on
  and nothing else, and it had the big number purely because it was
  there first.
- Both are rendered at a FIXED 2dp rather than `fmt`'s max-2. They sit
  side by side and are read against each other, so "58.7" next to
  "59.17" invites a misread of the gap between them.

## A suggested target, priced off the margin (2026-08-24, operator)

"Can you include a suggested TP - which would be a particular profit
percent after Break Even amount?" The base was the open question, and
the operator chose **% of the margin the pair ties up** — which is what
`EXITS.TP_CAPITAL_PCT` already means, so a manual target set this way
and a signal target agree about what "1%" is.

Both levels are in SPREAD, for the direction selected, because that is
the field they are typed into:

    break-even = fill  ∓  cost / k
    target     = fill  ∓  (cost + pct% x margin) / k

`k` is leg B's units, the same multiplier used everywhere else. A short
spread profits as it FALLS, so both levels sit below the fill and a long
is the mirror — the sign is `d = -1` for SELL_BASIS.

- The fill is the EXECUTABLE spread for that direction, not the mid, so
  the suggestion is anchored where the trade would actually open.
- **No margin figure means no suggestion.** Leverage unset -> the line
  says "set leverage to size a % target" rather than quietly falling
  back to a different base. A target computed off the wrong base is
  worse than no target: it looks like a considered number.
- `capital_required` was only on `/api/account-info`; the suggestion is
  computed in the panel, so it is published into the `signal` payload
  too.
- One click fills the Take Profit box, and the tooltip carries the
  arithmetic ("1% of $1,220 margin = $12.20 on top of the $1.10 round
  trip").

Worth reading before using it: at 0.02 lots the margin is ~$1,220 and
`k` is 2, so **1% of margin is 6.65 of spread** — far outside anything
this pair moves in a session. The number is honest; whether it is
reachable is the operator's call, and the break-even line beside it is
the one that is always attainable.

## Short only, long only, or both (2026-08-24, operator)

"While running the Algo, can the user select if he only wants to
execute Short Spread, Long Spread or both?" It could not. z fires
symmetrically and nothing stood between the sign of z and an order.

`SIGNALS.ALLOWED_DIRECTIONS` — `both` (default), `short`, `long` — is a
gate in `ZSignalGenerator.entry_signal`, checked immediately after the
direction is decided and BEFORE the trend filter and the cooldowns. The
order matters: "long only" is a standing decision, not a market
condition, and reporting it as a trend block would send the operator
looking at the wrong thing. It reports through the same `_blocking`
machinery as every other gate, so it shows up on the health line's
"held up by" and in the blocked-signal card, and it is deduped the same
way rather than logging on every tick.

- **ENTRIES only.** Exits are never filtered — a position must always
  be able to close, whatever the entry rule says today — and a MANUAL
  trade is never blocked by it, consistent with every other signal gate.
- SIGNALS is a hot section, so the operator changes their mind without
  a restart.
- Anything unrecognised falls back to `both`, because the failure mode
  of a typo must not be a silently halted engine.

## The broker-clock line flapped again (2026-08-24, LIVE)

```
21:02:41 INFO    Leg 'Account_Spot': broker clock is UTC-0.1h ...
21:03:12 WARNING Leg 'Account_Spot': broker clock is UTC+0.0h ...
```

The 2026-08-07 fix rounded the offset to the MINUTE, on the theory that
the jitter was a one-second tick stamp against a continuous clock. That
was only half of it. The offset is
`newest_tick.time - time.time()`, which conflates the broker's clock
with **how stale the newest tick is** — and on a quiet feed that is
minutes, not seconds. UTC-0.1h was a six-minute-old tick, not a clock
that moved.

Two defences now, because the resolution alone was never going to hold:

- **A running MAXIMUM.** Staleness can only bias the reading DOWN — a
  tick is never stamped in the future — so the largest reading seen is
  the best estimate, and it converges the moment one fresh tick lands.
- **Quantised to the HALF HOUR.** Broker clocks are whole or half hours
  from UTC (UTC+2, UTC+3, UTC+5:30); nothing between is a real setting.
  A DST roll is a full hour and still crosses a bucket, so it is still
  a WARNING.

Given up deliberately: a clock that moves BACKWARD is not re-detected
until a restart, because the maximum never falls. That is the right
trade against a spurious warning every thirty seconds.

## The touches are coloured by SIDE, like the leg cards (2026-08-24)

Operator: "does it change colors like green and red?" — they did not,
the prices were plain dark text — then "Make this very similar to the
Leg Card".

The first attempt tinted each touch to match the SPREAD it builds (all
red under short, all green under long). Wrong convention: the leg cards
above already colour **bid green and ask red**, and a price must not
change colour depending on which of two tables it happens to sit in.
Now `Leg B bid` is green wherever it appears and `Leg A ask` is red
wherever it appears.

Then: "This looks so bad — can break this up into smaller cards and make
it similar to the Legs Card". One flat row of four readings ran the two
touches together — `4709.114652.04`, two prices with no gap, because a
hand-rolled flex row gave them nothing to sit in. The row is now FOUR
CARDS in the leg cards' own shape: a header naming the reading, the
value large in the middle, and its parts in labelled Bootstrap columns
underneath. Copying the structure that already works beats approximating
it, and `col-6` inside a `row g-1` cannot collide the way a bare flex
row did.

The touch colours use **`price-up` / `price-down`, not Bootstrap's
`text-success` / `text-danger`**. base.html defines those two itself, so
the green and red survive a blocked CDN — a failure this app has already
had once (2026-08-06), and the leg cards are still exposed to it.

Third pass, "still looks shabby. Remove the outer card if not needed":

- **The wrapper card is gone.** Four cards inside a fifth is a frame
  around a frame, and it contributed nothing but padding. The Close
  Market/Limit buttons moved into the POSITION card — the thing they
  act on — rather than being orphaned with the wrapper.
- **The header badges are gone.** "SELL B / BUY A" was wide enough to
  push the card title onto a second line, so the header read as an icon
  with "Short" underneath it. The fact lives in the card's tooltip and
  in the note under the row; the header just says "Short spread", in
  the direction's colour.
- **`white-space: nowrap` on every touch.** They were breaking mid-
  number — `4709.1` on one line and `1` on the next. A number split
  across a line break is a different number.

Fourth pass, "can still be better" — and the honest answer was that the
four cards were not actually alike. Only Short and Long had the leg-card
rhythm of a VALUE over LABELLED COLUMNS; Z-Score had three words of
prose that never changed ("on the mid spread") and Position had nothing
under its badge. One `renderPair` now draws all three footers:

- **Z-Score** shows the BAND it has to land in (`Entry ≥ 3.00` /
  `Ceiling < 4.50`). A bare 4.10 says nothing about whether it is
  tradeable; against those two it says everything.
- **Position** shows the size one trade would be, so "how big is this if
  it fires" is answerable without leaving the row.
- **The card the current z points at gets a coloured ring** — red for
  short, green for long — but ONLY when the z is inside the band AND
  that direction is allowed. A z past the ceiling lights nothing, which
  is the entry ceiling's whole point: a momentum spike must not look
  actionable. Verified in Chromium at z = +4.10 (short rings),
  z = -4.10 (long rings) and z = 5.0 (neither).

## A warning nobody can act on is not a fix (2026-08-24, operator)

Third time round on the same card: "The net should be around $8 and not
$216. Swap needs to be negative."

The engine was right every time — `+58.00` was entered where `-58.00`
belonged, the warning was on the screen, and the net it produced was a
fabrication. What was missing was any way to ACT on the diagnosis
without leaving the dashboard, opening Settings, finding the right one
of four swap boxes and retyping the number with a minus in front.

- **`carry.credit_fix` names the exact field and value**
  (`swap_spot_long_per_lot` = -58.00), and the warning renders it as a
  one-click "set it to -58.00" that POSTs to `/api/config`.
- **Still a click.** A sign the engine flipped by itself is a sign
  nobody would ever notice was wrong — and the whole reason this module
  refuses to guess at swap units is that a silently-corrected input is
  worse than a refused one.
- **The specific diagnosis now runs FIRST.** `sanity` only knows that
  two estimates disagree, not which to trust; the credited-long-leg
  check knows exactly which input is wrong and can therefore offer the
  correction. The operator spent hours looking at the general message.
- The partial POST is safe because `apply_ui_config` iterates the
  payload's own keys — regression-tested that symbols, contract size
  and the other legs survive the write.
- **The fix carries its own asset key**, from the engine. The browser's
  `__manualAsset` comes from a different endpoint and can be unset, and
  `apply_ui_config` SKIPS the entire asset block when `asset` is
  missing — so the first cut would have reported success and written
  nothing. Both halves are pinned by tests.
- The swap is a `CARRY_ASSET_KEY`, so it hot-applies within ~10s. No
  restart.

The arithmetic of the mistake, worth keeping: the whole error is TWICE
the carry, because a charge added instead of subtracted moves the net by
2x. That is why $8 read as $216.

## The convergence loop, and an orphan booked at 1% of its cost (2026-08-27)

Operator: "If Current Spread > fair spread - open a position, after
profit position is closed, open again the same high to low trade (as
long as current spread > fair). This loop keeps going on as long as the
button for this is kept on."

**`statarb/carryloop.py`** does it, driven off the CARRY card's
swap-implied fair value (`carry_spread`) rather than fairvalue's
risk-free rate — the operator's choice, and the right one: it is tied to
money the broker actually charges, and it is size-free, so the same
threshold holds whatever the clip is.

The loop is a MANUAL decision automated, so it inherits the manual
rules — no signal gates, no risk limits — which is exactly why almost
all of it is refusals:

- **no `carry_spread`** (an unconvertible swap, no expiry) — a carry
  estimate missing a leg is not a smaller estimate. Entering on half of
  one is entering on a number nobody computed.
- **a `carry.sanity` warning** — the swap and the risk-free rate
  disagree about the SIGN of this basis, so one input is provably wrong.
  The dashboard already refuses to print a verdict there; something
  placing real orders refuses harder.
- **a stale or desynced quote** — the gap IS a level comparison, so it
  goes through the same `_stale_quote` gate as a target.
- **a gap inside the round trip** — `(cost / k) x EDGE_MULT` of spread,
  stated in spread so it is comparable to the two numbers beside it.
- **a refused open** stands the loop down rather than retrying three
  times a second against a broker that just said no.

**What bounds it is one thing: the per-cycle stop** (operator's choice
of the four offered). A winning cycle re-arms; a losing one — a scratch
included — switches the whole loop OFF and says why. Both distances are
therefore REQUIRED, in the engine and at the endpoint: without a stop
there is no bound at all, because a manual trade has no engine stop, and
a loop of stopless trades re-entering after every win is a machine for
turning many small wins into one unlimited loss.

- **Levels are DISTANCES, not the absolute spreads the rest of the panel
  takes.** Cycle 2 fills somewhere else, so a level typed for cycle 1 is
  by then either unreachable or already passed.
- **Anchored on the executable SHORT spread**, the same price `evaluate`
  compared against fair. The mid would put both levels half a round turn
  out, in the direction that flatters the trade.
- **It is a primed command, not persistent state like `algo_enabled`.**
  It places orders by itself, and a loop left on at 17:00 must not
  resume when a crashed process comes back at 02:00 with nobody
  watching — that is the 2026-08-07 replay incident's shape. Turning it
  back on is one click, and it is a decision.
- **SHORT only.** A basis below fair is the mirror trade; the operator
  asked for the high-to-low one.
- The structural bar still holds and is now asserted for carry too:
  signals / exits / spread / costs / pair_executor import neither
  `carry` nor `carryloop`.

**And the orphan sweep was booking 1% of what it cost.** The four
reconciler closes at 22:01:50 estimated `$0.81 / $0.16 / -$0.62 /
-$0.18` against real fills of `$81.10 / $15.80 / -$62.50 / -$17.60` —
exactly 100x, the oz per gold lot. `_close_orphan` computed
`(price - entry) x volume` and never multiplied by the **contract
size**, so the untracked-close ledger the operator reads and the
daily-loss breaker were both charged a hundredth of the damage. An
unknown symbol still gets 1.0 — a guessed multiplier is worse than a
plain one — but it now says so. The existing test asserted the bug
($10 where the answer was $1,000) and was encoding it.

Worth keeping about those four orphans: they came from POS_0004's close
falling out of the limit path onto a ticketless market order — the
2026-08-26 hedging-mode defect, this time on BOTH legs. The engine
booked that trade at **+$26.90**, computed against the OPENING prices of
the two offsetting positions; the money that actually reached the
accounts was the four cleanup rows, **+$16.80**. A recorded P&L for a
close that never happened is not the money.

## Which spread each decision reads (2026-08-24, operator)

Operator: "will the algo know which Bid and ask to take into
consideration?" It does, and the answer is deliberately NOT the same
everywhere. The rule, in one place:

| decision | reads | why |
|---|---|---|
| which side each leg trades | signal type | a SELL_BASIS sells leg B and buys leg A; MT5 takes the touch |
| limit peg price | that leg's own bid/ask, fresh tick | `_peg_price`, kept strictly inside the book |
| round-trip cost | BOTH legs' full bid-ask | direction-independent by construction |
| slippage | mid -> touch -> fill, direction-aware, flipped on exit | `slippage.selling_the_spread` |
| **entry signal (z)** | **MID** | mu/sigma/z need ONE continuous series |
| operator's stop / target | executable CLOSING side | prices they named, not statistics |
| armed manual trigger | executable ENTRY side | the level must be one the market offers |
| **reversion gate** | **MID** | it tests against `entry_mu`, a mean of mids |

The last row was wrong until now. `_reversion_home` was handed the
executable closing spread while comparing it against `entry_mu` — two
different definitions, which biased the gate by half a round turn in
whichever direction the position happened to face (a short is bought
back HIGHER, so its gate silently tightened). `evaluate` now takes
`mid_spread` alongside `spread`; the operator's levels read the
executable side and the statistical test reads the mid. `mid_spread`
falls back to `spread`, so single-spread callers are unchanged.

The distinction to keep: **a price someone named is compared against
what the market offers; a statistic is compared against the series it
was measured on.**

## "Go through these numbers thoroughly" (2026-08-24, operator)

The Filters card. Every figure reconciled to the cent — capture, the
1.2x requirement, the shortfall, both leg spreads, the total, the bps,
the z needed and the cost in sigmas all check out against the engine's
own arithmetic. What was wrong was the COLUMN.

Read straight down the money column it went **22.20 / 0.44 / 1.32 /
-0.88**: the first is per LOT and the rest are at the operator's size.
So capture appeared to dwarf a requirement on a card reporting a
shortfall. The round-trip table had the same shape — 0.32 / 0.78 / 1.10
and then **55.00**. Both per-lot figures moved into the middle column,
muted, so the money column is one basis throughout.

Found while checking it: **the capture formula printed was not the one
the engine evaluates.** It read `0.5 x z x sigma x 100` — the per-lot
form, using leg A's contract size — while the value is
`capture / lots_a` and capture is `f x z x sigma x k` with `k` in LEG
B's units. Those agree only when both legs trade the same lots, which
is true at beta 1 with equal contracts and false the moment either
moves. It now prints `x 2 units` from `rt_lots_b x rt_contract_b`, so
the derivation IS the computation rather than a form of it that happens
to agree. Same family as the `expected_capture` leg mix-up of
2026-08-11.

## A stray </div> put every card below it across the page (2026-08-24)

Operator: "This card has got misaligned" — the Reset card the full width
of the screen.

Moving the Carry card below Manual Spread Trade left ONE duplicate
`</div>`, which closed the sidebar COLUMN early. Every card after it
escaped the column and spanned the page. Nothing failed, nothing logged,
and the browser recovers silently — the only symptom is a layout that
looks wrong somewhere other than where the fault is.

`tests/test_nexus_ui.py::test_the_template_tags_balance` now parses all
five templates and fails on an unclosed or stray tag. It immediately
found SEVEN more strays at the end of settings.html, present since the
vendored Nexus UI landed (`ddd10fd`) — browsers had been discarding them
for months. Removed.

The bid/ask touches under each spread are TABLES now, not two lines of
text: "fut bid" and "spot ask" are different widths, so as free text the
two legs' prices never lined up with each other. 0.74rem -> 0.8rem, one
shared `renderTouches` for all three places that draw them.

Each leg's bid-ask width is stated ONCE, on the note, not inside both
tables (operator, 2026-08-24: "Leg A and Leg B Bid Ask table looks very
bad"). A width is a property of the BOOK, not of the direction, so it is
identical under short and long — printing it in both tables put the same
four numbers on screen twice and squeezed three columns into a
half-width cell. On the note the two widths sit beside the round turn
they sum to (`Leg A ±0.13 · Leg B ±0.34 = 0.47 apart`), which is what
makes the line checkable at a glance. And the rows
say **Leg A / Leg B**, not spot / fut (operator, 2026-08-24): the
config, the sizing card and the Settings page all speak in legs, and
only this table did not.

## The open-trade card, as an actual grid (2026-08-24, operator)

"This has to be more structured and well aligned. Should be stacked as
rows and columns and very easy to read like a trading terminal."

It was inline `label: value` pairs inside Bootstrap columns, so every
value started wherever its label happened to end and nothing lined up
down the card. Now a CSS grid —
`grid-template-columns: max-content 1fr max-content 1fr` — which sizes
the columns to the widest LABEL and then holds that width for every
row. Measured in Chromium: every value right-aligns to one of exactly
two edges (446px / 892px) and every label to one of two (8px / 461px).

Three things that were not obvious:

- **The value styling sits on a WRAPPER, not on the span.** `updatePosition`
  reassigns `className` on `position-pnl`, `position-spread-delta`,
  `position-target` and `position-stop` to colour them, which would wipe
  any class put on the span itself. The monospace and right-alignment go
  on an enclosing `.pv`; the colour class still lands on the span.
- **Optional sections use `display: contents`, not a nested grid.** A
  nested `.pos-grid` sizes its own columns and lands a few pixels off
  the rows above it (438 / 447 / 464 against the parent's 446) — which
  is precisely the fault being fixed. `contents` makes the children join
  the PARENT grid. Toggling `style.display` between `'none'` and `''`
  still works, because `''` reverts to `contents`.
- **Each optional group starts a fresh row**
  (`.pw > .pk:first-child { grid-column-start: 1 }`). Without it the
  cells flow into whatever space is left, which split Target and Stop
  across two rows — they are a pair and have to read as one.

The four wide readings (Levels, Level P&L, Expected value, Entry cost)
are one label and a run of figures, so they get their own full-width
`.pos-line` grid with a fixed 7.5rem label column: they line up with
each other, and the figures wrap inside their own cell instead of
pushing the label around.

The CSS lives in dashboard.html's own style block rather than coming
from the CDN, for the same reason `price-up` does.

**Card padding** came down at the same time ("Reduce the whitespace in
the cards"): `.card-body` from Bootstrap's 1rem to `0.5rem 0.7rem`,
`.card-header` to `0.35rem 0.7rem`, and `.card.mb-3` from 1rem to
0.6rem. base.html loads AFTER Bootstrap at equal specificity, so it
wins — measured 16px -> 11.2px horizontal. It does NOT override the
vertical padding where a `py-1` / `py-2` utility is present, because
those carry `!important`; at 4-8px those were already tight, so the
result is one consistent horizontal value and the existing vertical
ones.

Worth keeping about the Chromium driver: verifying this needed the
injected Bootstrap rules PREPENDED to `<head>`, because Bootstrap is a
`<link>` before base.html's `<style>`. Appending them reversed the
cascade and reported 16px — the opposite of the truth, and it would
have "proved" the change did nothing.

## "Why is it in profit above break-even?" (2026-08-25, operator)

A short filled at 54.98, long spread 55.27, break-even 54.38, and the
card reading **P&L +$0.02**. The operator: "How is the trade showing a
profit if the price (Long Spread) is more than the BE Price?"

It was not in profit. Two faults behind one reading.

**The P&L is marked at the MID.** `update_position_pnl` marks against
`market_data['spot_price']` / `['futures_price']`, both midpoints — the
basis the whole dollar ladder is built on, and the basis the engine's
DOLLAR_STOP fires from. But a short is bought back on the LONG side:

    mid mark      (54.98 - 54.97) x 2 = +$0.02
    close now     (54.98 - 55.27) x 2 = -$0.58
    gap           half the round turn, 0.30 x 2 = $0.60

Both are true about different things and only one is money you can
take. The first answer put the second on the card as a **Close now**
row and left the mark alone, on the grounds that moving it changes when
the dollar stop fires on every signal trade. The operator settled it:
**"Do not use Mid. I would like the exact - Bid and Ask Price and the
right Bid and Ask values should be taken."** So the mark moved, the
extra row is gone, and the P&L on the card IS what closing books. See
the section below.

**And the card was still drawing the ENGINE's risk on a MANUAL trade.**
Since that morning the engine's stop, gate, max-hold and time stop are
off for a hand-placed trade — but `spread_levels` still read
`plan['stop_usd']` and `gate_floor_usd`, so the card showed SL 58.50,
an EX column and "stop from target $2.11 / RR 0.3" on a position whose
Stop Loss box was EMPTY. **A stop that will never fire is worse than no
stop shown.** `Coordinator._restate_manual_risk` replaces those with
the operator's own before the levels are built: their Stop Loss priced
from the fill, or `stop_usd = 0` and "no Stop Loss set" when there is
none; `gate_floor_usd = 0` so the EX column disappears;
`breakeven_win_rate` None when there is no stop to compute it from.

## "Do not use Mid" — the mark, and the fees that survive it

The mark. `marketdata.closing_prices(md, signal_type)` returns the two
touches this position would actually be CLOSED at — a long spot leg at
the BID, a short futures leg at the ASK, and the mirror for BUY_BASIS —
and `update_position_pnl` marks each leg there. `fut - beta x spot` of
that pair IS `executable_spread(closing=True)`, pinned by a test, so
the P&L and the levels it is compared against can never be quoted on
different bases again.

What that changes, stated plainly because it is real money:

- **The dollar stop, the take-profit, the peak/trough distribution and
  the reversion gate's net check all act on the true figure now.** A
  stop fires when the position really is that far down, not when its
  midpoint is — half a round turn earlier in effect.
- **A position shows a loss the instant it opens**, equal to one round
  turn of both legs' bid-ask. That is correct: it is what closing
  immediately would cost, and it was always there — it was just being
  half-hidden by the mid and half-charged again as a fee.
- **Our P&L now agrees with MT5's own**, which marks each leg at its
  closing touch. They used to differ by that half turn.

**The fees, which is the half that is easy to miss.** NET was
`gross - rt_cost_usd`, and `rt_cost_usd` charges the whole round turn
of crossing. A mark taken at the closing touch on a position entered at
a real fill has ALREADY paid both crossings — they are in the two
prices — so subtracting the round turn again is the bid-ask twice.
`costs.cost_parts` splits it and `exits.mark_fees(plan)` returns the
part that is NOT in the mark: **commissions, and nothing else.**

This was already half-wrong before the mark moved. The entry fill is a
real fill, so a mid-marked gross carried the entry crossing, and
`gross - rt_cost` over-charged by the exit half — it never showed as an
error because every figure on the card agreed with every other figure
on the card. The same lesson as the levels: **internal consistency is
not correctness.**

`rt_cost_usd` is unchanged and still the full round trip. It prices a
trade that has NOT happened yet — the edge filter, the expected value,
the pre-trade cost card, the Telegram ENTRY message and the
modelled-vs-realised audit all want both crossings. Only a mark that
already contains them uses `mark_fees`.

- **BE moved with it.** `spread_levels` reads `mark_fees`, so break-even
  is now the fill less commission, read against the CLOSING side of the
  book — which is exactly what the operator was doing when they compared
  a long spread of 55.27 to a break-even of 54.38 and got the wrong
  answer. Both are on one basis now, and the test asserts that reaching
  BE books zero.
- **`mark_fees` falls back to `rt_cost_usd`** for a plan built before
  the split — the conservative direction, and the old behaviour exactly.
  It is also scaled with the other dollar keys on a partial fill.
- **The shadow tracker marks the same way** (its `market_data` argument
  is new): a what-if-held verdict taken at the mid would credit the
  hold with half a round turn it could never have collected.
- **The statistics did NOT move and must not.** z, sigma, `entry_mu`,
  the warm-start window and `_log_quote` are all on the MID. A series
  that flips definition with the direction under consideration is
  discontinuous, and its sigma carries the jump as noise. The rule
  stands: a price someone NAMED is compared against what the market
  offers; a STATISTIC is compared against the series it was measured on.

Also closed here, the last holdout of "one multiplier, everywhere":
`update_position_pnl`, `realized_pnl_from_fills` and `close_position`
take an optional `contract_b`, so leg B is priced in ITS OWN units.
They defaulted to leg A's, which is exact only when the legs share a
contract size. The coordinator passes `fut_lot_size`.

## The profit that only existed on a stale quote (2026-08-25, LIVE)

POS_0002, a manual short. It closed on MANUAL_TARGET and booked a LOSS:

```
trigger   executable closing spread 55.67   past the 55.76 target
mark      (56.70 - 55.67) x 10 = +$10.30    the recorded peak, at 61m
fill      4711.04 - 4653.86  =  57.18
realised  (56.70 - 57.18) x 10 = -$4.80     MT5 agreed to the cent:
                                            spot +$71.50, fut -$76.30
```

$15.10 of slippage against a $9.40 target. **The decision was correct
and the price was not.** The futures leg filled 1.29 ABOVE the ask we
were holding, which a market order cannot do unless the ask has moved.
The heartbeats say why: over the two minutes before, spot ran up 2.60
while the futures quote moved 1.29, so the spread appeared to fall
1.32 — about 2.9 sigma against a sigma of 0.45 — and three minutes
later it was back at 56.71, having gone nowhere. No tradeable move ever
happened. The target was sitting in the middle of a phantom dip.

**The feed looked perfect throughout: 108 quotes/min.** That figure is
both legs together, and one leg was carrying the count. A pair trade is
only as good as its WORSE leg — the spread is a DIFFERENCE, so one
lagging quote makes the whole number fictitious while the other leg
ticks beautifully.

`marketdata.QuoteAgeTracker` + `stale_quote`, gating every decision
that reads a price LEVEL:

- **Measured on the LOCAL monotonic clock and the quote's own identity,
  never on the tick's timestamp.** `tick.time - time.time()` conflates
  the broker's clock offset with staleness — the conflation that made
  the broker-clock line flap for weeks — and a guard that gates real
  orders must not inherit it. A quote stamped in 1970 is fresh if it
  just arrived.
- **Unknown is not fresh, and not stale either.** Ages are None until a
  leg has been seen to change twice, so the first poll of a start
  cannot block anything.
- **The asymmetry is the whole design.** ENTRIES and PROFIT-taking exits
  (TAKE_PROFIT / MANUAL_TARGET / REVERSION_EXIT) are withheld outright:
  waiting costs nothing, because a target that existed only on a stale
  quote was never available to take. A STOP is only DEFERRED, and only
  for `STALE_STOP_GRACE_SEC`, then it fires with a CRITICAL line saying
  the fill is unpriced — **a trade must always have a stop**, so an
  unrefreshed feed cannot become a reason to hold a loser for ever.
- **Exits that read no price are untouched**: MAX_HOLD, TIME_STOP, the
  overnight rule, MANUAL_CLOSE, the shutdown prompt and the reconciler
  are not looking at a quote, so a stale one tells them nothing.
- **The grace clock starts at the first DEFERRAL**, not at the first
  stale tick — otherwise a feed that was briefly stale hours ago would
  let the next stop straight through. A refreshed quote resets it.
- An armed manual entry STAYS ARMED rather than being cancelled: the
  level is still there when the quote refreshes, and if it is not then
  it was never offered.
- `EXECUTION.MAX_QUOTE_AGE_SEC` (default 2.0, 0 = off) and
  `STALE_STOP_GRACE_SEC` (10.0), both hot, both on the Settings page.
  The health line now prints `oldest leg X.Xs` beside the rate, so the
  threshold can be set from measurement rather than from opinion.

The regression test is the live sequence: leg A ticks the spread down
through the target while leg B is frozen, and nothing closes; leg B
quotes again and the target fires at once. Its control turns the guard
off and asserts the same sequence DOES close — otherwise the test could
be passing for an unrelated reason.

The other lever, unused so far: `MANUAL_TARGET` is deliberately not in
`URGENT_REASONS`, so a non-urgent exit obeys `EXECUTION.ENTRY_STYLE`.
Had this one been a resting limit at 55.76 it would simply not have
filled, instead of crossing 1.51 through a price that was not there.
Stops must stay market; a target does not have to.

## A threshold sitting on the live figure (2026-08-26, operator)

The guard above shipped at `MAX_QUOTE_AGE_SEC` 2.0. A day of it on a
healthy feed — 102 quotes/min, both legs — showed routine 2.0-2.5s gaps
on one leg or the other, so the guard sat exactly on its own threshold
and flipped OK↔BLOCKED continuously. Default now **5.0**, and the
health line's `oldest leg` figure is there to set it from measurement
rather than from a first guess.

The flapping was only half the damage. The status log is EVENT-DRIVEN,
which is right (2026-08-07: a fixed cadence wrote the same sentence 360
times an hour) — but the event is "a verdict changed" and the block is
**seven lines**, so a gate on its threshold turns the fix back into the
flood, worse than the cadence it replaced.

`TRADING.LOG_STATE_DWELL_SEC` (5.0, hot, on the Settings page, 0 =
print every change) makes a change wait until it HOLDS. Two rules keep
it honest:

- **A withheld change is COUNTED, not lost** — "(15 earlier changes did
  not hold)" beside whatever finally settles. A block that arrives
  quietly after thirty flips reads as a stable engine, and the flapping
  is precisely the operator's cue to go and widen the threshold. Only
  the states that were GIVEN UP ON are counted; the one being reported
  is the headline, not a flap.
- **The heartbeat is never withheld and states the LIVE verdict.** It
  exists to prove the engine is alive, and reporting the last state
  that happened to settle would make it a stale one.

## The stop was inside the entry crossing (2026-08-26, pre-live review)

Operator: "if I turned the Algo on, would it all work as expected?" No,
and the reason had been sitting in the shipped defaults since the mark
moved to the closing touch the day before.

A position is now marked at the touches it would CLOSE at, so its gross
P&L at t=0 is exactly **minus one round turn of both legs' bid-ask**.
That is correct and was always true — it is what closing immediately
would book. `DOLLAR_STOP` fires on GROSS. So a stop at or inside that
crossing is tripped **before the spread has moved at all**:

```
STOP_USD_PER_LOT 30 x 1 lot   = $30 stop
gold book 0.13 + 0.34         = $47 crossed on the way in
                              -> stopped on the tick it opens
```

**It does not wash out with size.** Both sides scale with lots, so the
same config self-stops at 0.1 lots, at 1 lot and at 10 lots alike —
trading smaller is not the fix. Every entry would have paid a round
trip for nothing, up to MAX_DAILY_TRADES, unattended.

`build_plan` now refuses such a plan, on the same grounds as the
viability veto directly above it: a trade that cannot survive its own
entry is not a trade. A MANUAL one is warned about and placed, like
every other veto here — the trader's stop is the trader's.

And the refusal is now **on the health line**. `entries` read "armed —
z +3.10, need |z| >= 3.0" while every signal was being turned away,
because `_plan_refusal` was published to the manual panel and nowhere
else. It now names the refusal, and a plan that BUILDS clears it so the
line cannot go on describing a config already fixed.

The test fixtures were carrying the same fault, which is how it
survived: `tests/test_exits.py` ran a $1,500 stop against $3,000 of
crossing and read as a perfectly ordinary plan.

## Recovered, and managed by nothing (2026-08-26, LIVE)

```
Recovered position POS_0001 from DB (GOLD SELL_BASIS, 0.02 lots)
Recovered position POS_0004 from DB (GOLD SELL_BASIS, 0.10 lots)
    exits    OK      1 position(s) being managed
```

Two recovered, one managed. `close_position` sets CLOSING before it
calls the broker, so a process that dies in that window leaves the row
CLOSING — and `load_open_position_states` deliberately loads it, which
means somebody knew it mattered. But `Position.from_dict` restores the
status verbatim and EVERY lookup in PositionManager filters on ACTIVE.
So it came back invisible: no exit ladder, no health block, no
dashboard, and **not in the reconciler's known-ticket set either**,
which is built from ACTIVE positions — so if the money really was at
the broker it read as an orphan rather than as itself.

POS_0001 sat like that across at least two restarts, reprinting its
mark-fees backfill line each time.

It now comes back ACTIVE, which is the same call `_close_failed` makes
for the in-process version of this (2026-08-07): a close that was not
seen to complete leaves the position OPEN. If it did complete, the
reconciler's 3-strike ghost clear resolves it — and that machinery is
ACTIVE-only too, so the promotion is what lets it run at all. The
promotion is PERSISTED, or the next restart re-reads CLOSING and the
whole thing repeats.

## "database is locked" stopped the exit loop (2026-08-26, LIVE)

```
13:04:04 ERROR Coordinator loop error: database is locked
13:04:12 ERROR Coordinator loop error: database is locked
13:04:19 ERROR Coordinator loop error: database is locked
13:04:27 ERROR Coordinator loop error: database is locked
```

Thirty seconds in which the ladder was not evaluating an OPEN LIVE
POSITION, plus a 500 on `/api/volume`. The webapp is a SECOND PROCESS
reading the same SQLite file continuously while the coordinator writes
it, and `sqlite3.connect` raises immediately on a held lock by default.

Two one-line defences: **WAL** (readers and the writer stop blocking
each other, which is exactly this shape) and a **30s busy timeout** (a
writer WAITS instead of raising — far longer than any statement here
takes, so it only ever replaces an exception). Every connection in
`database.py` goes through one `_connect()` now; there were fifteen
call sites each opening their own.

## A leg LAGGING is not a leg STOPPED (2026-08-26, LIVE)

POS_0004, the day after the staleness guard shipped, and the guard was
right not to fire — nothing was stale:

```
13:14:54   spread +54.96      heartbeat
13:16:03   spread  53.26      the 54.18 manual target fired here
           filled  55.30      2.04 away, $20.40 at k=10
13:16:08   spread +55.26      heartbeat, five seconds later
           feed OK, oldest leg 0.0s, throughout
```

Sigma was 0.29, so 53.26 is eight sigma below the mean and gone within
seconds. The recorded extremes settle it: peak **+$18.30** and trough
**-$35.40** both stamped at minute **280** — a 5.3-point swing in the
spread inside one minute on a 0.29 sigma. Gold fell ~12 points, the
futures leg led, and the difference between two legs a moment out of
step printed a level neither book was offering. +$9.14 became -$2.10.

`QuoteAgeTracker` cannot see this: both legs are ticking hard, which is
exactly what it checks for. `marketdata.SpreadJumpTracker` measures the
change BETWEEN QUOTES against the spread's own sigma
(`EXECUTION.MAX_SPREAD_JUMP_SIGMA`, 5.0, 0 = off).

- **The scale is the LEVEL's sigma, not the tick-to-tick change's**,
  and the change distribution is far tighter — so the threshold is
  generously wide and errs towards letting a real move through. That is
  the right direction for something that can withhold an exit.
- **A disturbance jumps TWICE — out and back — and both prints are
  untradeable.** So a jump makes the level unusable until the series has
  been quiet for `JUMP_SETTLE_SEC` (2.0), rather than for one quote.
- **Cold start has no opinion** (no sigma yet) but the series is still
  TRACKED, or the guard is blind for exactly one tick after warm-up —
  and one tick is all this fault takes.
- It joins the staleness note in ONE `_stale_quote`, so it inherits the
  whole asymmetry already built there: entries and profit-takers
  withheld, stops deferred only, clock-based exits untouched. Two
  faults, one question — is this a price the market is offering?

## The exit style had a control and no wiring (2026-08-26)

The answer to the trade above was "make the target rest instead of
cross" — and the operator could not have done it. The Settings page has
had an **Exit Execution Mode** selector since the vendored UI landed,
posted on every save and read back on every load, and it was never in
`webapi.FIELD_MAP`, so the server dropped it. The maker/taker-fee fault
of 2026-08-10 exactly.

Worse than dead: `_close_leg` read `ENTRY_STYLE` for exits, so the knob
that DID govern how a target was worked was the one labelled "Entry".

`EXECUTION.EXIT_STYLE` now exists and the selector writes to it;
blank falls back to `ENTRY_STYLE`, which is what that path read before,
so an existing config behaves identically. Stops are market whatever it
says.

## The carry card, priced where the trade goes on (2026-08-25)

Operator, three corrections in one message: "there is mid price of the
spread. you need the short spread price Bid and ask with a gap";
"0.02 in the calculation is wrong. Make it all relevant to 1 lot";
"Break even seems like the Wrong choice of words - it's the fair value
of the spread".

All three were right, and the third is the one worth keeping.

- **The price is the EXECUTABLE side, with its two touches.** A rich
  basis is captured by SELLING the spread, which fills on the short
  side; a negative one is bought and fills on the long. The card was
  showing the mid — a midpoint of two midpoints, and nobody converges
  from there. It now shows the side the trade would actually go on,
  under `Leg B bid` / `Leg A ask` (or the mirror), with the round turn
  between the two tradable spreads stated beside them. Same provenance
  the Short/Long spread cards carry, and the same vocabulary: a price
  must not appear under two names depending on which card it sits in.
- **Everything is quoted at ONE LOT of leg A and the hedge it implies**
  (`L_B = C_A / (beta x C_B)`, so `k = C_A / beta`). It had been priced
  at whatever `SIZING_MODE` derived, so every dollar on a REFERENCE card
  described today's configured size rather than the pair — and a rate
  card that moves with the clip cannot be compared from one day to the
  next. The round trip is recomputed at that size too: taking it from
  the caller's sizing plan put a 0.02-lot fee against a 1-lot
  convergence. The two SPREAD readings were always size-free (dividing
  by `k` cancels it), which is exactly why they are the rows to read,
  and a test pins that changing CLIP_LOTS moves neither them nor the
  dollars now.
- **"Break even" was the wrong words.** `-carry / k` is the FAIR VALUE
  of the spread — what the basis should be on financing alone for the
  days remaining, priced from the broker's own swap rather than
  fairvalue's risk-free rate. It carries no fees. What the card was
  showing under that label was `(cost - carry) / k`, which is fair value
  PLUS the round trip: a different statement, and the one that decides
  whether to place the trade. Both are shown now — `FAIR (SWAP)` and
  `TO CLEAR` — and they differ by exactly the round trip in spread
  units (regression-tested), so the card states the fee rather than
  hiding it inside a number labelled as something else.

## The two books (2026-08-25, operator)

"Stop manual trades feeding the breakers and streak reducer. We should
be able to easily distinguish 'Manual' and 'Algo' based trades. Manual
Trades PnL and all Analysis should be recorded somewhere."

The last half of the manual/algo separation. A hand-placed trade was
already governed by different rules; it was still DRIVING the algo's
governor, so four manual losses would pause the algo and shrink its
clip — the same conflict, in the other direction.

- **`on_position_closed(pnl, manual=True)`** books to
  `manual_realized_pnl` / `manual_trades_today` and returns. It never
  touches `consecutive_losses` or `daily_realized_pnl`, so it can reach
  neither `halted()` nor `size_multiplier()`. `total_realized_pnl` adds
  the two back up, because the account has one balance.
- **`record_trade(..., manual=True)` records nothing.** `daily_trades`
  feeds MAX_DAILY_TRADES and the lots-today figure, and
  `last_signal_time` drives the entry COOLDOWN — a hand-placed trade
  must not put the algo on one.
- **Both books roll over together** at the day boundary.

Distinguishing them: `trade_review.source` is written at close from
`plan['source']`, `webapi.trade_source` reads it, and both journal
mappers publish it. A row predating the column reports SIGNAL — which
is what the engine actually did with it: managed by the full ladder,
feeding the breakers. Calling it "unknown" would be more precise and
less true. The dashboard's Recent Trades and the Analysis journal both
carry a **By** column badged MANUAL / ALGO.

Recording the analysis: everything already in `trade_review` — peak and
trough with their minutes, the outcome tag, the frozen levels, the
slippage split — is written for a manual trade exactly as for a signal
one. `webapi.statistics_by_source` returns `{all, algo, manual}`, each
a full stats block, and the Analysis page shows the two side by side
above the combined tiles.

**A book with no losses now reports profit factor None, not 0.0.** It
is gross win / gross loss and undefined there, and 0.0 is the WORST
possible value — a manual book of nothing but winners rendered a red
"0.00". The split made small books common enough for that to show.

## The edge filter, audited (2026-08-25, operator)

"Thoroughly check all the Edge Filter ... Make sure the Algo takes the
Short Spread - the right Bid and Ask values. Confirm thoroughly."

Audited end to end and it is correct. What was verified, at beta 2 with
100/50 contract sizes (at beta 1 with equal contracts several WRONG
formulas give the right answer):

- **The round trip is what a real round trip pays.** Summed leg by leg
  against each leg's mid — sell fut at the BID and buy spot at the ASK
  going in, the mirror coming out — it equals the model to the cent,
  and it is IDENTICAL for a long. Direction-independent by
  construction, which is right: you cross the same two books either
  way, just in the other order.
- **It equals `(long_spread - short_spread) x k`**, the same quantity
  in spread units, and `md['spread_cost'] x k` as well. Three
  derivations agreeing.
- **The bid-ask is charged ONCE.** Enter at the short spread, exit at
  the long, nothing moves: the pair is down exactly the modelled round
  trip, no more.
- **Capture is a MID move and that is why the comparison is sound.** A
  short banks `d x k` LESS one round turn, and the round turn IS the
  cost the filter charges — pinned by moving the book and checking the
  realised P&L equals `capture - cost`.
- Each leg on its own units; commission per lot on its own lots;
  `SPREAD_COST_FACTOR` scaling only the crossing.
- It reads the raw touches, NOT `spot_spread`/`futures_spread`, which
  are the bid-ask **x100** for display and would overstate the cost
  a hundredfold.
- **`edge_z_needed` is the exact boundary** — probed either side of it
  — and `edge_cost_in_sigmas x sigma x k` is the cost back again.
- **The Filters card agrees with the live gate**, and its per-leg parts
  sum to the published total. The card recomputes that breakdown
  locally while the total comes from `round_trip_cost`, so the two
  implementations are pinned together.

One thing fixed: `_sizing_and_cost` swallowed any exception and
returned a card with no cost numbers. The cost model reads
`futures_bid`/`futures_ask` while the coordinator's PUBLISHED asset
block calls them `fut_bid`/`fut_ask` — that rename has already caused
one live fault — so handing it the wrong dict blanked the whole Filters
card silently. It now logs the missing key, deduped.

## A manual trade obeys the card, and nothing else (2026-08-25)

Operator: "When I take a manual trade, ignore all the Algo Logic. Only
focus on the items in the Manual Trade Card. This is Manual trading by
a trader and will not conflict with the Algo Logic."

The trade that prompted it, POS_0003, closed on **TIME_STOP at 15
minutes 1 second**. That number is:

    AR(1) half-life fitted on ~0.6s quotes      8.0s   (tick noise)
    max_hold = MAX_HOLD_HALF_LIVES(4) x 8s      32s    -> under the floor
    MIN_MAX_HOLD_SEC                            300s   -> what applied
    HARD_TIME_STOP_MULT(3) x 300s               900s   = 15 min

Not one part of it came from the trader or from the market. The engine
had also warned at entry that the target sat "PAST the mean, so it
needs an overshoot rather than a reversion; expect the clock to close
this trade" — and then the clock did.

So `plan['source'] == 'MANUAL'` now short-circuits `evaluate` to
`_manual_exit`, which reads exactly two things: the operator's Stop
Loss and their Take Profit, stop first. The Overnight rule is applied
by `_exit_reason` before the ladder and is also on the card; MANUAL_CLOSE
and the reconciler are outside the ladder entirely. **Off for a manual
trade: DOLLAR_STOP, TAKE_PROFIT, REVERSION_EXIT, MAX_HOLD, TIME_STOP,
HARD_MAX_HOLD_MIN and Z_STOP** — all of it strategy machinery for
managing a trade the strategy chose, on a thesis a hand-placed trade
does not have.

- **`build_plan` never vetoes a manual entry.** The viability test asks
  whether a SIGNAL-derived target clears the round trip; refusing a
  hand-placed trade on it is the engine overruling the trader. It bites
  exactly when no take-profit is set, which is when a refusal is least
  expected.
- **The source is stamped INSIDE `build_plan`**, not by the caller
  afterwards. `evaluate` reads it to decide who governs the trade, and
  a plan that reached the ladder unstamped would be run by the algo.
- **An unstamped plan is treated as a SIGNAL** — fail safe. The failure
  mode of a missing stamp must be a managed position, not an unmanaged
  one.
- **The RESOLVED / RISK / VALUE log lines are suppressed** for a manual
  trade and replaced by `describe_manual_plan`, which names the only
  things that can close it. Printing "time_stop=15min" beside a
  hand-placed order is how that clock read as a considered decision.

**Risk limits are off too** (operator, same day: "Yes, turn off risk
limits for manual trades too"). The circuit breaker, `MAX_LOT_SIZE`,
`MAX_POSITIONS_PER_ASSET` and the loss-streak/margin size reducers no
longer gate or shrink a hand-placed trade. Same argument: they are the
strategy's governor, there to stop the ALGO trading itself into trouble
unattended, which is not what a trader placing one order by hand is
doing. Every bypass is LOGGED at WARNING beside the trade that used it —
a limit that was overridden has to appear in the record — and a blank
Lots box now means the engine's SIZING with `size_multiplier=1.0`,
since the reducers are themselves a risk response.

Still standing, because none of them is a risk policy: the level
geometry check (a stop on the winning side fires the moment the trade
goes right — that is a typo, not a risk appetite), `_precheck_pair`'s
symbol minimums, and the broker's own rejection.

**The price of all this, stated everywhere it applies: a manual trade
with the Stop Loss box empty has NO STOP.** It runs until the target, the
overnight rule, or the operator. That is the instruction — a trader's
stop is the trader's — and it is not left implicit: the engine logs a
WARNING at entry, the hint line under the fields turns amber and says
it, the card's footer no longer claims the engine's dollar stop is
armed alongside, and a test asserts a manual trade $10,000 down after a
day is still open.

## "wanted" was a price nobody could trade at (2026-08-25, operator)

"Something is still wrong with wanted." It was, and the arithmetic was
not the part. On a SHORT the row read:

    wanted 56.2400 · quoted 55.9300 · filled 55.8000

56.24 is the MID. **No seller of the spread could ever have been filled
there** — the best available was 55.93, the touch beside it, and the
0.31 "crossing" is not a level the engine aimed at and missed: it is
half a bid-ask, which is simply what a midpoint IS. The word promised a
goal the number never was. Same family as the bare `x 2` and the bare
fair value: the figures were right and the label made them unreadable.

First pass renamed the columns MID / BEST / FILLED / CROSSING /
SLIPPAGE. The operator's answer: **"Mid is not required. Instead this
should be the price you expected — for a Short Spread, the price
expected when 'Activate Trade' was clicked."** Right, and it settles
what the reference should have been all along:

    Entry   EXPECTED 55.9300 · FILLED 55.8000 · SLIPPAGE +0.1300

- **EXPECTED is the executable touch at the moment the order was
  decided** — the short spread for a short, the long spread for a long.
  That is the number that was on the screen when Activate was clicked,
  so "did I get what I expected?" is answerable by subtracting two
  figures on one row.
- **CROSSING went with the mid.** Mid-to-touch has no meaning once the
  mid is not shown, and the bid-ask width is already stated under the
  spread cards ("Leg A ±0.17 · Leg B ±0.40 = 0.57 apart").
- The mid is still MEASURED, still what mu/sigma/z are taken on, and
  still stored in `trade_review` for the modelled-vs-realised tile. It
  is only off this table — pinned by a test, because deleting the
  computation would take that tile with it.

## The frozen geometry, as a table (2026-08-25, operator)

"Make this into a Table with rows and columns. Add a light coloured
border to all the cells. The column headings could be TP, SL etc and
values in the rows."

Each LEVEL is a column and each reading of it a row, so the spread and
the money it is worth sit one under the other under the same heading —
which is how they are read. As three lines of `BE 55.18 · EX 55.68 · TP
54.73 · SL 60.82` over `BE $0.00 · TP +$2.13 · SL -$10.04` the pairs
were only findable by counting separators, and the P&L row was missing
EX entirely so the counting did not even work.

- **One column list drives both rows** (`_levelCols`), so they cannot
  disagree about which columns exist. EX only appears when a gate floor
  actually pushes it past BE.
- **The hand-set marker moved to the HEADING** (`TP*` with a note under
  the table). A badge inside the cell crowded the number it was about.
- `.lvl-table` is local CSS, like `.pos-grid`: a blocked CDN must not
  take the borders and the alignment away.

Verified in Chromium: 5 columns x 2 rows on the levels table, 6 x 1 on
the entry cost, every cell `1px solid`, and every row carrying exactly
as many cells as there are headings.

## The Entry box shows the live price (2026-08-25, operator)

"Instead of 'blank' constantly update with the live price."

Shipped first as a placeholder, on the reasoning that a real value
turns Activate from "go now" into "go at this number" — which for a
short is only reached again if the spread comes back UP to it. Put to
the operator, who chose the **value**: "make it pre-fill with the live
price."

So it pre-fills, and the two things that make that safe are:

- **The sync stops dead the moment the field is taken** (`oninput` and
  `onfocus`). A value rewriting itself under someone typing a level is
  worse than useless on the one panel that places orders.
- **An empty box still means fire at the next poll**, and `clear` is one
  click away. Changing DIRECTION resumes tracking, because the other
  side is a different price and that is a fresh decision.

Verified in Chromium: pre-filled at the live short spread, a typed
99.1234 survives several ticks, `clear` sticks, and switching to long
re-tracks at the long spread.

`(blank=now)` came off the label — the box now shows the number rather
than describing it, and the label has to stay on one line beside the
link. It wrapped onto two the moment the link went in, which is the
fault the operator had just called out on Take Profit.

## A direction is a fact, not a statistic (2026-08-25, operator)

"You have shorted the spread. High to Low. Why is it showing as 'long'
... Can you thoroughly check the logic?" Four short-spread trades, all
four badged LONG, three of them showing "Z 0.0" beside the wrong badge.

**The direction was never RECORDED.** `trade_review` had no column for
it, so both UI mappers inferred it:

    'position_type': 'SHORT' if (row.get('entry_z') or 0) > 0 else 'LONG'

Wrong twice over. A MANUAL trade has no z requirement at all — the
operator picks the direction — and `(None or 0) > 0` is False, so every
trade without a recorded z rendered **LONG whatever it was**. Now
`trade_review.signal_type` is written at close and `webapi.position_type`
READS it; a row predating the column still falls back to the z sign,
which is the right inference for a SIGNAL entry (|z| >= ENTRY_Z, and the
direction follows its sign); with neither it returns None and every
badge site renders a dash. **An unknown direction must not render as a
confident one** — that was the whole fault.

Checking it "thoroughly" turned up three more:

- **The Δ-captured column had the sign inverted.** analysis.html read
  `dfav = position_type === 'LONG' ? -1 : 1`, and the convention is the
  opposite: SHORT (SELL_BASIS) sells the spread and profits as it FALLS
  (`d = -1`), LONG is the mirror. It is `ExitLadder.spread_levels`' own
  `d`, and it is why a short's card says "profit ↓". Every captured
  figure in the journal carried the wrong sign — **a profitable short
  read as a loss.**
- **Recent Trades' "Leg A" and "Leg B" columns could never show
  anything.** `trade_to_ui` publishes no per-leg prices and
  `trade_review` stores none, so `formatPrice(undefined)` printed "-" on
  every row ever rendered. They are now **Spread** (opened -> closed,
  which is the pair's own price and IS stored) and **Exit** (the reason,
  which was published and unused). That column is what answers "why did
  this close where it did" — the operator could not have diagnosed the
  next fault without it.
- **A reversion gate that was already home AT ENTRY.** See below.

## The gate that had nowhere to come home from (2026-08-25, operator)

"The trade is exiting without a profit and before a Stop loss."

`_reversion_home` asks whether the spread has come home — `|z| <=
EXIT_Z`. A **SIGNAL** entry cannot start there, because the entry gates
guarantee `|z| >= ENTRY_Z` and ENTRY_Z (3.0) > EXIT_Z (1.0). A
**MANUAL** entry skips those gates by design and is routinely placed at
z ~ 0, which is already inside the band.

So the gate was satisfied from the first tick, and it never measured
anything. That would be merely useless if the block below it were not:

    if age_sec >= 2 * max_hold:
        return 'REVERSION_EXIT'        # ANY P&L

which is the 2026-08-07 deadlock fix, and correct for a trade whose
reversion edge really is spent. On a vacuous gate it degrades into an
**unconditional timed exit at whatever loss the trade happens to
carry** — no profit, no stop hit. Four hand-placed trades, four losses
of about $2, which is roughly one round trip.

`plan['entry_home']` is frozen at entry (against the same `entry_mu` the
gate is measured on) and the whole reversion block is skipped when it is
set. The exit-path completeness rule still holds and is regression-
tested: the dollar stop, the operator's own stop and target, MAX_HOLD on
a profit, the hard TIME_STOP and the overnight rule are all untouched.
Only the reversion OPINION is withheld, and only where it has no
information.

Two anchor faults in the same block, the sequel to "The Wanted value
looks incorrect":

- **`manual_target_usd` was measured from the MID.** `build_plan` runs
  before the order exists, so at that point the mid is all there is —
  but `tp_usd` is compared against P&L, and P&L is measured from the
  FILL. `ExitLadder.reprice_target` restates it once the fill is known.
- **And the STOP is derived from it.** With TP/RR armed,
  `stop = tp / RR`, so at RR 0.3 a target overstated by the entry
  crossing widens the risk by **more than three times** the
  overstatement. Re-pricing therefore re-runs the whole stop choice —
  `_choose_stop` is now one method shared by both paths, rather than
  the selection existing once inside `build_plan` and being impossible
  to redo.

## One multiplier, everywhere (2026-08-25, operator)

"Yes, fix the leg B multiplier." The last holdouts from the sizing
derivation: `ExitLadder` and `slippage.py` still turned a spread move
into dollars with LEG A's `lots x contract_size`.

`k = L_B x C_B` — leg B's quantity — because the hedge is sized so that
`L_A x C_A = beta x L_B x C_B`. The two agree only at beta 1 with equal
contract sizes, which is the one configuration this has ever run in;
away from there every figure was out by exactly 1/beta:

- the sigma take-profit (`f x |z| x sigma x k`) and the full-reversion
  veto that can BLOCK an entry;
- the BE/EX/TP/SL spread levels, so the displayed stop named a level
  the dollar stop would not fire at;
- a manual target's dollar value, which the viability test measures;
- the entry-cost row's dollars.

`_capital_at_risk` was wrong in a second way — it priced BOTH legs'
notionals off leg A's units, so with different contract sizes the
margin (and therefore every %-of-capital target and stop) was off even
at beta 1. Each leg now uses its own.

Two things worth keeping:

- **`build_plan` publishes the `k` it used** (`spread_units`, plus
  `leg_b_lots` / `contract_b`), and the levels, the manual target and
  the slippage report all read it. Four places each deriving their own
  multiplier is how they drifted apart in the first place.
- **`slippage.spread_units` uses the lots that actually FILLED**, not
  the lots requested — a partial hedge is a smaller pair. It falls back
  to leg A when the asset declares no `fut_lot_size` (the common case,
  and the old behaviour exactly) and when leg B did not fill at all,
  because a zero there would price the whole entry cost at nothing.

`_hedge_units` also takes the asset being traded now. It used to scan
for the first ENABLED asset, which is the right answer only while one
pair is configured.

Pinned by the strong form rather than by restating the formula: move
one leg, add the two legs' P&L the way `positions.update_position_pnl`
does, and the pair must be down exactly `stop_usd` at the displayed
stop — checked by moving leg A and leg B separately, at beta 2 with
100/50 contract sizes. Ten of the thirteen new tests fail against the
old code.

## "The Wanted value looks incorrect" (2026-08-25, operator)

One reading on the in-position card:

```
Levels      BE 54.59 · EX 55.09 · TP 53.77 · SL 60.05 (profit ↓)
Level P&L   BE $0.00 · TP +$2.90 · SL −$9.66 gross
Entry cost  wanted 55.2150 · quoted 54.9000 · filled 54.7600
```

Every figure reconciled to the cent against the others, which is why it
survived. TWO definitions were wrong underneath.

**The spread preferred `tick.last`.** `compute_market_data` read the
most recent TRADE price whenever the broker reported one, and fell back
to the mid otherwise — while `short_spread`, `long_spread`,
`spread_cost`, `costs.round_trip_cost` and slippage's decision mid are
all built from bid/ask. So the number labelled `wanted` and the number
the levels were anchored on were **not the same quantity**, and
marketdata's own comment asserting `short <= mid <= long` "by
construction" was false: a futures print 0.30 above the ask puts the mid
ABOVE the long spread — above the best price anyone can buy the spread
at. It also breaks the series, which is the worse half: mu/sigma/z need
ONE continuous definition, and a spread that switches from a midpoint to
a trade print whenever a trade happens to cross carries that jump as
noise sigma then has to absorb. **The spread is the mid of the book,
always.** (On a feed where `last` is 0 — spot metals, most CFDs —
nothing changes at all, including the warm-start history.)

**The SPREAD levels were anchored on the MID, and the dollar ladder they
translate fires off the FILL.** Gross P&L is measured from the executed
prices, so a $9.67 stop trips when the spread has travelled that far
from what we filled at — 54.76 + 4.835 = **59.59**. The card said
**60.05**, because it measured from the 55.215 mid. Every level was out
by the entry crossing plus slippage, 0.455 here:

- the stop named a level 0.46 PAST where it actually fires, so an
  operator watching for 60.05 was already stopped out;
- break-even read 54.59 where the trade was still down **$0.91**.

`Coordinator.levels_anchor` returns the executed spread, falling back to
the mid when the entry could not be measured — levels an operator cannot
read at all are worse than slightly-off ones, and `fill_spread` reports
None rather than restating the mid as a fill. `plan['entry_spread']`
stays the MID on purpose: that one is the STATISTICAL anchor, and it is
what `entry_mu` and the z-series are measured on. Same distinction as
everywhere else here — a price someone named is compared against what
the market offers; a statistic is compared against the series it was
measured on.

The lesson the card keeps re-teaching: **internal consistency is not
correctness.** Four rows agreeing to the cent proves they share an
anchor, not that the anchor is right.

## Shutting down asks before it closes anything (2026-08-25, operator)

Operator: "Before shutting down - if there is an active position - Ask
'Do you want to close it?' If yes, close it. If no, keep the position
running."

`Coordinator.stop()` closed every active LIVE position at market,
tagged `SYSTEM_SHUTDOWN`, unconditionally. So restarting to change a
setting liquidated a live trade and paid the round trip for it, with
nothing asked and nothing to decline. It now prints the position and
waits:

```
==============================================================
  SHUTTING DOWN with 1 OPEN POSITION
  POS_0001  GOLD  SELL_BASIS  0.05 lots  P&L $-1.06

  y  close it now, at market
  N  leave it open at the broker — no engine, no stop,
     until you start up again (recovered on the next start)
==============================================================
  Close the position? [y/N]:
```

- **An unanswered prompt means NO.** Closing at market is irreversible;
  a position left open is read back from `position_state` on the next
  start and goes straight under the exit ladder again. The window where
  it is unmanaged is the window the process is down — which is exactly
  the window a hard kill already leaves it in. So the default cannot be
  the destructive one. No tty (a service, a test), the timeout, or a
  second Ctrl+C all resolve the same way, and the reader runs on a
  DAEMON thread so an unanswered prompt cannot hold the process open.
- **The prompt NAMES the position** — pair, direction, lots, live P&L.
  The operator answering this cannot go and look it up; the dashboard is
  already coming down.
- **`TRADING.CLOSE_ON_SHUTDOWN`** is `ask` (default) / `always` (the old
  behaviour, verbatim) / `never`. Anything unrecognised falls back to
  `ask`, because the failure mode of a typo must not be silently
  liquidating the book. Both it and `SHUTDOWN_PROMPT_SEC` are in
  `HOT_TRADING_KEYS` and on the Settings page — a knob with no control
  is how `COMMISSION_PER_LOT_*` sat at zero for months, and a *shutdown*
  setting that needs a restart to change is a particularly silly one.
- **`stop()` is idempotent.** `run()` calls it on KeyboardInterrupt and
  `main()` catches one too; asking twice, and acting on the second
  answer, is not a thing this should ever do.
- **The launcher had to stop killing the child first.** `Child.stop()`
  went straight to `terminate()` — on Windows `TerminateProcess`, which
  nothing can catch — so the prompt would have been killed before it
  could be answered. Children share this console, so a Ctrl+C has
  already reached them and they are running their own shutdown: the
  launcher now WAITS for the child to exit and only terminates one that
  does not go. The coordinator's window is `SHUTDOWN_PROMPT_SEC + 30`
  (`start.shutdown_grace`, reading the same key the coordinator waits
  on) so the answer has room to be acted on; leg runners keep a short 5s.

Paper is never asked — those positions are not at a broker.

Worth knowing, and unchanged: closing the console window with the X, or
killing from Task Manager, runs none of this. The position stays open
and restart recovery is what picks it up.

## The manual panel, on one grid (2026-08-25, operator)

"Can you make this better with the spread in the centre and Bid and Ask
below that? Make all the boxes in a grid with minimum whitespace.
'(fill here)' is not needed."

- **The price is CENTRED above its two touches**, the same shape the
  Short/Long cards use — so the number an order fills at is read the
  same way wherever it appears. It was a `label: value` line with the
  touches under it, which made the panel that places orders the only
  place on the page where the executable spread looked like a footnote.
- **All six controls are ONE grid** (`.mt-grid`, `repeat(6, 1fr)`)
  rather than four stacked Bootstrap rows. Six tracks divide evenly by
  2 AND by 3, so the Direction/Lots pair and the Entry/TP/Stop trio
  share the same column edges. Measured in Chromium at the real
  `col-lg-3` width: left 1070 and right 1380 on every row, with
  Overnight spanning both. Four independent rows each sized their own
  columns, so nothing lined up down a panel four inches wide.
- **Local CSS, like `.pos-grid`** — a blocked CDN must not turn this
  back into a stack of full-width controls.
- **Labels WRAP; `nowrap` was the fault.** A third of this column is
  ~100px and "Take Profit" carries its own % box, so nowrap ran it over
  "Stop Loss". With `align-items: end` a wrapped label makes its row
  taller and every input still bottom-aligns, which is the edge that
  has to hold. The % box also moved to sit directly after the words
  instead of being pushed to the cell's right edge by
  `justify-content-between`, where it sat hard against the next label
  and the two read as one.
- **"(fill here)" is gone.** The panel's whole job is to fill, so the
  suffix said nothing on every reading of it. A test forbids the string
  returning.
- The driver gained a **spill check** — no label may extend past its own
  grid cell — because the overlap was visible in a screenshot and
  invisible to every edge measurement that only looked at the inputs.
  It also needs `box-sizing: border-box` in the injected stub, since
  Bootstrap's reboot sets it globally: without it the inputs overflowed
  their tracks and the alignment assertions failed on the stub's fault,
  not the page's.

Card height 326px -> 325px, and one fewer vertical rhythm to follow.

**The TP % box then clipped its own value** (operator, same day:
"Increase the TP % box. The values are not visible") — it rendered
`0.:` at 2.2rem. A field whose value cannot be read is worse than no
field, because it still submits. Now 3.4rem with the spinner arrows
dropped, which is ~14px back on a box this narrow and no loss: nobody
nudges a profit target by 0.1 with a mouse. Sized in the stylesheet, so
width and spinners live together and a test can read the number; an
inline width would beat it silently. Measured in Chromium at `1`, `0.5`,
`12.5` and `100` — `scrollWidth == clientWidth` on all four, which is
the browser saying the text fits.

That first pass bought the width by letting the label WRAP, and the
operator's next words were "the Take Profit label going to two lines
looks odd" — rightly: the % box then sat on its own line directly above
the take-profit input, reading as a second field stacked over the real
one. The room comes from the GRID instead. Twelve tracks rather than
six, split **4 / 5 / 3** — the three level fields were never equally
wide, and an even third was the assumption at fault. Take Profit gets
the extra track its box needs; Stop Loss has the shortest label on the
row and gives one up. Twelve still divides by 2, so the Direction/Lots
row keeps the same outer edges. Card back to 327px.

Measuring the wrap needed a second attempt too. A label's HEIGHT does
not answer it — the inline % input is taller than a line of text all by
itself, so the one-line label measured 1.5 lines and the driver reported
a wrap that was not there. Counting distinct line-box tops from a Range
over the label's contents is the reading that means what it says.

## Crisp like a trading platform (2026-08-24, operator)

Four passes over the same two cards, each one the same fault: the
LAYOUT was carrying a meaning the numbers do not have.

- **"This doesn't work. It should be crisp like a trading platform."**
  The carry card had just been rewritten into a plain-English sentence,
  which was the wrong register for a price screen. It is now a ladder:
  small uppercase labels, monospace figures right-aligned in one column,
  `SPREAD / BREAKEVEN / EDGE` over `CONVERGENCE / SWAP / FEES / NET`.
  Same numbers, no sentences. The per-leg swap rows read
  `XAUUSD L −58.00/N   −$103.63` with the prose derivation in the
  tooltip.
- **"All numbers in the card should be around the same size."** The
  Signal & Position card had the z-score at 2rem, the two tradable
  spreads at 1.5rem on a second tier, and the mid at 1.5rem on a third.
  Nothing lined up, and the sizes implied a ranking that is not there —
  the z-score, the two executable spreads and the mid answer four
  different questions, none of them a footnote to another. All four now
  sit on ONE baseline at ONE size, with the touches under each spread.
- **"Current spread needs to be better represented with the Short and
  Long spread with Bid and Ask."** The Manual Spread Trade panel showed
  the MID while `_check_manual_arm` fires on the executable side, so the
  operator was arming against a level the engine never compares against.
  It now shows the spread for the SELECTED direction, labelled "Short
  spread (fill here)", with its two touches underneath, and it
  re-renders when the Direction selector changes.
- **"This number needs to be update equally fast."** That panel polls
  `/api/manual-trade` every 3s, ten times slower than the dashboard's
  own 300ms tick, so its price lagged the card beside it — on the one
  panel that places orders. `updateSignal` now re-renders it on every
  tick, immediately after refreshing the touches it reads.
- **The MID spread tile is gone** ("Remove this number. It doesn't make
  any sense"). A midpoint of two midpoints: no order fills there, and
  with both executable spreads on the card it sat between them adding
  nothing anyone acts on. It is still the series mu/sigma/z are measured
  on and still published. The element and all three write sites were
  removed TOGETHER — one of them had no null guard, so leaving the JS
  would have thrown inside `updateSignal` and killed the handler, the
  same failure mode as the 2026-08-06 temporal dead zone and just as
  invisible from Python. A test now forbids the id reappearing.
- The bid/ask lines under each spread went 0.6rem -> 0.74rem. They are
  read against the book, not decoration.
- **"Round off to 2 decimal places."** `$-4.46000000000946` on the
  Analysis tiles. Sums and quotients of floats, so the binary
  representation leaks through. Rounded in `statistics_from_rows` —
  at the SOURCE, so every consumer is fixed at once rather than leaving
  the next caller to remember a format string. The tiles also dropped
  from base.html's 1rem padding and 1.5rem value to 0.5rem and 1.15rem:
  eighteen tiles in three rows is a scannable grid, not a set of panels.

## The carry card, in words (2026-08-24, operator)

"Explain all this in simpler terms ... Easy for someone non technical to
understand. You only want to know if The Spread is more expensive now or
not. Make all language simple and less words."

The card had grown four rows of dollars (convergence / carry / round
trip / net) plus three of spread, and the operator only ever asked it
ONE question. It is now two numbers and an answer:

    Spread now   56.55        Needs to beat   52.43
              Wide enough — 4.12 to spare

with the arithmetic kept as a single sentence underneath: "At 0.02 lots,
waiting 89 nights costs $103.63 in swap and $1.22 in fees. If the spread
closes to zero it pays $113.09. You keep $8.24." Plain words were asked
for; the working disappearing was not.

`carry.sanity`'s warning REPLACES the verdict ("Check the swap first")
rather than printing beneath it, for the same reason as before: a
conclusion drawn from an input the engine can prove wrong should not
render.

Two dialogs removed the same day, both restating a question their own
control had already asked: the manual-trade confirmation (every level is
typed in a field directly above the button, and the bypass warning is
printed permanently underneath) and the close-position confirmation
(Market and Limit are separate buttons, each named for what it does;
both are disabled while a close is in flight, which is what actually
prevents a double-fire). The destructive RESETS still confirm — they
cannot be undone and have no second button naming the mode, and a test
pins that distinction.

Also: `webapi` published `futures_bid` / `futures_ask` while the
coordinator's asset block names them `fut_bid` / `fut_ask`, so the new
provenance lines under each executable spread read "fut bid —" beside a
live spot price. And the Signal & Position row was `align-items-center`,
which floated the Z-score half-way down the card once the spread column
grew two headline numbers; it is top-aligned now, and the in-position
detail rows dropped their per-row gutters for a line-height, which was
most of that card's height.

## "Why x 2? Why is the swap charged twice?" (2026-08-24, operator)

It is not. `56.5400 x 2` was `|spread| x k`, and `k` is
`sizing.spread_units` — leg B's lots times its contract size, here 0.02
x 100 = **2 OUNCES**. Printed bare it reads as a factor of two, and the
carry line beside it happened to be a similar magnitude, so the pair of
them looked like one number doubled.

Nothing was wrong in the arithmetic and everything was wrong in the
label. The unit was missing, and the two rows are on DIFFERENT bases
for good reason: the spread is per UNIT of the underlying while swap is
quoted per LOT. Both now show their derivation —
`56.5400 x 2 units (0.02 lots x 100)` and `0.02 lots x 89 nights` — so
neither can be read as the other.

The rule this keeps breaking: a multiplier with no unit is not a
checkable figure. Same fault as the bare fair value, the bare round
trip, and the bare carry total before it.

Removed the same day, all operator calls:

- **The fair-value row is gone from the spread card.** It survived three
  rounds of pushback — bare number, then derivation, then derivation in
  a tooltip — and it is REFERENCE sitting above the two prices that get
  filled. Still COMPUTED and still published, because `carry.sanity`
  reads it to catch a swap entered with the wrong sign; deleting the
  computation would take that check with it
  (regression-tested both halves).
- **The carry decay table is gone.** The shape was the argument for it —
  flattening onto the round trip rather than onto zero — and the
  operator did not need seven rows to hold that.
- **Carry to Expiry moved below Manual Spread Trade**, which is the card
  it informs.
- **Each executable spread shows the two touches it is built from**
  (`fut bid` / `spot ask` under short, `fut ask` / `spot bid` under
  long). The provenance is the point: they come from opposite sides of
  the book, and a bare pair of numbers does not show that.

## A pair could be created but never removed (2026-08-07)

Assets were only ever created — implicitly, by saving a symbol on a
broker row. Nothing could rename, disable or delete one. Two rows the
operator could not get rid of proved it: `XAUUSD_/GC1225`, a phantom
born of the old label-into-the-key bug, and `SILVER`, whose futures
symbol does not exist on the account. Both warned at every startup.

That is not cosmetic. `_setup_symbols` trades EVERY enabled asset whose
two symbols resolve, so a leftover row is one resolving symbol away
from a second live position on the same underlying, with its own sizing
and its own book.

`/api/assets` (GET list, POST rename/enable, DELETE) plus a Configured
Pairs table on Settings. Notes:

- The route is `<path:key>`, not `<string:key>`. The pair that most
  needs deleting has a SLASH in its key, and Werkzeug's default
  converter stops at one — a plain route 404s on precisely the row it
  exists to remove, whether the slash arrives raw or as %2F.
- A rename carries the recorded history across (`trades`, `positions`,
  `market_data`, `trade_review`, `sd_touches`, `shadow_trades`).
  Renaming GOLD to WTI_BRENT otherwise strands every past trade and the
  whole warm-start window. `series_key` still guards correctness — the
  rename does not make old rows usable if the SYMBOLS changed, it only
  stops them being orphaned.
- A pair with an open position returns 409 for all three operations:
  renaming orphans the position from its history, disabling or deleting
  takes it out of the exit loop.
- `showPrompt()` was added to base.html's shared modal rather than
  reaching for the native `prompt()`, which a test forbids.

Also added, both of which had NO control at all: `SIGNALS.MIN_SIGMA` —
the ONLY defence against a collapsed sigma manufacturing a tradable z,
which CLAUDE.md has flagged as residual risk since the z=53,026
incident — and `SIGNALS.MAX_ABS_Z`.

`fairvalue.mislabelled_pair` flags a live spread more than 3x its own
carry value away: the operator ran WTI vs Brent under a SPOT_FUTURE
label left over from gold, so the card rendered a theoretical basis two
orders of magnitude from the traded one, which reads as enormous edge
rather than as a mislabelled pair. It surfaces as a `pair` row in the
health block under a new `WARN` state — deliberately NOT in the "held
up by" set, since it reports something worth reading that is not
stopping anything.

## One account holding BOTH legs was unrepresentable (2026-08-07)

Operator: "Both the Legs are connected. Why this error?" — the
Exchanges page showing "No account is mapped to the FUTURES leg — the
coordinator cannot start" over a coordinator that had started fine
("spot on [X], futures on [X]" in the same session's log).

The page modelled ONE role per account. `api_exchanges`' `role_of`
returned `'SPOT' if legs['spot'] == name else 'FUTURES' if ...`, so on
the one-account topology (`leg_accounts = {'spot': X, 'futures': X}`)
the first match won, the row badged SPOT, and the banner then computed
FUTURES as unmapped. Pure display fault — the config was correct.

But the form was worse than the badge: the Leg selector had no way to
say "both", the single Symbol box mapped to whichever role the row
reported, and **the futures symbol was therefore unreachable from the
UI on this topology**. The only way it ever got set was the accident
that saving a role never RELEASED the other one, so an operator who
saved the row twice (once as Futures, once as Spot) left both mappings
behind.

Now: `roles_of` returns a list, the row renders a badge per role and
both symbols, the banner unions every row's roles, the selector has
"Both legs — one account" with a second symbol box, and saving
releases any leg the account no longer claims (without that, BOTH ->
single is unexpressible and the stale mapping survives every save).

## Oil exposed two things gold never could (2026-08-07)

The operator repointed the pair at USOIL_U6 / UKOIL_V6.

- **Every LIMIT scenario failed `10015 - Invalid price`** on oil while
  the identical code passed on gold. `place_pending_limit` enforced
  only the tick grid; MT5 ALSO requires a pending price to sit at
  least `trade_stops_level` POINTS from the market. CFI sets that on
  the energy symbols and leaves it 0 on XAUUSD_, so the bug could not
  appear until the instrument changed. `BrokerSession.legal_limit_price`
  now enforces both rules and RETURNS what it had to move and why —
  the re-peg path (`modify_pending`) is legalised the same way, since
  a stops level rejects every attempt to chase the market otherwise.
- **`fut_lot_size` was read, warned about, and thrown away.**
  `sizing.plan` takes `contract_b` from `asset['fut_lot_size']` and
  falls back to leg A's when unset — and `_adopt_broker_specs` never
  set it. Two legs with different contract sizes were sized as if they
  matched, off by exactly the ratio between them. Invisible on gold
  (100 oz both legs) and on this oil pair (1000 bbl both legs); one
  config away from live. The mismatch warning also told the operator
  to "fix HEDGE_RATIO", which is wrong and would redefine the spread
  series — beta is the price coefficient, and the hedge formula
  already divides by each leg's contract size.

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

Three flood/accuracy warts the same restart exposed, all fixed:

- **The broker-clock line reprinted every 30s at WARNING.** The offset
  is `tick.time - time.time()`, a one-second-resolution stamp against a
  continuous clock, so it alternates 10800/10799 and the exact-value
  dedup never matched. `_note_server_clock` now compares to the MINUTE
  — a genuine change (DST) moves 30 minutes or more, so it still warns
  when it matters.
- "still collecting — **0 more minutes** needed" (rounding down on a
  gate with seconds left). Rounds up.
- The LIVE banner printed "Clip size: 50.0 lots/leg" on a box running
  **notional** sizing at 1.15 lots. It is the last screen before real
  orders; it now states whichever sizing mode is actually in force.

## The unwind OPENED a second position (2026-08-24, LIVE)

A manual gold pair. The futures leg was refused — `10027 AutoTrading
disabled by client`, the algo button off in THAT terminal — so the
executor unwound the spot leg. Twenty seconds later:

```
Futures hedge filled nothing — unwinding 0.05 spot lots
[Account_Spot] unwound 0.05 lots of XAUUSD
Reconcile: orphan on [Account_Spot]: ticket 862 BUY  0.05 XAUUSD (1/3)
Reconcile: orphan on [Account_Spot]: ticket 863 SELL 0.05 XAUUSD (1/3)
```

862 was the entry and 863 was the "unwind". `PairExecutor._unwind` sent
`entry_side.opposite` as a plain market order, and **these accounts are
HEDGING mode**, where that does not close anything — it opens a second,
offsetting position. The engine logged success while the book held two
live rows.

The hedging rule was already in CLAUDE.md and the exit path already
obeyed it (`_market_close_ticket`); the unwind path never did. It only
surfaced now because the unwind is the rarest branch in the executor —
it needs one leg to fail outright — and the account it had been
exercised on was netting.

Contained, but not harmless: economically flat, so no directional
exposure, yet the two rows sat live for the 60s the reconciler's three
strikes take, and clearing them cost two more round trips. Had the
reconciler been down, they would have stayed.

`_unwind` now closes by TICKET, taking the entry's `position_tickets`
through from `_send_sliced`. Notes:

- **Volumes come from the BROKER**, not from what we sent — a ticket can
  be partly closed already, and asking to close more than is there
  fails the whole request. A ticket MT5 no longer lists is already gone
  and is skipped rather than treated as an error.
- **The excess-unwind walks tickets newest-first.** Only some of the
  spot lots come off in the partial-hedge branch, and the last child
  order is the one that took the position past what the hedge covers.
- **The opposite market order survives as a FALLBACK** — for a netting
  account, where it is the correct instrument, and for a ticket the
  broker refuses. Offsetting is worse than closing and far better than
  staying naked. It is now the exception and says so in the log.
- Regression test asserts the strong form: after a failed hedge the
  spot book is EMPTY, not "flat by offsetting", and no SELL was ever
  sent on the spot leg.

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

## A half-written config became a saved one (2026-08-11)

Operator: "No accounts are configured. Still it is connected to
accounts 100004 and 100006." The Exchanges page read "No accounts
configured yet" while the engine traded two of them from its in-memory
copy.

Nothing deleted them. The chain:

1. `Coordinator._persist_specs` / `_persist_trading` wrote config.json
   with a plain `open(path, 'w')`, which TRUNCATES the file and then
   streams JSON into it. Any reader in that window sees half a config.
2. `webapp.load_config_raw` caught the resulting `ValueError` and
   returned `{}`.
3. Every save on that page is a read-modify-write, so the next one
   wrote the `{}` back — taking the accounts, the leg mapping, the
   symbols and every setting with it.

Each step is individually reasonable, which is why it survived: a
tolerant reader is normally a virtue, and it is precisely wrong in
front of a read-modify-write.

Fixed at all three points:

- both coordinator writes go through a tmp file and `os.replace`, so
  the file is never half-there;
- `load_config_raw` distinguishes MISSING (legitimately empty, first
  run) from PRESENT-BUT-BROKEN, which falls back to `config.json.bak`
  and RAISES if there is none — refusing the save is the only safe
  answer;
- `save_config_raw` keeps that `.bak` and refuses to write a config
  that drops a non-empty `accounts`, `leg_accounts` or `assets`
  unless the caller passes `allow_shrink=True` (the two delete
  endpoints, which mean it).

A raised RuntimeError becomes a 503 with the reason rather than a
blank 500 page, because a dead button that says nothing is how this
went unnoticed.

## Changing a row's LEG rewrote the symbol (2026-08-11)

The Exchanges page read `MT5 · SPOT · UKOIL` with the FUTURES leg
unmapped. UKOIL is Leg B's instrument, sitting on Leg A.

The single Symbol box means Leg A on a SPOT row and Leg B on a FUTURES
row, but `updateLegFields()` only toggled the second box's visibility
and relabelled the first. Its CONTENTS were left alone, so switching
the selector carried the old leg's symbol across and the save wrote it
to the new leg — `spot_symbols = ['UKOIL']`, with the futures mapping
correctly released (the row no longer claimed it). One more save and
both legs would have been Brent against Brent.

Fixed by keying the symbols to the LEG rather than to the box:
`_LEG_SYMBOLS = {spot, futures}` is kept current on every `input`
(under the role in force at typing time), and the selector only
RE-RENDERS from it. Typing on one leg is preserved rather than
overwritten or leaked. Verified in Chromium across every transition.

Backstop in `/api/exchanges`: a save that would leave ONE account
holding both legs on the SAME symbol is refused. Scoped to one
account on purpose — two different accounts quoting the same symbol
name is the cross-broker case and is the entire point of the
architecture.

**A hidden input still SUBMITS** (same day, operator: "After saving
EU50 on Leg B and restarting it still saves UKOIL on Leg B"). The
second symbol box is hidden on a single-leg row but was still posted,
so the save carried `symbol=EU50` next to a stale
`futures_symbol=UKOIL` — and the server's `fut_symbol or symbol`
preferred the field the operator could not see. Two fixes, because
either alone leaves the other half live: the box is now `disabled`
whenever the row is not BOTH (a disabled control is left out of the
request entirely), and the ROLE decides the assignment with no
fallbacks — FUTURES takes `symbol`, SPOT takes `symbol`, only BOTH
reads the second box.

## One account needs one PORT, one LOGIN and one TERMINAL (2026-08-18)

Three ways to write down "these are two accounts" while describing
one, and each was found the hard way:

- **one port** — only one process binds it, so the second runner dies
  or both legs talk to the first (2026-08-11);
- **one login** — the same MT5 account under two names, hedging
  against itself;
- **one terminal installation** — a terminal holds a single login, so
  two accounts pointing at one folder are one account whatever the
  rows say (2026-08-18, twice: `Account_Spot` and `Account_Future`
  both on `...MetaTrader 5 - 1`, the dashboard showing login 100006
  against both).

`_endpoint_clash`, `_login_clash` and `_terminal_clash` refuse all
three at SAVE. The coordinator refuses the port and terminal cases at
startup too — belt and braces, and startup is where the earlier ones
were caught — but a refusal at save is a corrected field, while a
refusal at startup is five restart attempts with the reason scrolling
past. The terminal comparison lowercases both sides, matching what
`_resolve_legs` does, so the save cannot pass something startup then
rejects.

## Two accounts, one port (2026-08-11, operator: "Facing some issue")

Adding a second account. The log showed both names against the SAME
endpoint:

```
Leg 'Utsav Khanchandani' not reachable at 127.0.0.1:9101 ...
Leg 'MT5'                not reachable at 127.0.0.1:9101 ...
```

`normalise_endpoint` checked the FORM of an endpoint and nothing
checked that two accounts did not claim the same one. That is the
shared-terminal fault (already refused) one layer down, and it is worse
because it can half-work: only one process can bind a port, so the
second runner either fails to start or — if the first won the race —
BOTH legs connect to it, trade the SAME MT5 account, and every screen
goes on reporting two. Now refused at save (`_endpoint_clash`, naming
the account that holds it and the next free port), in the bulk save,
and at startup in `_resolve_legs`.

Two more things the same session exposed:

- **`[DIAGNOSE] PASS: 29 pass, 0 warn, 0 fail` while the config was
  broken.** The checklist runs inside the coordinator against the
  RUNNING config, and accounts/leg_accounts are structural, so it was
  faithfully describing the single-account setup still in memory. An
  operator reads that as "my new setup is good". The coordinator now
  publishes `running_legs` / `running_endpoints`, and the Exchanges
  page shows a restart-pending banner saying in terms that Test,
  Diagnose and the order suite describe what is RUNNING, not what is
  saved.
- **Hundreds of identical "not reachable" WARNINGs.** The webapp opens
  a short-lived RemoteLeg every time the Exchanges page polls (15s, two
  legs), so one down runner buried the coordinator's own output.
  `RemoteLeg._reported` dedups per (account, host, port) at CLASS level
  — the clients are short-lived, so instance state would never match
  twice — and a leg that reconnects clears its entry, because coming
  back is news too.

## Commission had no control and four dead ones (2026-08-10, operator)

Operator: "for every lot traded - the broker charges a commission or
brokerage. This would get included in the cost and the edge filter.
Provide a section in the Settings - near the charges where this can
also be included."

The engine side was already right — `costs.round_trip_cost` charges
`COMMISSION_PER_LOT_SPOT x lots_a + COMMISSION_PER_LOT_FUT x lots_b`,
each leg on its OWN lots — and `COSTS` is a hot-reload section. What
was missing was any way to set it, and what was there instead was
worse than nothing:

- **The four maker/taker bps boxes were decoration.** `spot_maker_fee_bps`
  and friends are not in `webapi.FIELD_MAP`, so the page posted them and
  the server dropped them. They defaulted to W3's crypto-venue numbers
  (8/10/2/5 bps) and sat under a summary line that totalled them, so the
  panel looked like it was working. An operator entering their brokerage
  there changed nothing at all.
- **`COMMISSION_PER_LOT_SPOT` / `_FUT` and `SPREAD_COST_FACTOR` were
  mapped but had no control anywhere**, which is why CLAUDE.md has said
  "COMMISSION_PER_LOT_* are still 0.0 and MUST be set" since the cost
  measurements were taken, with no way to act on it.

Replaced by a **Broker Charges** section holding the three real keys.
Notes:

- Commission is **per lot, per leg, round turn**, and the field is
  labelled with the running symbol — a lot is a different amount of
  money on each instrument, and "/ lot" alone invites the Leg B figure
  being typed into the Leg A box.
- **It defaults to 0 and must never default to anything else.** A
  fabricated cost is charged against every trade by the edge filter and
  the operator cannot tell it was never their number. The dead fields
  defaulting to 8 bps is exactly that failure.
- The preview computes the round trip with the SAME arithmetic as
  `costs.round_trip_cost`, from the engine's published `rt_lots_a/b`,
  `rt_contract_a/b`, `rt_spot_spread`, `rt_fut_spread` — recomputing
  the sizing in the page would be a second implementation that drifts.
  It states commission's share of the total and what the edge filter
  therefore demands, because that is the only reason the number matters.
- `SPREAD_COST_FACTOR` is on the same panel and says plainly that
  lowering it without measuring limit fills is how a model starts
  trading through its own costs.

## HEDGE_RATIO belongs to the PAIR (2026-08-10, operator)

Operator: "Can you make sure the Hedge Ratio is calculated and changed
everytime the pair is changed?" Beta is a property of the pair, and
nothing carried it across a pair change — the symbols are edited on the
Exchanges page, the launcher restarts, and the previous instrument's
beta is still in config.json defining the spread. Three incidents in
one day, all the same shape:

    beta 10       USOIL/UKOIL at 81.76 / 85.07  ->  spread  -732.53
    beta 0.0149   XAGUSD/XAUUSD                 ->  a 5,167-lot hedge
    beta 66.94    left on USOIL/UKOIL           ->  spread -5469.59

Two of those came from advice this repo used to give (the contract-size
check told the operator to "correct HEDGE_RATIO for the difference" —
wrong, and now gone). The third needed no advice at all: changing the
instruments was enough.

`statarb/hedgeratio.py` holds the whole rule, shared by the startup
adoption, the Settings suggestion and the Exchanges checklist — three
copies of "what should beta be" would eventually recommend one number
while a restart applied another.

- **The value is STAMPED** with `TRADING.HEDGE_RATIO_FOR`, the pair it
  was computed for (`"USOIL|UKOIL"`). Without a stamp a stale beta and
  a deliberately tuned one are indistinguishable, and the engine would
  have to either overwrite the operator's work or ignore the problem.
- **Stamp matches the running pair -> left alone.** Beta is a strategy
  parameter; an operator who tuned it keeps their number.
- **Stamp names a DIFFERENT pair -> re-derived** and written back to
  config.json. This is the case the operator asked for.
- **No stamp** (an install predating this) -> the value is KEPT and
  merely stamped, unless it is implausible against the live prices,
  which settles the question on its own.
- **`suggest()` depends on `pair_type` AND on whether the two prices
  are on the same scale.** Three cases, and two of them are beta 1:
  - Same underlying -> 1, and the spread IS the basis. The price ratio
    there (~1.014 on gold) collapses a $59 basis to pennies.
  - Different instruments, SAME scale (WTI 83 vs Brent 86, both $/bbl)
    -> 1, and the spread is the DIFFERENTIAL. This case used to fall
    into the ratio branch and produced the operator's "Why is the
    spread Incorrect?" (2026-08-10): +3.30 became -0.05. Nothing was
    miscomputed — 86.4550 - 1.04 x 83.1750 really is -0.047 — but the
    ratio CENTRES the series on zero by construction, discarding the
    level that names the trade. It buys nothing: the legs move together
    so sigma is all but identical, and only readability changes.
  - Different instruments, DIFFERENT scale (silver 65 vs gold 4,352)
    -> the price ratio, because beta 1 there is not a spread at all,
    it is gold's own price with a rounding error subtracted.
  The boundary is `COMPARABLE_LOW/HIGH` (0.5-2.0), deliberately wide:
  the only question it answers is "would beta 1 be dominated by one
  leg?", and at 2x it still would not be.
- **It runs inside `_setup_symbols`, before `_warm_start`**, because
  `_series_key` includes beta and the warm start seeds the window from
  rows matching it. Re-deriving after the seed would hand the strategy
  a mu and sigma the live spread never visits.
- **Never while a position is open.** Beta defines the series the
  position was entered on. The book is read from the DB directly, since
  position recovery has not run at that point; it logs CRITICAL and
  leaves the value alone.
- **The UI stamps its own saves** (`webapp._stamp_beta`). Otherwise:
  change the symbols, type the right beta in Settings, restart — and
  the engine sees a stamp naming the old pair and overwrites the number
  just typed.

Alongside it, `Coordinator._implausible_spread` now **BLOCKS entries**
rather than warning. It was a WARN, deliberately outside the "held up
by" set, and that was the wrong call for this check: the engine must
not open a position on a series it can prove is not the difference
between the two prices it is quoting. Exits are evaluated earlier in
`process_asset` and are untouched — a wrong beta must never trap a live
position. The threshold (spread > half the smaller leg) lives in
`hedgeratio.implausible` so the engine cannot adopt a beta it will then
refuse to trade on.

The drift badge also stops saturating: as a percentage OF the
configured beta, 66.94 against a live 1.0413 reads "-98.44%" and so
would 6,694. Past 2x it now says "64.3x too high".

## A blank manual Lots box meant CLIP_LOTS (2026-08-24, operator)

Operator: "Last manual trade: 50 lots exceeds MAX_LOT_SIZE 10 ... Why is
50 the lot size?" They had left the Lots box blank, and `_manual_open`
fell through to `TRADING.CLIP_LOTS` — which `config.example.json` ships
at 50, `start.py` copies verbatim on first run, and nobody had chosen
for that box.

The fallback was wrong for a deeper reason than the number. CLIP_LOTS is
only the anchor under `lots` sizing. Under `notional` sizing a SIGNAL
entry goes through `_sizing_plan` -> `sizing.plan`, which honours the
sizing mode, the volume step, each leg's minimum and the streak reducer
— so the dashboard read 1.15 lots from NOTIONAL_PER_LEG_USD while a
blank box on the same pair meant 50, roughly **$21m per leg of gold**.
Same panel, same pair, two orders of magnitude apart, and MAX_LOT_SIZE
was the only thing that caught it.

A blank box now means the SIZING PLAN, so the manual and signal paths
agree about what one trade is. Notes:

- **`not lots`, not `is None`.** The panel sends null for a blank box,
  but 0 has to mean the same thing rather than an order for nothing.
  That is the one part of the old `lots or CLIP_LOTS` that was right.
- **A typed figure is used exactly as typed** — past the volume step and
  past the streak reducer. Manual means manual, the same rule as a
  hand-entered exit target; MAX_LOT_SIZE and `_precheck_pair` still
  stand behind it.
- **A plan REFUSAL is reported, not worked around.** Below the pair's
  minimum notional the plan returns a reason and no lots; falling back
  to CLIP_LOTS there would place a trade the engine had just called
  untradable.
- Market data is fetched BEFORE the size is chosen now, because the
  plan needs prices.
- The placeholder read `clip` — a config key rather than a number, and
  under notional sizing the wrong key. It now shows the engine's actual
  derived lots, so the size is visible before arming.

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
`costs.expected_capture` was corrected 2026-08-11 (below) and
**ExitLadder and slippage.py were the other half, corrected 2026-08-25**
(see "One multiplier, everywhere").

**The edge filter measured capture on the wrong leg** (2026-08-11,
operator: "Why is the Edge so low?"). On GER40/EU50 at beta 0.2483 the
Filters card read `0.5 x z 1.84 x sigma 0.6897 x 1 = $0.63` against a
$17.40 round trip. Every line of that was internally consistent, and
the `x 1` was the fault: leg A traded 1 lot of a 1-unit contract while
leg B traded 4, so the same move was worth $2.54. Cost was already
split per leg (`round_trip_cost` takes `lots_b`/`contract_b`); capture
was not, so the filter compared leg B's dollars of cost against leg
A's of capture and understated the edge by exactly 1/beta. Invisible
on gold and oil at beta 1, four-fold here. `expected_capture` and
`edge_ok` now take leg B's size, and both call sites pass the sizing
plan. The verdict on that pair did not change — corrected, it is still
10x short — but the number the operator reads to judge a pair was
wrong.

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
- **config.example.json used to ship PRODUCTION scale** (CLIP_LOTS 50,
  SLICE_LOTS 10, MAX_LOT_SIZE 50, DAILY_LOT_TARGET 500) and start.py
  copies it verbatim on first run, so a fresh install was ~$43m of gold
  notional out of the box. The code defaults in config.py are 1.0, but
  they only apply when a key is ABSENT, so the file always won. FIXED
  2026-08-24 (operator: "Set CLIP_LOTS to something sane. It shouldn't
  be 50") — the four sizing keys now ship AT the code defaults
  (1 / 0 / 0 / 1), and `tests/test_startup_config.py` fails the build if
  the example ever exceeds them again, if the slice is bigger than the
  clip, or if the cap is under the clip. The owner's 500/50/10 spec is a
  target to scale UP to on a configured account; the file that seeds
  first runs is the wrong place to record it. This was flagged here on
  2026-08-07 and was still live on 2026-08-24, when a blank manual Lots
  box picked up the 50 — a note in a memory file is not a guard.
  Still true, and still unguarded: there is no pre-trade margin check —
  only the edge filter and MT5's own margin rejection stand in the way.
  `RISK_LIMITS.MAX_EXPOSURE_USD` is defined and **never read by
  anything** — a dead key like `swap_charge`, so the $200m in the
  example was never a limit at all.

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
