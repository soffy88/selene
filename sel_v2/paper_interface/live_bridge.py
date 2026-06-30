"""Live execution bridge for the sel_v2 strategies (audit P1-1).

The deployed sel_v2 strategy (Strategy 1 trend / Strategy 2 intraday) runs only in the
paper engine; it has NO path to the live, risk-gated, native-stop execution service — that
service is fed by a *different* signal system (the v4 ScoredSignal scorer). So "paper" and
"live" were different strategies with no shared order path, and paper results could never
transfer through tested plumbing.

This bridge closes that gap *safely*: it translates a sel_v2 entry decision into the
canonical `ScoredSignal` contract and (when explicitly enabled) publishes it onto
`signal.scored` — the exact stream the live pipeline starts from. From there a v2 decision
flows the full gated path: portfolio Kelly sizing → risk gate (drawdown / VaR / leverage /
the P0-1 liquidation-distance gate / correlation) → execution with the P0-3 native
protective stop. Nothing fires live unless EXEC_MODE leaves NOTIFY_ONLY *and* the P0-5 OOS
evidence gate is satisfied — this bridge only makes the v2 strategy *reachable* by that
path, it does not loosen any gate.

Default OFF: `emit()` is a no-op unless `SEL_V2_LIVE_BRIDGE` is truthy, so importing/using
the bridge never accidentally routes paper decisions toward live.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from shared.events.streams import STREAM_SIGNAL_SCORED, encode
from shared.models.signal import ScoredSignal, SignalType, Direction, Regime
from sel_v2.strategies.strategy_exit import S1_DRAWDOWN_STOP, S2_DRAWDOWN_STOP

logger = logging.getLogger("sel_v2.live_bridge")

# sel_v2 state (4H) → v4 Regime, best-effort for downstream regime-aware filters.
_STATE_TO_REGIME = {
    "Surging_Up": Regime.TRENDING_UP, "Surging_Down": Regime.TRENDING_DOWN,
    "Drifting_Up": Regime.TRENDING_UP, "Drifting_Down": Regime.TRENDING_DOWN,
    "Coiling": Regime.ACCUMULATION, "Drifting_Calm": Regime.RANGING,
    "Drifting_Charged": Regime.ACCUMULATION, "Critical": Regime.HIGH_VOLATILITY,
    "Cascade": Regime.HIGH_VOLATILITY,
}

_ENTER_ACTIONS = {"ENTER_LONG", "ENTER_SHORT"}


def _drawdown_stop_pct(strategy: str) -> float:
    """Protective-stop distance (positive fraction) for a strategy, from the REAL exit
    module so the live stop matches the paper/backtest stop exactly."""
    return abs(S2_DRAWDOWN_STOP if strategy == "strategy_2" else S1_DRAWDOWN_STOP)


def decision_to_scored_signal(
    decision,
    *,
    symbol: str,
    entry_price: float,
    strategy: str,
    win_probability: float = 0.60,
) -> Optional[ScoredSignal]:
    """Translate a sel_v2 entry decision (Strategy1EntryDecision or EntryDecision) into a
    ScoredSignal, or None when the decision is not an entry. Computes the protective stop
    from the strategy's real drawdown-stop pct so stop_loss > 0 (required by the risk gate
    and is_actionable). Both S1 (`.direction`) and S2 (`ENTER_LONG/SHORT` action) are handled.
    """
    action = getattr(decision, "action", None)
    # Normalise direction across the two decision shapes.
    direction = getattr(decision, "direction", None) or getattr(decision, "cusum_direction", None)
    if action in _ENTER_ACTIONS:
        direction = "LONG" if action == "ENTER_LONG" else "SHORT"
    if direction not in ("LONG", "SHORT"):
        return None
    if entry_price <= 0:
        return None

    is_long = direction == "LONG"
    stop_pct = _drawdown_stop_pct(strategy)
    stop_loss = entry_price * (1 - stop_pct) if is_long else entry_price * (1 + stop_pct)

    state = getattr(decision, "state_4h", None)
    size_pct = float(getattr(decision, "base_size_pct", 0.0) or 0.0)
    lev = float(getattr(decision, "suggested_leverage", 1.0) or 1.0)

    return ScoredSignal(
        symbol=symbol,
        signal_type=SignalType.LONG_SETUP if is_long else SignalType.SHORT_SETUP,
        direction=Direction.LONG if is_long else Direction.SHORT,
        regime=_STATE_TO_REGIME.get(state, Regime.UNKNOWN),
        win_probability=win_probability,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=0.0,   # v2 exits are state/CUSUM/time driven, not a fixed TP
        factor_scores={"sel_v2_strategy": 1.0, "base_size_pct": size_pct,
                       "suggested_leverage": lev},
        indicators={"source": "sel_v2", "strategy": strategy, "state_4h": state,
                    "reason": getattr(decision, "reason", "")},
        regime_adjusted=state is not None,
    )


def bridge_enabled() -> bool:
    """Whether the bridge may publish to the live pipeline. Default OFF — must be explicitly
    turned on. (Even when on, nothing trades live until EXEC_MODE leaves NOTIFY_ONLY and the
    P0-5 OOS gate is satisfied; this flag only controls reachability.)"""
    return os.getenv("SEL_V2_LIVE_BRIDGE", "").lower() in ("1", "true", "on", "yes")


class LiveBridge:
    """Publishes translated sel_v2 decisions onto `signal.scored` when enabled."""

    def __init__(self, redis_client, *, stream: str = STREAM_SIGNAL_SCORED):
        self._r = redis_client
        self._stream = stream

    async def emit(self, decision, *, symbol: str, entry_price: float, strategy: str,
                   win_probability: float = 0.60) -> Optional[str]:
        """Translate and publish a decision. Returns the ScoredSignal id when published,
        or None when the bridge is disabled or the decision is not an actionable entry.
        No-op (never raises) on a non-entry or when SEL_V2_LIVE_BRIDGE is off."""
        if not bridge_enabled():
            return None
        sig = decision_to_scored_signal(
            decision, symbol=symbol, entry_price=entry_price, strategy=strategy,
            win_probability=win_probability)
        if sig is None or not sig.is_actionable:
            return None
        await self._r.xadd(self._stream, encode(sig.to_dict()), maxlen=10_000, approximate=True)
        logger.info("sel_v2 → live pipeline: %s %s @ %s stop=%s (strategy=%s)",
                    sig.direction.value, symbol, entry_price, sig.stop_loss, strategy)
        return sig.id
