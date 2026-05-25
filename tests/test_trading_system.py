"""Automated tests for the statistical-arbitrage trading system.

Coverage focus: order execution, with emphasis on every limit-order type
(MARKET / LIMIT / PEGGED_LIMIT). The suite verifies:

  * the order-type enum the Settings UI saves,
  * entry/exit leg-side selection for LONG and SHORT spreads,
  * execution-mode routing (market vs. pegged-limit),
  * pegged limit-order price calculation and the BUY/SELL validity rules,
  * the one-leg-fill ("leg risk") state machine that stops a pegged spread
    from re-pegging forever when only one leg fills.

MetaTrader5 is Windows-only and is stubbed (see conftest.py); these tests
exercise pure logic and never reach a live broker.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from base import OrderType
from pegged_executor import (
    PeggedOrderExecutor,
    SpreadOrder,
    LegOrder,
    LegStatus,
    ExecutionMode,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
class FakeConfig:
    """Minimal stand-in for the application's TradingConfig."""

    def __init__(self, order_type="PEGGED_LIMIT", limit_order_timeout_sec=30,
                 limit_order_price_offset_bps=1.0):
        self.order_type = order_type
        self.limit_order_timeout_sec = limit_order_timeout_sec
        self.limit_order_price_offset_bps = limit_order_price_offset_bps


class FakeBroker:
    def __init__(self, symbol):
        self.symbol = symbol


SPOT_TICK = {"bid": 100.00, "ask": 100.10}
FUT_TICK = {"bid": 50.00, "ask": 50.05}


def make_executor(**cfg):
    return PeggedOrderExecutor(FakeConfig(**cfg))


def make_spread(spot_side="BUY", futures_side="SELL",
                spot_status=LegStatus.PENDING, futures_status=LegStatus.PENDING):
    return SpreadOrder(
        spot_leg=LegOrder(symbol="SPOT", side=spot_side, quantity=1.0,
                          status=spot_status),
        futures_leg=LegOrder(symbol="FUT", side=futures_side, quantity=1.0,
                             status=futures_status),
    )


def capture_spread():
    """Patch _execute_spread so execute_entry/exit return the built order
    without touching MT5."""
    return patch.object(PeggedOrderExecutor, "_execute_spread",
                        side_effect=lambda so, *a, **k: so)


# ---------------------------------------------------------------------------
# Order-type definitions
# ---------------------------------------------------------------------------
class TestOrderTypes:
    def test_all_three_order_types_exist(self):
        assert OrderType.MARKET.value == "MARKET"
        assert OrderType.LIMIT.value == "LIMIT"
        assert OrderType.PEGGED_LIMIT.value == "PEGGED_LIMIT"

    def test_order_type_set_matches_settings_ui(self):
        # The Settings page exposes exactly these three options.
        assert {"MARKET", "LIMIT", "PEGGED_LIMIT"} <= {ot.value for ot in OrderType}


class TestExecutionEnums:
    def test_execution_mode_values(self):
        assert ExecutionMode.MARKET.value == "MARKET"
        assert ExecutionMode.PEGGED_LIMIT.value == "PEGGED_LIMIT"

    def test_leg_status_values(self):
        assert {s.value for s in LegStatus} == {
            "PENDING", "OPEN", "PARTIAL", "FILLED", "CANCELLED", "FAILED"
        }


# ---------------------------------------------------------------------------
# SpreadOrder state machine (the leg-risk / one-leg-fill logic)
# ---------------------------------------------------------------------------
class TestSpreadOrderState:
    def test_complete_when_both_filled(self):
        so = make_spread("BUY", "SELL", LegStatus.FILLED, LegStatus.FILLED)
        assert so.is_complete
        assert not so.is_failed
        assert not so.has_partial_fill

    def test_not_complete_when_one_pending(self):
        so = make_spread("BUY", "SELL", LegStatus.FILLED, LegStatus.PENDING)
        assert not so.is_complete

    @pytest.mark.parametrize("spot,fut", [
        (LegStatus.FILLED, LegStatus.PENDING),
        (LegStatus.PENDING, LegStatus.FILLED),
        (LegStatus.PARTIAL, LegStatus.PENDING),
        (LegStatus.PENDING, LegStatus.PARTIAL),
        (LegStatus.FILLED, LegStatus.CANCELLED),
    ])
    def test_partial_fill_flags_leg_risk(self, spot, fut):
        # Exactly one leg (partly) filled => leg risk => must trigger fallback.
        assert make_spread("BUY", "SELL", spot, fut).has_partial_fill is True

    @pytest.mark.parametrize("spot,fut", [
        (LegStatus.FILLED, LegStatus.FILLED),
        (LegStatus.PENDING, LegStatus.PENDING),
        (LegStatus.PARTIAL, LegStatus.FILLED),
        (LegStatus.PENDING, LegStatus.CANCELLED),
    ])
    def test_no_leg_risk_when_symmetric(self, spot, fut):
        assert make_spread("BUY", "SELL", spot, fut).has_partial_fill is False

    def test_failed_when_either_leg_failed(self):
        assert make_spread("BUY", "SELL", LegStatus.FAILED, LegStatus.PENDING).is_failed
        assert make_spread("BUY", "SELL", LegStatus.PENDING, LegStatus.FAILED).is_failed

    def test_not_failed_when_no_leg_failed(self):
        assert not make_spread("BUY", "SELL", LegStatus.FILLED, LegStatus.PENDING).is_failed


# ---------------------------------------------------------------------------
# Entry / exit leg-side selection
# ---------------------------------------------------------------------------
class TestEntryExitSides:
    @pytest.mark.parametrize("ptype,exp_spot,exp_fut", [
        ("LONG", "BUY", "SELL"),
        ("SHORT", "SELL", "BUY"),
    ])
    def test_entry_sides(self, ptype, exp_spot, exp_fut):
        ex = make_executor()
        with capture_spread():
            so = ex.execute_entry(ptype, FakeBroker("SP"), FakeBroker("FU"),
                                  SPOT_TICK, FUT_TICK, 1.0)
        assert so.spot_leg.side == exp_spot
        assert so.futures_leg.side == exp_fut
        assert so.is_entry is True
        assert so.spot_leg.symbol == "SP"
        assert so.futures_leg.symbol == "FU"

    @pytest.mark.parametrize("ptype,exp_spot,exp_fut", [
        ("LONG", "SELL", "BUY"),
        ("SHORT", "BUY", "SELL"),
    ])
    def test_exit_sides_are_opposite_of_entry(self, ptype, exp_spot, exp_fut):
        ex = make_executor()
        with capture_spread():
            so = ex.execute_exit(ptype, FakeBroker("SP"), FakeBroker("FU"),
                                 SPOT_TICK, FUT_TICK, 1.0)
        assert so.spot_leg.side == exp_spot
        assert so.futures_leg.side == exp_fut
        assert so.is_entry is False

    def test_timeout_derived_from_config(self):
        ex = make_executor(limit_order_timeout_sec=45)
        before = datetime.now()
        with capture_spread():
            so = ex.execute_entry("LONG", FakeBroker("SP"), FakeBroker("FU"),
                                  SPOT_TICK, FUT_TICK, 1.0)
        after = datetime.now()
        assert before + timedelta(seconds=45) <= so.timeout_at <= after + timedelta(seconds=45)

    def test_busy_executor_rejects_new_entry(self):
        ex = make_executor()
        ex._executing = True
        assert ex.execute_entry("LONG", FakeBroker("SP"), FakeBroker("FU"),
                                SPOT_TICK, FUT_TICK, 1.0) is None

    def test_busy_executor_rejects_new_exit(self):
        ex = make_executor()
        ex._executing = True
        assert ex.execute_exit("LONG", FakeBroker("SP"), FakeBroker("FU"),
                               SPOT_TICK, FUT_TICK, 1.0) is None


# ---------------------------------------------------------------------------
# Execution-mode routing (market vs. limit/pegged)
# ---------------------------------------------------------------------------
class TestModeRouting:
    @pytest.mark.parametrize("order_type", ["LIMIT", "PEGGED_LIMIT"])
    def test_limit_modes_route_to_pegged(self, order_type):
        ex = make_executor(order_type=order_type)
        so = make_spread()
        with patch.object(ex, "_execute_pegged_limit", return_value=so) as peg, \
             patch.object(ex, "_execute_market", return_value=so) as mkt:
            ex._execute_spread(so, FakeBroker("SP"), FakeBroker("FU"), SPOT_TICK, FUT_TICK)
        peg.assert_called_once()
        mkt.assert_not_called()

    def test_market_mode_routes_to_market(self):
        ex = make_executor(order_type="MARKET")
        so = make_spread()
        with patch.object(ex, "_execute_pegged_limit", return_value=so) as peg, \
             patch.object(ex, "_execute_market", return_value=so) as mkt:
            ex._execute_spread(so, FakeBroker("SP"), FakeBroker("FU"), SPOT_TICK, FUT_TICK)
        mkt.assert_called_once()
        peg.assert_not_called()

    def test_executor_state_cleared_after_execution(self):
        ex = make_executor(order_type="MARKET")
        so = make_spread()
        with patch.object(ex, "_execute_market", return_value=so):
            ex._execute_spread(so, FakeBroker("SP"), FakeBroker("FU"), SPOT_TICK, FUT_TICK)
        assert ex._executing is False
        assert ex.active_order is None


# ---------------------------------------------------------------------------
# Pegged limit-order price calculation
# ---------------------------------------------------------------------------
class TestLimitPriceCalculation:
    def test_buy_limit_price_inside_spread(self):
        ex = make_executor(limit_order_price_offset_bps=1.0)
        so = make_spread("BUY", "BUY")
        ex._update_target_prices(so, SPOT_TICK, FUT_TICK)
        # BUY_LIMIT must sit below the ask and at/above the bid.
        assert SPOT_TICK["bid"] <= so.spot_leg.target_price < SPOT_TICK["ask"]
        assert FUT_TICK["bid"] <= so.futures_leg.target_price < FUT_TICK["ask"]

    def test_sell_limit_price_inside_spread(self):
        ex = make_executor(limit_order_price_offset_bps=1.0)
        so = make_spread("SELL", "SELL")
        ex._update_target_prices(so, SPOT_TICK, FUT_TICK)
        # SELL_LIMIT must sit above the bid and at/below the ask.
        assert SPOT_TICK["bid"] < so.spot_leg.target_price <= SPOT_TICK["ask"]
        assert FUT_TICK["bid"] < so.futures_leg.target_price <= FUT_TICK["ask"]

    def test_buy_offset_applied_above_bid(self):
        ex = make_executor(limit_order_price_offset_bps=5.0)  # 5 bps
        so = make_spread("BUY", "BUY")
        ex._update_target_prices(so, {"bid": 100.0, "ask": 101.0},
                                 {"bid": 100.0, "ask": 101.0})
        assert so.spot_leg.target_price == pytest.approx(100.05)  # 100 * 1.0005

    def test_buy_falls_back_to_bid_when_offset_crosses_ask(self):
        # Tight spread: a 50 bps offset would push the BUY target through the
        # ask, so it must fall back to the bid to remain a valid BUY_LIMIT.
        ex = make_executor(limit_order_price_offset_bps=50.0)
        so = make_spread("BUY", "BUY")
        ex._update_target_prices(so, {"bid": 100.0, "ask": 100.10},
                                 {"bid": 100.0, "ask": 100.10})
        assert so.spot_leg.target_price == 100.0

    def test_sell_falls_back_to_ask_when_offset_crosses_bid(self):
        ex = make_executor(limit_order_price_offset_bps=50.0)
        so = make_spread("SELL", "SELL")
        ex._update_target_prices(so, {"bid": 100.0, "ask": 100.10},
                                 {"bid": 100.0, "ask": 100.10})
        assert so.spot_leg.target_price == 100.10

    def test_default_offset_used_when_config_missing_attr(self):
        class Bare:
            pass

        ex = PeggedOrderExecutor(Bare())  # no limit_order_price_offset_bps
        so = make_spread("BUY", "SELL")
        ex._update_target_prices(so, {"bid": 100.0, "ask": 101.0},
                                 {"bid": 100.0, "ask": 101.0})
        assert so.spot_leg.target_price == pytest.approx(100.01)              # 1 bps BUY
        assert so.futures_leg.target_price == pytest.approx(101.0 * (1 - 0.0001))  # 1 bps SELL
