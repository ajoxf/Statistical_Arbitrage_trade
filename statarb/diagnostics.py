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

from . import hedgeratio, mt5_errors

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
        # Decode the code rather than printing one generic list. A live
        # -10001 with the terminal open and logged in used to answer
        # "Open the MT5 terminal" and "Log in", which describes work the
        # operator had already done and reads as the tool being broken.
        raw = terminal.get('error') or 'Cannot reach the terminal'
        summary, fixes = mt5_errors.explain(raw)
        checklist.add(scope, 'MT5 terminal', FAIL,
                      f'{summary} {raw}' if summary else raw,
                      fix=fixes or ['Open the MT5 terminal for this account',
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
    # EQUITY, not balance. equity = balance + credit + floating P&L,
    # and brokers routinely fund a demo with credit, so balance alone
    # reads as an empty account: live 2026-08-11, balance 0.00 beside
    # equity 5,000 (and -13.70 beside 4,986.30 on the account before
    # it). Equity is also what the risk manager and the margin breaker
    # actually measure, so it is the number that decides trading.
    credit = terminal.get('credit') or 0.0
    checklist.add(scope, 'Account login', PASS,
                  f"{terminal['login']} on {terminal['server']} — "
                  f"{terminal.get('equity', 0):,.2f} "
                  f"{terminal.get('currency', '')} equity"
                  + (f" (of which {credit:,.2f} is broker credit)"
                     if credit else ''),
                  details={'name': terminal.get('name'),
                           'balance': terminal.get('balance'),
                           'credit': terminal.get('credit'),
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

    # Each leg has its OWN configured contract size: `lot_size` for Leg
    # A, `fut_lot_size` for Leg B (sizing.plan reads exactly these).
    # This compared BOTH legs against Leg A's, so on a pair whose legs
    # genuinely differ it reported a mismatch that did not exist —
    # live 2026-08-10, XAUUSD's real 100 was measured against silver's
    # 5000 and read as "Broker says 100 per lot, config still says
    # 5000", sending the operator to the broker to check a number that
    # was already right.
    asset = asset or {}
    configured_contract = (asset.get('fut_lot_size') or asset.get('lot_size')
                           if role == 'futures' else asset.get('lot_size'))
    contract = report.get('contract_size')
    if contract and configured_contract:
        if abs(contract - configured_contract) < 1e-9:
            checklist.add(scope, 'Contract size', PASS,
                          f'{contract:g} per lot, matching the configured '
                          f'lot size')
        else:
            # NOT an operator task. There is no contract-size field on
            # the Settings page — the owner had it removed because MT5
            # already knows the answer — and the engine adopts the
            # broker's number at startup and writes it back to the
            # config. So this is a WARN saying the adoption has not run
            # yet, with the reason it has not, rather than a FAIL
            # telling the operator to edit a control that is not there.
            checklist.add(
                scope, 'Contract size', WARN,
                f'Broker says {contract:g} per lot, config still says '
                f'{configured_contract:g}. The engine takes the broker\'s '
                f'number for P&L and sizing and saves it back, so this '
                f'clears itself once startup has read BOTH symbols.',
                fix=['Nothing to type — the contract size is read from '
                     'the terminal, not entered',
                     'If it persists, the OTHER leg\'s symbol is not '
                     'resolving: specs are only adopted when both legs '
                     'are found',
                     'Restart the launcher after fixing the symbol'])

    # Settle "the broker told me a different number" arithmetically.
    # trade_contract_size is a declaration; tick_value / tick_size is
    # what MT5 will actually multiply a price move by to produce
    # profit. When the two disagree the money follows the second one,
    # so it is the tiebreaker — and when they agree, a spec sheet
    # saying otherwise is describing a different product (a different
    # symbol suffix, or the exchange contract rather than the CFD).
    tick_value = report.get('tick_value')
    tick_size = report.get('tick_size')
    if contract and tick_value and tick_size:
        implied = tick_value / tick_size
        details = {'trade_contract_size': contract,
                   'tick_value': tick_value, 'tick_size': tick_size,
                   'contract size implied by tick value': round(implied, 6)}
        if abs(implied - contract) <= max(1e-6, contract * 0.01):
            checklist.add(
                scope, 'Contract size check', PASS,
                f'{contract:g} per lot is confirmed by the tick value: '
                f'one {tick_size:g} tick is worth {tick_value:g}, so a '
                f'1.00 price move on one lot pays {contract:g}. This is '
                f'the number MT5 computes P&L from.', details=details)
        else:
            checklist.add(
                scope, 'Contract size check', FAIL,
                f'MT5 declares {contract:g} per lot but its own tick '
                f'value implies {implied:g} — one {tick_size:g} tick is '
                f'worth {tick_value:g}. P&L follows the tick value, so '
                f'position sizing and every dollar level would be off '
                f'by {implied / contract:.4g}x.',
                details=details,
                fix=['Place one minimum-lot round trip and compare MT5\'s '
                     'own profit against the price move — that is the '
                     'ground truth',
                     'Check the symbol suffix: brokers list the same '
                     'metal in several contract sizes',
                     'Ask the broker which figure the platform uses, not '
                     'which the spec sheet quotes'])

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
            f'{configured.date()} — an expired contract stops trading and '
            f'its quotes go stale',
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
    else:
        checklist.add(scope, 'Futures expiry', INFO,
                      'No expiry set. The spread does not depend on it — '
                      'set one only so the checklist can warn you before '
                      'the contract rolls.')


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

    # Two accounts at one broker is fine; two accounts sharing one
    # TERMINAL is not — a terminal holds a single login, so both legs
    # would trade the same account.
    spot_path = (spot_term.get('terminal_path') or '').strip().lower()
    fut_path = (fut_term.get('terminal_path') or '').strip().lower()
    if spot['account'] != futures['account'] and spot_path and fut_path:
        if spot_path == fut_path:
            checklist.add(
                'PAIR', 'Terminal installations', FAIL,
                f'Both accounts are running from the same MT5 '
                f'installation ({spot_path}) — one terminal serves one '
                f'login, so both legs would hit the same account',
                fix=['Install a second copy of MetaTrader 5 (the broker '
                     'installer allows a second folder, or use a '
                     'portable copy)',
                     'Point each account at its own terminal on the '
                     'Exchanges page'])
        else:
            checklist.add('PAIR', 'Terminal installations', PASS,
                          'Each account has its own MT5 installation')
        if spot_term.get('login') and \
                spot_term.get('login') == fut_term.get('login'):
            checklist.add(
                'PAIR', 'Logins', FAIL,
                f"Both terminals are logged into {spot_term['login']} — "
                f"there is only one account here, not two",
                fix=['Log the second terminal into the other account'])
        elif spot_term.get('server') and \
                spot_term.get('server') == fut_term.get('server'):
            checklist.add('PAIR', 'Broker', INFO,
                          f"Both accounts are at the same broker "
                          f"({spot_term['server']}) — supported; the basis "
                          f"is then that broker's own spot vs futures")

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

    # Contract sizes and HEDGE_RATIO are DIFFERENT things, and this
    # check used to conflate them: it computed spot_contract /
    # fut_contract, called it the "implied hedge ratio" and told the
    # operator to set HEDGE_RATIO to it.
    #
    # Beta is the PRICE coefficient of the spread series
    # (spread = P_fut - beta * P_spot). Contract sizes are handled when
    # sizing the hedge: L_B = L_A * C_A / (beta * C_B). Feeding a
    # contract ratio into beta redefines the series instead.
    #
    # Live 2026-08-10 the operator followed exactly that advice on a
    # 10x contract mismatch, and USOIL/UKOIL at 81.76 / 85.07 became a
    # spread of -732.53 with a hedge a tenth of the size it should be.
    # The advice is gone; what is left reports the facts and checks
    # beta against the thing it actually has to be comparable with —
    # the live price ratio.
    spot_contract = spot_sym.get('contract_size')
    fut_contract = fut_sym.get('contract_size')
    hedge_ratio = config.TRADING.get('HEDGE_RATIO', 1.0) or 1.0
    spot_price = spot_sym.get('bid')
    fut_price = fut_sym.get('bid')

    if spot_contract and fut_contract:
        lot_ratio = spot_contract / (hedge_ratio * fut_contract)
        checklist.add(
            'PAIR', 'Contract sizes', INFO,
            f'{spot_contract:g} per lot on Leg A vs {fut_contract:g} on '
            f'Leg B. The hedge is sized from BOTH, so Leg B trades '
            f'{lot_ratio:.4g} lots for every 1 on Leg A. This does NOT '
            f'set HEDGE_RATIO.',
            details={'spot contract': spot_contract,
                     'futures contract': fut_contract,
                     'lots on Leg B per lot on Leg A': round(lot_ratio, 4),
                     'configured HEDGE_RATIO': hedge_ratio})

    if spot_price and fut_price:
        ratio = fut_price / spot_price
        spread = fut_price - hedge_ratio * spot_price
        details = {'Leg A price': spot_price, 'Leg B price': fut_price,
                   'price ratio (Leg B / Leg A)': round(ratio, 4),
                   'configured HEDGE_RATIO': hedge_ratio,
                   'resulting spread': round(spread, 4)}
        # A pair spread is a small DIFFERENCE between comparable prices.
        # Dwarfing the prices is the signature of a beta error.
        if hedgeratio.implausible(hedge_ratio, spot_price, fut_price,
                                  spread) is not None:
            asset_cfg = next((v for v in (config.ASSETS or {}).values()
                              if v.get('enabled', True)), {})
            pair_type = (asset_cfg.get('pair_type') or 'SPOT_FUTURE').upper()
            suggested, _ = hedgeratio.suggest(pair_type, spot_price,
                                              fut_price)
            checklist.add(
                'PAIR', 'Hedge ratio', FAIL,
                f'HEDGE_RATIO {hedge_ratio:g} gives a spread of '
                f'{spread:+.4f} on legs priced {spot_price:.4f} / '
                f'{fut_price:.4f} — the "spread" is bigger than the '
                f'instruments, so mu, sigma, z and every exit level '
                f'would describe a series that does not exist. Entries '
                f'are blocked while this is true; exits still run.',
                details=details,
                fix=[(f'Restart the launcher — the engine re-derives '
                      f'HEDGE_RATIO ({suggested:g} for this pair, '
                      f'pair type {pair_type}) whenever the pair it was '
                      f'set for has changed'
                      if suggested is not None else
                      'Restart the launcher — the engine re-derives '
                      'HEDGE_RATIO whenever the pair it was set for has '
                      'changed'),
                     'For the SAME underlying (spot vs its future), '
                     'HEDGE_RATIO is 1',
                     f'For two different instruments, a spread only '
                     f'makes sense near the price ratio '
                     f'({ratio:.4f} right now)',
                     'It is NOT the contract-size ratio — contract sizes '
                     'are already handled when sizing the hedge'])
        else:
            checklist.add(
                'PAIR', 'Hedge ratio', PASS,
                f'HEDGE_RATIO {hedge_ratio:g} gives a spread of '
                f'{spread:+.4f} against prices {spot_price:.4f} / '
                f'{fut_price:.4f}', details=details)

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
