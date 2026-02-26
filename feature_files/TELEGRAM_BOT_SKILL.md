# Telegram Bot Integration Skill

A comprehensive guide for adding Telegram notifications to trading algorithms. This skill enables real-time trade alerts, execution metrics, and P&L tracking via Telegram.

## Overview

This integration provides:
- **Trade Entry Alerts**: Direction, prices, spread, z-score, execution metrics
- **Trade Exit Alerts**: Full P&L breakdown, entry/exit comparison, execution drift
- **Signal Alerts** (optional): When trading signals are generated
- **Error Alerts** (optional): Critical errors and failures

---

## Step 1: Create a Telegram Bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` command
3. Follow the prompts:
   - Enter a name for your bot (e.g., "My Trading Algo")
   - Enter a username (must end in `bot`, e.g., `my_trading_algo_bot`)
4. **Save the Bot Token** - looks like: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`

## Step 2: Get Your Chat ID

1. Search for **@userinfobot** or **@getidsbot** on Telegram
2. Start a conversation with the bot
3. It will reply with your Chat ID (e.g., `123456789`)

For group chats:
- Add your bot to the group
- The chat ID will be negative (e.g., `-100123456789`)

---

## Step 3: Add Configuration Fields

Add these fields to your config/settings model:

```python
# Telegram Notifications
telegram_enabled: bool = False
telegram_bot_token: str = ""
telegram_chat_id: str = ""
telegram_notify_trades: bool = True   # Notify on trade entry/exit
telegram_notify_signals: bool = False  # Notify on signal generation
telegram_notify_errors: bool = True    # Notify on errors
```

---

## Step 4: Core Notification Function

Add this base function to send messages via Telegram API:

```python
def send_telegram_notification(message: str, notify_type: str = 'trade') -> bool:
    """
    Send a notification via Telegram bot.

    Args:
        message: The message to send (supports HTML formatting)
        notify_type: Type of notification ('trade', 'signal', 'error')

    Returns:
        True if sent successfully, False otherwise
    """
    try:
        # Get config from your database/settings
        config = get_config()

        if not config or not config.telegram_enabled:
            return False

        if not config.telegram_bot_token or not config.telegram_chat_id:
            logger.warning("[TELEGRAM] Bot token or chat ID not configured")
            return False

        # Check notification type preferences
        if notify_type == 'trade' and not config.telegram_notify_trades:
            return False
        elif notify_type == 'signal' and not config.telegram_notify_signals:
            return False
        elif notify_type == 'error' and not config.telegram_notify_errors:
            return False

        import requests

        url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
        payload = {
            'chat_id': config.telegram_chat_id,
            'text': message,
            'parse_mode': 'HTML'
        }

        response = requests.post(url, json=payload, timeout=10)

        if response.status_code == 200:
            logger.info(f"[TELEGRAM] Notification sent: {notify_type}")
            return True
        else:
            logger.error(f"[TELEGRAM] Failed to send: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        logger.error(f"[TELEGRAM] Error sending notification: {e}")
        return False
```

---

## Step 5: Trade Entry Notification

```python
def notify_trade_entry(
    direction: str,
    spot_price: float,
    futures_price: float,
    spread: float,
    zscore: float,
    lot_size: float,
    trade_id: str,
    source: str = 'ALGO',
    exec_mode: str = 'MARKET',
    # Execution metrics (for limit/pegged orders)
    spot_target_price: float = None,
    futures_target_price: float = None,
    spot_fill_time_ms: int = None,
    futures_fill_time_ms: int = None,
    initial_zscore: float = None,
    initial_spread: float = None,
):
    """Send detailed Telegram notification for trade entry."""
    spread_val = futures_price - spot_price

    message = (
        f"🟢 <b>TRADE ENTRY</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Trade ID:</b> <code>{trade_id}</code>\n"
        f"<b>Source:</b> {source}\n"
        f"<b>Exec Mode:</b> {exec_mode}\n"
        f"<b>Direction:</b> {direction}\n"
        f"<b>Lots:</b> {lot_size}\n\n"
        f"<b>📊 PRICES</b>\n"
        f"├ Spot: <code>${spot_price:.2f}</code>\n"
        f"├ Futures: <code>${futures_price:.2f}</code>\n"
        f"├ Spread: <code>${spread_val:.2f}</code>\n"
        f"└ Z-Score: <code>{zscore:.2f}</code>\n"
    )

    # Add execution metrics for limit/pegged orders
    if exec_mode in ['LIMIT', 'PEGGED_LIMIT'] and spot_target_price is not None:
        spot_drift = spot_price - spot_target_price
        futures_drift = futures_price - futures_target_price if futures_target_price else 0
        spread_drift = spread_val - initial_spread if initial_spread else 0
        zscore_drift = zscore - initial_zscore if initial_zscore else 0

        message += (
            f"\n<b>⚡ EXECUTION</b>\n"
            f"├ Spot Target: <code>${spot_target_price:.2f}</code>\n"
            f"├ Spot Fill: <code>${spot_price:.2f}</code> ({'+' if spot_drift >= 0 else ''}{spot_drift:.2f})\n"
            f"├ Futures Target: <code>${futures_target_price:.2f}</code>\n"
            f"├ Futures Fill: <code>${futures_price:.2f}</code> ({'+' if futures_drift >= 0 else ''}{futures_drift:.2f})\n"
        )
        if spot_fill_time_ms is not None:
            message += f"├ Spot Fill Time: <code>{spot_fill_time_ms}ms</code>\n"
        if futures_fill_time_ms is not None:
            message += f"├ Futures Fill Time: <code>{futures_fill_time_ms}ms</code>\n"
        if initial_spread is not None:
            message += f"├ Spread Drift: <code>{'+' if spread_drift >= 0 else ''}{spread_drift:.2f}</code>\n"
        if initial_zscore is not None:
            message += f"└ Z-Score Drift: <code>{'+' if zscore_drift >= 0 else ''}{zscore_drift:.2f}</code>\n"

    send_telegram_notification(message, 'trade')
```

---

## Step 6: Trade Exit Notification

```python
def notify_trade_exit(
    direction: str,
    entry_spread: float,
    exit_spread: float,
    trade_id: str,
    exit_reason: str = 'EXIT',
    source: str = 'ALGO',
    exec_mode: str = 'MARKET',
    lot_size: float = 0.1,
    days_held: float = 0,
    entry_zscore: float = None,
    exit_zscore: float = None,
    entry_spot: float = None,
    entry_futures: float = None,
    exit_spot: float = None,
    exit_futures: float = None,
    spot_pnl: float = 0,
    futures_pnl: float = 0,
    gross_pnl: float = 0,
    swap: float = 0,
    commission: float = 0,
    net_pnl: float = 0,
    # Execution metrics (for limit/pegged orders)
    spot_target_price: float = None,
    futures_target_price: float = None,
    spot_fill_time_ms: int = None,
    futures_fill_time_ms: int = None,
):
    """Send detailed Telegram notification for trade exit."""
    emoji = "🟢" if net_pnl >= 0 else "🔴"
    pnl_sign = "+" if net_pnl >= 0 else ""

    # Calculate return %
    entry_value = abs(entry_spot or 0) * lot_size * 100  # Adjust contract_size as needed
    return_pct = (net_pnl / entry_value * 100) if entry_value > 0 else 0

    message = (
        f"{emoji} <b>TRADE EXIT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Trade ID:</b> <code>{trade_id}</code>\n"
        f"<b>Source:</b> {source}\n"
        f"<b>Exec Mode:</b> {exec_mode}\n"
        f"<b>Direction:</b> {direction}\n"
        f"<b>Exit Reason:</b> {exit_reason}\n"
        f"<b>Days Held:</b> {days_held:.2f}\n\n"
    )

    # Entry details
    message += f"<b>📊 ENTRY</b>\n"
    if entry_spot:
        message += f"├ Spot: <code>${entry_spot:.2f}</code>\n"
    if entry_futures:
        message += f"├ Futures: <code>${entry_futures:.2f}</code>\n"
    message += f"├ Spread: <code>${entry_spread:.2f}</code>\n"
    if entry_zscore is not None:
        message += f"└ Z-Score: <code>{entry_zscore:.2f}</code>\n\n"

    # Exit details
    message += f"<b>📊 EXIT</b>\n"
    if exit_spot:
        message += f"├ Spot: <code>${exit_spot:.2f}</code>\n"
    if exit_futures:
        message += f"├ Futures: <code>${exit_futures:.2f}</code>\n"
    message += f"├ Spread: <code>${exit_spread:.2f}</code>\n"
    if exit_zscore is not None:
        message += f"└ Z-Score: <code>{exit_zscore:.2f}</code>\n\n"

    # P&L Breakdown
    message += (
        f"<b>💰 P&L BREAKDOWN</b>\n"
        f"├ Spot P&L: <code>{'+' if spot_pnl >= 0 else ''}${spot_pnl:.2f}</code>\n"
        f"├ Futures P&L: <code>{'+' if futures_pnl >= 0 else ''}${futures_pnl:.2f}</code>\n"
        f"├ Gross P&L: <code>{'+' if gross_pnl >= 0 else ''}${gross_pnl:.2f}</code>\n"
        f"├ Swap: <code>-${swap:.2f}</code>\n"
        f"├ Commission: <code>-${commission:.2f}</code>\n"
        f"├ <b>Net P&L: <code>{pnl_sign}${net_pnl:.2f}</code></b>\n"
        f"└ Return: <code>{pnl_sign}{return_pct:.2f}%</code>\n"
    )

    # Add execution metrics for limit/pegged orders
    if exec_mode in ['LIMIT', 'PEGGED_LIMIT'] and spot_target_price is not None:
        spot_drift = exit_spot - spot_target_price if exit_spot else 0
        futures_drift = exit_futures - futures_target_price if exit_futures and futures_target_price else 0
        spread_change = exit_spread - entry_spread

        message += (
            f"\n<b>⚡ EXECUTION</b>\n"
            f"├ Spot Target: <code>${spot_target_price:.2f}</code>\n"
            f"├ Spot Fill: <code>${exit_spot:.2f}</code> ({'+' if spot_drift >= 0 else ''}{spot_drift:.2f})\n"
            f"├ Futures Target: <code>${futures_target_price:.2f}</code>\n"
            f"├ Futures Fill: <code>${exit_futures:.2f}</code> ({'+' if futures_drift >= 0 else ''}{futures_drift:.2f})\n"
        )
        if spot_fill_time_ms is not None:
            message += f"├ Spot Fill Time: <code>{spot_fill_time_ms}ms</code>\n"
        if futures_fill_time_ms is not None:
            message += f"├ Futures Fill Time: <code>{futures_fill_time_ms}ms</code>\n"
        message += f"├ Spread Change: <code>{'+' if spread_change >= 0 else ''}{spread_change:.2f}</code>\n"
        if entry_zscore is not None and exit_zscore is not None:
            zscore_change = exit_zscore - entry_zscore
            message += f"└ Z-Score Change: <code>{'+' if zscore_change >= 0 else ''}{zscore_change:.2f}</code>\n"

    send_telegram_notification(message, 'trade')
```

---

## Step 7: Signal & Error Notifications

```python
def notify_signal(signal_type: str, zscore: float, spread: float):
    """Send Telegram notification for signal generation."""
    emoji = "📈" if 'LONG' in signal_type else "📉" if 'SHORT' in signal_type else "⚡"
    message = (
        f"{emoji} <b>SIGNAL: {signal_type}</b>\n\n"
        f"<b>Z-Score:</b> {zscore:.2f}\n"
        f"<b>Spread:</b> ${spread:.2f}"
    )
    send_telegram_notification(message, 'signal')


def notify_error(error_message: str):
    """Send Telegram notification for errors."""
    message = f"⚠️ <b>ERROR</b>\n\n{error_message}"
    send_telegram_notification(message, 'error')
```

---

## Step 8: API Endpoint for Settings

Add an API endpoint to save Telegram settings:

```python
@app.route('/api/telegram/settings', methods=['POST'])
def api_telegram_settings():
    """Save Telegram notification settings and send a test message"""
    data = request.get_json()

    # Update config fields
    database.update_config_field('telegram_enabled', data.get('telegram_enabled', False))
    database.update_config_field('telegram_bot_token', data.get('telegram_bot_token', ''))
    database.update_config_field('telegram_chat_id', data.get('telegram_chat_id', ''))
    database.update_config_field('telegram_notify_trades', data.get('telegram_notify_trades', True))
    database.update_config_field('telegram_notify_signals', data.get('telegram_notify_signals', False))
    database.update_config_field('telegram_notify_errors', data.get('telegram_notify_errors', True))

    # Send test message if enabled
    if data.get('telegram_enabled') and data.get('telegram_bot_token') and data.get('telegram_chat_id'):
        try:
            import requests as req
            url = f"https://api.telegram.org/bot{data.get('telegram_bot_token')}/sendMessage"
            payload = {
                'chat_id': data.get('telegram_chat_id'),
                'text': '✅ <b>Telegram Notifications Connected!</b>\n\nYou will receive trade alerts here.',
                'parse_mode': 'HTML'
            }
            response = req.post(url, json=payload, timeout=10)
            if response.status_code == 200:
                return jsonify({'success': True, 'message': 'Settings saved! Test message sent.'})
            else:
                return jsonify({'success': True, 'message': f'Settings saved but test failed: {response.text}'})
        except Exception as e:
            return jsonify({'success': True, 'message': f'Settings saved but test failed: {str(e)}'})

    return jsonify({'success': True, 'message': 'Telegram settings saved (disabled)'})
```

---

## Step 9: UI Component (HTML/JS)

Add this to your dashboard:

```html
<!-- Telegram Notifications Card -->
<div class="card mb-4">
    <div class="card-header d-flex justify-content-between align-items-center">
        <span><i class="bi bi-telegram me-2"></i>Telegram Notifications</span>
        <div class="form-check form-switch">
            <input class="form-check-input" type="checkbox" id="telegram-enabled"
                   {% if config.telegram_enabled %}checked{% endif %}>
        </div>
    </div>
    <div class="card-body">
        <div class="mb-2">
            <label class="form-label small">Bot Token</label>
            <input type="password" class="form-control form-control-sm" id="telegram-bot-token"
                   value="{{ config.telegram_bot_token or '' }}" placeholder="123456:ABC-DEF...">
        </div>
        <div class="mb-2">
            <label class="form-label small">Chat ID</label>
            <input type="text" class="form-control form-control-sm" id="telegram-chat-id"
                   value="{{ config.telegram_chat_id or '' }}" placeholder="-100123456789">
        </div>
        <div class="mb-2">
            <div class="form-check form-check-inline">
                <input class="form-check-input" type="checkbox" id="telegram-notify-trades"
                       {% if config.telegram_notify_trades %}checked{% endif %}>
                <label class="form-check-label small">Trades</label>
            </div>
            <div class="form-check form-check-inline">
                <input class="form-check-input" type="checkbox" id="telegram-notify-signals"
                       {% if config.telegram_notify_signals %}checked{% endif %}>
                <label class="form-check-label small">Signals</label>
            </div>
            <div class="form-check form-check-inline">
                <input class="form-check-input" type="checkbox" id="telegram-notify-errors"
                       {% if config.telegram_notify_errors %}checked{% endif %}>
                <label class="form-check-label small">Errors</label>
            </div>
        </div>
        <button class="btn btn-sm btn-outline-primary w-100" onclick="saveTelegramSettings()">
            <i class="bi bi-save me-1"></i>Save & Test
        </button>
    </div>
</div>
```

JavaScript function:

```javascript
async function saveTelegramSettings() {
    const enabled = document.getElementById('telegram-enabled').checked;
    const botToken = document.getElementById('telegram-bot-token').value.trim();
    const chatId = document.getElementById('telegram-chat-id').value.trim();
    const notifyTrades = document.getElementById('telegram-notify-trades').checked;
    const notifySignals = document.getElementById('telegram-notify-signals').checked;
    const notifyErrors = document.getElementById('telegram-notify-errors').checked;

    if (enabled && (!botToken || !chatId)) {
        alert('Please enter both Bot Token and Chat ID');
        return;
    }

    try {
        const response = await fetch('/api/telegram/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                telegram_enabled: enabled,
                telegram_bot_token: botToken,
                telegram_chat_id: chatId,
                telegram_notify_trades: notifyTrades,
                telegram_notify_signals: notifySignals,
                telegram_notify_errors: notifyErrors
            })
        });

        const data = await response.json();
        alert(data.message || 'Settings saved!');
    } catch (error) {
        alert('Error saving settings: ' + error.message);
    }
}
```

---

## Step 10: Integration Points

Call the notification functions at these points in your algo:

### Trade Entry
```python
# After trade is successfully recorded
notify_trade_entry(
    direction=direction,
    spot_price=spot_fill_price,
    futures_price=futures_fill_price,
    spread=spread,
    zscore=signal.zscore,
    lot_size=config.lot_size,
    trade_id=trade_id,
    source='ALGO',
    exec_mode='PEGGED_LIMIT',  # or 'MARKET'
    spot_target_price=target_spot,
    futures_target_price=target_futures,
    spot_fill_time_ms=fill_time,
    initial_zscore=signal.zscore,
    initial_spread=signal.spread
)
```

### Trade Exit
```python
# After trade exit is recorded
notify_trade_exit(
    direction=position_direction,
    entry_spread=entry_spread,
    exit_spread=exit_spread,
    trade_id=trade_id,
    exit_reason=exit_reason,  # 'EXIT', 'STOP_LOSS', etc.
    source='ALGO',
    exec_mode='PEGGED_LIMIT',
    lot_size=lot_size,
    days_held=days_held,
    entry_zscore=entry_z,
    exit_zscore=exit_z,
    entry_spot=entry_spot_price,
    entry_futures=entry_futures_price,
    exit_spot=exit_spot_price,
    exit_futures=exit_futures_price,
    spot_pnl=spot_pnl,
    futures_pnl=futures_pnl,
    gross_pnl=gross_pnl,
    swap=swap_charges,
    commission=commission,
    net_pnl=net_pnl,
    spot_target_price=target_spot,
    futures_target_price=target_futures,
    spot_fill_time_ms=fill_time
)
```

---

## HTML Formatting Reference

Telegram supports these HTML tags:
- `<b>bold</b>` - **bold**
- `<i>italic</i>` - *italic*
- `<code>monospace</code>` - `monospace`
- `<pre>preformatted</pre>` - preformatted block
- `<a href="URL">link</a>` - hyperlink

Special characters to escape: `<`, `>`, `&`

---

## Emoji Reference

| Emoji | Use Case |
|-------|----------|
| 🟢 | Profit / Entry / Success |
| 🔴 | Loss / Error |
| 📈 | Long signal |
| 📉 | Short signal |
| ⚡ | Execution / Signal |
| 💰 | P&L |
| 📊 | Prices / Data |
| ⚠️ | Warning / Error |
| ✅ | Success / Connected |

---

## Troubleshooting

### Common Issues

1. **"Bot token invalid"**
   - Check token copied correctly from @BotFather
   - No spaces before/after token

2. **"Chat not found"**
   - Start a conversation with your bot first
   - For groups: ensure bot is added and has message permissions

3. **"Request timeout"**
   - Check internet connectivity
   - Telegram API may be temporarily unavailable

4. **Messages not arriving**
   - Verify `telegram_enabled` is True
   - Check notification type preferences
   - Review logs for `[TELEGRAM]` messages

---

## Interactive Bot Commands

The Telegram bot supports interactive commands that users can send to query trading status, positions, and P&L information. The bot uses long-polling to receive commands.

### Available Commands

| Command | Description |
|---------|-------------|
| `/help` | Show all available commands |
| `/status` | Bot status & system overview |
| `/positions` | Open positions with spread, lot size, entry details |
| `/trades` | Recent closed trades with entry/exit spreads and P&L |
| `/balance` | Account margin information (utilized & available) |
| `/pnl` | P&L summary (today, weekly, total) |
| `/eod` | End of day summary with all positions |

### Command Response Details

#### `/status` Response
- System status (RUNNING/STOPPED)
- Trading mode (PAPER/LIVE)
- Asset being traded
- Open positions count
- Max positions allowed
- Configured lot size
- Win rate and total P&L

#### `/positions` Response
For each open position:
- Trade ID
- Direction (Long Spread/Short Spread)
- **Lot Size**
- Entry Date
- Entry Z-Score
- **Entry Spread** (futures - spot)
- Spot Entry Price
- Futures Entry Price

#### `/trades` Response
For each recent closed trade (last 5):
- Trade ID
- Direction
- **Lot Size**
- Days Held
- **Entry Spread**
- **Exit Spread**
- **Net P&L**

#### `/balance` Response
For connected brokers:
- Balance
- Equity
- **Margin Used**
- **Free Margin (Available)**
- Margin Utilization %

#### `/pnl` Response
- Total trades, wins, losses
- Win rate percentage
- Average P&L per trade
- **Total P&L**
- **Today's P&L**
- **Last 7 days P&L**

#### `/eod` Response (End of Day Summary)
- Date
- **Open positions** with lot size and entry spread
- **Trades closed today** with entry/exit spreads and P&L
- **Daily P&L total**
- All-time P&L

---

## Scheduled End of Day Summary

The bot automatically sends an EOD summary at market close time. The time is configured using the **Overnight Close Hour/Minute** settings.

### Configuration

The EOD summary time uses these config fields:
- `overnight_close_hour` (default: 17)
- `overnight_close_minute` (default: 0)

### EOD Summary Contents

The automatic daily summary includes:
1. **Open Positions**
   - Trade ID, lot size, entry spread for each open position

2. **Closed Today**
   - All trades closed that day
   - Entry and exit spreads
   - Lot sizes
   - Individual P&L

3. **Daily P&L**
   - Today's net P&L (profit or loss highlighted)
   - All-time total P&L

---

## Command Implementation

### Polling System

The bot uses long-polling to receive updates:

```python
def start_telegram_polling():
    """Start the Telegram polling thread."""
    global _telegram_polling_active

    if _telegram_polling_active:
        return  # Already running

    polling_thread = threading.Thread(target=telegram_polling_loop, daemon=True)
    polling_thread.start()
```

### Command Handler

```python
def process_telegram_command(update: dict):
    """Process a single Telegram update/command."""
    message = update.get('message', {})
    text = message.get('text', '')
    chat_id = str(message.get('chat', {}).get('id', ''))

    # Verify authorization
    if chat_id != config.telegram_chat_id:
        return  # Unauthorized

    # Route to command handler
    command = text.split()[0].lower()

    if command == '/status':
        telegram_cmd_status(chat_id)
    elif command == '/positions':
        telegram_cmd_positions(chat_id)
    # ... etc
```

### Security

- Only responds to commands from the configured `telegram_chat_id`
- Unauthorized requests are logged and ignored
- No sensitive data (tokens, passwords) is ever returned in responses

---

## Future Enhancements

- **Inline Keyboards**: Add buttons for quick actions
- **Charts**: Send chart images with trades
- **Trade Commands**: `/close`, `/exit` to execute trades via Telegram

---

## Dependencies

```
pip install requests
```

No special Telegram library needed - uses simple HTTP API calls.
