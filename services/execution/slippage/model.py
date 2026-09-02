"""
CryptoWatch v4 — Three-Component Slippage Model
Solves v3 D2: execution gap between backtest and live trading.

Components:
  1. Bid-Ask Spread cost (always present)
  2. Market Impact (large orders move the price)
  3. Timing Slippage (price drift during order processing latency)
"""

import math
from dataclasses import dataclass


@dataclass
class SlippageEstimate:
    spread: float  # half-spread cost as pct
    impact: float  # market impact as pct
    timing: float  # timing slippage as pct
    total: float  # total expected slippage as pct
    total_usd: float  # absolute USD cost
    recommendation: str  # "LIMIT" or "MARKET"


class SlippageModel:
    """
    Three-component slippage model.
    Used in both live execution (pre-trade estimate) and backtesting.

    Component 1 — Bid-Ask Spread:
      Cost = spread / 2 (assuming fill at mid for limit, or at ask/bid for market)

    Component 2 — Market Impact (Square Root Model):
      Impact = volatility × sqrt(order_size / daily_volume)
      This captures the price pressure from our own order.

    Component 3 — Timing Slippage:
      Drift during the ~150ms latency from decision to fill.
      avg_drift_per_ms × 150ms
    """

    # Historical estimate: per-millisecond price drift for average crypto asset
    AVG_DRIFT_PCT_PER_MS = 0.000002  # 0.0002% per ms → ~0.03% per 150ms

    def estimate(
        self,
        notional_usd: float,
        price_volatility: float,  # recent annualized vol as decimal (0.80 = 80%)
        daily_volume_usd: float,
        spread_pct: float,  # current bid-ask spread as pct
        order_type: str,  # "MARKET" or "LIMIT"
        latency_ms: float = 150.0,
    ) -> SlippageEstimate:
        # 1. Spread cost (limit orders pay half-spread on entry; market pays full)
        if order_type == "LIMIT":
            spread_cost = spread_pct / 2 / 100
        else:
            spread_cost = spread_pct / 100

        # 2. Market impact — Square Root Model
        if daily_volume_usd > 0:
            daily_vol_pct = price_volatility / math.sqrt(252)  # daily vol
            participation = notional_usd / daily_volume_usd
            impact = daily_vol_pct * math.sqrt(participation)
        else:
            impact = 0.001  # fallback: 0.1%

        # 3. Timing slippage
        timing = self.AVG_DRIFT_PCT_PER_MS * latency_ms

        total = spread_cost + impact + timing
        total_usd = notional_usd * total

        # Recommend LIMIT if total cost exceeds 0.1% (save on spread)
        recommendation = "LIMIT" if total > 0.001 else "MARKET"

        return SlippageEstimate(
            spread=round(spread_cost * 100, 4),
            impact=round(impact * 100, 4),
            timing=round(timing * 100, 4),
            total=round(total * 100, 4),
            total_usd=round(total_usd, 2),
            recommendation=recommendation,
        )

    def adjust_entry_price(self, price: float, side: str, slippage_pct: float) -> float:
        """Adjust expected fill price for slippage (used in backtesting)."""
        adj = price * (1 + slippage_pct / 100)
        if side == "BUY":
            return round(adj, 8)  # buy higher than expected
        else:
            return round(price * (1 - slippage_pct / 100), 8)  # sell lower
