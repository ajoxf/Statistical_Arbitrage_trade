# Statistical Arbitrage Trade

Automated basis trading system for MetaTrader 5 — Gold & Silver, spot
vs futures, with signals driven by real swap-cost analysis.

## How it works

- Computes the **swap-implied basis** (what carry actually costs you)
  and z-scores the gap between it and the market basis (`swap_diff`)
  over a rolling window with a **frozen anchor** (μ/σ refresh every
  `STATS_INTERVAL_SEC`, so the mean doesn't chase the spread).
- **Entries** need every gate to pass: warm stats, `|z| ≥ ENTRY_Z`,
  below the entry ceiling (`STOP_Z`), trend filter (never fight a
  running spread), cooldowns and the z-reset gate after stops, and the
  **edge filter** — expected capture must clear 1.5× round-trip cost.
- **Exits act on dollars, not z** — levels frozen at entry from actual
  fills: dollar stop (ungated) > σ-fraction take-profit with a cost
  floor > gated reversion exit (never books a losing "profit-take") >
  max-hold at 4× measured half-life (only in profit) > z-stop backstop.
- **Execution is limit-first**: child orders rest at the peg, re-peg
  via order-modify as the market drifts, and cross the spread only on
  timeout. Stops always go straight to market. The futures hedge is
  sized to the actual spot fill; runt positions below 40% of the clip
  are fully unwound.
- **Self-healing**: positions persist to SQLite (crash-safe ordering),
  restarts recover and reconcile against the broker before trading,
  and a 20s reconciler auto-closes orphans / clears ghosts on a
  3-strike rule with an untracked-close ledger.
- **Circuit breakers**: daily-loss halt, −20% size after 3 straight
  losses, full pause after 6.
- PAPER mode runs the identical lifecycle through simulated fills at
  the touch; LIVE routes to the brokers. Accounts in **hedging mode**
  are closed by position ticket (never by opposite market order).

## Setup

```bash
pip install -r requirements.txt          # MetaTrader5 works on Windows only
cp config.example.json config.json       # edit symbols, accounts, expiry
cp .env.example .env                     # put account passwords here
```

`config.json` is gitignored — your account numbers stay local.
Passwords are never stored in config; each account names an
environment variable (`password_env`) read from `.env`.

**Keep `futures_expiry` current.** An expired contract date zeroes
the swap basis and disables signals (the system warns at startup).

## Run

```bash
python main.py                    # interactive prompts
python main.py --mode paper       # skip the mode prompt
python main.py --config my.json --mode live
```

## Test (no MT5 required)

```bash
pytest tests/ -q
```

The suite covers the money-touching paths — entry pairs, close pairs,
hedge-failure unwind, stop-loss logic, P&L contract sizing — against a
fake broker, so it runs on any OS.

## Multi-account: spot on Broker 1, futures on Broker 2

The MetaTrader5 package allows one terminal connection per process, so
each account gets its own **leg runner** process and a **coordinator**
fuses both price streams and routes each leg's order to its account:

```
leg runner A (spot terminal)  ←TCP→  coordinator  ←TCP→  leg runner B (futures terminal)
```

Start order (all on the same Windows machine, three consoles):

```bash
python run_leg.py --config config.json --account account_a
python run_leg.py --config config.json --account account_b
python run_coordinator.py --config config.json --mode paper   # or live
```

Each account in config needs its own `terminal_path` (two separate MT5
installations), `login`/`server`, and an `endpoint` (localhost port the
leg runner listens on). Leg runners stay up across coordinator
restarts.

**Same-account setups still work:** if both legs map to one account
(or an account has no `endpoint`), the coordinator connects to that
terminal in-process — no leg runners needed.

### Clip execution at size

Entries are executed as clips (`trading.CLIP_LOTS`, e.g. 50 lots/leg),
sliced into child orders (`SLICE_LOTS`, e.g. 10) so one IOC order
doesn't sweep the book. The futures hedge is always sized to what the
spot leg actually FILLED:

- spot fills nothing → abort, no hedge;
- hedge fills nothing → spot fill is fully unwound;
- hedge partially fills → unmatched spot excess is unwound, the
  position is kept at the matched size;
- any unwind failure logs CRITICAL (unhedged exposure — act manually).

`DAILY_LOT_TARGET` (e.g. 500) is a **throughput target, not a cap** —
progress is reported in the status line; nothing is rejected for
exceeding it. Hard limits are `MAX_LOT_SIZE`, `MAX_DAILY_TRADES`, and
`MAX_POSITIONS_PER_ASSET`. `HEDGE_RATIO` adjusts futures lots per spot
lot if the two brokers' contract sizes differ — verify it against both
brokers' specs before going live.

## Telegram notifications & commands

Create a bot via @BotFather, put `TELEGRAM_BOT_TOKEN` (and optionally
`TELEGRAM_CHAT_ID`) in `.env`, then send `/start` to the bot — the
chat id auto-registers. You'll get entry/exit alerts with fills and
the frozen exit plan, circuit-breaker halts, reconciler actions, and
startup/shutdown summaries. Commands: `/status`, `/positions`,
`/pnl`. Sends run on a background thread — Telegram can never block
or crash the trading loop. Leave the token blank to disable.

## Web dashboard (read-only)

```bash
python run_dashboard.py --db algo_trading.db --port 8080
```

A separate process (safe to run against LIVE) serving live status
(z-scores, lot-target progress, breaker state), open positions with
unrealized P&L, closed-trade reviews (entry/exit z, exit reason), and
the untracked-close ledger at http://127.0.0.1:8080. It reads the
SQLite DB plus `runtime_status.json`, which the coordinator refreshes
every ~10s.

## Project layout

```
main.py              single-account entry point (legacy flow)
run_leg.py           leg runner — one per MT5 account
run_coordinator.py   coordinator — signals, pairing, routing
run_watchdog.py      relaunches the coordinator on crash
run_dashboard.py     read-only web dashboard (own process)
statarb/             trading package (broker, legs, pair_executor, ...)
tests/               pytest suite with fakes (no MT5 needed)
legacy/              original single-file version (superseded)
```
