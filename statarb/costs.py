"""Round-trip cost model and the edge filter (the dead-day gate).

A trade only makes sense if the move we expect to capture clears
what four executions cost. Costs here are estimates from live spreads
plus configured commissions — audit them against realized fills and
tighten SPREAD_COST_FACTOR once limit fills prove out (an inflated
cost model silently blocks every good trade).
"""


def round_trip_cost(market_data, lots, contract_size, costs_cfg,
                    lots_b=None, contract_b=None):
    """Dollars to open AND close both legs.

    Each leg is priced in ITS OWN units. This used to multiply both
    legs' bid-asks by leg A's `lots * contract_size`, which is exact
    only when the legs match in both — true on gold (100 oz, equal
    lots) and on the oil pair, and wrong everywhere else.

    Live 2026-08-10 on XAGUSD/XAUUSD it charged gold's 0.24 spread
    against SILVER's 5,000 units: $1,200 for a leg whose real cost is
    0.24 x 100 x 1.15 = $28. The whole edge filter, the exit ladder's
    cost floor and the expected value all read that number.

    `lots_b` / `contract_b` default to leg A's, so a caller that has
    only one size gets exactly the old behaviour.
    """
    units_a = lots * contract_size
    units_b = (lots if lots_b is None else lots_b) * \
        (contract_size if contract_b is None else contract_b)
    spot_spread = market_data['spot_ask'] - market_data['spot_bid']
    fut_spread = market_data['futures_ask'] - market_data['futures_bid']
    # Crossing pays the spread once per round trip per leg (in at one
    # side, out at the other); factor <1 models limit fills
    spread_cost = (spot_spread * units_a + fut_spread * units_b) \
        * costs_cfg.get('SPREAD_COST_FACTOR', 1.0)
    # Commission is quoted per LOT of the leg it belongs to.
    commissions = (
        costs_cfg.get('COMMISSION_PER_LOT_SPOT', 0.0) * lots
        + costs_cfg.get('COMMISSION_PER_LOT_FUT', 0.0)
        * (lots if lots_b is None else lots_b))
    return spread_cost + commissions


def expected_capture(z, sigma, lots, contract_size, costs_cfg):
    """Dollars we target from this entry: the same number the exit's
    sigma-fraction take-profit aims for."""
    if z is None or sigma is None:
        return 0.0
    oz = lots * contract_size
    return costs_cfg.get('TARGET_FRACTION', 0.5) * abs(z) * sigma * oz


def edge_ok(z, sigma, lots, contract_size, market_data, costs_cfg,
            lots_b=None, contract_b=None):
    """(passes, capture, cost) — capture must be >= MIN_EDGE_MULTIPLE x cost."""
    capture = expected_capture(z, sigma, lots, contract_size, costs_cfg)
    cost = round_trip_cost(market_data, lots, contract_size, costs_cfg,
                           lots_b, contract_b)
    multiple = costs_cfg.get('MIN_EDGE_MULTIPLE', 1.5)
    return capture >= multiple * cost, capture, cost
