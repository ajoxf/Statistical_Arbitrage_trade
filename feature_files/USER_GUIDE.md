# Multi-Broker Arbitrage System - User Guide

## Overview

This system performs statistical arbitrage between spot and futures markets (e.g., Gold spot vs Gold futures). It monitors the price spread between the two instruments and trades when the spread deviates significantly from its historical mean.

---

## Step-by-Step Setup Guide

### Step 1: Install Requirements

```bash
pip install -r requirements.txt
```

Required packages:
- MetaTrader5 (for MT5 broker connectivity)
- Flask (web interface)
- pandas, numpy (data analysis)

### Step 2: Configure Brokers

1. Open the web interface: `python main.py`
2. Navigate to **Setup** page
3. Add your brokers:

**For MT5 Broker:**
- Broker ID: Unique identifier (e.g., "mt5_spot")
- Name: Display name (e.g., "IC Markets Spot")
- Type: MT5
- Role: SPOT or FUTURES
- MT5 Path: Path to MT5 terminal (e.g., `C:\Program Files\MetaTrader 5\terminal64.exe`)
- Account: Your MT5 account number
- Server: Broker server name
- Password: Trading password
- Symbol: Trading symbol (e.g., "XAUUSD")

**For FIX Broker:**
- Type: FIX
- FIX Host/Port: Broker's FIX server details
- Sender/Target CompID: FIX session identifiers
- Credentials: FIX username/password

### Step 3: Configure Trading Parameters

Navigate to **Settings** page:

**Signal Parameters:**
- Lookback Period: 90 (recommended for gold)
- Lookback Unit: minutes
- Entry SD: 2.0 (enter when spread is 2 standard deviations from mean)
- Exit SD: 0.5 (exit when spread returns near mean)
- Stop Loss SD: 3.0 (stop loss at 3 SD)

**Risk Management:**
- Lot Size: Start with 0.1 (small)
- Max Positions: 1 (recommended for beginners)
- Commission/Lot: Your broker's commission
- Min Profit/Lot: Minimum profit target
- Max Loss/Lot: Maximum acceptable loss

**Filters:**
- Hurst Filter: Enable (blocks trades in trending markets)
- STD Filter: Enable (blocks unprofitable trades)

### Step 4: Start Trading

1. Go to **Dashboard**
2. Ensure brokers show "CONNECTED" status
3. Toggle **Algorithm** to ENABLED
4. The system will automatically:
   - Monitor price spread
   - Generate signals when spread deviates
   - Execute trades when conditions are met

---

## Understanding the Dashboard

### Market Sessions Panel (Blue Bar)
Shows which global markets are currently open:
- **Sydney**: 22:00 - 07:00 UTC
- **Tokyo**: 00:00 - 09:00 UTC
- **London**: 08:00 - 17:00 UTC
- **New York**: 13:00 - 22:00 UTC

Green dot = Session active. Best trading: London/New York overlap.

### Z-Score Monitor
- Shows current spread deviation from mean
- Green zone (|z| < 0.5): Near mean, exit zone
- Red zone (|z| > 2.0): Entry zone

### Spread History Chart
- Tracks spread price over time
- Shows min/max spread values

### Hurst Exponent
- H < 0.5: Mean-reverting (good for arbitrage)
- H > 0.5: Trending (avoid trading)

---

## Key Features Explained

### STD Profitability Filter

This feature prevents unprofitable trades by ensuring expected profit exceeds costs.

**How it works:**
1. Calculates total round-trip costs:
   - Bid-ask spreads (entry + exit)
   - Commissions (4 legs: spot entry/exit, futures entry/exit)
   - Swap costs (if holding overnight)

2. Calculates minimum STD required:
   - Expected move = Entry SD - Exit SD (e.g., 2.0 - 0.5 = 1.5 SD)
   - Min STD = Total Cost / (Expected Move x Position Value) x Profit Margin

3. If current STD < Min STD, trade is blocked.

**Example:**
- Total costs: $94 (spreads + commissions)
- Current STD: $0.28
- Min required STD: $0.63
- Result: Trade blocked (would lose money)

**Solutions for "STD Too Low":**
1. Wait for higher volatility
2. Reduce commission costs (negotiate with broker)
3. Increase lot size (spreads fixed costs over larger position)
4. Trade during high-volatility sessions (London/NY overlap)

### Hurst Exponent Filter

Prevents trading in trending markets where mean reversion fails.

- H < 0.45: Strong mean reversion - TRADE
- H = 0.45-0.55: Random walk - CAUTION
- H > 0.55: Trending - DO NOT TRADE

---

## Trading Workflow

1. **Data Collection**: System collects price history for lookback period
2. **Signal Generation**:
   - Calculates spread = Spot Price - Futures Price
   - Computes Z-score = (Spread - Mean) / STD
3. **Entry Check**:
   - Z-score crosses entry threshold (e.g., ±2.0)
   - Hurst filter passes (H < threshold)
   - STD filter passes (profitable trade expected)
4. **Execution**:
   - Long Spread: Buy Spot, Sell Futures
   - Short Spread: Sell Spot, Buy Futures
5. **Exit**:
   - Z-score returns to exit threshold (e.g., ±0.5)
   - Or stop loss triggered
   - Or time stop triggered

---

## Best Practices

1. **Start with Paper Mode**: Test strategies without real money
2. **Monitor Continuously**: Check system during market hours
3. **Check Connectivity**: Ensure brokers stay connected
4. **Review SD Analysis**: Learn from historical SD touches
5. **Adjust Parameters**: Fine-tune based on market conditions

---

## Troubleshooting

### "STD Too Low" Message
- Market volatility is too low for profitable trades
- Wait for higher volatility or adjust parameters

### Broker Disconnected
- Check MT5 terminal is running
- Verify credentials are correct
- Check internet connection

### No Trades Executing
- Verify Algorithm is ENABLED
- Check filters aren't blocking all signals
- Review signal thresholds

### Position Won't Close
- Use "Force Remove" button if MT5 position is stale
- Manually close in MT5 if needed

---

## Support

For technical issues, check:
- MT5 terminal logs
- System console output
- Web interface error messages
