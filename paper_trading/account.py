"""AccountManager — NAV, realized PnL, daily drawdown, risk controls."""

from datetime import datetime, timedelta
from typing import Optional

from decision.config import RiskConfig
from paper_trading.schema import AccountState, Position, SimulatedFill


class AccountManager:
    def __init__(self, initial_nav: float, risk_config: RiskConfig) -> None:
        self.config = risk_config
        self._nav = initial_nav
        self._realized_pnl = 0.0
        self._daily_start_nav = initial_nav
        self._consecutive_losses = 0
        self._pause_until: Optional[datetime] = None
        self._cascade_cooling = False

    # ------------------------------------------------------------------
    def apply_fill(
        self,
        fill: SimulatedFill,
        realized_pnl: Optional[float] = None,
    ) -> None:
        """Update NAV for fees. If closing fill, also apply realized_pnl."""
        self._nav -= fill.fee_usdt
        if realized_pnl is not None:
            self._nav += realized_pnl
            self._realized_pnl += realized_pnl
            if realized_pnl < 0:
                self._consecutive_losses += 1
            else:
                self._consecutive_losses = 0

    # ------------------------------------------------------------------
    def check_risk(
        self,
        current_state: Optional[str],
        bar_time: datetime,
        position: Optional[Position],
    ) -> tuple[bool, str]:
        """Return (should_force_close, reason).

        Checks (in priority order):
        1. Cascade state → always force close if cascade_always_overrides
        2. Single-trade max loss breached (unrealized)
        3. Daily drawdown breached
        4. Consecutive loss stop hit → pause 24H, close position
        5. Active pause period
        """
        # 1. Cascade override — always fires even during pause
        if current_state == "Cascade" and self.config.cascade_always_overrides:
            self._cascade_cooling = True
            return True, "Cascade state — unconditional force close"

        # 2. Single-trade max loss (unrealized) — always fires even during pause
        if position is not None:
            max_loss_usdt = self._nav * self.config.max_loss_per_trade_pct
            if position.unrealized_pnl_usdt < -max_loss_usdt:
                return True, (
                    f"Single trade max loss breached: unrealized={position.unrealized_pnl_usdt:.2f} "
                    f"limit=-{max_loss_usdt:.2f}"
                )

        # 3. Active pause — if already paused, skip the remaining triggers to
        #    avoid re-firing consecutive-loss / drawdown checks on every bar.
        if self._pause_until is not None and bar_time < self._pause_until:
            # Pause does NOT force-close existing positions — it prevents new opens.
            return False, f"Paused until {self._pause_until.isoformat()}"

        # 4. Daily drawdown
        drawdown_pct = self._daily_drawdown_pct()
        if drawdown_pct >= self.config.max_daily_drawdown_pct:
            self._set_pause(bar_time)
            return True, (f"Daily drawdown breached: {drawdown_pct:.2%} >= {self.config.max_daily_drawdown_pct:.2%}")

        # 5. Consecutive loss stop
        if self._consecutive_losses >= self.config.consecutive_loss_stop:
            self._set_pause(bar_time)
            return True, (f"Consecutive loss stop: {self._consecutive_losses} losses in a row")

        return False, ""

    # ------------------------------------------------------------------
    def snapshot(self, time: datetime, position: Optional[Position]) -> AccountState:
        """Return current account state as a dataclass."""
        unrealized = position.unrealized_pnl_usdt if position else 0.0
        pos_side = position.side.value if position else None
        pos_size = position.current_size_usdt if position else 0.0

        is_paused = self._pause_until is not None and time < self._pause_until

        return AccountState(
            time=time,
            nav_usdt=self._nav,
            realized_pnl_usdt=self._realized_pnl,
            unrealized_pnl_usdt=unrealized,
            position_side=pos_side,
            position_size_usdt=pos_size,
            daily_drawdown_pct=self._daily_drawdown_pct(),
            consecutive_losses=self._consecutive_losses,
            is_paused=is_paused,
            pause_until=self._pause_until,
            cascade_cooling=self._cascade_cooling,
        )

    # ------------------------------------------------------------------
    def reset_daily(self, bar_time: datetime) -> None:
        """Reset daily drawdown tracking at the start of each new day."""
        self._daily_start_nav = self._nav
        # Clear pause if expired
        if self._pause_until is not None and bar_time >= self._pause_until:
            self._pause_until = None
        self._cascade_cooling = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _daily_drawdown_pct(self) -> float:
        if self._daily_start_nav == 0:
            return 0.0
        return max(0.0, (self._daily_start_nav - self._nav) / self._daily_start_nav)

    def _set_pause(self, bar_time: datetime) -> None:
        self._pause_until = bar_time + timedelta(hours=24)
