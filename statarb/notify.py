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
                if text.split('@')[0] == '/start' and chat:
                    # Auto-register: users never find the chat id by hand
                    self.chat_id = self.chat_id or chat
                    self._send("✅ Bot connected. Commands: /status "
                               "/positions /pnl /help", 'system')
                    continue
                if chat and chat != str(self.chat_id):
                    continue          # ignore strangers
                self._handle_command(text.split('@')[0])

    def _handle_command(self, command):
        if command == '/help':
            self._send("Commands: /status /positions /pnl /help", 'system')
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
    # Message builders (formats ported from the June app)
    # ------------------------------------------------------------------

    def notify_startup(self, mode, spot_leg, futures_leg, assets):
        self._send(
            f"🚀 <b>COORDINATOR STARTED</b>\n"
            f"Mode: <b>{mode}</b>\n"
            f"Spot leg: {spot_leg} | Futures leg: {futures_leg}\n"
            f"Assets: {', '.join(assets) or 'none'}", 'system')

    def notify_trade_opened(self, position, market_data, z=None):
        plan = position.exit_plan or {}
        spot = position.spot_trade
        fut = position.futures_trade
        message = (
            f"🟢 <b>TRADE ENTRY</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>ID:</b> <code>{position.position_id}</code>  "
            f"<b>{position.asset}</b> {position.signal_type.value}\n"
            f"<b>Lots:</b> {spot.lot_size:.2f} spot / "
            f"{fut.lot_size:.2f} futures\n\n"
            f"<b>📊 FILLS</b>\n"
            f"├ Spot: <code>${spot.executed_price:.2f}</code>\n"
            f"├ Futures: <code>${fut.executed_price:.2f}</code>\n"
            f"└ Basis: <code>${market_data['actual_basis']:.2f}</code>"
        )
        if z is not None:
            message += f"  (z=<code>{z:.2f}</code>)"
        if plan:
            message += "\n\n<b>🎯 EXIT PLAN</b>\n"
            tp = plan.get('tp_usd')
            if tp:
                message += f"├ TP: <code>${tp:,.0f}</code>\n"
            message += (
                f"├ Stop: <code>-${plan.get('stop_usd', 0):,.0f}</code>\n"
                f"└ Max hold: "
                f"<code>{plan.get('max_hold_sec', 0) / 60:.0f}min</code>")
        self._send(message, 'trade')

    def notify_trade_closed(self, position, exit_z=None, outcome=None):
        pnl = position.realized_pnl
        emoji = "🟢" if pnl >= 0 else "🔴"
        sign = "+" if pnl >= 0 else ""
        held = position.close_time - position.entry_time \
            if position.close_time else None
        held_str = (f"{held.total_seconds() / 3600:.1f}h" if held else "?")
        plan = position.exit_plan or {}
        message = (
            f"{emoji} <b>TRADE EXIT</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>ID:</b> <code>{position.position_id}</code>  "
            f"<b>{position.asset}</b> {position.signal_type.value}\n"
            f"<b>Reason:</b> {position.close_reason}  "
            f"<b>Held:</b> {held_str}\n"
        )
        if outcome:
            message += f"<b>Outcome:</b> {outcome}\n"
        message += f"\n<b>💰 Net P&L: <code>{sign}${pnl:,.2f}</code></b>"
        if position.peak_pnl is not None:
            message += (
                f"\nPeak/Trough: <code>${position.peak_pnl:+,.2f}</code> "
                f"({position.peak_min:.0f}m) / "
                f"<code>${position.trough_pnl:+,.2f}</code> "
                f"({position.trough_min:.0f}m)")
        if plan.get('entry_z') is not None:
            message += f"\nEntry z: <code>{plan['entry_z']:.2f}</code>"
        if exit_z is not None:
            message += f" → Exit z: <code>{exit_z:.2f}</code>"
        self._send(message, 'trade')

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
