"""PaperTradingRunner — full per-bar pipeline orchestrator."""
from __future__ import annotations

import logging
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
        )

        # 4. Risk check
        risk_result = self.risk_gate.check(
            proposed_action=proposed_decision.action,
            current_state=state_out.state,
            bar_time=bar_time,
            position=self.positions.current,
            account=account_snapshot,
            last_state_update_time=self._last_state_time,
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

        return trail
