"""DecisionReplayer — replay recorded DecisionTrail sequences with a (possibly different) config."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from decision.config import DecisionConfig
from paper_trading.engine import DecisionEngine
from paper_trading.risk import RiskGate, RiskCheckResult
from paper_trading.schema import AccountState, DecisionAction, Position, PositionSide
from paper_trading.trail import DecisionTrail, DecisionTrailBuilder

logger = logging.getLogger(__name__)


class DecisionReplayer:
    """
    Given a list of DecisionTrail records and a DecisionConfig, replays
    the decision sequence as if running from scratch.

    Used for: debugging, parameter sensitivity, config version comparison.
    """

    def __init__(self, config: DecisionConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    def replay(self, trails: list[DecisionTrail]) -> list[DecisionTrail]:
        """
        Re-run the decision engine + risk gate on each trail's feature/state snapshot.

        Returns new trails with the same input data but decisions re-computed using
        the provided config (which may differ from trail.config_hash).
        """
        if not trails:
            return []

        symbol = trails[0].symbol
        engine = DecisionEngine(self.config)
        risk_gate = RiskGate(self.config.risk, symbol)
        builder = DecisionTrailBuilder(symbol, self.config)

        result: list[DecisionTrail] = []

        for trail in trails:
            # Rebuild minimal StateOutput-like object from trail fields
            state_out = _ReplayStateOutput(trail)

            # Rebuild account snapshot from trail's recorded account state
            account_before = AccountState(
                time=trail.time,
                nav_usdt=trail.nav_usdt,
                realized_pnl_usdt=trail.realized_pnl_usdt,
                unrealized_pnl_usdt=trail.unrealized_pnl_usdt,
                position_side=trail.position_side,
                position_size_usdt=trail.position_size_usdt,
                daily_drawdown_pct=0.0,  # not stored in trail; use 0 for replay
                consecutive_losses=0,
                is_paused=False,
                pause_until=None,
                cascade_cooling=False,
            )

            # Rebuild position from trail (if any)
            current_position: Optional[Position] = None
            if trail.position_side is not None and trail.position_size_usdt > 0:
                current_position = Position(
                    symbol=symbol,
                    side=PositionSide(trail.position_side),
                    open_time=trail.time,
                    open_price=trail.close or 0.0,
                    size_usdt=trail.position_size_usdt,
                    current_size_usdt=trail.position_size_usdt,
                    unrealized_pnl_usdt=trail.unrealized_pnl_usdt,
                )

            # Record state BEFORE deciding (matches live pipeline order)
            builder.record_state(trail.current_state)

            # Re-run decision engine
            proposed_decision = engine.decide(
                current_state=trail.current_state,
                previous_state=trail.previous_state,
                current_position=current_position,
                account=account_before,
                bar_time=trail.time,
                current_price=trail.close or 0.0,
            )

            # Re-run risk gate
            risk_result = risk_gate.check(
                proposed_action=proposed_decision.action,
                current_state=trail.current_state,
                bar_time=trail.time,
                position=current_position,
                account=account_before,
                last_state_update_time=trail.time,  # no lag assumed in replay
            )

            # Preserve original fill data (replay doesn't re-simulate fills)
            # Build new trail with re-computed decisions
            new_trail = builder.build(
                bar_time=trail.time,
                state_output=state_out,
                proposed_decision=proposed_decision,
                risk_result=risk_result,
                fill=None,       # fills not replayed (would require position tracking)
                realized_pnl=None,
                account_before=account_before,
            )

            result.append(new_trail)

        return result


# ---------------------------------------------------------------------------
# Thin shim for replay — wraps a DecisionTrail as a StateOutput-compatible object
# ---------------------------------------------------------------------------

class _ReplayStateOutput:
    """Duck-typed StateOutput from a recorded DecisionTrail."""

    def __init__(self, trail: DecisionTrail) -> None:
        self.bar_time = trail.time
        self.symbol = trail.symbol
        self.state = trail.current_state
        self.state_enum = None
        self.direction = None
        self.is_cold_start = trail.current_state is None
        self.confidence = 1.0
        self.reason = trail.state_reason
        self.is_legal_transition = True
        self.transition_from = trail.previous_state
        self.health_warning = False
        self.close = trail.close
        self.sigma_p_24h = trail.sigma_p_24h
        self.H = trail.H
        self.TF = trail.TF
        self.OI = trail.OI
        self.funding_rate = trail.funding_rate
        self.LV = trail.LV
        self.absorption_ratio = trail.absorption_ratio
        self.OI_hurst = trail.OI_hurst
        self.feature_completeness = trail.feature_completeness
        self.none_reason = trail.state_none_reason
