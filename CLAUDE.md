# Statistical Arbitrage Trade — Project Memory

Basis/stat-arb trading system on MetaTrader 5 (Gold & Silver, spot vs
futures, swap-cost-based signals).

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
  spread.py             SpreadStats: frozen mu/sigma/z on swap_diff,
                        AR(1) half-life, trend slope
  signals.py            SignalGenerator (legacy) + ZSignalGenerator (gates:
                        ceiling, trend, cooldowns, z-reset, edge filter)
  costs.py              round-trip cost model + edge filter
  exits.py              ExitLadder — dollar levels frozen at entry
  reconcile.py          orphan close / ghost clear, 3-strike, ledger
  notify.py             Telegram alerts + /status /positions /pnl
                        (background thread, never blocks the loop;
                        token/chat from .env only)
  webapp.py             read-only Flask dashboard (own process, reads
                        SQLite + runtime_status.json)
  marketdata.py         basis/swap math shared by both loops
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
tests/                  391 tests, all fakes, no MT5 (runs anywhere)
legacy/                 original monolith, superseded — do not extend
```

## Strategy (decided 2026-07; spec v2 applied 2026-07)

- Entries: z-score on swap_diff (basis minus swap-implied basis — the
  carry-detrended spread). ALL gates must pass: warm stats, |z| >=
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
  as absolute swap_diff values (ExitLadder.spread_levels).
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

## Repo branch map (2026-07)

- `main` = this system (fast-forwarded 2026-07).
- `claude/limit-orders-trade-exits-q0quM` (June): older parallel
  system (adapters/, feature_files/ web app, Telegram bot, OKX).
  Fully superseded 2026-07: MT5 lessons, Telegram notifications and
  the web dashboard are all ported to this system. Only the OKX
  adapter remains unported — the sole reason to keep that branch.
- Other `claude/*` branches: session artifacts, superseded.

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
- futures_expiry is OPTIONAL (owner: "just pick up the symbols, don't
  get into expiry"). Missing expiry used to KeyError in
  validate_expiries and kill startup. Now: no expiry -> the engine
  trades the RAW basis (swap_diff = futures - spot, carry_adjusted
  False); an expiry in the future -> carry-detrended as before; a PAST
  expiry -> warns AND falls back to the raw basis instead of zeroing
  the spread (a zero spread means z never moves — a dead engine that
  looks alive). Two accounts at the SAME broker need two separate MT5
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
  contract silently zeroes the swap basis and disables signals
  (a warning is logged at startup).
- MT5 package is Windows-only; on Linux/dev machines everything but
  the live connection works (tests, config, imports).
