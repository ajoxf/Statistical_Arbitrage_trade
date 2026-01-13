# AI Trade Analyst System - Design Specification

## Overview

An intelligent system that monitors the trade journal in real-time, analyzes every trade, identifies patterns in winning/losing trades, and provides actionable insights.

---

## PHASE 1: Real-Time Trade Analysis Agent

### Purpose
Automatically analyze each closed trade and explain WHY it won or lost.

### Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    AI TRADE ANALYST AGENT                           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│   ┌─────────────┐    ┌──────────────┐    ┌─────────────────────┐   │
│   │   Trade     │───▶│   Analysis   │───▶│   Report Generator  │   │
│   │   Monitor   │    │   Engine     │    │   (Claude API)      │   │
│   └─────────────┘    └──────────────┘    └─────────────────────┘   │
│         │                   │                      │                │
│         ▼                   ▼                      ▼                │
│   ┌─────────────┐    ┌──────────────┐    ┌─────────────────────┐   │
│   │  Database   │    │   Market     │    │   Dashboard/        │   │
│   │  Listener   │    │   Context    │    │   Notifications     │   │
│   └─────────────┘    └──────────────┘    └─────────────────────┘   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### Data Points Analyzed Per Trade

#### 1. Entry Analysis
- Z-score at entry (was it truly at threshold?)
- Spread level vs historical mean
- Time of entry (NY session? London? Asian?)
- Day of week
- Hurst exponent at entry (mean-reverting or trending?)
- Half-life estimate
- Recent volatility (1h, 4h, 24h)
- Distance to major support/resistance

#### 2. Exit Analysis
- Exit trigger type:
  - `CLOSE` - Hit exit threshold (+2.0σ)
  - `STOP_LOSS` - Hit stop loss (6.0σ)
  - `MAX_LOSS` - Hit dollar loss limit
  - `TIME_STOP` - Position aged out
  - `OVERNIGHT_CLOSE` - Swap avoidance
  - `MANUAL` - User closed
- Z-score at exit
- Time held
- Maximum adverse excursion (MAE)
- Maximum favorable excursion (MFE)

#### 3. P&L Breakdown
- Gross P&L (price movement only)
- Spread costs (bid-ask)
- Commission
- Swap charges
- Net P&L
- Cost ratio (costs / gross P&L)

#### 4. Market Context
- Was there a news event? (NFP, FOMC, CPI)
- Session (Asian, London, NY)
- Day of week effect
- Correlation with DXY/SPX
- Gold/Silver specific events

### Analysis Report Template

```
═══════════════════════════════════════════════════════════════════
📊 TRADE ANALYSIS REPORT - Trade #103
═══════════════════════════════════════════════════════════════════

SUMMARY: ❌ LOSING TRADE (-$730 Net P&L)

┌─────────────────────────────────────────────────────────────────┐
│ WHAT HAPPENED                                                   │
├─────────────────────────────────────────────────────────────────┤
│ Entry: 2026-01-13 17:56 at Z = -3.67                           │
│ Exit:  2026-01-13 17:56 at Z = -1.94                           │
│ Direction: Long Spread (Buy Futures, Sell Spot)                │
│ Duration: <1 minute                                            │
│ Exit Reason: MAX_LOSS (Dollar stop triggered)                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ WHY IT LOST                                                     │
├─────────────────────────────────────────────────────────────────┤
│ PRIMARY CAUSE: Premature exit due to max_loss_per_lot          │
│                                                                 │
│ • Entered at Z=-3.67, needed to reach Z=+2.0 for profit       │
│ • This requires 5.67σ spread movement                          │
│ • With current volatility, estimated time: 45-90 minutes       │
│ • MAX_LOSS triggered at $600 before mean reversion occurred   │
│                                                                 │
│ CONTRIBUTING FACTORS:                                           │
│ • High transaction costs: $1,060 (54% of position size)        │
│ • Lot size too large: 20 lots = $2,000/σ exposure              │
│ • Max loss too tight: $30/lot × 20 = $600 total               │
│ • Entry near NY close: Lower liquidity, wider spreads          │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ RECOMMENDATIONS                                                 │
├─────────────────────────────────────────────────────────────────┤
│ 1. Increase max_loss_per_lot to $150-200                       │
│ 2. Reduce lot size to 10 lots (halves cost exposure)           │
│ 3. Avoid entries after 17:00 (wider spreads)                   │
│ 4. Current cost structure requires $960+ gross for profit      │
└─────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════
```

---

## PHASE 2: ML Pattern Recognition System

### Purpose
Learn from historical trades to predict outcomes and optimize parameters.

### Minimum Data Requirements

| Model Type | Minimum Trades | Recommended | Notes |
|------------|----------------|-------------|-------|
| Basic Patterns | 100 | 300 | Win/loss by time of day |
| Logistic Regression | 300 | 500 | Binary win/loss prediction |
| Random Forest | 500 | 1,000 | Feature importance ranking |
| XGBoost | 500 | 1,500 | Entry/exit optimization |
| Neural Network | 1,000 | 3,000+ | Deep pattern recognition |
| Reinforcement Learning | 2,000 | 5,000+ | Dynamic strategy adjustment |

### Feature Engineering

#### Time-Based Features
```python
features = {
    # Session Features
    'is_asian_session': bool,      # 00:00-08:00 UTC
    'is_london_session': bool,     # 08:00-16:00 UTC
    'is_ny_session': bool,         # 13:00-21:00 UTC
    'is_ny_open': bool,            # 13:30-14:30 UTC (high volatility)
    'is_london_close': bool,       # 15:30-16:30 UTC
    'is_overlap_session': bool,    # 13:00-16:00 UTC (London/NY)

    # Day Features
    'day_of_week': int,            # 0=Monday, 4=Friday
    'is_monday': bool,             # Gap risk
    'is_friday': bool,             # Position squaring
    'is_month_end': bool,          # Rebalancing flows

    # Calendar Events
    'hours_to_nfp': float,         # Non-Farm Payrolls (first Friday)
    'hours_to_fomc': float,        # Fed meetings
    'hours_to_cpi': float,         # Inflation data
    'hours_to_ecb': float,         # ECB decisions
    'is_opex_week': bool,          # Options expiration
}
```

#### Statistical Features
```python
statistical_features = {
    # Entry Conditions
    'entry_zscore': float,
    'entry_spread': float,
    'spread_mean': float,
    'spread_std': float,
    'entry_deviation_from_mean': float,

    # Regime Detection
    'hurst_exponent': float,       # <0.5 mean-reverting, >0.5 trending
    'half_life': float,            # Expected reversion time (bars)
    'adf_pvalue': float,           # Stationarity test
    'is_mean_reverting': bool,     # Hurst < 0.45

    # Volatility Features
    'volatility_1h': float,
    'volatility_4h': float,
    'volatility_24h': float,
    'volatility_regime': str,      # 'low', 'normal', 'high', 'extreme'
    'vol_of_vol': float,           # Volatility clustering

    # Momentum Features
    'spread_momentum_5': float,    # 5-bar momentum
    'spread_momentum_20': float,   # 20-bar momentum
    'spread_acceleration': float,  # Rate of change of momentum
}
```

#### Market Context Features
```python
market_features = {
    # Correlations
    'dxy_correlation_20': float,   # Dollar index correlation
    'spx_correlation_20': float,   # S&P 500 correlation
    'vix_level': float,            # Risk sentiment
    'gold_silver_ratio': float,    # Metal sentiment

    # Liquidity
    'bid_ask_spread_spot': float,
    'bid_ask_spread_futures': float,
    'volume_ratio': float,         # Current vs average volume

    # Positioning
    'cot_net_position': float,     # COT report data
    'open_interest_change': float,
}
```

### ML Model Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                    ML PREDICTION PIPELINE                           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────────┐  │
│  │   Raw    │───▶│ Feature  │───▶│  Model   │───▶│  Prediction  │  │
│  │  Trade   │    │ Engineer │    │ Ensemble │    │  + Confidence│  │
│  │  Data    │    │          │    │          │    │              │  │
│  └──────────┘    └──────────┘    └──────────┘    └──────────────┘  │
│       │              │               │                  │           │
│       │              │               │                  ▼           │
│       │              │               │          ┌──────────────┐   │
│       │              │               │          │   Trading    │   │
│       │              │               │          │   Decision   │   │
│       │              │               │          └──────────────┘   │
│       │              │               │                              │
│       ▼              ▼               ▼                              │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                    FEEDBACK LOOP                              │  │
│  │  • Track prediction accuracy                                  │  │
│  │  • Update model weights                                       │  │
│  │  • Retrain periodically                                       │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## PHASE 3: Reinforcement Learning System

### Purpose
Dynamically optimize trading parameters based on market conditions.

### RL Agent Design

```python
class TradingRLAgent:
    """
    State: Market conditions + current position
    Actions: Entry/Exit decisions + parameter adjustments
    Reward: Risk-adjusted returns (Sharpe ratio)
    """

    # State Space (what the agent observes)
    state_space = {
        'zscore': continuous(-6, 6),
        'hurst': continuous(0, 1),
        'volatility_regime': discrete(['low', 'normal', 'high']),
        'session': discrete(['asian', 'london', 'ny', 'overlap']),
        'has_position': binary,
        'unrealized_pnl': continuous,
        'time_in_trade': continuous,
    }

    # Action Space (what the agent can do)
    action_space = {
        'entry_action': discrete(['no_entry', 'enter_long', 'enter_short']),
        'exit_action': discrete(['hold', 'exit']),
        'lot_size_adjustment': continuous(0.5, 2.0),  # Multiplier
        'entry_threshold_adjust': continuous(-0.5, 0.5),  # σ adjustment
    }

    # Reward Function
    def calculate_reward(self, trade_result):
        if trade_result.is_winner:
            base_reward = trade_result.net_pnl / trade_result.max_risk
        else:
            base_reward = trade_result.net_pnl / trade_result.max_risk * 1.5  # Penalize losses more

        # Risk-adjusted component
        sharpe_component = self.rolling_sharpe_ratio * 0.2

        # Drawdown penalty
        drawdown_penalty = -abs(self.current_drawdown) * 0.1

        return base_reward + sharpe_component + drawdown_penalty
```

### RL Training Process

```
Episode = 1 Trading Day
Step = 1 Price Update (every 5 seconds)

For each episode:
    1. Reset environment to start of day
    2. Agent observes market state
    3. Agent decides: Enter? Exit? Adjust parameters?
    4. Execute action in simulation
    5. Observe reward (P&L)
    6. Update Q-values / Policy gradient
    7. Repeat until end of day

After N episodes:
    - Evaluate on held-out test data
    - Compare to baseline strategy
    - Deploy if improvement > threshold
```

---

## Calendar Events Database

### High-Impact Events for Gold/Precious Metals

| Event | Typical Impact | Best Practice |
|-------|---------------|---------------|
| **NFP** (First Friday monthly) | ±$20-50 gold move | No entries 2h before, close positions |
| **FOMC** (8x per year) | ±$30-80 gold move | No entries 4h before |
| **CPI** (Monthly) | ±$15-40 gold move | No entries 1h before |
| **ECB Decision** | ±$10-25 gold move | No entries 1h before |
| **GDP** (Quarterly) | ±$10-20 gold move | Caution |
| **ISM PMI** | ±$5-15 gold move | Normal trading |
| **Options Expiry** | Increased volatility | Reduce position size |
| **Month End** | Rebalancing flows | Wider stops |
| **Year End** | Book squaring | Avoid trading |

### Session Characteristics

| Session | Time (UTC) | Characteristics | Optimal For |
|---------|------------|-----------------|-------------|
| **Asian** | 00:00-08:00 | Low volatility, range-bound | Mean reversion entries |
| **London Open** | 07:00-09:00 | Breakouts, trend starts | Momentum |
| **London** | 08:00-16:00 | High liquidity, clear trends | All strategies |
| **NY Open** | 13:00-15:00 | High volatility, reversals | Caution for stat arb |
| **Overlap** | 13:00-16:00 | Highest liquidity | Best for large positions |
| **NY Afternoon** | 18:00-21:00 | Lower liquidity | Avoid entries |

---

## Implementation Roadmap

### Week 1-2: Phase 1 - Real-Time Analyst
- [ ] Build trade monitor that watches database
- [ ] Create analysis engine with all data points
- [ ] Integrate Claude API for natural language reports
- [ ] Build dashboard to display analysis
- [ ] Add email/Telegram notifications for losing trades

### Week 3-4: Phase 2 - ML Pattern Recognition
- [ ] Collect 300+ trades
- [ ] Feature engineering pipeline
- [ ] Train initial Random Forest model
- [ ] Build feature importance dashboard
- [ ] A/B test ML signals vs baseline

### Month 2-3: Phase 3 - Reinforcement Learning
- [ ] Build RL simulation environment
- [ ] Train DQN/PPO agent on historical data
- [ ] Paper trade with RL recommendations
- [ ] Gradual deployment with position limits

---

## Data Requirements Summary

| Phase | Trades Needed | Time to Collect (20 trades/day) |
|-------|---------------|--------------------------------|
| Phase 1 (Analyst) | 0 (works immediately) | N/A |
| Phase 2 (Basic ML) | 300-500 | 15-25 days |
| Phase 2 (Advanced ML) | 1,000-1,500 | 50-75 days |
| Phase 3 (RL) | 2,000-5,000 | 100-250 days |

---

## Immediate Actions

Based on your current trade data, these are the immediate fixes needed:

1. **CRITICAL: Increase max_loss_per_lot to $150-200**
   - Current: ~$30/lot (triggering at $600)
   - Required: Enough room for 5.5σ spread movement

2. **CRITICAL: Reduce lot size OR reduce costs**
   - 20 lots = $960 round-trip costs
   - Consider 10 lots = ~$480 costs (50% reduction)

3. **Optimize entry timing**
   - Avoid entries after 17:00 UTC (wider spreads)
   - Prefer London/NY overlap (13:00-16:00 UTC)

4. **Statistical requirement**
   - Break-even requires gross profit > $960
   - With 20 lots and current volatility, need ~$0.48 spread move per oz
   - This is approximately 1.5σ movement minimum
