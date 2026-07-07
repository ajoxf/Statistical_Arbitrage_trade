"""Round-trip cost model and the edge filter (the dead-day gate).

A trade only makes sense if the move we expect to capture clears
what four executions cost. Costs here are estimates from live spreads
plus configured commissions — audit them against realized fills and
tighten SPREAD_COST_FACTOR once limit fills prove out (an inflated
cost model silently blocks every good trade).
"""


def round_trip_cost(market_data, lots, contract_size, costs_cfg):
    """Dollars to open AND close both legs at `lots` per leg."""
    oz = lots * contract_size
    spot_spread = market_data['spot_ask'] - market_data['spot_bid']
    fut_spread = market_data['futures_ask'] - market_data['futures_bid']
    # Crossing pays the spread once per round trip per leg (in at one
    # side, out at the other); factor <1 models limit fills
    spread_cost = (spot_spread + fut_spread) * oz \
        * costs_cfg.get('SPREAD_COST_FACTOR', 1.0)
    commissions = (costs_cfg.get('COMMISSION_PER_LOT_SPOT', 0.0)
                   + costs_cfg.get('COMMISSION_PER_LOT_FUT', 0.0)) * lots
    return spread_cost + commissions


def expected_capture(z, sigma, lots, contract_size, costs_cfg):
    """Dollars we target from this entry: the same number the exit's
    sigma-fraction take-profit aims for."""
    if z is None or sigma is None:
        return 0.0
    oz = lots * contract_size
    return costs_cfg.get('TARGET_FRACTION', 0.5) * abs(z) * sigma * oz


def edge_ok(z, sigma, lots, contract_size, market_data, costs_cfg):
    """(passes, capture, cost) — capture must be >= MIN_EDGE_MULTIPLE x cost."""
    capture = expected_capture(z, sigma, lots, contract_size, costs_cfg)
    cost = round_trip_cost(market_data, lots, contract_size, costs_cfg)
    multiple = costs_cfg.get('MIN_EDGE_MULTIPLE', 1.5)
    return capture >= multiple * cost, capture, cost
