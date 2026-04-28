"""DecisionTrail record + DecisionTrailBuilder — Wave 4."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from decision.config import DecisionConfig
from paper_trading.schema import AccountState, Decision, SimulatedFill
from paper_trading.risk import RiskCheckResult
from sel_engine.paper_interface.schema import StateOutput


# ---------------------------------------------------------------------------
# DecisionTrail dataclass
# ---------------------------------------------------------------------------

@dataclass
class DecisionTrail:
    """Complete decision context for one 1H bar. Immutable once created."""

    # Time
    time: datetime
    symbol: str
    config_hash: str             # YAML config version
    using_claude_default: bool   # True when using Claude-derived rules (not Wiki-confirmed)

    # State context
    current_state: Optional[str]
    previous_state: Optional[str]
    state_history: list          # last N states (oldest first)
    state_reason: str

    # Feature snapshot (complete at decision time)
    close: Optional[float]
    sigma_p_24h: Optional[float]
    H: Optional[float]
    TF: Optional[float]
    OI: Optional[float]
    funding_rate: Optional[float]
    LV: Optional[float]
    absorption_ratio: Optional[float]
    OI_hurst: Optional[float]
    feature_completeness: float

    # Account state at decision time
    nav_usdt: float
    position_side: Optional[str]
    position_size_usdt: float
    unrealized_pnl_usdt: float
    realized_pnl_usdt: float

    # Decision result
    proposed_action: str         # from DecisionEngine (before risk gate)
    final_action: str            # after risk gate
    rule_id: str                 # which YAML rule matched
    risk_triggered: Optional[str]  # None if risk passed, else rule name
    risk_details: str

    # Fill result (None if no fill this bar)
    fill_price: Optional[float]
    fill_size_usdt: Optional[float]
    fill_fee_usdt: Optional[float]
    realized_pnl_this_bar: Optional[float]

    # Risk check summary
    risk_check_passed: bool

    # None-state metadata
    state_none_reason: Optional[str] = None  # StateNoneReason.value when state=None

    # Rule 2 diagnostics (populated when signal_lag fires)
    risk_rule_2_subtype: Optional[str] = None       # "missing_data" | "no_match"
    state_none_reason_in_lag: Optional[dict] = None  # {"missing_data": N, "no_match": N, "cold_start": N}


# ---------------------------------------------------------------------------
# DecisionTrailBuilder
# ---------------------------------------------------------------------------

class DecisionTrailBuilder:
    """Accumulates per-bar state and assembles DecisionTrail records."""

    def __init__(self, symbol: str, config: DecisionConfig) -> None:
        self._symbol = symbol
        self._config = config
        self._state_history: deque = deque(maxlen=12)  # last 12 states

    # ------------------------------------------------------------------
    def record_state(self, state: Optional[str]) -> None:
        """Call every bar to maintain state_history."""
        self._state_history.append(state or "None")

    # ------------------------------------------------------------------
    def build(
        self,
        bar_time: datetime,
        state_output: StateOutput,
        proposed_decision: Decision,
        risk_result: RiskCheckResult,
        fill: Optional[SimulatedFill],
        realized_pnl: Optional[float],
        account_before: AccountState,
    ) -> DecisionTrail:
        """Assemble all inputs into a single DecisionTrail record."""
        # Determine final action
        if risk_result.force_action is not None:
            final_action = risk_result.force_action.value
        else:
            final_action = proposed_decision.action.value

        # using_claude_default: True when config author is "claude-default"
        # (i.e. not yet Wiki-confirmed). We read from the YAML meta.
        using_claude_default = True  # conservative default; override if config carries meta
        # The DecisionConfig doesn't currently surface the author field, but the YAML
        # metadata notes "claude-default" as author — treat as True for all v0.x configs.

        # Feature completeness: fraction of named features that are non-None
        feature_fields = [
            state_output.close,
            state_output.sigma_p_24h,
            state_output.H,
            state_output.TF,
            state_output.OI,
            state_output.funding_rate,
            state_output.LV,
        ]
        # absorption_ratio and OI_hurst aren't on StateOutput but are in DecisionTrail
        # They will be None when StateOutput doesn't carry them
        absorption_ratio: Optional[float] = getattr(state_output, "absorption_ratio", None)
        OI_hurst: Optional[float] = getattr(state_output, "OI_hurst", None)

        total_features = len(feature_fields) + 2  # +2 for absorption_ratio, OI_hurst
        non_none = sum(1 for f in feature_fields if f is not None)
        non_none += (1 if absorption_ratio is not None else 0)
        non_none += (1 if OI_hurst is not None else 0)
        feature_completeness = non_none / total_features

        return DecisionTrail(
            time=bar_time,
            symbol=self._symbol,
            config_hash=self._config.config_hash,
            using_claude_default=using_claude_default,
            current_state=state_output.state,
            previous_state=state_output.transition_from,
            state_history=list(self._state_history),
            state_reason=state_output.reason,
            close=state_output.close,
            sigma_p_24h=state_output.sigma_p_24h,
            H=state_output.H,
            TF=state_output.TF,
            OI=state_output.OI,
            funding_rate=state_output.funding_rate,
            LV=state_output.LV,
            absorption_ratio=absorption_ratio,
            OI_hurst=OI_hurst,
            feature_completeness=feature_completeness,
            nav_usdt=account_before.nav_usdt,
            position_side=account_before.position_side,
            position_size_usdt=account_before.position_size_usdt,
            unrealized_pnl_usdt=account_before.unrealized_pnl_usdt,
            realized_pnl_usdt=account_before.realized_pnl_usdt,
            proposed_action=proposed_decision.action.value,
            final_action=final_action,
            rule_id=proposed_decision.rule_id,
            risk_triggered=risk_result.triggered_rule,
            risk_details=risk_result.details,
            fill_price=fill.fill_price if fill is not None else None,
            fill_size_usdt=fill.fill_size_usdt if fill is not None else None,
            fill_fee_usdt=fill.fee_usdt if fill is not None else None,
            realized_pnl_this_bar=realized_pnl,
            risk_check_passed=risk_result.passed,
            state_none_reason=state_output.none_reason,
            risk_rule_2_subtype=risk_result.rule_2_subtype,
            state_none_reason_in_lag=risk_result.none_reasons_in_lag,
        )
