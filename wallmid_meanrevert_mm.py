from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict, Any
import json
import math

# ─────────────────────────────────────────────────────────────────────────────
# IMC Prosperity 4 — Maximum Profit Trading Algorithm
#
# Product-specific strategies based on top Prosperity 3 team approaches:
#   • Rainforest Resin  → Fixed fair-value market making  (~39K/round)
#   • Kelp              → WallMid adaptive market making   (~5K/round)
#   • Squid Ink         → Volatility spike mean reversion  (~8K/round)
# ─────────────────────────────────────────────────────────────────────────────

# Position limits per product (enforced by the exchange)
POSITION_LIMITS = {
    "RAINFOREST_RESIN": 50,
    "KELP": 50,
    "SQUID_INK": 50,
}

# Rainforest Resin: fixed fair value
RESIN_FAIR_VALUE = 10_000
RESIN_TAKE_EDGE = 1          # Take orders at fair ± this edge
RESIN_MAKE_EDGE = 2          # Post passive orders at fair ± this edge
RESIN_MAKE_EDGE_WIDE = 4     # Wider edge when inventory is skewed
RESIN_FLATTEN_THRESHOLD = 40 # Flatten inventory above this

# Kelp: adaptive market making
KELP_MAKE_EDGE = 1           # Post passive orders at WallMid ± this
KELP_MAKE_EDGE_WIDE = 2      # Wider when inventory is high
KELP_FLATTEN_THRESHOLD = 40  # Flatten above this

# Squid Ink: mean reversion
SQUID_WINDOW = 10            # Rolling window for Z-score
SQUID_ENTRY_Z = 2.5          # Enter position when |Z| > this
SQUID_EXIT_Z = 0.5           # Exit position when |Z| < this
SQUID_MAX_TRADE_SIZE = 8     # Max lots per trade


class Trader:
    """
    Main trader class. The exchange calls run() on each timestep.
    State is persisted between timesteps via traderData (JSON string).
    """

    def bid(self):
        return 0

    def run(self, state: TradingState):
        # ── Parse persisted state ────────────────────────────────────────
        try:
            trader_data = json.loads(state.traderData) if state.traderData else {}
        except (json.JSONDecodeError, TypeError):
            trader_data = {}

        result: Dict[str, List[Order]] = {}
        conversions = 0

        # ── Run each product's strategy ──────────────────────────────────
        for product in state.order_depths:
            od: OrderDepth = state.order_depths[product]

            if not od.buy_orders or not od.sell_orders:
                continue

            pos = state.position.get(product, 0)
            limit = POSITION_LIMITS.get(product, 20)

            if product == "RAINFOREST_RESIN":
                orders, trader_data = self._trade_resin(
                    product, od, pos, limit, trader_data
                )
            elif product == "KELP":
                orders, trader_data = self._trade_kelp(
                    product, od, pos, limit, trader_data
                )
            elif product == "SQUID_INK":
                orders, trader_data = self._trade_squid_ink(
                    product, od, pos, limit, trader_data, state
                )
            else:
                # Fallback: generic market making around mid
                orders, trader_data = self._trade_generic(
                    product, od, pos, limit, trader_data
                )

            if orders:
                result[product] = orders

        return result, conversions, json.dumps(trader_data)

    # ═════════════════════════════════════════════════════════════════════
    #  STRATEGY 1: RAINFOREST RESIN — Fixed Fair Value Market Making
    # ═════════════════════════════════════════════════════════════════════
    def _trade_resin(
        self,
        product: str,
        od: OrderDepth,
        pos: int,
        limit: int,
        trader_data: dict,
    ) -> tuple:
        orders: List[Order] = []
        fair = RESIN_FAIR_VALUE  # 10,000

        # ── PHASE 1: Take mispriced orders ───────────────────────────────
        # Buy any sells at or below (fair - edge)
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price > fair - RESIN_TAKE_EDGE:
                break
            ask_vol = abs(od.sell_orders[ask_price])
            can_buy = limit - pos
            qty = min(ask_vol, can_buy)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                pos += qty

        # Sell any buys at or above (fair + edge)
        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price < fair + RESIN_TAKE_EDGE:
                break
            bid_vol = od.buy_orders[bid_price]
            can_sell = limit + pos  # pos is signed; can sell up to limit+pos
            qty = min(bid_vol, can_sell)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                pos -= qty

        # ── PHASE 2: Post passive orders (make) ─────────────────────────
        # Determine edge based on inventory skew
        make_edge = RESIN_MAKE_EDGE
        if abs(pos) > RESIN_FLATTEN_THRESHOLD:
            make_edge = 0  # Flatten: trade at fair value
        elif abs(pos) > limit * 0.6:
            make_edge = RESIN_MAKE_EDGE_WIDE if pos < 0 else RESIN_MAKE_EDGE

        # Bid (buy) side — skew down if long
        bid_price = fair - make_edge
        if pos > limit * 0.5:
            bid_price = fair - RESIN_MAKE_EDGE_WIDE  # Widen bid when long
        can_buy = limit - pos
        if can_buy > 0:
            bid_size = min(can_buy, 15)
            orders.append(Order(product, bid_price, bid_size))

        # Ask (sell) side — skew up if short
        ask_price = fair + make_edge
        if pos < -limit * 0.5:
            ask_price = fair + RESIN_MAKE_EDGE_WIDE  # Widen ask when short
        can_sell = limit + pos
        if can_sell > 0:
            ask_size = min(can_sell, 15)
            orders.append(Order(product, ask_price, -ask_size))

        return orders, trader_data

    # ═════════════════════════════════════════════════════════════════════
    #  STRATEGY 2: KELP — WallMid Adaptive Market Making
    # ═════════════════════════════════════════════════════════════════════
    def _trade_kelp(
        self,
        product: str,
        od: OrderDepth,
        pos: int,
        limit: int,
        trader_data: dict,
    ) -> tuple:
        orders: List[Order] = []

        # ── Compute WallMid ──────────────────────────────────────────────
        # The "wall" is the price level with the deepest liquidity on each side.
        # This is a better fair value estimate than raw mid-price.
        wall_bid = max(od.buy_orders.keys(), key=lambda p: od.buy_orders[p])
        wall_ask = min(od.sell_orders.keys(), key=lambda p: abs(od.sell_orders[p]))

        # If multiple levels have the same volume, use the one closest to mid
        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())

        # Find deepest volume levels
        max_bid_vol = max(od.buy_orders.values())
        max_ask_vol = max(abs(v) for v in od.sell_orders.values())

        # Wall = level with max volume; if tie, closest to center
        wall_bid_candidates = [p for p, v in od.buy_orders.items() if v == max_bid_vol]
        wall_ask_candidates = [p for p, v in od.sell_orders.items() if abs(v) == max_ask_vol]
        wall_bid = max(wall_bid_candidates)  # highest price among max-vol bids
        wall_ask = min(wall_ask_candidates)  # lowest price among max-vol asks

        wall_mid = (wall_bid + wall_ask) / 2
        spread = best_ask - best_bid

        # ── PHASE 1: Take mispriced orders ───────────────────────────────
        for ask_price in sorted(od.sell_orders.keys()):
            if ask_price >= wall_mid:
                break
            ask_vol = abs(od.sell_orders[ask_price])
            can_buy = limit - pos
            qty = min(ask_vol, can_buy)
            if qty > 0:
                orders.append(Order(product, ask_price, qty))
                pos += qty

        for bid_price in sorted(od.buy_orders.keys(), reverse=True):
            if bid_price <= wall_mid:
                break
            bid_vol = od.buy_orders[bid_price]
            can_sell = limit + pos
            qty = min(bid_vol, can_sell)
            if qty > 0:
                orders.append(Order(product, bid_price, -qty))
                pos -= qty

        # ── PHASE 2: Post passive orders ─────────────────────────────────
        # Skew quotes based on inventory to encourage rebalancing
        inventory_ratio = pos / limit if limit > 0 else 0

        # Bid side
        bid_edge = KELP_MAKE_EDGE
        if pos > KELP_FLATTEN_THRESHOLD:
            bid_edge = KELP_MAKE_EDGE_WIDE + 1  # Pull bid down when very long
        elif pos > limit * 0.5:
            bid_edge = KELP_MAKE_EDGE_WIDE

        bid_price = int(wall_mid - bid_edge)
        can_buy = limit - pos
        if can_buy > 0:
            bid_size = min(can_buy, max(1, int(15 * (1 - inventory_ratio))))
            orders.append(Order(product, bid_price, bid_size))

        # Ask side
        ask_edge = KELP_MAKE_EDGE
        if pos < -KELP_FLATTEN_THRESHOLD:
            ask_edge = KELP_MAKE_EDGE_WIDE + 1  # Push ask up when very short
        elif pos < -limit * 0.5:
            ask_edge = KELP_MAKE_EDGE_WIDE

        ask_price = int(wall_mid + ask_edge)
        can_sell = limit + pos
        if can_sell > 0:
            ask_size = min(can_sell, max(1, int(15 * (1 + inventory_ratio))))
            orders.append(Order(product, ask_price, -ask_size))

        return orders, trader_data

    # ═════════════════════════════════════════════════════════════════════
    #  STRATEGY 3: SQUID INK — Volatility Spike Mean Reversion
    # ═════════════════════════════════════════════════════════════════════
    def _trade_squid_ink(
        self,
        product: str,
        od: OrderDepth,
        pos: int,
        limit: int,
        trader_data: dict,
        state: TradingState,
    ) -> tuple:
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        mid = (best_bid + best_ask) / 2

        # ── Update rolling price history ─────────────────────────────────
        history_key = f"{product}_history"
        history = trader_data.get(history_key, [])
        history.append(mid)
        if len(history) > SQUID_WINDOW:
            history = history[-SQUID_WINDOW:]
        trader_data[history_key] = history

        # ── Track daily high/low for bot detection ───────────────────────
        day_key = f"{product}_day"
        day_data = trader_data.get(day_key, {"high": mid, "low": mid})

        # Reset daily tracking if timestamp wraps (new day starts at 0)
        if state.timestamp == 0:
            day_data = {"high": mid, "low": mid}
        else:
            day_data["high"] = max(day_data["high"], mid)
            day_data["low"] = min(day_data["low"], mid)
        trader_data[day_key] = day_data

        # Need enough data for statistics
        if len(history) < SQUID_WINDOW:
            return orders, trader_data

        # ── Compute Z-score ──────────────────────────────────────────────
        mean_price = sum(history) / len(history)
        variance = sum((p - mean_price) ** 2 for p in history) / len(history)
        std_price = math.sqrt(variance) if variance > 0 else 1.0

        z_score = (mid - mean_price) / std_price

        # ── Trade on Z-score signals ─────────────────────────────────────
        # Check if price is near daily extreme (potential bot signal)
        near_daily_low = mid <= day_data["low"] * 1.002
        near_daily_high = mid >= day_data["high"] * 0.998

        if z_score > SQUID_ENTRY_Z or near_daily_high:
            # Price spiked UP → expect reversion DOWN → SELL
            can_sell = limit + pos
            size = min(SQUID_MAX_TRADE_SIZE, can_sell)
            if size > 0:
                orders.append(Order(product, best_bid, -size))
                # Also place a limit ask slightly above for more edge
                if can_sell - size > 0:
                    extra = min(SQUID_MAX_TRADE_SIZE, can_sell - size)
                    orders.append(Order(product, best_ask, -extra))

        elif z_score < -SQUID_ENTRY_Z or near_daily_low:
            # Price dropped → expect reversion UP → BUY
            can_buy = limit - pos
            size = min(SQUID_MAX_TRADE_SIZE, can_buy)
            if size > 0:
                orders.append(Order(product, best_ask, size))
                # Also place a limit bid slightly below for more edge
                if can_buy - size > 0:
                    extra = min(SQUID_MAX_TRADE_SIZE, can_buy - size)
                    orders.append(Order(product, best_bid, extra))

        elif abs(z_score) < SQUID_EXIT_Z and abs(pos) > 0:
            # Z-score returned to normal → flatten position
            if pos > 0:
                # Sell to flatten
                size = min(pos, SQUID_MAX_TRADE_SIZE)
                orders.append(Order(product, best_bid, -size))
            elif pos < 0:
                # Buy to flatten
                size = min(abs(pos), SQUID_MAX_TRADE_SIZE)
                orders.append(Order(product, best_ask, size))

        else:
            # ── Light market making when no signal ───────────────────────
            spread = best_ask - best_bid
            if spread >= 3 and abs(pos) < limit * 0.6:
                mm_size = min(3, limit - abs(pos))
                if mm_size > 0:
                    orders.append(Order(product, best_bid + 1, mm_size))
                    orders.append(Order(product, best_ask - 1, -mm_size))

        return orders, trader_data

    # ═════════════════════════════════════════════════════════════════════
    #  FALLBACK: Generic market making for unknown products
    # ═════════════════════════════════════════════════════════════════════
    def _trade_generic(
        self,
        product: str,
        od: OrderDepth,
        pos: int,
        limit: int,
        trader_data: dict,
    ) -> tuple:
        orders: List[Order] = []

        best_bid = max(od.buy_orders.keys())
        best_ask = min(od.sell_orders.keys())
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid

        # Simple market making around mid
        if spread >= 2:
            can_buy = limit - pos
            can_sell = limit + pos

            if can_buy > 0:
                size = min(5, can_buy)
                orders.append(Order(product, int(mid - 1), size))

            if can_sell > 0:
                size = min(5, can_sell)
                orders.append(Order(product, int(mid + 1), -size))

        return orders, trader_data
