"""Telegram notifications + interactive commands.

Ported from the June feature_files app (message formats proven in
live use) onto the new engine:

- All sends go through a background worker thread and queue — the
  trading loop NEVER blocks on Telegram HTTP.
- Disabled cleanly when no token/chat configured (every notify_* call
  becomes a no-op).
- Optional command polling (/status /positions /pnl /help) via
  getUpdates long-poll; /start auto-registers the chat id (their
  live-tested fix — users never found the chat id by hand).

Credentials come from the environment only (TELEGRAM_BOT_TOKEN,
TELEGRAM_CHAT_ID) — never from config files.
"""

import json
import logging
import os
import queue
import threading
import urllib.parse
import urllib.request


def _http_transport(url, payload, timeout=10):
    data = urllib.parse.urlencode(payload).encode('utf-8')
    with urllib.request.urlopen(
            urllib.request.Request(url, data=data), timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


class TelegramNotifier:
    def __init__(self, config, token=None, chat_id=None, transport=None):
        self.config = config
        self.token = token or os.environ.get('TELEGRAM_BOT_TOKEN', '')
        self.chat_id = chat_id or os.environ.get('TELEGRAM_CHAT_ID', '')
        self.transport = transport or _http_transport
        self.command_handler = None      # set by the coordinator
        self._queue = queue.Queue()
        self._worker = None
        self._poller = None
        self._stop = threading.Event()
        self._last_update_id = 0

        tg_cfg = getattr(config, 'TELEGRAM', {})
        self.enabled = bool(self.token) and tg_cfg.get('ENABLED', True)
        if self.enabled:
            self._worker = threading.Thread(target=self._drain, daemon=True)
            self._worker.start()
            if tg_cfg.get('COMMANDS', True):
                self._poller = threading.Thread(target=self._poll_commands,
                                                daemon=True)
                self._poller.start()
            logging.info("Telegram notifier active (chat %s)",
                         self.chat_id or "pending /start")
        else:
            logging.info("Telegram notifier disabled (no token)")

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _api(self, method, payload):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        return self.transport(url, payload)

    def _drain(self):
        while not self._stop.is_set():
            try:
                message = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if message is None:
                break
            try:
                if not self.chat_id:
                    logging.warning("Telegram: no chat id yet — send /start "
                                    "to the bot; dropping message")
                    continue
                self._api('sendMessage', {
                    'chat_id': self.chat_id,
                    'text': message,
                    'parse_mode': 'HTML',
                })
            except Exception as e:
                logging.error("Telegram send failed: %s", e)

    def _send(self, message, kind='trade'):
        if not self.enabled:
            return
        tg_cfg = getattr(self.config, 'TELEGRAM', {})
        gates = {'trade': 'NOTIFY_TRADES', 'signal': 'NOTIFY_SIGNALS',
                 'error': 'NOTIFY_ERRORS', 'system': 'NOTIFY_SYSTEM'}
        if not tg_cfg.get(gates.get(kind, 'NOTIFY_TRADES'), True):
            return
        self._queue.put(message)

    def stop(self):
        self._stop.set()
        self._queue.put(None)

    # ------------------------------------------------------------------
    # Commands (/status /positions /pnl)
    # ------------------------------------------------------------------

    def _poll_commands(self):
        while not self._stop.is_set():
            try:
                updates = self._api('getUpdates', {
                    'offset': self._last_update_id + 1, 'timeout': 25,
                }).get('result', [])
            except Exception:
                self._stop.wait(5)
                continue
            for update in updates:
                self._last_update_id = max(self._last_update_id,
                                           update.get('update_id', 0))
                message = update.get('message') or {}
                text = (message.get('text') or '').strip()
                chat = str((message.get('chat') or {}).get('id', ''))
                if not text.startswith('/'):
                    continue
                if text.split('@')[0].split()[0] == '/start' and chat:
                    # Auto-register: users never find the chat id by hand
                    self.chat_id = self.chat_id or chat
                    self.register_command_menu()
                    self._send("✅ Bot connected.\n" + self.HELP_TEXT,
                               'system')
                    continue
                if chat and chat != str(self.chat_id):
                    continue          # ignore strangers
                self._handle_command(text.split('@')[0])

    HELP_TEXT = "\n".join([
        "<b>COMMANDS</b>",
        f"<code>{'/dashboard':<13}</code>full system snapshot",
        f"<code>{'/ping':<13}</code>alive check (always responds)",
        f"<code>{'/status':<13}</code>engine & algo state",
        f"<code>{'/positions':<13}</code>open positions + levels",
        f"<code>{'/trades':<13}</code>recent closed trades",
        f"<code>{'/balance':<13}</code>account balances (both legs)",
        f"<code>{'/pnl':<13}</code>P&L summary",
        f"<code>{'/stats':<13}</code>edge stats + max drawdown",
        f"<code>{'/shadow':<13}</code>what-if-held: did exits revert?",
        f"<code>{'/eod':<13}</code>end-of-day report",
        f"<code>{'/settings':<13}</code>show tunable settings",
        f"<code>{'/set k v':<13}</code>change a setting live",
        f"<code>{'/pause':<13}</code>halt new entries",
        f"<code>{'/resume':<13}</code>re-enable new entries",
        f"<code>{'/closeall':<13}</code>emergency: close all positions",
    ])

    COMMAND_MENU = [
        ('dashboard', 'Full system snapshot'), ('status', 'Engine state'),
        ('positions', 'Open positions'), ('trades', 'Recent trades'),
        ('balance', 'Account balances'), ('pnl', 'P&L summary'),
        ('stats', 'Edge stats'), ('shadow', 'What-if-held'),
        ('eod', 'End-of-day report'), ('settings', 'Show settings'),
        ('pause', 'Halt entries'), ('resume', 'Enable entries'),
        ('closeall', 'Close all positions'), ('help', 'Command list'),
    ]

    def register_command_menu(self):
        """Best-effort: register the Telegram '/' command menu."""
        if not self.enabled:
            return
        try:
            self._api('setMyCommands', {'commands': json.dumps(
                [{'command': c, 'description': d}
                 for c, d in self.COMMAND_MENU])})
        except Exception as e:
            logging.debug("setMyCommands failed: %s", e)

    def _handle_command(self, command):
        if command in ('/help', '/menu'):
            self._send(self.HELP_TEXT, 'system')
            return
        if command == '/ping':
            self._send("pong 🏓 — bot thread alive", 'system')
            return
        if self.command_handler is None:
            self._send("Engine not attached yet", 'system')
            return
        try:
            reply = self.command_handler(command)
        except Exception as e:
            reply = f"⚠️ Command failed: {e}"
        if reply:
            self._send(reply, 'system')

    # ------------------------------------------------------------------
    # Message builders (formats ported 1:1 from W3 telegram_bot.py,
    # adapted to MT5 fields: lots, contract sizes, the pair spread)
    # ------------------------------------------------------------------

    OUTCOME_LINES = {
        'TARGET_HIT': "TARGET HIT — banked on P&L, no z needed",
        'REVERSION_BANKED': "REVERSION BANKED — z came home, gate satisfied",
        'TIME_EXIT': "TIME EXIT — cut by the clock (max-hold/time-stop)",
        'STOPPED_IN_TREND':
            "STOPPED IN TREND — z never reverted, divergence was real",
        'STOPPED_AFTER_FULL_REVERSION':
            "STOPPED AFTER FULL REVERSION — z came home but price never "
            "recovered BE (mean drift; edge was spent)",
    }

    @staticmethod
    def _row(label, value):
        return f"<code>{label:<13}</code>{value}"

    def notify_startup(self, mode, spot_leg, futures_leg, assets):
        self._send(
            f"🚀 <b>COORDINATOR STARTED</b>\n"
            f"Mode: <b>{mode}</b>\n"
            f"Spot leg: {spot_leg} | Futures leg: {futures_leg}\n"
            f"Assets: {', '.join(assets) or 'none'}\n"
            f"Menu: /help", 'system')

    def notify_trade_opened(self, position, market_data, z=None,
                            contract_size=1.0, is_paper=False):
        plan = position.exit_plan or {}
        levels = plan.get('levels') or {}
        spot, fut = position.spot_trade, position.futures_trade
        R = self._row
        oz = spot.lot_size * contract_size
        notional = ((spot.executed_price or 0)
                    + (fut.executed_price or 0)) * oz
        spread = plan.get('entry_spread')
        if spread is None:
            spread = market_data.get('spread',
                                     market_data.get('actual_basis'))
        fees = plan.get('rt_cost_usd', 0.0)

        rows = [
            R("ID", f"<code>{position.position_id}</code>"),
            R("Entry Time",
              position.entry_time.strftime('%Y-%m-%d %H:%M:%S')),
            "",
            R("Lots", f"{spot.lot_size:.2f} spot / {fut.lot_size:.2f} fut"),
            R("Notional", f"${notional:,.0f}"),
            "",
            R("Spot Entry", f"${spot.executed_price:,.2f}"),
            R("Fut Entry", f"${fut.executed_price:,.2f}"),
        ]
        if spread is not None:
            rows.append(R("Spread", f"{spread:+.4f}"))
        rows += ["", R("Z-score", f"{z:+.4f}" if z is not None
                       else "manual / warm-up")]
        if plan.get('entry_sigma'):
            rows.append(R("Spread SD", f"{plan['entry_sigma']:.4f}"))
        if plan.get('entry_mu') is not None:
            rows.append(R("Spread Mean", f"{plan['entry_mu']:+.4f}"))
        if plan.get('half_life_sec'):
            rows.append(R("Half-Life",
                          f"{plan['half_life_sec'] / 60:.1f} min"))

        if levels:
            arrow = "↓ favorable" if levels.get('favorable') == 'down' \
                else "↑ favorable"
            rows += ["", f"<b>EXIT GEOMETRY</b>  ({arrow})",
                     R("BE", f"{levels['be']:+.4f}  ($0 net)")]
            if levels.get('ex') is not None:
                rows.append(R("EX", f"{levels['ex']:+.4f}  (gate "
                              f"+${plan.get('gate_floor_usd', 0):,.0f})"))
            if levels.get('tp') is not None:
                rows.append(R("TP", f"{levels['tp']:+.4f}  "
                              f"(+${plan.get('tp_usd') or 0:,.0f})"))
            if levels.get('sl') is not None:
                rows.append(R("SL", f"{levels['sl']:+.4f}  "
                              f"(-${plan.get('stop_usd', 0):,.0f} gross)"))
            if plan.get('max_hold_sec'):
                rows.append(R("Max Hold",
                              f"{plan['max_hold_sec'] / 60:.0f} min"))
        if plan.get('capital_at_risk'):
            rows.append(R("Capital",
                          f"${plan['capital_at_risk']:,.0f} at risk"))
        breakeven_move = fees / oz if oz else 0
        rows += ["", R("Est. Fees", f"-${fees:,.2f}  (round-trip)"),
                 R("Breakeven", f"{breakeven_move:+.4f} spread move")]
        if plan.get('source') == 'MANUAL':
            rows.append(R("Source", "MANUAL (web UI)"))

        parts = [f"<b>TRADE ENTRY  ·  {position.signal_type.value}  ·  "
                 f"{position.asset}</b>", "\n".join(rows)]
        if is_paper:
            parts.append("<i>Paper Trading</i>")
        self._send("\n".join(parts), 'trade')

    def notify_trade_closed(self, position, exit_z=None, outcome=None,
                            exit_spread=None, contract_size=1.0,
                            is_paper=False):
        plan = position.exit_plan or {}
        spot, fut = position.spot_trade, position.futures_trade
        R = self._row
        gross = position.realized_pnl or 0.0
        fees = plan.get('rt_cost_usd', 0.0)
        net = gross - fees
        oz = spot.lot_size * contract_size
        notional = ((spot.executed_price or 0)
                    + (fut.executed_price or 0)) * oz
        net_pct = (net / notional * 100) if notional else 0.0
        result = "PROFIT" if net >= 0 else "LOSS"

        duration = "—"
        exit_time = "—"
        held_min = None
        if position.close_time:
            exit_time = position.close_time.strftime('%Y-%m-%d %H:%M:%S')
            seconds = int((position.close_time
                           - position.entry_time).total_seconds())
            held_min = seconds / 60
            duration = (f"{seconds // 60}m {seconds % 60}s"
                        if seconds < 3600 else
                        f"{seconds // 3600}h {(seconds % 3600) // 60}m")

        rows = [R("Reason", position.close_reason or "EXIT"),
                R("Duration", duration),
                R("Exit Time", exit_time), ""]
        if spot.executed_price:
            rows.append(R("Spot Entry", f"${spot.executed_price:,.2f}"))
        if position.exit_spot_price:
            rows.append(R("Spot Exit",
                          f"${position.exit_spot_price:,.2f}"))
        if fut.executed_price:
            rows.append(R("Fut Entry", f"${fut.executed_price:,.2f}"))
        if position.exit_fut_price:
            rows.append(R("Fut Exit", f"${position.exit_fut_price:,.2f}"))
        entry_spread = plan.get('entry_spread')
        entry_z = plan.get('entry_z')
        if entry_spread is not None:
            z_note = f"  (Z: {entry_z:+.4f})" if entry_z is not None else ""
            rows += ["", R("Entry Spread", f"{entry_spread:+.4f}{z_note}")]
        if exit_spread is not None:
            z_note = f"  (Z: {exit_z:+.4f})" if exit_z is not None else ""
            rows.append(R("Exit Spread", f"{exit_spread:+.4f}{z_note}"))
        if entry_spread is not None and exit_spread is not None:
            direction_sign = -1 if position.signal_type.value == \
                'SELL_BASIS' else 1
            change = direction_sign * (exit_spread - entry_spread)
            rows.append(R("Spread Chg", f"{change:+.4f} (favorable +)"))
        rows += ["",
                 R("Gross PnL", f"${gross:+,.2f}"),
                 R("Est. Fees", f"-${fees:,.2f}  (round-trip)"),
                 R("Net PnL", f"${net:+,.2f}  ({net_pct:+.3f}%)")]

        # ── Crisp lifecycle analysis: exact numbers, rule-based verdict ──
        rows += ["", "<b>ANALYSIS</b>"]
        if outcome:
            rows.append(R("Outcome",
                          self.OUTCOME_LINES.get(outcome, outcome)))
        reason_u = (position.close_reason or "").upper()
        if reason_u == 'DOLLAR_STOP' and plan.get('stop_usd'):
            rows.append(R("Stop type", f"DOLLAR stop — gross ≤ "
                          f"-${plan['stop_usd']:,.0f} (capital cap)"))
        elif reason_u == 'Z_STOP':
            note = f"z-stop (|z| ≥ {self.config.SIGNALS['STOP_Z']:g})"
            if plan.get('stop_usd'):
                note += (f" — dollar stop -${plan['stop_usd']:,.0f} NOT "
                         f"reached (gross ${gross:+,.2f})")
            rows.append(R("Stop type", note))
        if position.peak_pnl is not None:
            pk = f"+${position.peak_pnl:,.2f}"
            if position.peak_min is not None:
                pk += f" ({position.peak_min:.0f}m)"
            tr = f"{position.trough_pnl:+,.2f}"
            if position.trough_min is not None:
                tr += f" ({position.trough_min:.0f}m)"
            rows.append(R("Peak/Trough", f"{pk} / {tr}"))
        if entry_z and plan.get('entry_sigma') and oz:
            available = abs(entry_z) * plan['entry_sigma'] * oz
            if available > 0:
                rows.append(R("Capture", f"gross ${gross:+,.2f} of "
                              f"${available:,.2f} avail "
                              f"({gross / available * 100:+.0f}%)"))
        if held_min is not None:
            hold = f"{held_min:.0f}m"
            if plan.get('max_hold_sec'):
                max_hold_min = plan['max_hold_sec'] / 60
                hold += (f"  (max {max_hold_min:.0f}m "
                         f"×{held_min / max_hold_min:.1f})")
            rows.append(R("Hold", hold))
        if entry_z is not None and exit_z is not None:
            z_path = f"{entry_z:+.2f} → {exit_z:+.2f}"
            if position.z_min is not None and position.z_max is not None:
                z_path += (f"  (range {position.z_min:+.2f}…"
                           f"{position.z_max:+.2f})")
            rows.append(R("Z path", z_path))

        parts = [f"<b>TRADE EXIT  ·  {position.signal_type.value}  ·  "
                 f"{position.asset}  ·  {result}</b>", "\n".join(rows)]
        if is_paper:
            parts.append("<i>Paper Trading</i>")
        self._send("\n".join(parts), 'trade')

    def notify_breaker(self, reason):
        self._send(f"🛑 <b>CIRCUIT BREAKER</b>\n"
                   f"New entries HALTED: {reason}", 'error')

    def notify_reconcile(self, action, leg_name, detail):
        self._send(f"⚠️ <b>RECONCILE: {action}</b>\n"
                   f"Leg: {leg_name}\n{detail}", 'error')

    def notify_error(self, error_message):
        self._send(f"⚠️ <b>ERROR</b>\n{error_message}", 'error')

    def notify_shutdown(self, metrics):
        self._send(
            f"⏹ <b>COORDINATOR STOPPED</b>\n"
            f"Total P&L: <code>${metrics['total_pnl']:,.0f}</code> | "
            f"Trades: {metrics['total_trades']} | "
            f"Win rate: {metrics['win_rate']:.1f}%", 'system')
