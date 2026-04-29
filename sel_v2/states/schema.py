"""
Schema for sel v2.0 state machine.

StateLabel: 6 states (v2 uses single Surging, not Up/Down like v1)
BarFeatures: per-bar feature vector feeding state conditions
ConditionResult: tristate result (True/False/None) for each state check
StateRecord: one row → v2_state_history
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


class StateLabel(str, enum.Enum):
    COILING = "Coiling"
    SURGING = "Surging"
    DRIFTING_CALM = "Drifting_Calm"
    DRIFTING_CHARGED = "Drifting_Charged"
    CRITICAL = "Critical"
    CASCADE = "Cascade"


# v2.0 §5.8 priority order (higher = evaluated first)
STATE_PRIORITY: dict[StateLabel, int] = {
    StateLabel.CASCADE: 6,
    StateLabel.CRITICAL: 5,
    StateLabel.SURGING: 4,
    StateLabel.COILING: 3,
    StateLabel.DRIFTING_CHARGED: 2,
    StateLabel.DRIFTING_CALM: 1,
}

# v2.0 §5 minimum dwell in 4H bars
MIN_DWELL_BARS: dict[StateLabel, int] = {
    StateLabel.COILING: 6,
    StateLabel.SURGING: 2,
    StateLabel.DRIFTING_CALM: 8,
    StateLabel.DRIFTING_CHARGED: 4,
    StateLabel.CRITICAL: 1,
    StateLabel.CASCADE: 0,  # no lower limit
}


@dataclass
class ConditionResult:
    """Tristate result for a state entry condition check."""
    met: Optional[bool]            # True / False / None (data unavailable)
    none_conditions: list[str] = field(default_factory=list)   # STUB sub-conditions
    false_conditions: list[str] = field(default_factory=list)  # explicitly False
    details: dict = field(default_factory=dict)               # diagnostic data


@dataclass
class BarFeatures:
    """
    Feature vector for one 4H bar close.

    All Optional[x] fields are STUB placeholders — set to None when the
    underlying data source (LOB, OI, funding, liquidations) is unavailable.
    The state conditions treat None as "data missing" (not False).
    """
    timestamp: datetime
    bar_index: int           # position in the bars array (0-based)
    close: float
    log_price: float

    # ── σ-based (computable from price) ──────────────────────────────────────
    sigma_4h: float                        # rolling std of log_returns last 180 bars
    sigma_pctile: float                    # rank of sigma_4h in last 180 sigma values [0,1]
    sigma_monotone_3bar: Optional[bool]    # True if last 3 bar sigmas strictly increasing

    # ── Price action (computable from price) ─────────────────────────────────
    price_breakout_up: Optional[bool]    # close > max(close[i-6:i]) — 24h high breach
    price_breakout_down: Optional[bool]  # close < min(close[i-6:i]) — 24h low breach

    # ── LOB (STUB — no LOB collector yet) ────────────────────────────────────
    entropy_4h: Optional[float] = None          # Shannon entropy of LOB
    entropy_pctile: Optional[float] = None      # rank in 30-day history
    entropy_variance_rising: Optional[bool] = None  # entropy variance trending up

    # ── OI / Funding (STUB) ──────────────────────────────────────────────────
    oi_change_rate: Optional[float] = None       # OI % change per 4H bar
    oi_change_rate_pctile: Optional[float] = None
    oi_acceleration: Optional[bool] = None       # OI rate of change increasing
    funding_rate: Optional[float] = None
    funding_pctile: Optional[float] = None       # |funding| rank in 60-day history
    funding_persistent: Optional[bool] = None    # same sign for 6+ consecutive bars

    # ── Liquidity / Cascade triggers (STUB) ──────────────────────────────────
    lob_depth_pctile: Optional[float] = None    # LOB depth rank in 7-day history
    liquidation_pulse: Optional[bool] = None    # liquidation rate > 5× 30-day mean
    cross_exchange_spread: Optional[float] = None  # spread %

    # ── OFI (STUB) ────────────────────────────────────────────────────────────
    ofi_cumulative_pctile: Optional[float] = None  # 7-day rank

    # ── Hawkes H2 (computable from price) ────────────────────────────────────
    hawkes_br: Optional[float] = None          # branching ratio α/β from rolling MLE
    hawkes_br_threshold: float = 0.85          # from v2_strategy_params

    # ── TDA1 (computable from price) ─────────────────────────────────────────
    tda_l1: Optional[float] = None             # Persistence Landscape L^1 norm
    tda_l1_pctile: Optional[float] = None      # rank in rolling 90-day window
    tda_l1_threshold: float = 0.000097         # from v2_strategy_params
    tda_l1_monotone_3bar: Optional[bool] = None  # last 3 bars strictly increasing

    # ── Cold start ────────────────────────────────────────────────────────────
    cold_start: bool = False  # True during first 180 bars (σ window not filled)


@dataclass
class StateRecord:
    """One row in v2_state_history."""
    timestamp: datetime
    state: StateLabel
    sub_state: Optional[str]
    state_features: dict            # JSON-serializable; all feature values + diagnostics
    transition_from: Optional[StateLabel]
    transition_via: Optional[str]   # one of 7 transition vocabulary words
    duration_4h: Optional[int]      # bars spent in current state so far
    cold_start: bool                # True if any condition returned None
    none_reason: Optional[str]      # description of what's None
    is_legal: bool = True           # False for direct illegal state jumps
