"""Connectivity checklist for the Exchanges page.

Ported from the old app's broker "Diagnose" panel and widened for two
accounts: as well as asking each terminal whether it is attached,
logged in and allowed to trade, this checks that the two legs actually
fit each other — same account currency, contract sizes that match the
configured lot size and hedge ratio, volumes that can carry the clip,
and a futures contract that has not expired.

Pure functions over the reports the legs hand back, so the whole
checklist is testable without MT5.
"""

from datetime import datetime

PASS, WARN, FAIL, INFO = 'PASS', 'WARN', 'FAIL', 'INFO'
_RANK = {PASS: 0, INFO: 0, WARN: 1, FAIL: 2}


class Checklist:
    def __init__(self):
        self.checks = []

    def add(self, scope, name, status, message, details=None, fix=None):
        check = {'scope': scope, 'name': name, 'status': status,
                 'message': message}
        if details:
            check['details'] = details
        if fix:
            check['fix'] = fix          # list of steps, as in the old app
        self.checks.append(check)
        return status == PASS

    @property
    def overall(self):
        worst = max((_RANK[c['status']] for c in self.checks), default=0)
        return {0: PASS, 1: WARN, 2: FAIL}[worst]

    def result(self):
        counts = {PASS: 0, WARN: 0, FAIL: 0, INFO: 0}
        for check in self.checks:
            counts[check['status']] += 1
        return {'checks': self.checks, 'overall': self.overall,
                'passed': counts[PASS], 'warnings': counts[WARN],
                'failed': counts[FAIL], 'info': counts[INFO],
                'ran_at': datetime.now().strftime('%H:%M:%S')}


def _multiple_of(value, step):
    if not step:
        return True
    return abs(round(value / step) * step - value) < step * 1e-6


def check_leg(checklist, role, leg_name, terminal, symbol_report, config,
              asset, expected_login=None, configured_leverage=None):
    """One account and the symbol this leg trades on it."""
    scope = f'{role.upper()} · {leg_name}'

    if terminal.get('library') is False:
        checklist.add(scope, 'MT5 library', FAIL,
                      terminal.get('error', 'MetaTrader5 not installed'),
                      fix=['pip install MetaTrader5 on the trading machine',
                           'The package is Windows-only'])
        return
    if not terminal.get('terminal'):
        checklist.add(scope, 'MT5 terminal', FAIL,
                      terminal.get('error') or 'Cannot reach the terminal',
                      fix=['Open the MT5 terminal for this account',
                           'Log in to the trading account',
                           'Check the terminal path in Settings',
                           'Start the leg runner for this account'])
        return
    checklist.add(scope, 'MT5 terminal', PASS,
                  f"Attached to {terminal.get('terminal_name') or 'MT5'}"
                  + (f" ({terminal['ping_ms']:.0f} ms to broker)"
                     if terminal.get('ping_ms') else ''))

    if terminal.get('terminal_connected') is False:
        checklist.add(scope, 'Broker connection', FAIL,
                      'Terminal is open but not connected to the broker',
                      fix=['Check the internet connection',
                           'Re-select the server in the MT5 login dialog'])

    if not terminal.get('logged_in'):
        checklist.add(scope, 'Account login', FAIL,
                      'Terminal is not logged in to any account',
                      fix=['MT5: File > Login to Trade Account',
                           'Enter this account\'s login, password, server',
                           'Passwords are stored in .env from Settings'])
        return
    checklist.add(scope, 'Account login', PASS,
                  f"{terminal['login']} on {terminal['server']} — "
                  f"{terminal.get('balance', 0):,.2f} "
                  f"{terminal.get('currency', '')}",
                  details={'name': terminal.get('name'),
                           'equity': terminal.get('equity'),
                           'free margin': terminal.get('margin_free')})

    if expected_login and terminal['login'] != expected_login:
        checklist.add(
            scope, 'Account mapping', FAIL,
            f"This terminal is logged into {terminal['login']}, but the "
            f"config maps '{leg_name}' to {expected_login}",
            fix=['Two accounts on one terminal is not possible — each '
                 'account needs its own terminal and leg runner',
                 'Either log this terminal into the configured account, '
                 'or correct the login in Settings'])

    if terminal.get('algo_trading'):
        checklist.add(scope, 'Algo trading', PASS,
                      'Algorithmic trading is enabled in the terminal')
    else:
        checklist.add(scope, 'Algo trading', FAIL,
                      'Algo trading is DISABLED — every order will be '
                      'rejected',
                      fix=['MT5: Tools > Options > Expert Advisors',
                           'Tick "Allow algorithmic trading"',
                           'Confirm the Algo Trading toolbar button is on'])

    if terminal.get('trade_allowed'):
        checklist.add(scope, 'Trading permission', PASS,
                      'The account may trade')
    else:
        checklist.add(scope, 'Trading permission', FAIL,
                      'Trading is not allowed on this account',
                      fix=['An investor (read-only) password logs in but '
                           'cannot trade — use the master password',
                           'Ask the broker whether trading is disabled'])
    if terminal.get('trade_expert') is False:
        checklist.add(scope, 'Expert trading', WARN,
                      'The broker has disabled EA trading for this account',
                      fix=['Ask the broker to enable Expert Advisor '
                           'trading on the account'])

    # Hedging mode: our closes target position tickets. On a netting
    # account an opposite order nets instead, and the exit logic breaks.
    if terminal.get('hedging'):
        checklist.add(scope, 'Margin mode', PASS,
                      'Hedging account — closes by position ticket work')
    elif terminal.get('margin_mode') is not None:
        checklist.add(scope, 'Margin mode', WARN,
                      'This looks like a NETTING account. The engine '
                      'closes by position ticket (hedging assumption).',
                      fix=['Ask the broker for a hedging account, or',
                           'Verify closes behave on a test round trip '
                           'before going live'])

    detected = terminal.get('leverage')
    if configured_leverage and detected:
        if int(detected) == int(configured_leverage):
            checklist.add(scope, 'Leverage', PASS,
                          f'{detected}x on the account, matching Settings')
        else:
            checklist.add(
                scope, 'Leverage', WARN,
                f'Account is {detected}x but Settings says '
                f'{int(configured_leverage)}x — margin and %-of-capital '
                f'levels will be wrong',
                fix=[f'Set this leg\'s leverage to {detected}x in Settings, '
                     f'or ask the broker to change the account',
                     'MT5 leverage is set broker-side; the app cannot '
                     'change it'])
    elif detected:
        checklist.add(scope, 'Leverage', INFO, f'{detected}x on the account')

    _check_symbol(checklist, scope, role, symbol_report, config, asset,
                  terminal)


def _check_symbol(checklist, scope, role, report, config, asset, terminal):
    symbol = report.get('symbol')
    if not symbol:
        checklist.add(scope, 'Symbol', FAIL, 'No symbol configured for '
                                             'this leg',
                      fix=['Set the symbol on the Exchanges page'])
        return
    if not report.get('found'):
        checklist.add(
            scope, 'Symbol', FAIL,
            report.get('error') or f'"{symbol}" not found on this broker',
            fix=['Brokers name the same instrument differently '
                 '(XAUUSD, GOLD, XAUUSD.r)',
                 'Use "Find symbol" on the Exchanges page to search this '
                 'account, then save the exact name'])
        return
    checklist.add(scope, 'Symbol', PASS,
                  f'"{symbol}" found'
                  + (f" — {report['description']}"
                     if report.get('description') else ''))

    if report.get('visible'):
        checklist.add(scope, 'Market Watch', PASS, f'{symbol} is visible')
    else:
        checklist.add(scope, 'Market Watch', WARN,
                      f'{symbol} was not in Market Watch (auto-selected now)',
                      fix=['MT5: right-click Market Watch > Symbols > Show'])

    bid, ask = report.get('bid'), report.get('ask')
    if bid and ask:
        checklist.add(scope, 'Price data', PASS,
                      f'bid {bid} / ask {ask} (spread {ask - bid:.5f})')
    else:
        checklist.add(scope, 'Price data', FAIL,
                      'No quotes — the engine cannot compute a basis',
                      fix=['The market may be closed',
                           'Check the symbol is subscribed with your broker'])

    if report.get('trade_allowed') is False:
        checklist.add(scope, 'Symbol trading', FAIL,
                      f'{symbol} is disabled or close-only right now',
                      fix=['Out-of-session symbols report close-only — '
                           'retry during the trading session'])

    clip = config.TRADING.get('CLIP_LOTS', 0)
    slice_lots = config.TRADING.get('SLICE_LOTS', 0)
    minimum = report.get('volume_min')
    maximum = report.get('volume_max')
    step = report.get('volume_step')
    details = {'min': minimum, 'max': maximum, 'step': step}
    if slice_lots and maximum and slice_lots > maximum:
        checklist.add(scope, 'Order size', FAIL,
                      f'Slice size {slice_lots} exceeds the broker maximum '
                      f'{maximum} per order', details=details,
                      fix=[f'Lower SLICE_LOTS to {maximum} or less '
                           f'in Settings'])
    elif slice_lots and step and not _multiple_of(slice_lots, step):
        checklist.add(scope, 'Order size', WARN,
                      f'Slice size {slice_lots} is not a multiple of the '
                      f'{step} volume step — the broker will round it',
                      details=details)
    elif clip and minimum and clip < minimum:
        checklist.add(scope, 'Order size', FAIL,
                      f'Clip {clip} is below the broker minimum {minimum}',
                      details=details)
    else:
        checklist.add(scope, 'Order size', PASS,
                      f'clip {clip} in slices of {slice_lots} fits '
                      f'(min {minimum}, step {step}, max {maximum})',
                      details=details)

    configured_contract = (asset or {}).get('lot_size')
    contract = report.get('contract_size')
    if contract and configured_contract:
        if abs(contract - configured_contract) < 1e-9:
            checklist.add(scope, 'Contract size', PASS,
                          f'{contract:g} per lot, matching the configured '
                          f'lot size')
        else:
            checklist.add(
                scope, 'Contract size', FAIL,
                f'Broker says {contract:g} per lot but the asset is '
                f'configured as {configured_contract:g} — P&L and the '
                f'hedge ratio would be wrong by '
                f'{contract / configured_contract:.2f}x',
                fix=[f'Set the contract size to {contract:g} in Settings, '
                     f'or correct HEDGE_RATIO for the difference'])

    if role == 'futures':
        _check_expiry(checklist, scope, report, asset)


def _check_expiry(checklist, scope, report, asset):
    now = datetime.now()
    broker_expiry = report.get('expiry')
    configured = (asset or {}).get('futures_expiry')
    if isinstance(configured, str):
        try:
            configured = datetime.fromisoformat(configured)
        except ValueError:
            configured = None

    if configured and configured < now:
        checklist.add(
            scope, 'Futures expiry', FAIL,
            f'The configured contract expired on '
            f'{configured.date()} — an expired expiry silently zeroes the '
            f'swap basis and disables every signal',
            fix=['Set the live contract\'s expiry date in Settings',
                 'Update the futures symbol to the front month'])
    elif broker_expiry:
        broker_date = datetime.fromtimestamp(broker_expiry)
        days = (broker_date - now).days
        status = FAIL if days < 0 else (WARN if days <= 5 else PASS)
        message = (f'Broker contract expires {broker_date.date()} '
                   f'({days} days)')
        if configured and abs((broker_date - configured).days) > 1:
            status = WARN
            message += f'; Settings says {configured.date()}'
        checklist.add(scope, 'Futures expiry', status, message,
                      fix=(['Roll to the next contract before expiry']
                           if status != PASS else None))
    elif configured:
        checklist.add(scope, 'Futures expiry', PASS,
                      f'Configured expiry {configured.date()} '
                      f'({(configured - now).days} days away)')


def check_pair(checklist, spot, futures, config):
    """The checks that only exist because there are TWO accounts."""
    spot_term, fut_term = spot['terminal'], futures['terminal']
    spot_sym, fut_sym = spot['symbol'], futures['symbol']

    if spot['account'] == futures['account']:
        checklist.add('PAIR', 'Topology', INFO,
                      f"Both legs on one account ({spot['account']}) — "
                      f"single terminal, no leg runners needed")
    else:
        checklist.add('PAIR', 'Topology', PASS,
                      f"Leg A (spot) on {spot['account']}, Leg B (futures) "
                      f"on {futures['account']} — one leg runner each")

    spot_ccy = spot_term.get('currency')
    fut_ccy = fut_term.get('currency')
    if spot_ccy and fut_ccy:
        if spot_ccy == fut_ccy:
            checklist.add('PAIR', 'Account currency', PASS,
                          f'Both accounts are in {spot_ccy}')
        else:
            checklist.add(
                'PAIR', 'Account currency', WARN,
                f'{spot["account"]} is in {spot_ccy} but '
                f'{futures["account"]} is in {fut_ccy} — combined equity '
                f'and P&L add unlike currencies',
                fix=['Use accounts in the same currency, or read the '
                     'per-account figures rather than the totals'])

    if not (spot_sym.get('found') and fut_sym.get('found')):
        return          # per-leg checks already reported the failure

    if spot_sym['symbol'] == fut_sym['symbol'] \
            and spot['account'] == futures['account']:
        checklist.add('PAIR', 'Symbols', FAIL,
                      'Both legs point at the same symbol on the same '
                      'account — there is no spread to trade',
                      fix=['Set the futures symbol to the futures contract'])
    else:
        checklist.add('PAIR', 'Symbols', PASS,
                      f"{spot_sym['symbol']} (spot) vs "
                      f"{fut_sym['symbol']} (futures)")

    spot_contract = spot_sym.get('contract_size')
    fut_contract = fut_sym.get('contract_size')
    hedge_ratio = config.TRADING.get('HEDGE_RATIO', 1.0)
    if spot_contract and fut_contract:
        implied = spot_contract / fut_contract
        details = {'spot contract': spot_contract,
                   'futures contract': fut_contract,
                   'implied hedge ratio': round(implied, 4),
                   'configured HEDGE_RATIO': hedge_ratio}
        if abs(implied - hedge_ratio) < 0.01:
            checklist.add('PAIR', 'Hedge ratio', PASS,
                          f'{spot_contract:g} oz spot vs {fut_contract:g} oz '
                          f'futures per lot — HEDGE_RATIO {hedge_ratio:g} '
                          f'is right', details=details)
        else:
            checklist.add(
                'PAIR', 'Hedge ratio', FAIL,
                f'Contract sizes imply a hedge ratio of {implied:.4f} but '
                f'HEDGE_RATIO is {hedge_ratio:g} — the hedge would be '
                f'{abs(implied / hedge_ratio - 1) * 100:.0f}% off',
                details=details,
                fix=[f'Set HEDGE_RATIO to {implied:.4f} in Settings',
                     'This is the number that decides whether the "hedged" '
                     'position is actually flat'])

    spot_price = spot_sym.get('bid')
    fut_price = fut_sym.get('bid')
    if spot_price and fut_price:
        basis = fut_price - spot_price
        checklist.add('PAIR', 'Live basis', PASS,
                      f'futures {fut_price} − spot {spot_price} = '
                      f'{basis:+.4f}',
                      details={'spot': spot_price, 'futures': fut_price})
        if abs(basis) > 0.5 * spot_price:
            checklist.add('PAIR', 'Live basis', WARN,
                          'The two legs are far apart — are these really '
                          'the same underlying?')

    ages = []
    for side in (spot_sym, fut_sym):
        if side.get('tick_time'):
            ages.append(datetime.now().timestamp() - side['tick_time'])
    if ages and max(ages) > 60:
        checklist.add('PAIR', 'Quote freshness', WARN,
                      f'Oldest quote is {max(ages):.0f}s old — the market '
                      f'may be closed or the feed stalled')
    elif ages:
        checklist.add('PAIR', 'Quote freshness', PASS,
                      f'Both legs quoting within {max(ages):.0f}s')


def build_report(config, spot, futures, expected_logins=None,
                 leverages=None):
    """Full checklist: each leg, then the pair.

    spot/futures: {'account', 'role', 'terminal': report, 'symbol': report}
    """
    checklist = Checklist()
    expected_logins = expected_logins or {}
    leverages = leverages or {}
    asset = spot.get('asset') or {}
    for side in (spot, futures):
        check_leg(checklist, side['role'], side['account'],
                  side['terminal'], side['symbol'], config,
                  side.get('asset') or asset,
                  expected_login=expected_logins.get(side['account']),
                  configured_leverage=leverages.get(side['role']))
    check_pair(checklist, spot, futures, config)
    return checklist.result()
