"""Performance metrics tracking."""


class PerformanceTracker:
    def __init__(self):
        self.reset_daily_metrics()
        self.total_trades = 0
        self.winning_trades = 0
        self.total_pnl = 0.0
        self.max_drawdown = 0.0
        self.peak_pnl = 0.0

    def reset_daily_metrics(self):
        self.daily_pnl = 0.0
        self.daily_trades = 0
        self.daily_winners = 0

    def update_with_closed_position(self, position):
        self.total_trades += 1
        self.daily_trades += 1

        pnl = position.realized_pnl
        self.total_pnl += pnl
        self.daily_pnl += pnl

        if pnl > 0:
            self.winning_trades += 1
            self.daily_winners += 1

        if self.total_pnl > self.peak_pnl:
            self.peak_pnl = self.total_pnl

        drawdown = self.peak_pnl - self.total_pnl
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

    def get_metrics(self):
        win_rate = (self.winning_trades / max(self.total_trades, 1)) * 100
        daily_win_rate = (self.daily_winners / max(self.daily_trades, 1)) * 100
        return {
            'total_pnl': self.total_pnl,
            'daily_pnl': self.daily_pnl,
            'total_trades': self.total_trades,
            'daily_trades': self.daily_trades,
            'win_rate': win_rate,
            'daily_win_rate': daily_win_rate,
            'max_drawdown': self.max_drawdown,
        }
