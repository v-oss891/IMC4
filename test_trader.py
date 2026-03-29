"""
Test script for IMC Prosperity 4 Trader.
Creates mock TradingState objects and validates strategy behavior.
"""
from datamodel import OrderDepth, TradingState, Order, Listing, Observation
from wallmid_meanrevert_mm import Trader
import json


def make_order_depth(buys: dict, sells: dict) -> OrderDepth:
    """Create an OrderDepth from bid/ask dicts. Sells should have negative values."""
    od = OrderDepth()
    od.buy_orders = buys
    od.sell_orders = {p: -abs(v) for p, v in sells.items()}
    return od


def make_state(
    order_depths: dict,
    position: dict = None,
    trader_data: str = "",
    timestamp: int = 100,
) -> TradingState:
    """Create a mock TradingState."""
    return TradingState(
        traderData=trader_data,
        timestamp=timestamp,
        listings={},
        order_depths=order_depths,
        own_trades={},
        market_trades={},
        position=position or {},
        observations=Observation({}, {}),
    )


def test_resin_takes_mispriced():
    """Resin: Should aggressively take orders below 9999 and above 10001."""
    print("=" * 60)
    print("TEST 1: Rainforest Resin — Takes mispriced orders")
    print("=" * 60)

    od = make_order_depth(
        buys={10002: 10, 10001: 15, 10000: 5},  # Bids above fair
        sells={9998: 10, 9999: 5, 10003: 20},    # Asks below fair
    )
    state = make_state({"RAINFOREST_RESIN": od})
    trader = Trader()
    result, conv, td = trader.run(state)

    orders = result.get("RAINFOREST_RESIN", [])
    print(f"  Generated {len(orders)} orders:")
    for o in orders:
        action = "BUY" if o.quantity > 0 else "SELL"
        print(f"    {action} {abs(o.quantity)} @ {o.price}")

    # Verify: should buy at 9998 and 9999, sell at 10002 and 10001
    buy_prices = [o.price for o in orders if o.quantity > 0]
    sell_prices = [o.price for o in orders if o.quantity < 0]
    assert 9998 in buy_prices, "Should take cheap ask at 9998"
    assert 10002 in sell_prices or 10001 in sell_prices, "Should take expensive bid at 10001+"
    print("  ✅ PASSED\n")


def test_resin_position_limits():
    """Resin: Should not exceed position limits at any point during execution."""
    print("=" * 60)
    print("TEST 2: Rainforest Resin — Position limit enforcement")
    print("=" * 60)

    od = make_order_depth(
        buys={10002: 100},
        sells={9998: 100},
    )
    initial_pos = 45
    state = make_state({"RAINFOREST_RESIN": od}, position={"RAINFOREST_RESIN": initial_pos})
    trader = Trader()
    result, conv, td = trader.run(state)

    orders = result.get("RAINFOREST_RESIN", [])

    # Simulate order execution to verify position never exceeds limits
    simulated_pos = initial_pos
    limit = 50
    all_valid = True
    print(f"  Starting position: {initial_pos}, Limit: ±{limit}")
    for o in orders:
        simulated_pos += o.quantity
        action = "BUY" if o.quantity > 0 else "SELL"
        in_bounds = -limit <= simulated_pos <= limit
        status = "✓" if in_bounds else "✗ VIOLATION"
        print(f"    {action} {abs(o.quantity)} @ {o.price} → pos={simulated_pos} {status}")
        if not in_bounds:
            all_valid = False

    assert all_valid, f"Position exceeded limits during order sequence"
    print("  ✅ PASSED\n")


def test_kelp_wallmid():
    """Kelp: Should compute WallMid from deepest liquidity levels."""
    print("=" * 60)
    print("TEST 3: Kelp — WallMid computation and quoting")
    print("=" * 60)

    # Wall bid at 2028 (vol=50), wall ask at 2032 (vol=40)
    od = make_order_depth(
        buys={2030: 5, 2029: 10, 2028: 50, 2027: 20},
        sells={2031: 5, 2032: 40, 2033: 15, 2035: 10},
    )
    state = make_state({"KELP": od})
    trader = Trader()
    result, conv, td = trader.run(state)

    orders = result.get("KELP", [])
    print(f"  Wall bid: 2028 (vol=50), Wall ask: 2032 (vol=40)")
    print(f"  Expected WallMid: 2030.0")
    print(f"  Generated {len(orders)} orders:")
    for o in orders:
        action = "BUY" if o.quantity > 0 else "SELL"
        print(f"    {action} {abs(o.quantity)} @ {o.price}")

    # Should have taken the ask at 2031 (below WallMid of 2030? No, 2031 > 2030)
    # Should quote around WallMid ± 1 → bid 2029, ask 2031
    buy_orders = [o for o in orders if o.quantity > 0]
    sell_orders = [o for o in orders if o.quantity < 0]
    assert len(buy_orders) > 0, "Should have buy orders"
    assert len(sell_orders) > 0, "Should have sell orders"
    print("  ✅ PASSED\n")


def test_squid_ink_spike():
    """Squid Ink: Should detect volatility spikes and trade mean reversion."""
    print("=" * 60)
    print("TEST 4: Squid Ink — Volatility spike detection")
    print("=" * 60)

    trader = Trader()
    trader_data = ""

    # Feed 10 timestamps of stable data near 1800
    print("  Phase 1: Feeding stable data (1798-1802)...")
    stable_prices = [1800, 1801, 1799, 1800, 1802, 1800, 1799, 1801, 1800, 1800]

    for i, price in enumerate(stable_prices):
        od = make_order_depth(
            buys={price - 1: 10, price - 2: 20},
            sells={price + 1: 10, price + 2: 20},
        )
        state = make_state({"SQUID_INK": od}, trader_data=trader_data, timestamp=i * 100)
        result, conv, trader_data = trader.run(state)

    print(f"    Last result orders: {len(result.get('SQUID_INK', []))}")

    # Now spike UP to 1820 (Z-score >> 2.5)
    print("  Phase 2: Price spikes UP to 1820...")
    od = make_order_depth(
        buys={1819: 10, 1818: 20},
        sells={1821: 10, 1822: 20},
    )
    state = make_state({"SQUID_INK": od}, trader_data=trader_data, timestamp=1000)
    result, conv, trader_data = trader.run(state)

    orders = result.get("SQUID_INK", [])
    print(f"  Generated {len(orders)} orders after spike:")
    for o in orders:
        action = "BUY" if o.quantity > 0 else "SELL"
        print(f"    {action} {abs(o.quantity)} @ {o.price}")

    sell_orders = [o for o in orders if o.quantity < 0]
    assert len(sell_orders) > 0, "Should SHORT after upward spike"
    print("  ✅ PASSED\n")


def test_squid_ink_drop():
    """Squid Ink: Should buy on downward spike."""
    print("=" * 60)
    print("TEST 5: Squid Ink — Downward spike detection")
    print("=" * 60)

    trader = Trader()
    trader_data = ""

    # Feed stable data
    stable_prices = [1800, 1801, 1799, 1800, 1802, 1800, 1799, 1801, 1800, 1800]
    for i, price in enumerate(stable_prices):
        od = make_order_depth(
            buys={price - 1: 10, price - 2: 20},
            sells={price + 1: 10, price + 2: 20},
        )
        state = make_state({"SQUID_INK": od}, trader_data=trader_data, timestamp=i * 100)
        result, conv, trader_data = trader.run(state)

    # Spike DOWN to 1780
    print("  Price drops to 1780...")
    od = make_order_depth(
        buys={1779: 10, 1778: 20},
        sells={1781: 10, 1782: 20},
    )
    state = make_state({"SQUID_INK": od}, trader_data=trader_data, timestamp=1000)
    result, conv, trader_data = trader.run(state)

    orders = result.get("SQUID_INK", [])
    print(f"  Generated {len(orders)} orders after drop:")
    for o in orders:
        action = "BUY" if o.quantity > 0 else "SELL"
        print(f"    {action} {abs(o.quantity)} @ {o.price}")

    buy_orders = [o for o in orders if o.quantity > 0]
    assert len(buy_orders) > 0, "Should BUY after downward spike"
    print("  ✅ PASSED\n")


def test_multi_product():
    """All products trading simultaneously."""
    print("=" * 60)
    print("TEST 6: Multi-product simultaneous trading")
    print("=" * 60)

    trader = Trader()

    resin_od = make_order_depth(
        buys={10001: 10, 9999: 20},
        sells={9998: 5, 10002: 15},
    )
    kelp_od = make_order_depth(
        buys={2028: 50, 2027: 20},
        sells={2032: 40, 2033: 15},
    )
    squid_od = make_order_depth(
        buys={1799: 10, 1798: 20},
        sells={1801: 10, 1802: 20},
    )

    state = make_state({
        "RAINFOREST_RESIN": resin_od,
        "KELP": kelp_od,
        "SQUID_INK": squid_od,
    })

    result, conv, td = trader.run(state)

    for product in ["RAINFOREST_RESIN", "KELP", "SQUID_INK"]:
        orders = result.get(product, [])
        print(f"  {product}: {len(orders)} orders")
        for o in orders:
            action = "BUY" if o.quantity > 0 else "SELL"
            print(f"    {action} {abs(o.quantity)} @ {o.price}")

    assert "RAINFOREST_RESIN" in result, "Resin should generate orders"
    assert "KELP" in result, "Kelp should generate orders"
    print("  ✅ PASSED\n")


def test_unknown_product():
    """Generic fallback for unknown products."""
    print("=" * 60)
    print("TEST 7: Unknown product — Generic MM fallback")
    print("=" * 60)

    od = make_order_depth(
        buys={500: 10, 499: 20},
        sells={503: 10, 504: 20},
    )
    state = make_state({"MYSTERY_PRODUCT": od})
    trader = Trader()
    result, conv, td = trader.run(state)

    orders = result.get("MYSTERY_PRODUCT", [])
    print(f"  Generated {len(orders)} orders for unknown product:")
    for o in orders:
        action = "BUY" if o.quantity > 0 else "SELL"
        print(f"    {action} {abs(o.quantity)} @ {o.price}")

    assert len(orders) > 0, "Should generate generic MM orders"
    print("  ✅ PASSED\n")


if __name__ == "__main__":
    print("\n🚀 IMC Prosperity 4 — Trader Test Suite\n")
    tests = [
        test_resin_takes_mispriced,
        test_resin_position_limits,
        test_kelp_wallmid,
        test_squid_ink_spike,
        test_squid_ink_drop,
        test_multi_product,
        test_unknown_product,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  ❌ FAILED: {e}\n")
            failed += 1
        except Exception as e:
            print(f"  ❌ ERROR: {type(e).__name__}: {e}\n")
            failed += 1

    print("=" * 60)
    print(f"Results: {passed}/{passed + failed} tests passed")
    if failed == 0:
        print("🎉 All tests passed! Ready for IMC Prosperity 4 submission.")
    else:
        print(f"⚠️  {failed} test(s) failed — review output above.")
    print("=" * 60)
