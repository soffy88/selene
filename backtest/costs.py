"""Execution cost & capacity model for the backtester (optimization item #14).

Replaces the flat 0.02% slippage + 0.0004 fee with a per-trade model:
    one-side cost = half_spread + taker_fee + impact_coeff * sqrt(notional / ADV)
The square-root term is the standard market-impact law; it makes cost grow with
trade size relative to average daily volume, which the flat constant ignored.
Round-trip cost (entry + exit) = 2 × one-side.

Capacity: a trade whose notional exceeds max_participation × ADV is over the
liquidity the venue can absorb; capacity_capped_notional() clamps it.
"""

from __future__ import annotations

import math

DEFAULT_SPREAD_BPS = 2.0  # half of this is paid per side
DEFAULT_TAKER_FEE = 0.0004  # 4 bps
DEFAULT_IMPACT_COEFF = 0.1  # sqrt-impact coefficient
DEFAULT_MAX_PARTICIPATION = 0.10  # ≤10% of ADV

# Per-symbol cost profiles (audit P2-c). A single flat spread/impact for every symbol
# over-charges deep books (BTC) and under-charges thin ones (alts), biasing net-of-cost
# backtest P&L. These are liquidity-informed PRIORS keyed by venue depth — NOT yet fitted
# from realised fills; replace with TCA-calibrated values once live-fill data exists.
# Keys are the base symbol sans separators (BTCUSDT). Unknown symbols fall back to DEFAULT_*.
SYMBOL_COST_PROFILES = {
    "BTCUSDT": {"spread_bps": 0.5, "taker_fee": 0.0004, "impact_coeff": 0.08},
    "ETHUSDT": {"spread_bps": 1.0, "taker_fee": 0.0004, "impact_coeff": 0.10},
    "SOLUSDT": {"spread_bps": 2.0, "taker_fee": 0.0005, "impact_coeff": 0.15},
}


def cost_params_for(symbol: str | None) -> dict:
    """Per-symbol {spread_bps, taker_fee, impact_coeff} for one_side_cost_pct(**...).
    Falls back to the flat DEFAULT_* profile for unknown / None symbols."""
    default = {
        "spread_bps": DEFAULT_SPREAD_BPS,
        "taker_fee": DEFAULT_TAKER_FEE,
        "impact_coeff": DEFAULT_IMPACT_COEFF,
    }
    if not symbol:
        return default
    return SYMBOL_COST_PROFILES.get(symbol.replace("-", "").upper(), default)


def one_side_cost_pct(
    notional_usd: float,
    adv_usd: float,
    *,
    spread_bps: float = DEFAULT_SPREAD_BPS,
    taker_fee: float = DEFAULT_TAKER_FEE,
    impact_coeff: float = DEFAULT_IMPACT_COEFF,
    is_maker: bool = False,
) -> float:
    """One-side execution cost as a fraction of notional."""
    half_spread = (spread_bps / 10_000.0) / 2.0
    fee = 0.0 if is_maker else taker_fee
    impact = 0.0
    if adv_usd > 0 and notional_usd > 0:
        impact = impact_coeff * math.sqrt(notional_usd / adv_usd)
    return half_spread + fee + impact


def round_trip_cost_pct(notional_usd: float, adv_usd: float, **kw) -> float:
    """Entry + exit cost as a fraction of notional."""
    return 2.0 * one_side_cost_pct(notional_usd, adv_usd, **kw)


def capacity_capped_notional(
    notional_usd: float,
    adv_usd: float,
    max_participation: float = DEFAULT_MAX_PARTICIPATION,
) -> float:
    """Clamp a trade's notional to the venue's absorbable size (max_participation×ADV).
    Returns the original notional when ADV is unknown (<=0)."""
    if adv_usd <= 0:
        return notional_usd
    return min(notional_usd, max_participation * adv_usd)
