#!/usr/bin/env python3
"""
AI Trade Analyst Agent - Enhanced Version
Monitors trade journal, verifies MT5 execution, and provides accurate root cause analysis.

Features:
- Verifies MT5 orders match Trade Journal (prices, times, volumes)
- Loads actual Settings configuration for accurate analysis
- Distinguishes between timing issues, max_loss issues, cost issues, etc.
- Provides actionable recommendations based on YOUR settings

Usage:
    python trade_analyst.py --watch           # Watch for new trades in real-time
    python trade_analyst.py --analyze 103     # Analyze specific trade
    python trade_analyst.py --report          # Generate daily report
    python trade_analyst.py --verify 103      # Verify MT5 execution for trade
"""

import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
import time

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Try to import MT5
try:
    import MetaTrader5 as mt5
    HAS_MT5 = True
except ImportError:
    HAS_MT5 = False
    print("Warning: MetaTrader5 not installed. MT5 verification disabled.")

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False


@dataclass
class TradingConfig:
    """Trading configuration from Settings"""
    # Entry/Exit Thresholds
    entry_std_dev: float = 3.5
    exit_std_dev: float = 2.0
    stop_loss_std_dev: float = 6.0

    # Position Settings
    lot_size: float = 20.0
    max_positions: int = 1

    # Time Settings
    time_stop_loss_days: int = 0
    close_before_overnight: bool = False
    overnight_close_hour: int = 16
    overnight_close_minute: int = 55
    no_entry_after_enabled: bool = False
    no_entry_after_hour: int = 16
    no_entry_after_minute: int = 30

    # Cost Settings
    min_profit_per_lot: float = 50.0
    max_loss_per_lot: float = 100.0
    commission_per_lot: float = 0.0

    # Hurst Settings
    hurst_enabled: bool = True
    hurst_threshold: float = 0.5
    trending_duration_minutes: int = 15

    # Order Settings
    order_type: str = 'MARKET'
    limit_order_timeout: int = 60

    # Asset Settings
    spot_symbol: str = ''
    futures_symbol: str = ''
    contract_size: float = 100.0

    # Server timezone
    server_timezone: str = 'UTC'


@dataclass
class TradeData:
    """Structured trade data from journal"""
    trade_id: str = ''
    trade_number: int = 0
    direction: str = ''
    lot_size: float = 0.0
    entry_time: datetime = field(default_factory=datetime.now)
    exit_time: datetime = field(default_factory=datetime.now)
    entry_zscore: float = 0.0
    exit_zscore: float = 0.0
    entry_spread: float = 0.0
    exit_spread: float = 0.0
    entry_spot_price: float = 0.0
    entry_futures_price: float = 0.0
    exit_spot_price: float = 0.0
    exit_futures_price: float = 0.0
    gross_pnl: float = 0.0
    spread_cost: float = 0.0
    swap_cost: float = 0.0
    commission: float = 0.0
    net_pnl: float = 0.0
    close_reason: str = ''
    duration_seconds: int = 0
    # MT5 ticket numbers for verification
    spot_entry_ticket: int = 0
    futures_entry_ticket: int = 0
    spot_exit_ticket: int = 0
    futures_exit_ticket: int = 0


@dataclass
class MT5Verification:
    """MT5 order verification results"""
    verified: bool = False
    spot_entry_match: bool = False
    futures_entry_match: bool = False
    spot_exit_match: bool = False
    futures_exit_match: bool = False
    discrepancies: List[str] = field(default_factory=list)
    mt5_spot_entry_price: float = 0.0
    mt5_futures_entry_price: float = 0.0
    mt5_spot_exit_price: float = 0.0
    mt5_futures_exit_price: float = 0.0
    mt5_spot_volume: float = 0.0
    mt5_futures_volume: float = 0.0


class ConfigLoader:
    """Loads trading configuration from database"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def load_config(self) -> TradingConfig:
        """Load configuration from trading_config table"""
        config = TradingConfig()

        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM trading_config WHERE id = 1')
            row = cursor.fetchone()
            conn.close()

            if row:
                # Map row to config (indices based on schema)
                config.entry_std_dev = float(row[15]) if len(row) > 15 and row[15] else 3.5
                config.exit_std_dev = float(row[16]) if len(row) > 16 and row[16] else 2.0
                config.stop_loss_std_dev = float(row[17]) if len(row) > 17 and row[17] else 6.0
                config.time_stop_loss_days = int(row[18]) if len(row) > 18 and row[18] else 0
                config.max_positions = int(row[19]) if len(row) > 19 and row[19] else 1
                config.lot_size = float(row[20]) if len(row) > 20 and row[20] else 20.0
                config.commission_per_lot = float(row[23]) if len(row) > 23 and row[23] else 0
                config.hurst_threshold = float(row[24]) if len(row) > 24 and row[24] else 0.5
                config.trending_duration_minutes = int(row[25]) if len(row) > 25 and row[25] else 15
                config.hurst_enabled = bool(row[26]) if len(row) > 26 else True
                config.close_before_overnight = bool(row[27]) if len(row) > 27 else False
                config.overnight_close_hour = int(row[28]) if len(row) > 28 and row[28] else 16
                config.overnight_close_minute = int(row[29]) if len(row) > 29 and row[29] else 55
                config.min_profit_per_lot = float(row[31]) if len(row) > 31 and row[31] else 50
                config.max_loss_per_lot = float(row[32]) if len(row) > 32 and row[32] else 100
                config.spot_symbol = row[34] if len(row) > 34 and row[34] else ''
                config.futures_symbol = row[35] if len(row) > 35 and row[35] else ''
                config.contract_size = float(row[37]) if len(row) > 37 and row[37] else 100
                config.order_type = row[39] if len(row) > 39 and row[39] else 'MARKET'
                config.limit_order_timeout = int(row[40]) if len(row) > 40 and row[40] else 60

                # New settings
                if len(row) > 43:
                    config.no_entry_after_enabled = bool(row[43]) if row[43] else False
                if len(row) > 44:
                    config.no_entry_after_hour = int(row[44]) if row[44] else 16
                if len(row) > 45:
                    config.no_entry_after_minute = int(row[45]) if row[45] else 30

        except Exception as e:
            print(f"Warning: Could not load config from database: {e}")
            print("Using default configuration values.")

        # Get server timezone
        try:
            if os.path.exists('/etc/timezone'):
                with open('/etc/timezone', 'r') as f:
                    config.server_timezone = f.read().strip()
        except:
            config.server_timezone = 'UTC'

        return config


class MT5Verifier:
    """Verifies MT5 order execution matches Trade Journal"""

    def __init__(self):
        self.connected = False
        if HAS_MT5:
            self.connected = mt5.initialize()

    def verify_trade(self, trade: TradeData, config: TradingConfig) -> MT5Verification:
        """Verify MT5 execution matches journal entry"""
        result = MT5Verification()

        if not HAS_MT5 or not self.connected:
            result.discrepancies.append("MT5 not connected - cannot verify")
            return result

        try:
            # Get deals from MT5 history for the trade timeframe
            from_time = trade.entry_time - timedelta(minutes=5)
            to_time = trade.exit_time + timedelta(minutes=5)

            deals = mt5.history_deals_get(from_time, to_time)

            if deals is None or len(deals) == 0:
                result.discrepancies.append(f"No MT5 deals found between {from_time} and {to_time}")
                return result

            # Find matching deals by symbol and approximate time
            spot_symbol = config.spot_symbol
            futures_symbol = config.futures_symbol

            spot_entry_deals = []
            spot_exit_deals = []
            futures_entry_deals = []
            futures_exit_deals = []

            for deal in deals:
                deal_time = datetime.fromtimestamp(deal.time)

                # Entry deals (within 2 minutes of entry time)
                if abs((deal_time - trade.entry_time).total_seconds()) < 120:
                    if deal.symbol == spot_symbol:
                        spot_entry_deals.append(deal)
                    elif deal.symbol == futures_symbol:
                        futures_entry_deals.append(deal)

                # Exit deals (within 2 minutes of exit time)
                if abs((deal_time - trade.exit_time).total_seconds()) < 120:
                    if deal.symbol == spot_symbol:
                        spot_exit_deals.append(deal)
                    elif deal.symbol == futures_symbol:
                        futures_exit_deals.append(deal)

            # Verify spot entry
            if spot_entry_deals:
                deal = spot_entry_deals[0]
                result.mt5_spot_entry_price = deal.price
                result.mt5_spot_volume = deal.volume

                if trade.entry_spot_price > 0:
                    price_diff = abs(deal.price - trade.entry_spot_price)
                    if price_diff < 1.0:  # Within $1
                        result.spot_entry_match = True
                    else:
                        result.discrepancies.append(
                            f"Spot entry price mismatch: Journal=${trade.entry_spot_price:.3f}, MT5=${deal.price:.3f} (diff=${price_diff:.3f})"
                        )

                if abs(deal.volume - trade.lot_size) > 0.01:
                    result.discrepancies.append(
                        f"Spot entry volume mismatch: Journal={trade.lot_size}, MT5={deal.volume}"
                    )
            else:
                result.discrepancies.append(f"No spot entry deal found near {trade.entry_time}")

            # Verify futures entry
            if futures_entry_deals:
                deal = futures_entry_deals[0]
                result.mt5_futures_entry_price = deal.price
                result.mt5_futures_volume = deal.volume

                if trade.entry_futures_price > 0:
                    price_diff = abs(deal.price - trade.entry_futures_price)
                    if price_diff < 1.0:
                        result.futures_entry_match = True
                    else:
                        result.discrepancies.append(
                            f"Futures entry price mismatch: Journal=${trade.entry_futures_price:.3f}, MT5=${deal.price:.3f} (diff=${price_diff:.3f})"
                        )
            else:
                result.discrepancies.append(f"No futures entry deal found near {trade.entry_time}")

            # Verify spot exit
            if spot_exit_deals:
                deal = spot_exit_deals[0]
                result.mt5_spot_exit_price = deal.price

                if trade.exit_spot_price > 0:
                    price_diff = abs(deal.price - trade.exit_spot_price)
                    if price_diff < 1.0:
                        result.spot_exit_match = True
                    else:
                        result.discrepancies.append(
                            f"Spot exit price mismatch: Journal=${trade.exit_spot_price:.3f}, MT5=${deal.price:.3f} (diff=${price_diff:.3f})"
                        )
            else:
                result.discrepancies.append(f"No spot exit deal found near {trade.exit_time}")

            # Verify futures exit
            if futures_exit_deals:
                deal = futures_exit_deals[0]
                result.mt5_futures_exit_price = deal.price

                if trade.exit_futures_price > 0:
                    price_diff = abs(deal.price - trade.exit_futures_price)
                    if price_diff < 1.0:
                        result.futures_exit_match = True
                    else:
                        result.discrepancies.append(
                            f"Futures exit price mismatch: Journal=${trade.exit_futures_price:.3f}, MT5=${deal.price:.3f} (diff=${price_diff:.3f})"
                        )
            else:
                result.discrepancies.append(f"No futures exit deal found near {trade.exit_time}")

            # Overall verification
            result.verified = (
                result.spot_entry_match and
                result.futures_entry_match and
                result.spot_exit_match and
                result.futures_exit_match
            )

        except Exception as e:
            result.discrepancies.append(f"MT5 verification error: {str(e)}")

        return result


class TradeAnalyzer:
    """Enhanced trade analyzer with config awareness and MT5 verification"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.config_loader = ConfigLoader(db_path)
        self.config = self.config_loader.load_config()
        self.mt5_verifier = MT5Verifier() if HAS_MT5 else None
        self.anthropic_client = None

        if HAS_ANTHROPIC and os.environ.get('ANTHROPIC_API_KEY'):
            self.anthropic_client = anthropic.Anthropic()

        print(f"\n📋 Loaded Settings Configuration:")
        print(f"   Entry Threshold: ±{self.config.entry_std_dev}σ")
        print(f"   Exit Threshold: ±{self.config.exit_std_dev}σ")
        print(f"   Stop Loss: ±{self.config.stop_loss_std_dev}σ")
        print(f"   Max Loss/Lot: ${self.config.max_loss_per_lot}")
        print(f"   Lot Size: {self.config.lot_size}")
        print(f"   Overnight Close: {'Enabled' if self.config.close_before_overnight else 'Disabled'} at {self.config.overnight_close_hour:02d}:{self.config.overnight_close_minute:02d}")
        print(f"   No Entry After: {'Enabled' if self.config.no_entry_after_enabled else 'Disabled'} at {self.config.no_entry_after_hour:02d}:{self.config.no_entry_after_minute:02d}")
        print(f"   Server Timezone: {self.config.server_timezone}")
        print()

    def get_trade(self, trade_number: int) -> Optional[TradeData]:
        """Fetch trade from database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get column names
        cursor.execute('PRAGMA table_info(trade_journal)')
        columns = [col[1] for col in cursor.fetchall()]

        cursor.execute('SELECT * FROM trade_journal WHERE id = ? OR rowid = ? ORDER BY id DESC LIMIT 1',
                      (trade_number, trade_number))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        # Create column mapping
        col_map = {col: idx for idx, col in enumerate(columns)}

        trade = TradeData()
        trade.trade_number = trade_number

        # Map columns to trade data (handle different schema versions)
        if 'trade_id' in col_map:
            trade.trade_id = row[col_map['trade_id']] or ''
        if 'direction' in col_map:
            trade.direction = row[col_map['direction']] or ''
        if 'lot_size' in col_map:
            trade.lot_size = float(row[col_map['lot_size']] or 0)
        if 'entry_time' in col_map:
            trade.entry_time = self._parse_datetime(row[col_map['entry_time']])
        if 'exit_time' in col_map:
            trade.exit_time = self._parse_datetime(row[col_map['exit_time']])
        if 'entry_zscore' in col_map:
            trade.entry_zscore = float(row[col_map['entry_zscore']] or 0)
        if 'exit_zscore' in col_map:
            trade.exit_zscore = float(row[col_map['exit_zscore']] or 0)
        if 'entry_spread' in col_map:
            trade.entry_spread = float(row[col_map['entry_spread']] or 0)
        if 'exit_spread' in col_map:
            trade.exit_spread = float(row[col_map['exit_spread']] or 0)
        if 'entry_spot_price' in col_map:
            trade.entry_spot_price = float(row[col_map['entry_spot_price']] or 0)
        if 'entry_futures_price' in col_map:
            trade.entry_futures_price = float(row[col_map['entry_futures_price']] or 0)
        if 'exit_spot_price' in col_map:
            trade.exit_spot_price = float(row[col_map['exit_spot_price']] or 0)
        if 'exit_futures_price' in col_map:
            trade.exit_futures_price = float(row[col_map['exit_futures_price']] or 0)
        if 'gross_pnl' in col_map:
            trade.gross_pnl = float(row[col_map['gross_pnl']] or 0)
        if 'spread_cost' in col_map:
            trade.spread_cost = float(row[col_map['spread_cost']] or 0)
        if 'swap_cost' in col_map:
            trade.swap_cost = float(row[col_map['swap_cost']] or 0)
        if 'commission' in col_map:
            trade.commission = float(row[col_map['commission']] or 0)
        if 'net_pnl' in col_map:
            trade.net_pnl = float(row[col_map['net_pnl']] or 0)
        if 'close_reason' in col_map:
            trade.close_reason = row[col_map['close_reason']] or ''

        # Calculate duration
        trade.duration_seconds = int((trade.exit_time - trade.entry_time).total_seconds())

        return trade

    def _parse_datetime(self, dt_str) -> datetime:
        """Parse datetime string"""
        if isinstance(dt_str, datetime):
            return dt_str
        if not dt_str:
            return datetime.now()
        try:
            return datetime.strptime(str(dt_str), '%Y-%m-%d %H:%M:%S')
        except:
            try:
                return datetime.strptime(str(dt_str), '%Y-%m-%d %H:%M')
            except:
                return datetime.now()

    def get_recent_trades(self, limit: int = 50) -> List[TradeData]:
        """Fetch recent trades"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('SELECT rowid FROM trade_journal ORDER BY rowid DESC LIMIT ?', (limit,))
        rows = cursor.fetchall()
        conn.close()

        trades = []
        for row in rows:
            trade = self.get_trade(row[0])
            if trade:
                trades.append(trade)

        return trades

    def analyze_trade(self, trade: TradeData) -> Dict[str, Any]:
        """Comprehensive trade analysis using actual settings"""

        analysis = {
            'trade_number': trade.trade_number,
            'is_winner': trade.net_pnl > 0,
            'net_pnl': trade.net_pnl,
            'gross_pnl': trade.gross_pnl,
            'issues': [],
            'root_cause': None,
            'recommendations': [],
            'config_used': {},
            'mt5_verification': None
        }

        # Store config values used for analysis
        analysis['config_used'] = {
            'entry_std': self.config.entry_std_dev,
            'exit_std': self.config.exit_std_dev,
            'stop_loss_std': self.config.stop_loss_std_dev,
            'max_loss_per_lot': self.config.max_loss_per_lot,
            'lot_size': self.config.lot_size,
            'overnight_close_enabled': self.config.close_before_overnight,
            'overnight_close_time': f"{self.config.overnight_close_hour:02d}:{self.config.overnight_close_minute:02d}",
            'no_entry_after_enabled': self.config.no_entry_after_enabled,
            'no_entry_after_time': f"{self.config.no_entry_after_hour:02d}:{self.config.no_entry_after_minute:02d}",
            'server_timezone': self.config.server_timezone
        }

        # === MT5 VERIFICATION ===
        if self.mt5_verifier:
            mt5_result = self.mt5_verifier.verify_trade(trade, self.config)
            analysis['mt5_verification'] = {
                'verified': mt5_result.verified,
                'discrepancies': mt5_result.discrepancies,
                'spot_entry_match': mt5_result.spot_entry_match,
                'futures_entry_match': mt5_result.futures_entry_match,
                'spot_exit_match': mt5_result.spot_exit_match,
                'futures_exit_match': mt5_result.futures_exit_match
            }

            if mt5_result.discrepancies:
                for disc in mt5_result.discrepancies:
                    analysis['issues'].append({
                        'type': 'MT5_MISMATCH',
                        'severity': 'HIGH',
                        'message': disc
                    })

        # === TIMING ANALYSIS ===
        entry_hour = trade.entry_time.hour
        entry_minute = trade.entry_time.minute

        # Check if entered after no_entry_after time
        if self.config.no_entry_after_enabled:
            no_entry_minutes = self.config.no_entry_after_hour * 60 + self.config.no_entry_after_minute
            entry_minutes = entry_hour * 60 + entry_minute

            if entry_minutes >= no_entry_minutes:
                analysis['issues'].append({
                    'type': 'LATE_ENTRY',
                    'severity': 'CRITICAL',
                    'message': f'Trade entered at {entry_hour:02d}:{entry_minute:02d} but no_entry_after is set to {self.config.no_entry_after_hour:02d}:{self.config.no_entry_after_minute:02d}. This trade should NOT have been taken!'
                })

        # Check if closed by overnight close
        if self.config.close_before_overnight:
            overnight_minutes = self.config.overnight_close_hour * 60 + self.config.overnight_close_minute
            exit_hour = trade.exit_time.hour
            exit_minute = trade.exit_time.minute
            exit_minutes = exit_hour * 60 + exit_minute

            # If exit time is at or just after overnight close time
            if abs(exit_minutes - overnight_minutes) < 5:  # Within 5 minutes
                analysis['issues'].append({
                    'type': 'OVERNIGHT_CLOSE_EXIT',
                    'severity': 'HIGH',
                    'message': f'Trade closed at {exit_hour:02d}:{exit_minute:02d} by OVERNIGHT_CLOSE (set to {self.config.overnight_close_hour:02d}:{self.config.overnight_close_minute:02d}). Trade entered too late to reach exit target.'
                })

            # If entry was after overnight close time (shouldn't happen but check)
            entry_minutes = entry_hour * 60 + entry_minute
            if entry_minutes >= overnight_minutes:
                analysis['issues'].append({
                    'type': 'ENTRY_AFTER_OVERNIGHT',
                    'severity': 'CRITICAL',
                    'message': f'Trade entered at {entry_hour:02d}:{entry_minute:02d} AFTER overnight close time ({self.config.overnight_close_hour:02d}:{self.config.overnight_close_minute:02d}). Trade was immediately closed!'
                })

        # === EXIT ANALYSIS ===
        exit_std = self.config.exit_std_dev
        stop_loss_std = self.config.stop_loss_std_dev

        # Expected exit z-score
        if trade.direction == 'Long Spread':
            expected_exit_z = exit_std  # Should exit at +2.0
            stop_loss_z = -stop_loss_std  # Stop at -6.0
        else:
            expected_exit_z = -exit_std  # Should exit at -2.0
            stop_loss_z = stop_loss_std  # Stop at +6.0

        reached_exit_target = False
        if trade.direction == 'Long Spread':
            reached_exit_target = trade.exit_zscore >= exit_std
        else:
            reached_exit_target = trade.exit_zscore <= -exit_std

        hit_stop_loss = False
        if trade.direction == 'Long Spread':
            hit_stop_loss = trade.exit_zscore <= -stop_loss_std
        else:
            hit_stop_loss = trade.exit_zscore >= stop_loss_std

        analysis['exit_analysis'] = {
            'entry_zscore': trade.entry_zscore,
            'exit_zscore': trade.exit_zscore,
            'expected_exit_zscore': expected_exit_z,
            'reached_exit_target': reached_exit_target,
            'hit_stop_loss': hit_stop_loss,
            'close_reason': trade.close_reason,
            'duration_seconds': trade.duration_seconds,
            'duration_formatted': self._format_duration(trade.duration_seconds)
        }

        # Check for premature exit
        if not reached_exit_target and not hit_stop_loss:
            # Trade exited before reaching either target or stop loss
            # What caused this?

            if 'OVERNIGHT' in trade.close_reason.upper() or 'overnight' in trade.close_reason.lower():
                analysis['issues'].append({
                    'type': 'OVERNIGHT_EXIT',
                    'severity': 'HIGH',
                    'message': f'Trade closed by overnight protection before reaching exit target (Z={trade.exit_zscore:.2f}, target={expected_exit_z:.1f})'
                })
            elif 'MAX_LOSS' in trade.close_reason.upper() or 'max_loss' in trade.close_reason.lower():
                max_loss_total = self.config.max_loss_per_lot * trade.lot_size
                analysis['issues'].append({
                    'type': 'MAX_LOSS_EXIT',
                    'severity': 'HIGH',
                    'message': f'Trade closed by MAX_LOSS (${self.config.max_loss_per_lot}/lot × {trade.lot_size} lots = ${max_loss_total:.0f} limit)'
                })
            elif trade.duration_seconds < 120:  # Less than 2 minutes
                # Very quick exit - likely timing issue or max_loss
                analysis['issues'].append({
                    'type': 'IMMEDIATE_EXIT',
                    'severity': 'CRITICAL',
                    'message': f'Trade exited in {trade.duration_seconds}s - likely OVERNIGHT_CLOSE triggered immediately after entry'
                })

        # === COST ANALYSIS ===
        total_costs = trade.spread_cost + trade.swap_cost + trade.commission
        analysis['cost_analysis'] = {
            'spread_cost': trade.spread_cost,
            'swap_cost': trade.swap_cost,
            'commission': trade.commission,
            'total_costs': total_costs,
            'cost_per_lot': total_costs / trade.lot_size if trade.lot_size > 0 else 0,
            'breakeven_gross': total_costs
        }

        if total_costs > 500:
            analysis['issues'].append({
                'type': 'HIGH_COSTS',
                'severity': 'MEDIUM',
                'message': f'High transaction costs: ${total_costs:.2f}. Need ${total_costs:.0f}+ gross profit to break even.'
            })

        if trade.gross_pnl > 0 and trade.net_pnl < 0:
            analysis['issues'].append({
                'type': 'COSTS_ATE_PROFIT',
                'severity': 'HIGH',
                'message': f'Trade was profitable (${trade.gross_pnl:.2f} gross) but costs (${total_costs:.2f}) resulted in net loss.'
            })

        # === DETERMINE ROOT CAUSE ===
        analysis['root_cause'] = self._determine_root_cause(analysis, trade)

        # === GENERATE RECOMMENDATIONS ===
        analysis['recommendations'] = self._generate_recommendations(analysis, trade)

        return analysis

    def _determine_root_cause(self, analysis: Dict, trade: TradeData) -> Dict:
        """Determine the PRIMARY root cause based on issues and settings"""

        if analysis['is_winner']:
            return {
                'cause': 'WINNING_TRADE',
                'description': 'Trade reached profit target successfully',
                'severity': 'SUCCESS'
            }

        issues = analysis['issues']

        # Priority 1: Timing issues (entered too late / overnight close)
        timing_issues = [i for i in issues if i['type'] in ['LATE_ENTRY', 'ENTRY_AFTER_OVERNIGHT', 'OVERNIGHT_CLOSE_EXIT', 'OVERNIGHT_EXIT', 'IMMEDIATE_EXIT']]
        if timing_issues:
            most_severe = timing_issues[0]

            if most_severe['type'] in ['ENTRY_AFTER_OVERNIGHT', 'OVERNIGHT_CLOSE_EXIT', 'IMMEDIATE_EXIT']:
                return {
                    'cause': 'TIMING_ISSUE',
                    'description': f'Trade entered too late and was closed by OVERNIGHT_CLOSE. Server time is {self.config.server_timezone}, overnight close at {self.config.overnight_close_hour:02d}:{self.config.overnight_close_minute:02d}.',
                    'severity': 'CRITICAL',
                    'fix': f'Either disable OVERNIGHT_CLOSE or set NO_ENTRY_AFTER to prevent trades entering after {self.config.overnight_close_hour-1:02d}:00'
                }
            elif most_severe['type'] == 'LATE_ENTRY':
                return {
                    'cause': 'LATE_ENTRY',
                    'description': 'Trade entered after no_entry_after time - should not have been taken',
                    'severity': 'CRITICAL',
                    'fix': 'Check why trade was allowed to enter after no_entry_after setting'
                }

        # Priority 2: MAX_LOSS exit
        max_loss_issues = [i for i in issues if i['type'] == 'MAX_LOSS_EXIT']
        if max_loss_issues:
            max_loss_total = self.config.max_loss_per_lot * trade.lot_size
            return {
                'cause': 'MAX_LOSS_TOO_TIGHT',
                'description': f'MAX_LOSS (${max_loss_total:.0f}) triggered before reaching exit target. With {trade.lot_size} lots, you need more room for spread to move.',
                'severity': 'HIGH',
                'fix': f'Increase max_loss_per_lot from ${self.config.max_loss_per_lot:.0f} to ${self.config.max_loss_per_lot * 2:.0f}-${self.config.max_loss_per_lot * 3:.0f}, OR reduce lot size'
            }

        # Priority 3: Cost issues
        cost_issues = [i for i in issues if i['type'] in ['HIGH_COSTS', 'COSTS_ATE_PROFIT']]
        if cost_issues:
            return {
                'cause': 'EXCESSIVE_COSTS',
                'description': f'Transaction costs (${analysis["cost_analysis"]["total_costs"]:.2f}) exceed profit potential',
                'severity': 'HIGH',
                'fix': 'Reduce lot size or use LIMIT orders to reduce spread costs'
            }

        # Priority 4: MT5 mismatch
        mt5_issues = [i for i in issues if i['type'] == 'MT5_MISMATCH']
        if mt5_issues:
            return {
                'cause': 'MT5_EXECUTION_ISSUE',
                'description': 'MT5 execution prices do not match Trade Journal entries',
                'severity': 'HIGH',
                'fix': 'Check MT5 order fills and slippage. Consider using LIMIT orders.'
            }

        # Default: Market movement
        return {
            'cause': 'ADVERSE_MARKET_MOVEMENT',
            'description': 'Trade lost due to unfavorable market conditions',
            'severity': 'MEDIUM',
            'fix': 'Review entry timing and consider tighter entry thresholds'
        }

    def _generate_recommendations(self, analysis: Dict, trade: TradeData) -> List[str]:
        """Generate actionable recommendations based on analysis"""
        recommendations = []
        root_cause = analysis['root_cause']['cause']

        if root_cause == 'TIMING_ISSUE':
            recommendations.append(
                f"CRITICAL: Your server is in {self.config.server_timezone}. "
                f"Overnight close is set to {self.config.overnight_close_hour:02d}:{self.config.overnight_close_minute:02d} {self.config.server_timezone}."
            )
            if self.config.server_timezone in ['UTC', 'Etc/UTC']:
                recommendations.append(
                    "For 4:55 PM EST overnight close, set to 21:55 (not 16:55) since server is UTC."
                )
            recommendations.append(
                f"Enable NO_ENTRY_AFTER and set to {self.config.overnight_close_hour - 1:02d}:00 to prevent late entries."
            )

        elif root_cause == 'MAX_LOSS_TOO_TIGHT':
            max_loss_total = self.config.max_loss_per_lot * trade.lot_size
            recommended_max_loss = self.config.max_loss_per_lot * 2.5
            recommendations.append(
                f"Increase max_loss_per_lot from ${self.config.max_loss_per_lot:.0f} to ${recommended_max_loss:.0f}"
            )
            recommendations.append(
                f"This gives total max loss of ${recommended_max_loss * trade.lot_size:.0f} instead of ${max_loss_total:.0f}"
            )
            recommendations.append(
                f"Alternative: Reduce lot size from {trade.lot_size} to {trade.lot_size / 2:.0f}"
            )

        elif root_cause == 'EXCESSIVE_COSTS':
            recommendations.append(
                f"Consider reducing lot size from {trade.lot_size} to {trade.lot_size / 2:.0f} to halve costs"
            )
            if self.config.order_type == 'MARKET':
                recommendations.append(
                    "Switch to LIMIT orders to avoid paying bid-ask spread"
                )

        elif root_cause == 'MT5_EXECUTION_ISSUE':
            recommendations.append("Review MT5 order execution and check for slippage")
            recommendations.append("Consider using LIMIT orders for better fill prices")

        return recommendations

    def _format_duration(self, seconds: int) -> str:
        """Format duration in human-readable format"""
        if seconds < 60:
            return f'{seconds}s'
        elif seconds < 3600:
            return f'{seconds // 60}m {seconds % 60}s'
        else:
            hours = seconds // 3600
            mins = (seconds % 3600) // 60
            return f'{hours}h {mins}m'

    def generate_report(self, analysis: Dict) -> str:
        """Generate detailed analysis report"""
        trade = analysis

        status = '✅ WINNING' if trade['is_winner'] else '❌ LOSING'

        report = f"""
═══════════════════════════════════════════════════════════════════════════════════
📊 TRADE ANALYSIS REPORT - Trade #{trade['trade_number']}
═══════════════════════════════════════════════════════════════════════════════════

SUMMARY: {status} TRADE | Gross: ${trade['gross_pnl']:.2f} | Net: ${trade['net_pnl']:.2f}

┌─────────────────────────────────────────────────────────────────────────────────┐
│ YOUR CURRENT SETTINGS (from Settings page)                                      │
├─────────────────────────────────────────────────────────────────────────────────┤
│ Entry: ±{trade['config_used']['entry_std']}σ | Exit: ±{trade['config_used']['exit_std']}σ | Stop: ±{trade['config_used']['stop_loss_std']}σ     │
│ Max Loss/Lot: ${trade['config_used']['max_loss_per_lot']}  |  Lot Size: {trade['config_used']['lot_size']}                           │
│ Overnight Close: {trade['config_used']['overnight_close_enabled']} at {trade['config_used']['overnight_close_time']}                             │
│ No Entry After: {trade['config_used']['no_entry_after_enabled']} at {trade['config_used']['no_entry_after_time']}                               │
│ Server Timezone: {trade['config_used']['server_timezone']}                                               │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│ EXIT ANALYSIS                                                                   │
├─────────────────────────────────────────────────────────────────────────────────┤
│ Entry Z: {trade['exit_analysis']['entry_zscore']:+.2f}  →  Exit Z: {trade['exit_analysis']['exit_zscore']:+.2f}  (Target: {trade['exit_analysis']['expected_exit_zscore']:+.1f})             │
│ Reached Target: {'YES ✓' if trade['exit_analysis']['reached_exit_target'] else 'NO ✗'}                                                        │
│ Hit Stop Loss: {'YES' if trade['exit_analysis']['hit_stop_loss'] else 'NO'}                                                          │
│ Close Reason: {trade['exit_analysis']['close_reason'][:40]}                             │
│ Duration: {trade['exit_analysis']['duration_formatted']}                                                        │
└─────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────┐
│ COST BREAKDOWN                                                                  │
├─────────────────────────────────────────────────────────────────────────────────┤
│ Spread Cost:    ${trade['cost_analysis']['spread_cost']:>10.2f}                                            │
│ Swap Cost:      ${trade['cost_analysis']['swap_cost']:>10.2f}                                            │
│ Commission:     ${trade['cost_analysis']['commission']:>10.2f}                                            │
│ ─────────────────────────────                                                   │
│ TOTAL COSTS:    ${trade['cost_analysis']['total_costs']:>10.2f}                                            │
│ Breakeven:      ${trade['cost_analysis']['breakeven_gross']:>10.2f} gross profit needed                       │
└─────────────────────────────────────────────────────────────────────────────────┘
"""

        # MT5 Verification section
        if trade.get('mt5_verification'):
            mt5 = trade['mt5_verification']
            report += f"""
┌─────────────────────────────────────────────────────────────────────────────────┐
│ MT5 VERIFICATION                                                                │
├─────────────────────────────────────────────────────────────────────────────────┤
│ Overall: {'✅ VERIFIED' if mt5['verified'] else '❌ DISCREPANCIES FOUND'}                                              │
│ Spot Entry:    {'✓ Match' if mt5['spot_entry_match'] else '✗ Mismatch'}                                               │
│ Futures Entry: {'✓ Match' if mt5['futures_entry_match'] else '✗ Mismatch'}                                               │
│ Spot Exit:     {'✓ Match' if mt5['spot_exit_match'] else '✗ Mismatch'}                                               │
│ Futures Exit:  {'✓ Match' if mt5['futures_exit_match'] else '✗ Mismatch'}                                               │
"""
            if mt5['discrepancies']:
                report += "│ Discrepancies:                                                                  │\n"
                for disc in mt5['discrepancies'][:3]:
                    report += f"│   • {disc[:70]:<70} │\n"
            report += "└─────────────────────────────────────────────────────────────────────────────────┘\n"

        # Root cause section
        root = trade['root_cause']
        report += f"""
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 🔍 ROOT CAUSE ANALYSIS                                                          │
├─────────────────────────────────────────────────────────────────────────────────┤
│ CAUSE: {root['cause']:<30} Severity: {root['severity']:<10}             │
│                                                                                 │
│ {root['description'][:75]:<75} │
"""
        if root.get('fix'):
            report += f"│                                                                                 │\n"
            report += f"│ FIX: {root['fix'][:72]:<72} │\n"
        report += "└─────────────────────────────────────────────────────────────────────────────────┘\n"

        # Issues section
        if trade['issues']:
            report += f"""
┌─────────────────────────────────────────────────────────────────────────────────┐
│ ⚠ ISSUES IDENTIFIED ({len(trade['issues'])} total)                                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
"""
            for issue in trade['issues'][:5]:
                severity_icon = {'CRITICAL': '🔴', 'HIGH': '🟠', 'MEDIUM': '🟡', 'LOW': '🟢'}.get(issue['severity'], '⚪')
                report += f"│ {severity_icon} [{issue['severity']:<8}] {issue['type']:<25}                     │\n"
                # Wrap long messages
                msg = issue['message']
                if len(msg) > 70:
                    report += f"│   {msg[:70]:<70} │\n"
                    report += f"│   {msg[70:140]:<70} │\n"
                else:
                    report += f"│   {msg:<70} │\n"
            report += "└─────────────────────────────────────────────────────────────────────────────────┘\n"

        # Recommendations section
        if trade['recommendations']:
            report += f"""
┌─────────────────────────────────────────────────────────────────────────────────┐
│ 💡 RECOMMENDATIONS                                                              │
├─────────────────────────────────────────────────────────────────────────────────┤
"""
            for i, rec in enumerate(trade['recommendations'][:5], 1):
                # Wrap long recommendations
                if len(rec) > 70:
                    report += f"│ {i}. {rec[:70]:<70} │\n"
                    report += f"│    {rec[70:140]:<70} │\n"
                else:
                    report += f"│ {i}. {rec:<70} │\n"
            report += "└─────────────────────────────────────────────────────────────────────────────────┘\n"

        report += "═══════════════════════════════════════════════════════════════════════════════════\n"

        return report


class TradeMonitor:
    """Monitors database for new trades and triggers analysis"""

    def __init__(self, analyzer: TradeAnalyzer, poll_interval: int = 10):
        self.analyzer = analyzer
        self.poll_interval = poll_interval
        self.last_trade_id = 0

    def watch(self):
        """Watch for new trades and analyze them"""
        print("🔍 Trade Monitor Started - Watching for new trades...")
        print(f"   Poll interval: {self.poll_interval}s")
        print("   Press Ctrl+C to stop\n")

        # Get current last trade ID
        trades = self.analyzer.get_recent_trades(1)
        if trades:
            self.last_trade_id = trades[0].trade_number
            print(f"   Starting from trade #{self.last_trade_id}\n")

        while True:
            try:
                trades = self.analyzer.get_recent_trades(5)

                for trade in reversed(trades):
                    if trade.trade_number > self.last_trade_id:
                        print(f"\n🆕 NEW TRADE DETECTED: #{trade.trade_number}")

                        analysis = self.analyzer.analyze_trade(trade)
                        report = self.analyzer.generate_report(analysis)
                        print(report)

                        self.last_trade_id = trade.trade_number

                time.sleep(self.poll_interval)

            except KeyboardInterrupt:
                print("\n\n👋 Trade Monitor Stopped")
                break
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(self.poll_interval)


def main():
    parser = argparse.ArgumentParser(description='AI Trade Analyst - Enhanced')
    parser.add_argument('--watch', action='store_true', help='Watch for new trades')
    parser.add_argument('--analyze', type=int, help='Analyze specific trade number')
    parser.add_argument('--report', action='store_true', help='Generate report for recent trades')
    parser.add_argument('--verify', type=int, help='Verify MT5 execution for trade')
    parser.add_argument('--db', default='../basis_trading.db', help='Database path')

    args = parser.parse_args()

    # Find database
    db_path = args.db
    if not os.path.exists(db_path):
        # Try parent directory
        parent_db = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'basis_trading.db')
        if os.path.exists(parent_db):
            db_path = parent_db
        else:
            print(f"Database not found: {db_path}")
            print("Trying current directory...")
            if os.path.exists('basis_trading.db'):
                db_path = 'basis_trading.db'
            else:
                sys.exit(1)

    print(f"📂 Using database: {db_path}")

    analyzer = TradeAnalyzer(db_path)

    if args.watch:
        monitor = TradeMonitor(analyzer)
        monitor.watch()

    elif args.analyze:
        trade = analyzer.get_trade(args.analyze)
        if trade:
            analysis = analyzer.analyze_trade(trade)
            report = analyzer.generate_report(analysis)
            print(report)
        else:
            print(f"Trade #{args.analyze} not found")

    elif args.verify:
        trade = analyzer.get_trade(args.verify)
        if trade:
            print(f"\n🔍 Verifying MT5 execution for Trade #{args.verify}...")
            if analyzer.mt5_verifier:
                result = analyzer.mt5_verifier.verify_trade(trade, analyzer.config)
                print(f"\nVerification Result: {'✅ PASSED' if result.verified else '❌ FAILED'}")
                if result.discrepancies:
                    print("\nDiscrepancies found:")
                    for disc in result.discrepancies:
                        print(f"  • {disc}")
            else:
                print("MT5 not available for verification")
        else:
            print(f"Trade #{args.verify} not found")

    elif args.report:
        trades = analyzer.get_recent_trades(10)

        print("\n" + "="*80)
        print("📊 RECENT TRADES SUMMARY")
        print("="*80)

        if not trades:
            print("No trades found")
            return

        winners = 0
        total_pnl = 0

        for trade in trades:
            analysis = analyzer.analyze_trade(trade)
            if analysis['is_winner']:
                winners += 1
            total_pnl += trade.net_pnl

        win_rate = winners / len(trades) * 100 if trades else 0

        print(f"\nTotal Trades: {len(trades)}")
        print(f"Winners: {winners} ({win_rate:.1f}%)")
        print(f"Losers: {len(trades) - winners}")
        print(f"Net P&L: ${total_pnl:.2f}")
        print()

        # Analyze each
        for trade in trades[:5]:
            analysis = analyzer.analyze_trade(trade)
            status = '✅' if analysis['is_winner'] else '❌'
            root = analysis['root_cause']['cause']
            print(f"{status} Trade #{trade.trade_number}: ${trade.net_pnl:>8.2f} | {root}")

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
