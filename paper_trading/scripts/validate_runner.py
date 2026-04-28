"""
Validation script: Run 800 synthetic bars through PaperTradingRunner (dry run, no DB/Redis).

Prints: final NAV, realized PnL, # trades, # risk triggers, state distribution.
Exit 0 on success.

Usage:
    python -m paper_trading.scripts.validate_runner
"""
from __future__ import annotations

import asyncio
import math
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Synthetic StateOutputService shim
# ---------------------------------------------------------------------------

class _SyntheticStateOutputService:
    """Generates deterministic synthetic state outputs without DB or Redis."""

    _STATE_CYCLE = [
        "Coiling",
        "Coiling",
        "Coiling",
        "Surging_Up",
        "Surging_Up",
        "Drifting_Charged",
        "Drifting_Calm",
        "Drifting_Calm",
        "Coiling",
        "Surging_Down",
        "Drifting_Calm",
        "Cascade",
        "Drifting_Calm",
        "Coiling",
    ]

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self._idx = 0
        self._prev_state: Optional[str] = None

    async def process_bar(self, bar_time, close, pg, redis, oi=None):
        cycle_len = len(self._STATE_CYCLE)
        state = self._STATE_CYCLE[self._idx % cycle_len]
        self._idx += 1
        out = _SyntheticStateOutput(bar_time, self.symbol, state, self._prev_state, close)
        self._prev_state = state
        return out


class _SyntheticStateOutput:
    def __init__(self, bar_time, symbol, state, prev_state, close_price):
        self.bar_time = bar_time
        self.symbol = symbol
        self.state = state
        self.state_enum = None
        self.direction = None
        self.is_cold_start = False
        self.confidence = 1.0
        self.reason = f"synthetic:{state}"
        self.is_legal_transition = True
        self.transition_from = prev_state
        self.health_warning = False
        self.close = close_price
        self.sigma_p_24h = 0.15
        self.H = 0.45
        self.TF = None
        self.OI = None
        self.funding_rate = 0.0001
        self.LV = None
        self.absorption_ratio = None
        self.OI_hurst = None
        self.feature_completeness = 4 / 9  # 4 non-None out of 9


# ---------------------------------------------------------------------------
# Patched runner
# ---------------------------------------------------------------------------

class _SyntheticRunner:
    """PaperTradingRunner variant that uses the synthetic state service."""

    def __init__(self, config):
        from paper_trading.account import AccountManager
        from paper_trading.engine import DecisionEngine
        from paper_trading.fills import FillSimulator
        from paper_trading.positions import PositionTracker
        from paper_trading.risk import RiskGate
        from paper_trading.trail import DecisionTrailBuilder

        self.config = config
        self.symbol = "BTCUSDT"
        self.state_svc = _SyntheticStateOutputService(self.symbol)
        self.decision_engine = DecisionEngine(config)
        self.risk_gate = RiskGate(config.risk, self.symbol)
        self.fills = FillSimulator(config.execution)
        self.positions = PositionTracker()
        self.account = AccountManager(config.account.initial_nav_usdt, config.risk)
        self.trail_builder = DecisionTrailBuilder(self.symbol, config)
        self._last_state_time: Optional[datetime] = None

    async def process_bar(self, bar_time, close, oi=None):
        from paper_trading.schema import DecisionAction, PositionSide

        state_out = await self.state_svc.process_bar(bar_time, close, None, None, oi)
        if state_out.state is not None:
            self._last_state_time = bar_time
        self.trail_builder.record_state(state_out.state)

        self.positions.update_unrealized(close)
        account_snapshot = self.account.snapshot(bar_time, self.positions.current)

        proposed_decision = self.decision_engine.decide(
            current_state=state_out.state,
            previous_state=state_out.transition_from,
            current_position=self.positions.current,
            account=account_snapshot,
            bar_time=bar_time,
            current_price=close,
        )

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

        if final_action in (DecisionAction.OPEN_LONG, DecisionAction.OPEN_SHORT):
            if self.positions.current is None and proposed_decision.target_size_usdt:
                side = (
                    PositionSide.LONG
                    if final_action == DecisionAction.OPEN_LONG
                    else PositionSide.SHORT
                )
                fill = self.fills.simulate_open(
                    bar_time, self.symbol, side,
                    proposed_decision.target_size_usdt, close,
                    self.config.config_hash,
                )
                self.positions.open(fill, bar_time, close)
                self.account.apply_fill(fill)

        elif final_action == DecisionAction.CLOSE and self.positions.current is not None:
            fill, realized_pnl = self.fills.simulate_close(
                bar_time, self.symbol, self.positions.current, close,
                self.config.config_hash,
            )
            self.positions.close(fill, realized_pnl)
            self.account.apply_fill(fill, realized_pnl)

        trail = self.trail_builder.build(
            bar_time, state_out, proposed_decision, risk_result, fill, realized_pnl,
            account_snapshot,
        )
        return trail


# ---------------------------------------------------------------------------
# Main validation routine
# ---------------------------------------------------------------------------

async def run_validation(num_bars: int = 800) -> None:
    from decision.config import load_config

    config = load_config()
    runner = _SyntheticRunner(config)

    start_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    base_price = 60_000.0

    trails = []
    for i in range(num_bars):
        bar_time = start_time + timedelta(hours=i)
        # Deterministic price: sinusoidal drift to produce realistic-ish bars
        close = base_price + 1_000 * math.sin(i * 0.1) + 500 * math.sin(i * 0.03)
        trail = await runner.process_bar(bar_time, close)
        trails.append(trail)

    # ---- Summary statistics ----
    final_nav = runner.account.snapshot(
        start_time + timedelta(hours=num_bars - 1),
        runner.positions.current
    ).nav_usdt

    realized_pnl = runner.account._realized_pnl

    trades = sum(1 for t in trails if t.fill_price is not None)
    risk_triggers = sum(1 for t in trails if t.risk_triggered is not None)

    state_dist = Counter(t.current_state or "None" for t in trails)

    print("=" * 60)
    print(f"PaperTradingRunner validation — {num_bars} bars")
    print("=" * 60)
    print(f"Final NAV:        {final_nav:>12.2f} USDT")
    print(f"Realized PnL:     {realized_pnl:>12.2f} USDT")
    print(f"Trades (fills):   {trades:>12d}")
    print(f"Risk triggers:    {risk_triggers:>12d}")
    print()
    print("State distribution:")
    for state, count in sorted(state_dist.items()):
        pct = count / num_bars * 100
        print(f"  {state:<25s} {count:>5d}  ({pct:.1f}%)")
    print("=" * 60)
    print("PASS — validation complete")


def main() -> None:
    asyncio.run(run_validation(800))
    sys.exit(0)


if __name__ == "__main__":
    main()
