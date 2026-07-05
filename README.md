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

## Multi-account status (Leg A / Leg B on different accounts)

`config.json` already supports defining two accounts and mapping legs
to them (`leg_accounts`). **However**, the MetaTrader5 Python package
allows only one connection per process, so this build connects to a
single account (the spot leg's) and logs a warning if the legs differ.
True simultaneous streaming from two accounts requires one process per
account plus a coordinator — this is the next planned milestone. See
CLAUDE.md for the full design constraints.

## Project layout

```
main.py            entry point
statarb/           trading package (broker, execution, risk, signals, ...)
tests/             pytest suite with FakeBroker
legacy/            original single-file version (superseded)
```
