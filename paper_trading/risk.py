"""RiskGate — pre-execution risk checkpoint for the paper trading engine.

Priority order (highest → lowest):
1. CASCADE state → always CLOSE (overrides even 24H pause)
2. Signal lag > threshold → CLOSE (data integrity risk)
3. Single trade max loss exceeded (unrealized) → CLOSE that position
4. Daily drawdown exceeded → NO_ACTION (pause new opens, allow closes)
5. Consecutive losses stop → NO_ACTION (24H pause, allow closes)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from decision.config import RiskConfig
from paper_trading.schema import AccountState, DecisionAction, Position

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result / event dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RiskCheckResult:
    passed: bool
    force_action: Optional[DecisionAction]  # if not passed: the forced action (CLOSE or NO_ACTION)
    triggered_rule: Optional[str]           # e.g. "single_trade_max_loss", "cascade"
    details: str
    rule_2_subtype: Optional[str] = None       # "missing_data" | "no_match" (signal_lag only)
    none_reasons_in_lag: Optional[dict] = None  # {"missing_data": N, "no_match": N, ...} lag window dist
    alert_required: bool = False               # True when candidate B suppresses close and needs operator alert


@dataclass
class RiskEvent:
    time: datetime
    symbol: str
    rule: str
    details: str
    action_taken: str


# ---------------------------------------------------------------------------
# RiskGate
# ---------------------------------------------------------------------------

def _classify_lag_subtype(none_reasons: Optional[dict]) -> str:
    """Return 'missing_data' if any collector-fault bar in the lag window; else 'no_match'."""
    if none_reasons and none_reasons.get("missing_data", 0) > 0:
        return "missing_data"
    return "no_match"


class RiskGate:
    """Pre-execution risk checkpoint.

    Every proposed decision passes through ``check()`` before any fill is
    executed.  If a risk rule fires the proposed action is overridden and a
    ``RiskEvent`` is appended to the internal log.

    State that must survive process restarts (pause expiry, consecutive loss
    count) is serialised via ``to_state_dict`` / ``from_state_dict`` and
    persisted externally by ``RiskStore``.
    """

    def __init__(self, risk_config: RiskConfig, symbol: str) -> None:
        self.config = risk_config
        self.symbol = symbol
        self._risk_events: list[RiskEvent] = []
        self._pause_until: Optional[datetime] = None
        self._consecutive_losses: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        proposed_action: DecisionAction,
        current_state: Optional[str],
        bar_time: datetime,
        position: Optional[Position],
        account: AccountState,
        last_state_update_time: Optional[datetime],
        none_reasons_in_lag: Optional[dict] = None,
    ) -> RiskCheckResult:
        """Evaluate all risk rules in priority order.

        Returns a ``RiskCheckResult``.  If ``passed`` is False the caller
        MUST use ``force_action`` instead of the originally proposed action.
        """

        # Rule 1: CASCADE always closes (no exceptions, even during 24H pause)
        if current_state == "Cascade" and self.config.cascade_always_overrides:
            return self._fire(
                "cascade",
                "Cascade state: force close all",
                DecisionAction.CLOSE,
                bar_time,
            )

        # Rule 2: Signal lag > threshold → close (data integrity risk)
        # none_reasons_in_lag distinguishes collector fault (missing_data) from genuine no-match.
        # Candidate A (current): CLOSE in both cases; subtype tagged for diagnostics.
        # See audit/engineering_concerns.md EC-07 for candidates A/B/C.
        if last_state_update_time is not None:
            lag_hours = (bar_time - last_state_update_time).total_seconds() / 3600
            if lag_hours > self.config.signal_lag_max_hours:
                subtype = _classify_lag_subtype(none_reasons_in_lag)
                return self._fire_rule_2(lag_hours, subtype, none_reasons_in_lag, bar_time)

        # Rule 3: Single trade max loss (unrealized) — fires before drawdown / pause checks
        if position is not None:
            loss_pct = -position.unrealized_pnl_usdt / account.nav_usdt
            if loss_pct > self.config.max_loss_per_trade_pct:
                return self._fire(
                    "single_trade_max_loss",
                    f"Unrealized loss {loss_pct:.2%} > {self.config.max_loss_per_trade_pct:.2%}",
                    DecisionAction.CLOSE,
                    bar_time,
                )

        # Rule 4: Daily drawdown — block new opens, allow existing closes
        if account.daily_drawdown_pct > self.config.max_daily_drawdown_pct:
            return self._fire(
                "daily_drawdown",
                f"Daily drawdown {account.daily_drawdown_pct:.2%} > {self.config.max_daily_drawdown_pct:.2%}",
                DecisionAction.NO_ACTION,
                bar_time,
            )

        # Rule 5a: Already in a 24H consecutive-loss pause → block new opens
        if self._pause_until is not None and bar_time < self._pause_until:
            if proposed_action in (DecisionAction.OPEN_LONG, DecisionAction.OPEN_SHORT):
                return self._fire(
                    "consecutive_loss_pause",
                    f"In 24H pause until {self._pause_until.isoformat()}",
                    DecisionAction.NO_ACTION,
                    bar_time,
                )

        # Rule 5b: Consecutive loss threshold just hit → set pause
        if account.consecutive_losses >= self.config.consecutive_loss_stop:
            self._pause_until = bar_time + timedelta(hours=24)
            return self._fire(
                "consecutive_loss_stop",
                (
                    f"{account.consecutive_losses} consecutive losses "
                    f">= {self.config.consecutive_loss_stop}"
                ),
                DecisionAction.NO_ACTION,
                bar_time,
            )

        return RiskCheckResult(
            passed=True, force_action=None, triggered_rule=None, details="ok"
        )

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    def recent_events(self, n: int = 20) -> list[RiskEvent]:
        """Return the last *n* risk events (most recent last)."""
        return self._risk_events[-n:]

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def to_state_dict(self) -> dict:
        """Return a JSON-serialisable dict of mutable state."""
        return {
            "pause_until": self._pause_until.isoformat() if self._pause_until else None,
            "consecutive_losses": self._consecutive_losses,
        }

    @classmethod
    def from_state_dict(
        cls, config: RiskConfig, symbol: str, state: dict
    ) -> "RiskGate":
        """Restore a RiskGate from a previously persisted state dict."""
        gate = cls(config, symbol)
        if state.get("pause_until"):
            gate._pause_until = datetime.fromisoformat(state["pause_until"])
        gate._consecutive_losses = state.get("consecutive_losses", 0)
        return gate

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fire_rule_2(
        self,
        lag_hours: float,
        subtype: str,
        none_reasons_in_lag: Optional[dict],
        bar_time: datetime,
    ) -> RiskCheckResult:
        details = (
            f"State signal lag {lag_hours:.1f}H > {self.config.signal_lag_max_hours}H"
            f" [subtype={subtype}]"
        )
        # Candidate B: collector fault (missing_data) → HOLD + alert; genuine lag (no_match) → CLOSE.
        if subtype == "missing_data":
            force_action = DecisionAction.NO_ACTION
            alert_required = True
        else:
            force_action = DecisionAction.CLOSE
            alert_required = False

        event = RiskEvent(
            time=bar_time,
            symbol=self.symbol,
            rule="signal_lag",
            details=details,
            action_taken=force_action.value,
        )
        self._risk_events.append(event)
        logger.warning("RISK[signal_lag] %s → %s", details, force_action.value)
        return RiskCheckResult(
            passed=False,
            force_action=force_action,
            triggered_rule="signal_lag",
            details=details,
            rule_2_subtype=subtype,
            none_reasons_in_lag=none_reasons_in_lag,
            alert_required=alert_required,
        )

    def _fire(
        self,
        rule: str,
        details: str,
        action: DecisionAction,
        time: datetime,
    ) -> RiskCheckResult:
        event = RiskEvent(
            time=time,
            symbol=self.symbol,
            rule=rule,
            details=details,
            action_taken=action.value,
        )
        self._risk_events.append(event)
        logger.warning("RISK[%s] %s → %s", rule, details, action.value)
        return RiskCheckResult(
            passed=False,
            force_action=action,
            triggered_rule=rule,
            details=details,
        )
