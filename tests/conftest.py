import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from statarb.broker import OrderResult  # noqa: E402
from statarb.config import AlgoTradingConfig  # noqa: E402
from statarb.database import DataLogger  # noqa: E402


class FakeBroker:
    """In-memory broker: records orders, no MT5 required."""

    def __init__(self, fail_symbols=None, price=100.0):
        self.orders = []          # (symbol, side_value, volume)
        self.fail_symbols = set(fail_symbols or [])
        self.price = price
        self.next_ticket = 1000

    def send_market_order(self, symbol, side, volume,
                          slippage_points=1.0, comment=""):
        if symbol in self.fail_symbols:
            return OrderResult(False, error="forced failure")
        self.orders.append((symbol, side.value, volume))
        self.next_ticket += 1
        return OrderResult(True, requested_price=self.price,
                           executed_price=self.price,
                           ticket=self.next_ticket)


@pytest.fixture
def config():
    return AlgoTradingConfig()


@pytest.fixture
def data_logger(tmp_path):
    return DataLogger(db_path=str(tmp_path / "test.db"))


@pytest.fixture
def fake_broker():
    return FakeBroker()
