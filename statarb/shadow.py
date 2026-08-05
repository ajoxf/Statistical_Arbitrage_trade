"""Shadow "what-if-held" tracker (ported from W3).

After a REAL close, keep marking the position virtually: would holding
have reverted to target, crawled back to break-even, or kept bleeding?
Each completed shadow scores the exit decision with data instead of
regret. Verdicts:

- REVERTED_TO_TARGET    — the would-be net hit the original TP
- REVERTED_TO_BREAK_EVEN— net touched >= $0 before the horizon
- KEPT_BLEEDING         — never made it back inside the horizon

Horizon = max(2 x the trade's max-hold, 2h). Pure bookkeeping — no
orders, ever.
"""

import logging
import time as time_mod

from .models import SignalType


class ShadowTracker:
    def __init__(self, data_logger, clock=time_mod.time):
        self.data_logger = data_logger
        self.clock = clock
        self.active = []

    def start(self, position, contract_size):
        if position.spot_trade.executed_price is None \
                or position.futures_trade.executed_price is None:
            return
        plan = position.exit_plan or {}
        max_hold = plan.get('max_hold_sec') or 14400
        self.active.append({
            'position_id': position.position_id,
            'asset': position.asset,
            'signal_type': position.signal_type,
            'entry_spot': position.spot_trade.executed_price,
            'entry_fut': position.futures_trade.executed_price,
            'spot_lots': position.spot_trade.lot_size,
            'fut_lots': position.futures_trade.lot_size,
            'contract_size': contract_size,
            'fees': plan.get('rt_cost_usd', 0.0),
            'tp_usd': plan.get('tp_usd'),
            'exit_pnl': position.realized_pnl,
            'exit_reason': position.close_reason,
            'started': self.clock(),
            'horizon_sec': max(2 * max_hold, 7200),
            'net': None, 'peak': None, 'trough': None,
            'hit_be_min': None, 'hit_tp_min': None,
        })
        logging.info("Shadow tracking %s (what-if-held, horizon %.0fmin)",
                     position.position_id,
                     self.active[-1]['horizon_sec'] / 60)

    def update(self, asset, spot_price, futures_price):
        now = self.clock()
        finished = []
        for shadow in self.active:
            if shadow['asset'] != asset:
                continue
            oz_spot = shadow['spot_lots'] * shadow['contract_size']
            oz_fut = shadow['fut_lots'] * shadow['contract_size']
            if shadow['signal_type'] == SignalType.SELL_BASIS:
                gross = (spot_price - shadow['entry_spot']) * oz_spot \
                    + (shadow['entry_fut'] - futures_price) * oz_fut
            else:
                gross = (shadow['entry_spot'] - spot_price) * oz_spot \
                    + (futures_price - shadow['entry_fut']) * oz_fut
            net = gross - shadow['fees']
            shadow['net'] = net
            minutes = (now - shadow['started']) / 60

            if shadow['peak'] is None or net > shadow['peak']:
                shadow['peak'] = net
            if shadow['trough'] is None or net < shadow['trough']:
                shadow['trough'] = net
            if shadow['hit_be_min'] is None and net >= 0:
                shadow['hit_be_min'] = minutes

            if shadow['tp_usd'] and net >= shadow['tp_usd']:
                shadow['hit_tp_min'] = minutes
                shadow['verdict'] = 'REVERTED_TO_TARGET'
                finished.append(shadow)
            elif now - shadow['started'] >= shadow['horizon_sec']:
                shadow['verdict'] = ('REVERTED_TO_BREAK_EVEN'
                                     if shadow['hit_be_min'] is not None
                                     else 'KEPT_BLEEDING')
                finished.append(shadow)

        for shadow in finished:
            self.active.remove(shadow)
            self.data_logger.log_shadow(shadow)
            logging.info("Shadow complete %s: %s (what-if net $%.0f, "
                         "actual exit $%.0f)", shadow['position_id'],
                         shadow['verdict'], shadow['net'],
                         shadow['exit_pnl'] or 0)

    def snapshot(self):
        return [{
            'position_id': s['position_id'], 'asset': s['asset'],
            'exit_reason': s['exit_reason'], 'exit_pnl': s['exit_pnl'],
            'net': s['net'], 'peak': s['peak'], 'trough': s['trough'],
            'minutes': (self.clock() - s['started']) / 60,
            'horizon_min': s['horizon_sec'] / 60,
        } for s in self.active]
