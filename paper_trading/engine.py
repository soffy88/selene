"""Decision engine — maps (state, prev_state, position, account) → Decision."""
from datetime import datetime
from typing import Optional

from decision.config import DecisionConfig
from paper_trading.schema import (
    AccountState,
    Decision,
    DecisionAction,
    Position,
)


class DecisionEngine:
    def __init__(self, config: DecisionConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    def decide(
        self,
        current_state: Optional[str],
        previous_state: Optional[str],
        current_position: Optional[Position],
        account: AccountState,
        bar_time: datetime,
        current_price: float,
    ) -> Decision:
        """Apply decision matrix to determine the action.

        Logic:
        1. current_state is None (cold start) → NO_ACTION
        2. Look up rule via config.find_rule(current_state, previous_state)
        3. No rule found → HOLD (default)
        4. Compute target_size_usdt from position_multiplier
        5. Special case: hold with multiplier == 0.5 → target = current * 0.5
        """
        # Cold start
        if current_state is None:
            return Decision(
                time=bar_time,
                symbol=self._symbol(current_position),
                action=DecisionAction.NO_ACTION,
                rule_id="cold_start",
                config_hash=self.config.config_hash,
                current_state=None,
                previous_state=previous_state,
                position_multiplier=None,
                target_size_usdt=None,
                reason="Cold start — no state available",
            )

        rule = self.config.find_rule(current_state, previous_state)

        if rule is None:
            return Decision(
                time=bar_time,
                symbol=self._symbol(current_position),
                action=DecisionAction.HOLD,
                rule_id="default_hold",
                config_hash=self.config.config_hash,
                current_state=current_state,
                previous_state=previous_state,
                position_multiplier=None,
                target_size_usdt=(
                    current_position.current_size_usdt if current_position else None
                ),
                reason=f"No rule matched ({current_state} ← {previous_state}); defaulting to HOLD",
            )

        action = DecisionAction(rule.action)
        target_size = self._compute_target_size(
            rule.position_multiplier, account, current_position
        )

        reason = rule.notes or f"Rule {rule.id} matched"

        return Decision(
            time=bar_time,
            symbol=self._symbol(current_position),
            action=action,
            rule_id=rule.id,
            config_hash=self.config.config_hash,
            current_state=current_state,
            previous_state=previous_state,
            position_multiplier=rule.position_multiplier,
            target_size_usdt=target_size,
            reason=reason,
        )

    # ------------------------------------------------------------------
    def _compute_target_size(
        self,
        multiplier: Optional[float],
        account: AccountState,
        current_position: Optional[Position],
    ) -> Optional[float]:
        """Translate a position_multiplier into a USDT target size.

        multiplier=None  → keep current size (hold)
        multiplier=0.0   → 0 (close)
        multiplier 0<x<1 → fraction of current position (reduce)
        multiplier=1.0   → base_size_pct × NAV
        multiplier>1.0   → base_size_pct × NAV × multiplier
        """
        if multiplier is None:
            return current_position.current_size_usdt if current_position else None
        if multiplier == 0.0:
            return 0.0
        if current_position and 0 < multiplier < 1.0:
            return current_position.current_size_usdt * multiplier
        return account.nav_usdt * self.config.position.base_size_pct * multiplier

    # ------------------------------------------------------------------
    @staticmethod
    def _symbol(position: Optional[Position]) -> str:
        return position.symbol if position else "UNKNOWN"
