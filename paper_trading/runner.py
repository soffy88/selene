"""PaperTradingRunner — full per-bar pipeline orchestrator."""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Optional

from decision.config import DecisionConfig
from paper_trading.account import AccountManager
from paper_trading.engine import DecisionEngine
from paper_trading.fills import FillSimulator
from paper_trading.positions import PositionTracker
from paper_trading.risk import RiskGate
from paper_trading.schema import DecisionAction, PositionSide
from paper_trading.trail import DecisionTrail, DecisionTrailBuilder

logger = logging.getLogger(__name__)


class PaperTradingRunner:
    """
    Full paper trading pipeline for one symbol, called every 1H bar.

    Integrates: StateOutputService → DecisionEngine → RiskGate → FillSimulator
    → PositionTracker → AccountManager → DecisionTrailBuilder → DB writes.
    """

    def __init__(self, config: DecisionConfig, symbol: str = "BTCUSDT") -> None:
        from sel_engine.paper_interface.service import StateOutputService

        self.config = config
        self.symbol = symbol
        self.state_svc = StateOutputService(symbol)
        self.decision_engine = DecisionEngine(config)
        self.risk_gate = RiskGate(config.risk, symbol)
        self.fills = FillSimulator(config.execution)
        self.positions = PositionTracker()
        self.account = AccountManager(config.account.initial_nav_usdt, config.risk)
        self.trail_builder = DecisionTrailBuilder(symbol, config)
        self._last_state_time: Optional[datetime] = None
        # Rolling window of (bar_time, none_reason) pairs for Rule 2 subtype classification.
        # maxlen=720 covers 30 days at 1H bars — far beyond any realistic lag window.
        self._recent_none_reasons: deque = deque(maxlen=720)
        self._last_alert_time: Optional[datetime] = None  # for Rule 2 alert deduplication

    # ------------------------------------------------------------------
    async def process_bar(
        self,
        bar_time: datetime,
        close: float,
        pg,          # asyncpg pool (None for dry run)
        redis,       # redis client (None for dry run)
        oi: Optional[float] = None,
    ) -> DecisionTrail:
        """Process one 1H bar end. Returns the complete decision trail."""

        # 1. Get state output
        state_out = await self.state_svc.process_bar(bar_time, close, pg, redis, oi)
        if state_out.state is not None:
            self._last_state_time = bar_time
        self._recent_none_reasons.append((bar_time, state_out.none_reason))
        self.trail_builder.record_state(state_out.state)

        # 2. Update unrealized PnL
        self.positions.update_unrealized(close)
        account_snapshot = self.account.snapshot(bar_time, self.positions.current)

        # 3. Decide
        proposed_decision = self.decision_engine.decide(
            current_state=state_out.state,
            previous_state=state_out.transition_from,
            current_position=self.positions.current,
            account=account_snapshot,
            bar_time=bar_time,
            current_price=close,
            none_reason=state_out.none_reason,
        )

        # 4. Risk check — compute none_reason distribution in lag window for Rule 2 diagnostics
        none_reasons_in_lag: Optional[dict] = None
        if self._last_state_time is not None:
            none_reasons_in_lag = self._get_none_reasons_in_window(
                self._last_state_time, bar_time
            )
        risk_result = self.risk_gate.check(
            proposed_action=proposed_decision.action,
            current_state=state_out.state,
            bar_time=bar_time,
            position=self.positions.current,
            account=account_snapshot,
            last_state_update_time=self._last_state_time,
            none_reasons_in_lag=none_reasons_in_lag,
        )

        final_action = risk_result.force_action or proposed_decision.action
        fill = None
        realized_pnl = None

        # 5. Execute
        if final_action in (DecisionAction.OPEN_LONG, DecisionAction.OPEN_SHORT):
            if self.positions.current is None and proposed_decision.target_size_usdt:
                side = (
                    PositionSide.LONG
                    if final_action == DecisionAction.OPEN_LONG
                    else PositionSide.SHORT
                )
                fill = self.fills.simulate_open(
                    bar_time,
                    self.symbol,
                    side,
                    proposed_decision.target_size_usdt,
                    close,
                    self.config.config_hash,
                )
                self.positions.open(fill, bar_time, close)
                self.account.apply_fill(fill)

        elif final_action == DecisionAction.CLOSE and self.positions.current is not None:
            fill, realized_pnl = self.fills.simulate_close(
                bar_time,
                self.symbol,
                self.positions.current,
                close,
                self.config.config_hash,
            )
            self.positions.close(fill, realized_pnl)
            self.account.apply_fill(fill, realized_pnl)

        # 6. Build trail
        trail = self.trail_builder.build(
            bar_time,
            state_out,
            proposed_decision,
            risk_result,
            fill,
            realized_pnl,
            account_snapshot,
        )

        # 7. Persist (skip if no DB)
        if pg is not None:
            from paper_trading.db.trail_store import TrailStore
            await TrailStore.insert(pg, trail)

        # 8. Emit Rule 2 alert if candidate B fired and redis is available
        if trail.alert_required and redis is not None:
            await self._emit_rule2_alert_if_needed(redis, trail, bar_time)

        return trail

    # ------------------------------------------------------------------
    async def _emit_rule2_alert_if_needed(self, redis, trail, bar_time: datetime) -> None:
        """Publish a risk_alert to STREAM_SYSTEM_ALERTS, rate-limited by dedup interval.

        The dedup interval (missing_data_alert_dedup_hours) is an engineering convenience
        value, not from v1.0 spec. First entry into a missing_data lag window always alerts;
        subsequent bars in the same window are suppressed until dedup_hours have elapsed.
        """
        dedup_hours = self.config.risk.missing_data_alert_dedup_hours
        if (
            self._last_alert_time is not None
            and (bar_time - self._last_alert_time).total_seconds() < dedup_hours * 3600
        ):
            return  # within dedup window — suppress

        self._last_alert_time = bar_time
        lag_reasons = trail.state_none_reason_in_lag or {}
        missing_count = lag_reasons.get("missing_data", 0)
        total_none = sum(lag_reasons.values()) or 1

        pos_summary = (
            f"{trail.position_side} ${trail.position_size_usdt:,.0f}"
            f" (unrealized: ${trail.unrealized_pnl_usdt:+.0f})"
            if trail.position_side
            else "no position"
        )

        reason_text = (
            f"[Selene Risk Alert] Rule 2 - Missing Data Lag\n\n"
            f"Bar: {bar_time.isoformat()}\n"
            f"Lag details: {trail.risk_details}\n"
            f"Missing data ratio: {missing_count}/{total_none}\n"
            f"None reasons: {lag_reasons}\n"
            f"Current position: {pos_summary}\n\n"
            f"Action: HOLD (no close)\n"
            f"Likely cause: collector data missing"
        )

        try:
            await redis.xadd(
                "system.alerts",
                {
                    "type": "risk_alert",
                    "reason": reason_text,
                    "bar_time": bar_time.isoformat(),
                    "symbol": self.symbol,
                    "rule_2_subtype": "missing_data",
                },
                maxlen=10000,
                approximate=True,
            )
            logger.info("Rule 2 alert emitted for %s at %s", self.symbol, bar_time)
        except Exception as exc:
            logger.error("Failed to emit Rule 2 alert: %s", exc)

    # ------------------------------------------------------------------
    def _get_none_reasons_in_window(self, start: datetime, end: datetime) -> dict:
        """Count none_reason occurrences for bars in (start, end].

        Used to classify Rule 2 signal_lag events as MISSING_DATA vs NO_MATCH.
        Only bars where none_reason is not None (i.e. state was None) are counted.
        """
        counts: dict = {"missing_data": 0, "no_match": 0, "cold_start": 0}
        for bar_time, reason in self._recent_none_reasons:
            if start < bar_time <= end and reason is not None:
                if reason in counts:
                    counts[reason] += 1
        return counts
