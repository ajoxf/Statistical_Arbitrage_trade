Run a pre-live safety review of this trading system:

1. Run `pytest tests/ -q` and report the result. If anything fails, stop and fix before continuing.
2. Re-read statarb/execution.py, statarb/positions.py and statarb/risk.py and verify:
   - every order path handles a failed/partial fill,
   - a failed hedge leg is always unwound,
   - positions can always be closed (both signal exit and stop loss),
   - risk limits (lot size, daily trades, max positions) cannot be bypassed.
3. Check config: futures_expiry dates are in the future, swap_charge values are set, and no credentials appear in any tracked file (`git grep -i password` should only hit password_env references and docs).
4. Report a clear GO / NO-GO verdict for LIVE mode with reasons.
