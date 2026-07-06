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

## Architecture (as of 2026-07)

```
main.py                 single-account entry point (legacy flow)
run_leg.py              leg runner: one process per MT5 account
run_coordinator.py      coordinator: fuses legs, signals, routes orders
statarb/
  config.py             file-based config + AccountConfig (.env passwords,
                        endpoint per account) + TRADING (clip/slice/target)
  broker.py             BrokerSession — ONLY module that imports MetaTrader5
  legs.py               LocalLeg / RemoteLeg — one interface for both
  ipc.py                JSON-lines over localhost TCP
  leg_runner.py         LegServer — serves ticks/orders for one account
  pair_executor.py      cross-account clips: slicing, hedge match, unwind
  coordinator.py        Coordinator — the multi-account trading loop
  marketdata.py         basis/swap math shared by both loops
  models.py             enums (OrderSide, SignalType, ...), Trade, Position
  execution.py          OrderManager — single-account entry/close pairs
  positions.py          PositionManager — lifecycle + P&L (contract-size aware)
  risk.py               RiskManager — limits, stop loss, lots-today tracking
  signals.py            SignalGenerator — swap-premium thresholds
  performance.py        PerformanceTracker
  database.py           DataLogger (SQLite)
  system.py             AlgorithmicTradingSystem — single-account loop
tests/                  pytest suite with fakes (runs anywhere)
legacy/                 original monolith, superseded — do not extend
```

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
3. Futures expiry dates in config must be set to the live contract.
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
