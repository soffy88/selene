"""
CryptoWatch v4 — Capital Allocation
Kelly Criterion + Risk Parity for position sizing.
These are the formulas that determine HOW MUCH to bet, not whether to bet.
"""
import logging
import math
from typing import Optional

from shared.quant import funding_adjusted_cost

logger = logging.getLogger(__name__)


# ── Kelly Criterion ───────────────────────────────────────────────────────────

def kelly_fraction(
    win_probability: float,
    risk_reward:     float,
    fraction:        float = 0.5,   # Half Kelly by default (ADR-103)
    cost:            float = 0.0,   # fee + expected slippage, fraction (e.g. 0.0008 = 0.08%)
) -> float:
    """
    Cost-adjusted Kelly.

    Full Kelly: f* = (p*b - q) / b
    Cost-adjusted: effective_b = b - cost, effective_q_payoff = 1 + cost
        f* = (p * (b - cost) - q * (1 + cost)) / (b - cost)

    The adjustment ensures that when expected edge is fully absorbed by
    fees and slippage, Kelly returns 0 instead of over-betting.

    Returns the fraction of capital to risk (0.0 – 1.0).
    Negative or zero edge → 0 (don't bet).
    """
    if risk_reward <= 0 or win_probability <= 0:
        return 0.0

    p = win_probability
    q = 1.0 - p
    b_net = risk_reward - cost
    if b_net <= 0:
        return 0.0

    full_kelly = (p * b_net - q * (1.0 + cost)) / b_net
    adjusted   = full_kelly * fraction
    return max(0.0, round(adjusted, 6))


def position_size_from_kelly(
    account_equity:    float,
    kelly_f:           float,         # from kelly_fraction()
    entry_price:       float,
    stop_price:        float,
    max_position_pct:  float = 0.10,  # hard cap: 10% of equity per position
    drawdown_scalar:   float = 1.0,   # shrinks with portfolio drawdown level
) -> float:
    """
    Convert Kelly fraction to actual position quantity.

    Kelly tells us what fraction of equity to RISK.
    We then back out the quantity from the stop distance.

    quantity = (equity × kelly_f × drawdown_scalar) / stop_distance
    """
    stop_dist = abs(entry_price - stop_price)
    if stop_dist == 0 or entry_price == 0:
        return 0.0

    risk_amount   = account_equity * kelly_f * drawdown_scalar
    quantity      = risk_amount / stop_dist

    # Apply max position cap
    max_notional  = account_equity * max_position_pct
    max_quantity  = max_notional / entry_price
    quantity      = min(quantity, max_quantity)

    return round(quantity, 8)


# ── Risk Parity ───────────────────────────────────────────────────────────────

def risk_parity_weights(volatilities: dict[str, float]) -> dict[str, float]:
    """
    Risk Parity: allocate capital so each strategy contributes equally to risk.
    w_i = (1/σ_i) / Σ(1/σ_j)

    Args:
        volatilities: {strategy_name: annualized_volatility}

    Returns:
        {strategy_name: capital_weight}  (sums to 1.0)
    """
    if not volatilities:
        return {}

    # Replace zero vols with a small floor to avoid division by zero
    vols = {k: max(v, 0.001) for k, v in volatilities.items()}
    inv_vols = {k: 1.0 / v for k, v in vols.items()}
    total_inv = sum(inv_vols.values())

    if total_inv == 0:
        n = len(volatilities)
        return {k: 1.0 / n for k in volatilities}

    weights = {k: inv_v / total_inv for k, inv_v in inv_vols.items()}
    # Normalize: divide by sum, then fix residual to guarantee exact sum = 1.0
    total = sum(weights.values())
    keys = list(weights)
    normed = {k: weights[k] / total for k in keys}
    # Round to 8dp, then add residual to largest weight
    rounded = {k: round(normed[k], 8) for k in keys}
    residual = 1.0 - sum(rounded.values())
    if residual != 0.0:
        biggest = max(rounded, key=rounded.get)
        rounded[biggest] = round(rounded[biggest] + residual, 8)
    return rounded



# ── Portfolio-level sizing ────────────────────────────────────────────────────

class CapitalAllocator:
    """
    Combines Kelly sizing with Risk Parity multi-strategy allocation.
    This is the single component that determines all position sizes.
    """

    def __init__(
        self,
        total_equity:       float,
        kelly_fraction_:    float = 0.5,
        target_volatility:  float = 0.20,
        max_single_pos_pct: float = 0.10,
        round_trip_cost:    float = 0.0012,   # 0.12% = fee(2×taker 0.05%) + 0.02% slippage
    ):
        self.equity          = total_equity
        self.kelly_f         = kelly_fraction_
        self.target_vol      = target_volatility
        self.max_pos_pct     = max_single_pos_pct
        self.round_trip_cost = round_trip_cost
        self._strategy_vols: dict[str, float] = {}
        self._strategy_allocations: dict[str, float] = {}

    def update_equity(self, equity: float) -> None:
        self.equity = equity

    def update_strategy_volatility(self, strategy: str, vol: float) -> None:
        self._strategy_vols[strategy] = vol
        self._recompute_allocations()

    def _recompute_allocations(self) -> None:
        if self._strategy_vols:
            self._strategy_allocations = risk_parity_weights(self._strategy_vols)

    def compute_position_size(
        self,
        strategy:        str,
        win_probability: float,
        risk_reward:     float,
        entry_price:     float,
        stop_price:      float,
        drawdown_scalar: float = 1.0,
        funding_rate:    float | None = None,
        side:            str = "LONG",
        hold_hours:      float = 24.0,
    ) -> dict:
        """
        Full pipeline: Risk Parity allocation → Kelly fraction → quantity.
        Returns dict with all intermediate values for auditability.
        """
        # Strategy allocation from Risk Parity
        strategy_alloc = self._strategy_allocations.get(strategy, 1.0 / max(len(self._strategy_vols), 1))
        strategy_capital = self.equity * strategy_alloc

        # Cost = round-trip fees/slippage + expected perpetual funding drag over the hold.
        cost = funding_adjusted_cost(self.round_trip_cost, funding_rate, hold_hours, side)

        # Kelly fraction within strategy allocation (cost-adjusted)
        kf = kelly_fraction(
            win_probability, risk_reward, self.kelly_f,
            cost=cost,
        )

        # Position size
        qty = position_size_from_kelly(
            account_equity=strategy_capital,
            kelly_f=kf,
            entry_price=entry_price,
            stop_price=stop_price,
            max_position_pct=self.max_pos_pct,
            drawdown_scalar=drawdown_scalar,
        )

        notional = qty * entry_price
        risk_usd = qty * abs(entry_price - stop_price)

        return {
            "quantity":         qty,
            "notional_usd":     round(notional, 2),
            "risk_usd":         round(risk_usd, 2),
            "risk_pct_equity":  round(risk_usd / self.equity, 6) if self.equity > 0 else 0,
            "kelly_fraction":   kf,
            "strategy_alloc":   round(strategy_alloc, 6),
            "strategy_capital": round(strategy_capital, 2),
            "drawdown_scalar":  drawdown_scalar,
            "cost_with_funding": round(cost, 6),
        }

    def get_allocations(self) -> dict:
        return {
            "weights": self._strategy_allocations,
            "total_equity": self.equity,
            "strategy_capitals": {
                k: round(self.equity * v, 2)
                for k, v in self._strategy_allocations.items()
            },
        }
