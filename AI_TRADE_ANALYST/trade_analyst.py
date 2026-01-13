#!/usr/bin/env python3
"""
AI Trade Analyst Agent
Monitors trade journal and provides real-time analysis of wins and losses.

Usage:
    python trade_analyst.py --watch           # Watch for new trades in real-time
    python trade_analyst.py --analyze 103     # Analyze specific trade
    python trade_analyst.py --report          # Generate daily report
"""

import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
import time

# Add parent directory for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False
    print("Warning: anthropic package not installed. Using local analysis only.")


@dataclass
class TradeData:
    """Structured trade data"""
    trade_id: str
    trade_number: int
    direction: str
    lot_size: float
    entry_time: datetime
    exit_time: datetime
    entry_zscore: float
    exit_zscore: float
    entry_spread: float
    exit_spread: float
    entry_spot_price: float
    entry_futures_price: float
    exit_spot_price: float
    exit_futures_price: float
    gross_pnl: float
    spread_cost: float
    swap_cost: float
    commission: float
    net_pnl: float
    close_reason: str
    duration_seconds: int


class CalendarEvents:
    """Economic calendar events that impact gold trading"""

    HIGH_IMPACT_EVENTS = {
        'NFP': {'description': 'Non-Farm Payrolls', 'typical_impact': 30, 'buffer_hours': 2},
        'FOMC': {'description': 'Fed Rate Decision', 'typical_impact': 50, 'buffer_hours': 4},
        'CPI': {'description': 'Consumer Price Index', 'typical_impact': 25, 'buffer_hours': 1},
        'GDP': {'description': 'Gross Domestic Product', 'typical_impact': 15, 'buffer_hours': 1},
        'ECB': {'description': 'ECB Rate Decision', 'typical_impact': 20, 'buffer_hours': 1},
    }

    # 2026 FOMC meeting dates (approximate - user should verify)
    FOMC_DATES_2026 = [
        '2026-01-28', '2026-03-18', '2026-05-06', '2026-06-17',
        '2026-07-29', '2026-09-16', '2026-11-04', '2026-12-16'
    ]

    @classmethod
    def get_session(cls, utc_hour: int) -> str:
        """Determine trading session based on UTC hour"""
        if 0 <= utc_hour < 8:
            return 'Asian'
        elif 8 <= utc_hour < 13:
            return 'London'
        elif 13 <= utc_hour < 16:
            return 'London/NY Overlap'
        elif 16 <= utc_hour < 21:
            return 'NY'
        else:
            return 'After Hours'

    @classmethod
    def is_ny_open(cls, utc_hour: int, utc_minute: int) -> bool:
        """Check if within NY open volatility window (13:30-14:30 UTC)"""
        if utc_hour == 13 and utc_minute >= 30:
            return True
        if utc_hour == 14 and utc_minute <= 30:
            return True
        return False

    @classmethod
    def is_nfp_day(cls, date: datetime) -> bool:
        """Check if date is first Friday of month (NFP day)"""
        if date.weekday() != 4:  # Not Friday
            return False
        return date.day <= 7  # First Friday is within first 7 days

    @classmethod
    def get_nearest_fomc(cls, date: datetime) -> Optional[str]:
        """Get nearest upcoming FOMC date"""
        date_str = date.strftime('%Y-%m-%d')
        for fomc_date in cls.FOMC_DATES_2026:
            if fomc_date >= date_str:
                return fomc_date
        return None


class TradeAnalyzer:
    """Analyzes individual trades and identifies issues"""

    def __init__(self, db_path: str, config: Optional[Dict] = None):
        self.db_path = db_path
        self.config = config or {}
        self.anthropic_client = None

        if HAS_ANTHROPIC and os.environ.get('ANTHROPIC_API_KEY'):
            self.anthropic_client = anthropic.Anthropic()

    def get_trade(self, trade_number: int) -> Optional[TradeData]:
        """Fetch trade data from database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM trade_journal
            WHERE id = ? OR trade_number = ?
            ORDER BY id DESC LIMIT 1
        ''', (trade_number, trade_number))

        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        # Parse row into TradeData
        return self._parse_trade_row(row)

    def get_recent_trades(self, limit: int = 50) -> List[TradeData]:
        """Fetch recent trades"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT * FROM trade_journal
            ORDER BY id DESC LIMIT ?
        ''', (limit,))

        rows = cursor.fetchall()
        conn.close()

        return [self._parse_trade_row(row) for row in rows if row]

    def _parse_trade_row(self, row) -> TradeData:
        """Parse database row into TradeData object"""
        # Adjust indices based on your actual schema
        return TradeData(
            trade_id=row[1] if len(row) > 1 else '',
            trade_number=row[0],
            direction=row[2] if len(row) > 2 else '',
            lot_size=float(row[3]) if len(row) > 3 and row[3] else 20,
            entry_time=self._parse_datetime(row[4]) if len(row) > 4 else datetime.now(),
            exit_time=self._parse_datetime(row[5]) if len(row) > 5 else datetime.now(),
            entry_zscore=float(row[6]) if len(row) > 6 and row[6] else 0,
            exit_zscore=float(row[7]) if len(row) > 7 and row[7] else 0,
            entry_spread=float(row[8]) if len(row) > 8 and row[8] else 0,
            exit_spread=float(row[9]) if len(row) > 9 and row[9] else 0,
            entry_spot_price=float(row[10]) if len(row) > 10 and row[10] else 0,
            entry_futures_price=float(row[11]) if len(row) > 11 and row[11] else 0,
            exit_spot_price=float(row[12]) if len(row) > 12 and row[12] else 0,
            exit_futures_price=float(row[13]) if len(row) > 13 and row[13] else 0,
            gross_pnl=float(row[14]) if len(row) > 14 and row[14] else 0,
            spread_cost=float(row[15]) if len(row) > 15 and row[15] else 0,
            swap_cost=float(row[16]) if len(row) > 16 and row[16] else 0,
            commission=float(row[17]) if len(row) > 17 and row[17] else 0,
            net_pnl=float(row[18]) if len(row) > 18 and row[18] else 0,
            close_reason=row[19] if len(row) > 19 else 'UNKNOWN',
            duration_seconds=int((self._parse_datetime(row[5]) - self._parse_datetime(row[4])).total_seconds()) if len(row) > 5 else 0
        )

    def _parse_datetime(self, dt_str) -> datetime:
        """Parse datetime string"""
        if isinstance(dt_str, datetime):
            return dt_str
        try:
            return datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
        except:
            try:
                return datetime.strptime(dt_str, '%Y-%m-%d %H:%M')
            except:
                return datetime.now()

    def analyze_trade(self, trade: TradeData) -> Dict[str, Any]:
        """Perform comprehensive trade analysis"""
        analysis = {
            'trade_number': trade.trade_number,
            'is_winner': trade.net_pnl > 0,
            'net_pnl': trade.net_pnl,
            'issues': [],
            'factors': [],
            'recommendations': []
        }

        # === ANALYZE ENTRY ===
        entry_analysis = self._analyze_entry(trade)
        analysis['entry'] = entry_analysis
        analysis['issues'].extend(entry_analysis.get('issues', []))

        # === ANALYZE EXIT ===
        exit_analysis = self._analyze_exit(trade)
        analysis['exit'] = exit_analysis
        analysis['issues'].extend(exit_analysis.get('issues', []))

        # === ANALYZE COSTS ===
        cost_analysis = self._analyze_costs(trade)
        analysis['costs'] = cost_analysis
        analysis['issues'].extend(cost_analysis.get('issues', []))

        # === ANALYZE TIMING ===
        timing_analysis = self._analyze_timing(trade)
        analysis['timing'] = timing_analysis
        analysis['issues'].extend(timing_analysis.get('issues', []))

        # === GENERATE RECOMMENDATIONS ===
        analysis['recommendations'] = self._generate_recommendations(analysis)

        # === ROOT CAUSE ===
        analysis['root_cause'] = self._determine_root_cause(analysis)

        return analysis

    def _analyze_entry(self, trade: TradeData) -> Dict:
        """Analyze entry conditions"""
        result = {'issues': []}

        # Check if entry was at valid threshold
        entry_std = self.config.get('entry_std', 3.5)
        expected_entry_z = -entry_std if trade.direction == 'Long Spread' else entry_std

        result['entry_zscore'] = trade.entry_zscore
        result['expected_zscore'] = expected_entry_z
        result['entry_deviation'] = abs(abs(trade.entry_zscore) - entry_std)

        if abs(trade.entry_zscore) < entry_std - 0.2:
            result['issues'].append({
                'type': 'ENTRY_TOO_EARLY',
                'severity': 'MEDIUM',
                'message': f'Entered at Z={trade.entry_zscore:.2f} before reaching threshold ({expected_entry_z:.1f})'
            })

        return result

    def _analyze_exit(self, trade: TradeData) -> Dict:
        """Analyze exit conditions"""
        result = {'issues': []}

        exit_std = self.config.get('exit_std', 2.0)
        result['exit_zscore'] = trade.exit_zscore
        result['close_reason'] = trade.close_reason
        result['duration_seconds'] = trade.duration_seconds
        result['duration_formatted'] = self._format_duration(trade.duration_seconds)

        # Check if premature exit
        if trade.direction == 'Long Spread':
            expected_exit_z = exit_std  # Should exit at +2.0
            reached_target = trade.exit_zscore >= exit_std
        else:
            expected_exit_z = -exit_std  # Should exit at -2.0
            reached_target = trade.exit_zscore <= -exit_std

        result['expected_exit_zscore'] = expected_exit_z
        result['reached_target'] = reached_target

        # Z-score movement analysis
        z_movement = trade.exit_zscore - trade.entry_zscore
        result['z_movement'] = z_movement

        if trade.direction == 'Long Spread':
            z_needed = exit_std - trade.entry_zscore
        else:
            z_needed = trade.entry_zscore - (-exit_std)

        result['z_needed_for_target'] = z_needed
        result['z_progress_pct'] = (z_movement / z_needed * 100) if z_needed != 0 else 0

        # Issue detection
        if not reached_target:
            if trade.close_reason == 'MAX_LOSS':
                result['issues'].append({
                    'type': 'MAX_LOSS_EXIT',
                    'severity': 'HIGH',
                    'message': f'Exited due to max_loss before reaching target. Z moved {z_movement:.2f} but needed {z_needed:.2f}'
                })
            elif trade.close_reason == 'STOP_LOSS':
                result['issues'].append({
                    'type': 'STOP_LOSS_EXIT',
                    'severity': 'MEDIUM',
                    'message': f'Hit stop loss at Z={trade.exit_zscore:.2f}'
                })
            elif trade.duration_seconds < 60:
                result['issues'].append({
                    'type': 'IMMEDIATE_EXIT',
                    'severity': 'CRITICAL',
                    'message': f'Trade closed within {trade.duration_seconds}s - likely max_loss_per_lot too tight'
                })

        return result

    def _analyze_costs(self, trade: TradeData) -> Dict:
        """Analyze transaction costs"""
        result = {'issues': []}

        total_costs = trade.spread_cost + trade.swap_cost + trade.commission
        result['total_costs'] = total_costs
        result['spread_cost'] = trade.spread_cost
        result['swap_cost'] = trade.swap_cost
        result['commission'] = trade.commission

        # Cost as percentage of gross
        if trade.gross_pnl != 0:
            result['cost_ratio'] = abs(total_costs / trade.gross_pnl) * 100
        else:
            result['cost_ratio'] = float('inf')

        # Cost per lot
        result['cost_per_lot'] = total_costs / trade.lot_size if trade.lot_size > 0 else 0

        # Breakeven requirement
        result['breakeven_gross'] = total_costs

        # Issue detection
        if total_costs > 500:
            result['issues'].append({
                'type': 'HIGH_COSTS',
                'severity': 'HIGH',
                'message': f'Transaction costs ${total_costs:.2f} are very high. Need ${total_costs:.2f}+ gross profit to break even.'
            })

        if trade.gross_pnl > 0 and trade.net_pnl < 0:
            result['issues'].append({
                'type': 'COSTS_ATE_PROFIT',
                'severity': 'CRITICAL',
                'message': f'Trade was profitable (${trade.gross_pnl:.2f} gross) but costs (${total_costs:.2f}) turned it into a loss.'
            })

        return result

    def _analyze_timing(self, trade: TradeData) -> Dict:
        """Analyze trade timing"""
        result = {'issues': []}

        entry_hour = trade.entry_time.hour
        entry_minute = trade.entry_time.minute

        result['session'] = CalendarEvents.get_session(entry_hour)
        result['is_ny_open'] = CalendarEvents.is_ny_open(entry_hour, entry_minute)
        result['is_nfp_day'] = CalendarEvents.is_nfp_day(trade.entry_time)
        result['day_of_week'] = trade.entry_time.strftime('%A')

        # Issue detection
        if entry_hour >= 17:
            result['issues'].append({
                'type': 'LATE_SESSION_ENTRY',
                'severity': 'LOW',
                'message': f'Entered at {entry_hour}:{entry_minute:02d} UTC - lower liquidity period'
            })

        if result['is_nfp_day']:
            result['issues'].append({
                'type': 'NFP_DAY',
                'severity': 'HIGH',
                'message': 'Trade on NFP day - high volatility event'
            })

        if result['is_ny_open']:
            result['issues'].append({
                'type': 'NY_OPEN_VOLATILITY',
                'severity': 'MEDIUM',
                'message': 'Trade during NY open - high volatility window'
            })

        return result

    def _determine_root_cause(self, analysis: Dict) -> Dict:
        """Determine the primary root cause of a losing trade"""
        if analysis['is_winner']:
            return {
                'cause': 'WINNING_TRADE',
                'description': 'Trade reached profit target successfully',
                'severity': 'SUCCESS'
            }

        # Prioritize issues
        critical_issues = [i for i in analysis['issues'] if i.get('severity') == 'CRITICAL']
        high_issues = [i for i in analysis['issues'] if i.get('severity') == 'HIGH']

        if critical_issues:
            issue = critical_issues[0]
            if issue['type'] == 'IMMEDIATE_EXIT':
                return {
                    'cause': 'MAX_LOSS_TOO_TIGHT',
                    'description': 'max_loss_per_lot is too low relative to lot size. Trade exits before mean reversion occurs.',
                    'severity': 'CRITICAL',
                    'fix': 'Increase max_loss_per_lot to $150-250 OR reduce lot size'
                }
            elif issue['type'] == 'COSTS_ATE_PROFIT':
                return {
                    'cause': 'EXCESSIVE_COSTS',
                    'description': 'Transaction costs exceed gross profit potential',
                    'severity': 'CRITICAL',
                    'fix': 'Reduce lot size or use LIMIT orders to reduce spread costs'
                }

        if high_issues:
            issue = high_issues[0]
            if issue['type'] == 'MAX_LOSS_EXIT':
                return {
                    'cause': 'PREMATURE_MAX_LOSS_EXIT',
                    'description': 'Dollar stop (max_loss) triggered before statistical exit was reached',
                    'severity': 'HIGH',
                    'fix': 'Increase max_loss_per_lot or reduce lot size'
                }
            elif issue['type'] == 'HIGH_COSTS':
                return {
                    'cause': 'HIGH_TRANSACTION_COSTS',
                    'description': 'Transaction costs require large gross profit to break even',
                    'severity': 'HIGH',
                    'fix': 'Reduce lot size from 20 to 10 to halve costs'
                }

        return {
            'cause': 'MARKET_MOVEMENT',
            'description': 'Trade lost due to adverse market conditions',
            'severity': 'MEDIUM',
            'fix': 'Review entry timing and market conditions'
        }

    def _generate_recommendations(self, analysis: Dict) -> List[str]:
        """Generate actionable recommendations"""
        recommendations = []

        # Check for max_loss issues
        exit_issues = [i for i in analysis['issues'] if 'MAX_LOSS' in i.get('type', '')]
        if exit_issues:
            recommendations.append('CRITICAL: Increase max_loss_per_lot from current value to $150-250')

        # Check for cost issues
        if analysis['costs'].get('total_costs', 0) > 800:
            recommendations.append('Consider reducing lot size from 20 to 10 lots to reduce costs by 50%')

        # Check for timing issues
        if analysis['timing'].get('session') == 'After Hours':
            recommendations.append('Avoid entries during after-hours session (lower liquidity)')

        # Check for immediate exits
        if analysis['exit'].get('duration_seconds', 999) < 120:
            recommendations.append('Trade closed too quickly - increase max_loss_per_lot to allow mean reversion')

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
        """Generate human-readable analysis report"""
        trade = analysis

        status = '✅ WINNING' if trade['is_winner'] else '❌ LOSING'
        pnl_color = 'green' if trade['is_winner'] else 'red'

        report = f"""
═══════════════════════════════════════════════════════════════════════════════
📊 TRADE ANALYSIS REPORT - Trade #{trade['trade_number']}
═══════════════════════════════════════════════════════════════════════════════

SUMMARY: {status} TRADE (Net P&L: ${trade['net_pnl']:.2f})

┌─────────────────────────────────────────────────────────────────────────────┐
│ EXIT ANALYSIS                                                               │
├─────────────────────────────────────────────────────────────────────────────┤
│ Exit Reason: {trade['exit']['close_reason']:<20}                           │
│ Duration: {trade['exit']['duration_formatted']:<25}                        │
│ Entry Z: {trade['entry']['entry_zscore']:+.2f}  →  Exit Z: {trade['exit']['exit_zscore']:+.2f}                       │
│ Z Movement: {trade['exit']['z_movement']:+.2f} (needed {trade['exit']['z_needed_for_target']:+.2f} for target)      │
│ Progress to Target: {trade['exit']['z_progress_pct']:.1f}%                                       │
│ Reached Target: {'YES' if trade['exit']['reached_target'] else 'NO':<20}                       │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ COST BREAKDOWN                                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│ Spread Cost:    ${trade['costs']['spread_cost']:>10.2f}                                         │
│ Swap Cost:      ${trade['costs']['swap_cost']:>10.2f}                                         │
│ Commission:     ${trade['costs']['commission']:>10.2f}                                         │
│ ─────────────────────────────                                               │
│ TOTAL COSTS:    ${trade['costs']['total_costs']:>10.2f}                                         │
│ Cost/Lot:       ${trade['costs']['cost_per_lot']:>10.2f}                                         │
│                                                                             │
│ Breakeven requires: ${trade['costs']['breakeven_gross']:.2f} gross profit                       │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ TIMING ANALYSIS                                                             │
├─────────────────────────────────────────────────────────────────────────────┤
│ Session: {trade['timing']['session']:<30}                                  │
│ Day: {trade['timing']['day_of_week']:<35}                                  │
│ NY Open Window: {'YES' if trade['timing']['is_ny_open'] else 'NO':<30}                │
│ NFP Day: {'YES' if trade['timing']['is_nfp_day'] else 'NO':<35}               │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ ROOT CAUSE                                                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│ {trade['root_cause']['cause']}: {trade['root_cause']['description'][:50]}  │
│                                                                             │
│ FIX: {trade['root_cause'].get('fix', 'N/A')[:60]}                          │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ ISSUES IDENTIFIED ({len(trade['issues'])} total)                                             │
├─────────────────────────────────────────────────────────────────────────────┤"""

        for issue in trade['issues'][:5]:
            severity_icon = {'CRITICAL': '🔴', 'HIGH': '🟠', 'MEDIUM': '🟡', 'LOW': '🟢'}.get(issue['severity'], '⚪')
            report += f"\n│ {severity_icon} [{issue['severity']}] {issue['type'][:30]:<30}                 │"
            report += f"\n│   {issue['message'][:65]:<65} │"

        report += """
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│ RECOMMENDATIONS                                                             │
├─────────────────────────────────────────────────────────────────────────────┤"""

        for i, rec in enumerate(trade['recommendations'][:4], 1):
            report += f"\n│ {i}. {rec[:70]:<70} │"

        report += """
└─────────────────────────────────────────────────────────────────────────────┘
═══════════════════════════════════════════════════════════════════════════════
"""
        return report

    def get_ai_analysis(self, trade: TradeData, analysis: Dict) -> Optional[str]:
        """Get AI-powered analysis using Claude API"""
        if not self.anthropic_client:
            return None

        prompt = f"""Analyze this statistical arbitrage trade and explain why it lost money:

Trade Data:
- Direction: {trade.direction}
- Lot Size: {trade.lot_size}
- Entry Z-score: {trade.entry_zscore}
- Exit Z-score: {trade.exit_zscore}
- Entry Spread: ${trade.entry_spread:.3f}
- Exit Spread: ${trade.exit_spread:.3f}
- Duration: {analysis['exit']['duration_formatted']}
- Close Reason: {trade.close_reason}
- Gross P&L: ${trade.gross_pnl:.2f}
- Total Costs: ${analysis['costs']['total_costs']:.2f}
- Net P&L: ${trade.net_pnl:.2f}
- Session: {analysis['timing']['session']}

Issues Identified:
{json.dumps([i['message'] for i in analysis['issues']], indent=2)}

Provide:
1. A concise explanation of what went wrong (2-3 sentences)
2. The most likely root cause
3. Specific parameter changes to fix this"""

        try:
            message = self.anthropic_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )
            return message.content[0].text
        except Exception as e:
            return f"AI analysis unavailable: {e}"


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
    parser = argparse.ArgumentParser(description='AI Trade Analyst')
    parser.add_argument('--watch', action='store_true', help='Watch for new trades')
    parser.add_argument('--analyze', type=int, help='Analyze specific trade number')
    parser.add_argument('--report', action='store_true', help='Generate report for recent trades')
    parser.add_argument('--db', default='basis_trading.db', help='Database path')

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
            sys.exit(1)

    # Load config if available
    config = {
        'entry_std': 3.5,
        'exit_std': 2.0,
        'stop_loss_std': 6.0,
        'max_loss_per_lot': 100
    }

    analyzer = TradeAnalyzer(db_path, config)

    if args.watch:
        monitor = TradeMonitor(analyzer)
        monitor.watch()

    elif args.analyze:
        trade = analyzer.get_trade(args.analyze)
        if trade:
            analysis = analyzer.analyze_trade(trade)
            report = analyzer.generate_report(analysis)
            print(report)

            # Try AI analysis
            ai_analysis = analyzer.get_ai_analysis(trade, analysis)
            if ai_analysis:
                print("\n🤖 AI ANALYSIS:")
                print("-" * 40)
                print(ai_analysis)
        else:
            print(f"Trade #{args.analyze} not found")

    elif args.report:
        trades = analyzer.get_recent_trades(10)

        print("\n📊 RECENT TRADES SUMMARY")
        print("=" * 60)

        winners = sum(1 for t in trades if analyzer.analyze_trade(t)['is_winner'])
        total_pnl = sum(t.net_pnl for t in trades)

        print(f"Total Trades: {len(trades)}")
        print(f"Winners: {winners} ({winners/len(trades)*100:.1f}%)")
        print(f"Losers: {len(trades) - winners}")
        print(f"Net P&L: ${total_pnl:.2f}")

        # Analyze each
        for trade in trades[:5]:
            analysis = analyzer.analyze_trade(trade)
            status = '✅' if analysis['is_winner'] else '❌'
            print(f"\n{status} Trade #{trade.trade_number}: ${trade.net_pnl:.2f}")
            print(f"   Root Cause: {analysis['root_cause']['cause']}")

    else:
        parser.print_help()


if __name__ == '__main__':
    main()
