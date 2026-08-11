"""Standalone MT5 connection checker — run this when the system is not
connecting to MT5 or orders are not appearing in the terminal.

It needs NOTHING else running: no leg runners, no coordinator, no
dashboard. It connects to each configured account IN TURN (one MT5
connection per process, so sequentially), and reports exactly where the
chain breaks, with the fix.

    python check_mt5.py                  # connect + read prices
    python check_mt5.py --order          # ALSO place a MINIMUM-lot
                                         # order, verify it in MT5, and
                                         # close it again
    python check_mt5.py --account account_a
    python check_mt5.py --config config.json

Run it on the machine the terminals are on, in the same Python
environment as the app (`(StatArb)` prompt), from the repo folder.
"""

import argparse
import os
import platform
import socket
import struct
import sys
import time

from statarb.config import AlgoTradingConfig

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

# What MT5's initialize errors mean, and what to do about them.
# Shared with statarb/diagnostics.py so the standalone checker and
# the Exchanges-page checklist can never give different advice for
# the same code — they did, and the web one was the wrong half.
from statarb.mt5_errors import INIT_ERRORS      # noqa: E402


ORDER_ERRORS = {
    10004: "Requote — the price moved; retry",
    10006: "Request rejected by the broker",
    10013: "Invalid request (usually a malformed field)",
    10014: "Invalid volume — below the minimum or off the volume step",
    10015: "Invalid price — must be a fresh tick, rounded to tick size",
    10016: "Invalid stops",
    10018: "Market is closed for this symbol",
    10019: "Not enough money for this volume",
    10027: "ALGO TRADING IS DISABLED IN THE TERMINAL — this is the most "
           "common cause of 'orders are not being placed'",
    10030: "Unsupported filling mode for this symbol",
    10031: "No connection to the trade server",
}

ok_count = warn_count = fail_count = 0


def line(status, message, fixes=()):
    global ok_count, warn_count, fail_count
    mark = {'ok': '  [ OK ]', 'warn': '  [WARN]', 'fail': '  [FAIL]',
            'info': '  [info]'}[status]
    if status == 'ok':
        ok_count += 1
    elif status == 'warn':
        warn_count += 1
    elif status == 'fail':
        fail_count += 1
    print(f"{mark} {message}")
    for fix in fixes:
        print(f"         -> {fix}")


def header(text):
    print()
    print(text)
    print('-' * len(text))


def explain_init_error(code):
    known = INIT_ERRORS.get(code)
    if known:
        return known
    return (f"MT5 returned error {code}", [
        "Open the terminal, log in, and make sure algo trading is on"])


def check_environment():
    header("Environment")
    bits = struct.calcsize('P') * 8
    line('ok' if bits == 64 else 'fail',
         f"Python {platform.python_version()} ({bits}-bit) on "
         f"{platform.system()}",
         () if bits == 64 else
         ["MT5 needs 64-bit Python — reinstall Python (64-bit) and "
          "recreate the virtual environment"])
    if platform.system() != 'Windows':
        line('fail', "This is not Windows — the MetaTrader5 package only "
                     "works on Windows",
             ["Run the system on the Windows machine where the terminals "
              "are installed"])
    if mt5 is None:
        line('fail', "MetaTrader5 package is NOT installed in this Python",
             ["pip install MetaTrader5",
              "Make sure you are in the same environment the app runs in "
              "(the (StatArb) prompt)"])
        return False
    line('ok', f"MetaTrader5 package {getattr(mt5, '__version__', '?')} "
               f"installed")
    return True


def check_config_files(config, env_path='.env'):
    """The two settings that silently take the whole system down: a
    malformed leg-runner endpoint (both processes crash at startup) and
    a .env line dotenv cannot read (the password never loads, so MT5
    login fails with no explanation)."""
    from statarb import ipc

    header("Configuration files")
    legs = config.leg_accounts or {}
    for role in ('spot', 'futures'):
        name = legs.get(role)
        if not name:
            line('fail', f"No account is mapped to the {role.upper()} leg",
                 [f"Exchanges page: edit an account and set its Leg to "
                  f"{role.title()}",
                  "The coordinator cannot start until both legs are mapped"])
        elif name not in config.accounts:
            line('fail', f"The {role.upper()} leg points at '{name}', "
                         f"which is not a configured account",
                 [f"Configured: {', '.join(config.accounts) or 'none'}",
                  "Re-assign the leg on the Exchanges page"])
        else:
            line('ok', f"{role.upper()} leg -> {name}")
    if legs.get('spot') and legs.get('spot') == legs.get('futures'):
        line('warn', f"Both legs are mapped to '{legs['spot']}' — one "
                     f"account carries the whole spread",
             ["Fine for testing; for the two-broker strategy map each leg "
              "to its own account"])
    for name, account in config.accounts.items():
        if not account.endpoint:
            continue
        try:
            host, port = ipc.parse_endpoint(account.endpoint)
            if f'{host}:{port}' != str(account.endpoint).strip():
                line('warn', f"{name}: endpoint '{account.endpoint}' was "
                             f"read as {host}:{port}",
                     [f"Set it to {host}:{port} on the Exchanges page"])
            else:
                line('ok', f"{name}: endpoint {host}:{port}")
        except ValueError as e:
            line('fail', f"{name}: {e}",
                 ["The coordinator AND this account's leg runner both "
                  "crash at startup until this is fixed",
                  "Settings > MT5 Brokers > this account > Endpoint"])

    ports = {}
    for name, account in config.accounts.items():
        if not account.endpoint:
            continue
        try:
            ports.setdefault(ipc.parse_endpoint(account.endpoint)[1],
                             []).append(name)
        except ValueError:
            pass
    for port, names in ports.items():
        if len(names) > 1:
            line('fail', f"Accounts {', '.join(names)} share port {port}",
                 ["Each account needs its OWN port — e.g. 9101 and 9102"])

    if not os.path.exists(env_path):
        line('warn', f"No {env_path} file — passwords are stored there",
             ["Set each account's password on the Settings page"])
        return
    bad = []
    with open(env_path, 'r', encoding='utf-8') as handle:
        for number, text in enumerate(handle, 1):
            stripped = text.strip()
            if not stripped or stripped.startswith('#'):
                continue
            key = stripped.split('=', 1)[0].strip()
            if '=' not in stripped or not key \
                    or not key.replace('_', '').isalnum() \
                    or key[0].isdigit():
                bad.append((number, stripped[:40]))
    if bad:
        line('fail', f"{len(bad)} line(s) in {env_path} cannot be read by "
                     f"dotenv: " + ', '.join(f"line {n} ({t}…)"
                                             for n, t in bad),
             ["A key with a space or a missing '=' makes dotenv skip the "
              "line — the password it holds NEVER reaches MT5",
              "This happens when an account name has a space in it "
              "(MT5_PASSWORD_UT 2)",
              "Re-save each account's password on the Settings page: it "
              "now writes a safe key name and quotes the value"])
    else:
        line('ok', f"{env_path} parses cleanly")

    for name, account in config.accounts.items():
        var = getattr(account, 'password_env', None)
        if not var:
            continue
        if ' ' in var or not var.replace('_', '').isalnum():
            line('fail', f"{name}: password_env '{var}' is not a legal "
                         f"environment variable name",
                 ["Re-save this account's password on the Settings page"])
        elif not os.environ.get(var):
            line('warn', f"{name}: {var} is not set — no password loaded",
                 ["Set the password on the Settings page",
                  "Or open this account's terminal and log in by hand"])
        else:
            line('ok', f"{name}: password loaded from {var}")


def check_endpoints(config):
    """Are the leg runners actually listening? A coordinator with no
    prices usually means these are not up."""
    header("Leg runner endpoints")
    endpoints = [(name, acct.endpoint)
                 for name, acct in config.accounts.items() if acct.endpoint]
    if not endpoints:
        line('info', "No endpoints configured — single-account topology "
                     "(the coordinator connects to MT5 itself)")
        return
    for name, endpoint in endpoints:
        host, _, port = endpoint.partition(':')
        try:
            with socket.create_connection((host, int(port)), timeout=1.5):
                line('ok', f"{name}: leg runner is listening on {endpoint}")
        except OSError as e:
            line('warn', f"{name}: nothing listening on {endpoint} ({e})",
                 ["The leg runner for this account is not running",
                  "start.py launches one per account — check its window "
                  f"and leg_{name}.log for why it exited",
                  "If the port is taken by something else, change the "
                  "endpoint in Settings"])


def check_account(name, account, symbols, place_order=False):
    header(f"Account '{name}'  (login {account.login or '?'} @ "
           f"{account.server or '?'})")

    if account.login and not account.password:
        env_var = getattr(account, 'password_env', None)
        line('warn', "No password available for this account",
             [(f"Set it on the Settings page — it is stored in .env as "
               f"{env_var}" if env_var else
               "No password_env is configured for this account; set the "
               "password on the Settings page"),
              "Without it, the app can only attach to a terminal that is "
              "ALREADY open and logged in to this account"])

    attempts = []
    if account.terminal_path:
        attempts.append(("terminal path + credentials",
                         dict(path=account.terminal_path,
                              login=account.login,
                              password=account.password or "",
                              server=account.server or "")))
        if not os.path.exists(account.terminal_path):
            line('fail', f"terminal_path does not exist: "
                         f"{account.terminal_path}",
                 ["Fix the path in Settings, or clear it and open the "
                  "terminal by hand before starting the app"])
    if account.login:
        attempts.append(("running terminal + credentials",
                         dict(login=account.login,
                              password=account.password or "",
                              server=account.server or "")))
    attempts.append(("running terminal (already logged in)", {}))

    connected = False
    for label, kwargs in attempts:
        try:
            connected = mt5.initialize(**kwargs)
        except Exception as e:
            line('warn', f"initialize({label}) raised: {e}")
            continue
        if connected:
            line('ok', f"Connected via: {label}")
            break
        code, text = mt5.last_error()
        meaning, fixes = explain_init_error(code)
        line('warn', f"initialize({label}) failed: {code} {text} — "
                     f"{meaning}")
        # Release before the next form. A failed initialize can leave
        # the library half-attached, so the following attempt reports a
        # fault belonging to the previous one — in a tool whose whole
        # job is to say which attempt worked, that is worse than no
        # answer.
        try:
            mt5.shutdown()
        except Exception:
            pass
    if not connected:
        line('fail', "Could not connect to MT5 for this account at all",
             explain_init_error(mt5.last_error()[0])[1])
        try:
            mt5.shutdown()
        except Exception:
            pass
        return

    terminal = mt5.terminal_info()
    if terminal is None:
        line('fail', "No terminal info after connecting")
        mt5.shutdown()
        return
    line('info', f"Terminal: {terminal.name} build "
                 f"{getattr(terminal, 'build', '?')} at {terminal.path}")
    line('ok' if terminal.connected else 'fail',
         f"Terminal {'is' if terminal.connected else 'is NOT'} connected "
         f"to the broker",
         () if terminal.connected else
         ["Check the internet connection and the server in the login "
          "dialog (bottom-right of MT5 shows the link state)"])
    line('ok' if terminal.trade_allowed else 'fail',
         f"Algo trading is "
         f"{'ENABLED' if terminal.trade_allowed else 'DISABLED'} in the "
         f"terminal",
         () if terminal.trade_allowed else
         ["THIS is why orders are not being placed",
          "MT5: Tools > Options > Expert Advisors > tick 'Allow "
          "algorithmic trading'",
          "Also check the 'Algo Trading' toolbar button is pressed in"])

    info = mt5.account_info()
    if info is None:
        line('fail', "Terminal is not logged in to any account",
             ["MT5: File > Login to Trade Account"])
        mt5.shutdown()
        return
    line('ok', f"Logged in: {info.login} on {info.server} — "
               f"{info.balance:,.2f} {info.currency}, {info.leverage}x "
               f"leverage")
    if account.login and info.login != account.login:
        line('fail', f"WRONG ACCOUNT: this terminal is logged into "
                     f"{info.login}, config says {account.login}",
             ["Each account needs its OWN terminal installation and its "
              "own leg runner",
              "Two leg runners pointed at one terminal will both trade "
              "the same account"])
    line('ok' if info.trade_allowed else 'fail',
         f"Account trading is "
         f"{'allowed' if info.trade_allowed else 'NOT allowed'}",
         () if info.trade_allowed else
         ["You may be logged in with the INVESTOR (read-only) password",
          "Log in again with the master password"])
    hedging = info.margin_mode == getattr(
        mt5, 'ACCOUNT_MARGIN_MODE_RETAIL_HEDGING', 2)
    line('ok' if hedging else 'warn',
         f"Margin mode: {'HEDGING' if hedging else 'NETTING'}",
         () if hedging else
         ["The engine closes positions by ticket, which assumes hedging",
          "Ask the broker for a hedging account"])

    for symbol in symbols:
        symbol_check(symbol, place_order)

    mt5.shutdown()


def symbol_check(symbol, place_order):
    info = mt5.symbol_info(symbol)
    if info is None:
        line('fail', f"Symbol '{symbol}' does not exist on this broker",
             ["Brokers spell it differently: XAUUSD, GOLD, XAUUSD.r, "
              "GOLD.spot",
              "Open Market Watch > right-click > Symbols and find the "
              "exact name, then set it on the Exchanges page"])
        return
    if not info.visible:
        mt5.symbol_select(symbol, True)
        line('warn', f"'{symbol}' was hidden; selected into Market Watch")
        info = mt5.symbol_info(symbol)

    tick = mt5.symbol_info_tick(symbol)
    if not tick or not tick.bid:
        line('fail', f"'{symbol}' has no quotes",
             ["The market may be closed right now",
              "Check the symbol is enabled for your account with the "
              "broker"])
        return
    age = time.time() - tick.time
    line('ok' if age < 120 else 'warn',
         f"'{symbol}': bid {tick.bid} ask {tick.ask}, "
         f"{info.trade_contract_size:g}/lot, min {info.volume_min} "
         f"step {info.volume_step} max {info.volume_max} "
         f"(quote {age:.0f}s old)",
         () if age < 120 else ["Quotes are stale — market closed?"])

    if place_order:
        order_round_trip(symbol, info)


def order_round_trip(symbol, info):
    """Place a MINIMUM-lot market order, confirm MT5 has it, close it."""
    volume = info.volume_min
    tick = mt5.symbol_info_tick(symbol)
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
        "type": mt5.ORDER_TYPE_BUY, "price": tick.ask, "deviation": 20,
        "magic": 12345, "comment": "CHECK_MT5",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _filling_mode(info),
    }
    result = mt5.order_send(request)
    if result is None:
        line('fail', f"order_send returned None for {symbol}: "
                     f"{mt5.last_error()}")
        return
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        line('fail', f"Order REJECTED on {symbol}: {result.retcode} "
                     f"{result.comment}",
             [ORDER_ERRORS.get(result.retcode,
                               "Look the retcode up in the MT5 docs")])
        return
    line('ok', f"Order accepted on {symbol}: order {result.order} "
               f"{volume} @ {result.price}")

    # The proof: read it back out of the terminal.
    position_id, deal = None, None
    for _ in range(5):
        deals = mt5.history_deals_get(ticket=result.order) or ()
        for d in deals:
            if d.order == result.order:
                deal, position_id = d, d.position_id
        if deal:
            break
        time.sleep(0.4)          # deal history lags the fill
    if deal:
        line('ok', f"MT5 CONFIRMS the deal: deal {deal.ticket}, position "
                   f"{position_id}, {deal.volume} @ {deal.price}, "
                   f"commission {deal.commission}")
    else:
        line('fail', "The order was accepted but no deal appears in MT5 "
                     "history",
             ["Check the terminal's Trade and History tabs directly"])

    if position_id:
        close = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
            "volume": volume, "type": mt5.ORDER_TYPE_SELL,
            "position": int(position_id), "price": tick.bid,
            "deviation": 20, "magic": 12345, "comment": "CHECK_MT5 close",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": _filling_mode(info)})
        if close and close.retcode == mt5.TRADE_RETCODE_DONE:
            line('ok', f"Closed by ticket {position_id} @ {close.price} — "
                       f"round trip complete, nothing left open")
        else:
            code = getattr(close, 'retcode', '?')
            line('fail', f"COULD NOT CLOSE position {position_id}: {code} "
                         f"{getattr(close, 'comment', '')}",
                 ["Close it by hand in the terminal NOW",
                  ORDER_ERRORS.get(code, "")])


def _filling_mode(info):
    if info.filling_mode & getattr(mt5, 'SYMBOL_FILLING_FOK', 1):
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & getattr(mt5, 'SYMBOL_FILLING_IOC', 2):
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def main():
    parser = argparse.ArgumentParser(
        description="Check the MT5 connection for every configured account")
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--env', default='.env')
    parser.add_argument('--account', default=None,
                        help='Check only this account')
    parser.add_argument('--order', action='store_true',
                        help='ALSO place a real minimum-lot order and close '
                             'it (proves orders reach MT5)')
    args = parser.parse_args()

    print("=" * 68)
    print(" MT5 connection check")
    print("=" * 68)

    if not check_environment():
        sys.exit(1)

    if not os.path.exists(args.config):
        line('fail', f"Config not found: {args.config}",
             ["Run this from the folder that holds config.json",
              "Or pass --config C:\\path\\to\\config.json"])
        sys.exit(1)
    config = AlgoTradingConfig.from_file(args.config)

    header("Configuration")
    line('info', f"Accounts: {', '.join(config.accounts)}")
    line('info', f"Leg mapping: spot -> "
                 f"{config.leg_accounts.get('spot')}, futures -> "
                 f"{config.leg_accounts.get('futures')}")

    symbols_for = {}
    for asset_key, asset in config.ASSETS.items():
        if not asset.get('enabled', True):
            continue
        spot_account = config.leg_accounts.get('spot')
        fut_account = config.leg_accounts.get('futures')
        symbols_for.setdefault(spot_account, []).extend(
            asset.get('spot_symbols') or [])
        symbols_for.setdefault(fut_account, []).extend(
            asset.get('futures_symbols') or [])
        line('info', f"{asset_key}: spot {asset.get('spot_symbols')} on "
                     f"{spot_account}, futures "
                     f"{asset.get('futures_symbols')} on {fut_account}")

    check_config_files(config, args.env)
    check_endpoints(config)

    if args.order:
        print("\n!! --order will place REAL minimum-lot orders and close "
              "them again.")
        print("   Stop the algo first. Ctrl+C now if that is not what you "
              "want.")
        time.sleep(3)

    names = [args.account] if args.account else list(config.accounts)
    for name in names:
        if name not in config.accounts:
            line('fail', f"Unknown account '{name}'. Configured: "
                         f"{list(config.accounts)}")
            continue
        check_account(name, config.accounts[name],
                      symbols_for.get(name, []), place_order=args.order)

    header("Summary")
    print(f"  {ok_count} ok, {warn_count} warnings, {fail_count} failures")
    if fail_count:
        print("\n  Fix the [FAIL] lines above, in order, then run this "
              "again.")
        print("  The three usual causes are: the terminal is not open and "
              "logged in,")
        print("  algo trading is switched off, or the symbol is named "
              "differently")
        print("  on that broker.")
    else:
        print("\n  MT5 is reachable on every configured account.")
        if not args.order:
            print("  Run  python check_mt5.py --order  to prove orders "
                  "actually get placed.")
    sys.exit(1 if fail_count else 0)


if __name__ == '__main__':
    main()
