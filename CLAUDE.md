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
  simultaneous two-account streaming requires **one process per
  account** (each with its own terminal installation via `path=`)
  plus a coordinator that fuses prices and routes orders. This
  coordinator is NOT yet built — `leg_accounts` in config.json can
  name two accounts, but a single process will connect only to the
  spot leg's account and log a warning.
- **Never run LIVE mode without the tests passing** (`pytest tests/`).
  The money paths (entry pair, close pair, hedge unwind, stop loss)
  are covered by tests using a FakeBroker — no MT5 needed.
- Credentials live in `.env` (gitignored), referenced from
  config.json via `password_env`. Never in code, config, or chat.

## Architecture (as of 2026-07)

```
main.py                 entry point (argparse: --config, --mode)
statarb/
  config.py             file-based config + AccountConfig (.env passwords)
  broker.py             BrokerSession — ONLY module that imports MetaTrader5
  models.py             enums (OrderSide, SignalType, ...), Trade, Position
  execution.py          OrderManager — entry pairs, close pairs, hedge unwind
  positions.py          PositionManager — lifecycle + P&L (contract-size aware)
  risk.py               RiskManager — limits, stop loss
  signals.py            SignalGenerator — swap-premium thresholds
  performance.py        PerformanceTracker
  database.py           DataLogger (SQLite)
  system.py             AlgorithmicTradingSystem — wiring, loop, display
tests/                  pytest suite with FakeBroker (runs anywhere)
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

## Open decisions (waiting on owner)

1. Multi-account architecture: two processes + coordinator
   (recommended) vs sequential account switching. Blocked on owner.
2. Leg mapping: spot-vs-futures across accounts, or same symbol on
   two brokers (cross-broker spread — different signal math)?
3. Where the owner's "working config" lives — was never pushed.

## Conventions

- Develop on the designated `claude/...` branch; commit + push every
  session. No more versioning by filename (`15_..._f.py` era is over).
- Run `pytest tests/ -q` before any commit touching statarb/.
- Futures expiry dates in config must be kept current — an expired
  contract silently zeroes the swap basis and disables signals
  (a warning is logged at startup).
- MT5 package is Windows-only; on Linux/dev machines everything but
  the live connection works (tests, config, imports).
