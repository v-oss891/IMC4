from datamodel import OrderDepth, TradingState, Order
from typing import List, Dict
import json
import numpy as np
from typing import Any

class Trader:
    def bid(self):
        return 0

    def run(self, state: TradingState):
        print("Monte Carlo Trader active")

        state_data = json.loads(state.traderData or '{}')
        result = {}
        conversions = 0
        trader_data = {}

        for product in state.order_depths:
            od = state.order_depths[product]
            orders = []

            if not od.buy_orders or not od.sell_orders:
                continue

            best_bid = max(od.buy_orders)
            best_ask = min(od.sell_orders)
            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid

            # Vol estimate
            bid_vol = sum(od.buy_orders.values())
            ask_vol = sum(abs(v) for v in od.sell_orders.values())
            vol = spread / mid * np.sqrt(bid_vol + ask_vol) / 10  # proxy

            # MC simulation: GBM paths
            n_paths = 100
            dt = 0.01
            n_steps = 20
            paths = np.cumsum(np.random.normal(0, vol * np.sqrt(dt), (n_paths, n_steps)), axis=1) * mid
            mc_price = np.median(paths[:, -1])

            pos = state.position.get(product, 0)
            size = 1 if abs(pos) < 5 else 2

            dev = (mc_price - mid) / spread

            if dev > 0.1:
                p = best_ask * 0.999
                orders.append(Order(product, int(p), -size))
            elif dev < -0.1:
                p = best_bid * 1.001
                orders.append(Order(product, int(p), size))

            # MM
            if abs(pos) < 10:
                orders.append(Order(product, int(best_bid + spread * 0.25), 1))
                orders.append(Order(product, int(best_ask - spread * 0.25), -1))

            if orders:
                result[product] = orders

            trader_data[product] = {'vol': float(vol), 'mc_price': float(mc_price)}

        traderData = json.dumps(trader_data)
        return result, conversions, traderData

