# Statistical Arbitrage Trade

Automated basis trading system for MetaTrader 5 — Gold & Silver, spot
vs futures, with signals driven by real swap-cost analysis.

## How it works

- Computes the **swap-implied basis** (what carry actually costs you)
  and compares it to the **market basis** (futures − spot).
- When the market premium is rich (default > +20%): **SELL BASIS**
  (buy spot, sell futures). When at a deep discount (< −15%):
  **BUY BASIS** (sell spot, buy futures). Exits when the premium
  normalizes, with stop-loss and position/trade limits throughout.
- PAPER mode simulates; LIVE mode places real IOC market orders with
  hedge-leg protection (if the second leg fails, the first is unwound).

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

## Project layout

```
main.py              single-account entry point (legacy flow)
run_leg.py           leg runner — one per MT5 account
run_coordinator.py   coordinator — signals, pairing, routing
statarb/             trading package (broker, legs, pair_executor, ...)
tests/               pytest suite with fakes (no MT5 needed)
legacy/              original single-file version (superseded)
```
