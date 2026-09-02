"""
Exit decision trees for Strategy 1 and Strategy 2  (v2.0 §13.3 + §14.3)

Both strategies share Cascade as the top-priority red line.
Strategy 1 and Strategy 2 differ in:
  - Drawdown threshold: -3% (S1) vs -2% (S2)
  - Time stop: 7 days (S1) vs 24h (S2)
  - Partial exits: S1 has Critical reduce-50%; S2 has CUSUM-regression batch exit

ExitDecision.action values:
  HOLD               — no exit condition triggered
  REDUCE_50          — reduce position 50% (Critical state, Strategy 1 only)
  EXIT_BATCH_33      — exit 1/3 of position (Strategy 2 CUSUM regression batch)
  EXIT_FULL          — exit entire position

All checkers are PURE functions — they receive all state as arguments and return
an ExitDecision. Position state updates (tracking peak CUSUM, batch count) are
managed by the caller (SubAccount).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from sel_v2.strategies.cusum_short import CUSUMTrigger

ExitAction = Literal["HOLD", "REDUCE_50", "EXIT_BATCH_33", "EXIT_FULL"]

# §13.3: Strategy 1 thresholds
S1_DRAWDOWN_STOP: float = -0.03  # -3%
S1_TIME_STOP_HOURS: int = 168  # 7 days

# §14.3: Strategy 2 thresholds
S2_DRAWDOWN_STOP: float = -0.02  # -2%
S2_TIME_STOP_HOURS: int = 24

# §14.3: CUSUM regression batch thresholds (fraction of peak remaining)
S2_BATCH1_THRESHOLD: float = 0.50  # C falls to 50% of peak → exit 1/3
S2_BATCH2_THRESHOLD: float = 0.25  # C falls to 25% of peak → exit another 1/3
S2_BATCH3_THRESHOLD: float = 0.00  # C at baseline (≤ 0) → exit remaining 1/3


@dataclass
class ExitDecision:
    action: ExitAction
    reason: str
    priority: int  # lower = higher priority (1=Cascade, 2=drawdown, 3=cusum, …)
    partial_fraction: Optional[float] = None  # for REDUCE_50 / EXIT_BATCH_33


# ── Priority constants ─────────────────────────────────────────────────────────

_P_CASCADE = 1
_P_DRAWDOWN = 2
_P_CUSUM_REVERSE = 3
_P_CUSUM_NEW_HIGH = 4
_P_STATE_EXIT = 5
_P_CRITICAL_REDUCE = 6
_P_CUSUM_BATCH = 7
_P_TIME_STOP = 8
_P_HOLD = 99


# ── Shared helper ──────────────────────────────────────────────────────────────


def _unrealized_pnl_pct(
    direction: Literal["LONG", "SHORT"],
    entry_price: float,
    mark_price: float,
) -> float:
    if direction == "LONG":
        return (mark_price - entry_price) / entry_price
    return (entry_price - mark_price) / entry_price


# ── Strategy 1 exit checker ───────────────────────────────────────────────────


def check_strategy1_exit(
    direction: Literal["LONG", "SHORT"],
    entry_price: float,
    entry_time: datetime,
    mark_price: float,
    current_time: datetime,
    state_4h: Optional[str],
    cusum_trigger: CUSUMTrigger,
    prev_state_4h: Optional[str] = None,
    critical_already_reduced: bool = False,
) -> ExitDecision:
    """
    Evaluate all Strategy 1 exit conditions. Returns the highest-priority
    exit decision. Caller should act on the returned decision once.

    Args:
        direction:               'LONG' or 'SHORT'
        entry_price:             execution price at trade entry
        entry_time:              datetime of entry
        mark_price:              current mark price for PnL calculation
        current_time:            current bar time
        state_4h:                current 4H market state label
        cusum_trigger:           result of CUSUMShort.update() for this tick
        prev_state_4h:           previous bar's state (to detect state transitions)
        critical_already_reduced: True if Critical 50% reduction was already done
    """
    best = ExitDecision(action="HOLD", reason="No exit condition triggered", priority=_P_HOLD)

    def _update(candidate: ExitDecision) -> None:
        nonlocal best
        if candidate.priority < best.priority:
            best = candidate

    # ── L3: Cascade red line ──────────────────────────────────────────────────
    if state_4h == "Cascade":
        _update(
            ExitDecision(
                action="EXIT_FULL",
                reason="Cascade red line — immediate full exit",
                priority=_P_CASCADE,
            )
        )

    # ── L2: drawdown stop -3% ─────────────────────────────────────────────────
    pnl_pct = _unrealized_pnl_pct(direction, entry_price, mark_price)
    if pnl_pct <= S1_DRAWDOWN_STOP:
        _update(
            ExitDecision(
                action="EXIT_FULL",
                reason=f"Drawdown {pnl_pct:.2%} ≤ {S1_DRAWDOWN_STOP:.0%} stop",
                priority=_P_DRAWDOWN,
            )
        )

    # ── L2: CUSUM reverse trigger ─────────────────────────────────────────────
    if cusum_trigger.triggered:
        reverse = (direction == "LONG" and cusum_trigger.direction == "SHORT") or (
            direction == "SHORT" and cusum_trigger.direction == "LONG"
        )
        if reverse:
            _update(
                ExitDecision(
                    action="EXIT_FULL",
                    reason=(f"CUSUM reverse: holding {direction}, triggered {cusum_trigger.direction}"),
                    priority=_P_CUSUM_REVERSE,
                )
            )

    # ── L2: state transition from Surging → Drifting / Critical ──────────────
    if prev_state_4h == "Surging" and state_4h in ("Drifting-Calm", "Drifting-Charged", "Critical"):
        _update(
            ExitDecision(
                action="EXIT_FULL",
                reason=f"State exit: Surging → {state_4h}",
                priority=_P_STATE_EXIT,
            )
        )

    # ── L2: Critical → reduce 50% (once only) ────────────────────────────────
    if state_4h == "Critical" and not critical_already_reduced and prev_state_4h not in (None, "Critical", "Cascade"):
        _update(
            ExitDecision(
                action="REDUCE_50",
                reason="Critical state entered — reduce position 50%",
                priority=_P_CRITICAL_REDUCE,
                partial_fraction=0.50,
            )
        )

    # ── L3: Time Stop 7 days ──────────────────────────────────────────────────
    holding_hours = (current_time - entry_time).total_seconds() / 3600
    if holding_hours >= S1_TIME_STOP_HOURS:
        _update(
            ExitDecision(
                action="EXIT_FULL",
                reason=f"Time stop: held {holding_hours:.1f}h ≥ {S1_TIME_STOP_HOURS}h",
                priority=_P_TIME_STOP,
            )
        )

    return best


# ── Strategy 2 exit checker ───────────────────────────────────────────────────


def check_strategy2_exit(
    direction: Literal["LONG", "SHORT"],
    entry_price: float,
    entry_time: datetime,
    mark_price: float,
    current_time: datetime,
    state_4h: Optional[str],
    cusum_trigger: CUSUMTrigger,
    cusum_peak_since_entry: float,
    batches_triggered: int,
) -> ExitDecision:
    """
    Evaluate all Strategy 2 exit conditions.

    Args:
        direction:               'LONG' or 'SHORT'
        entry_price:             execution price at trade entry
        entry_time:              datetime of entry
        mark_price:              current mark price
        current_time:            current time
        state_4h:                current 4H market state label
        cusum_trigger:           result of CUSUMShort.update()
        cusum_peak_since_entry:  highest C value (same direction as entry) seen
                                 since the position was opened — tracked externally
        batches_triggered:       how many 33% batch exits have already fired (0/1/2)
    """
    best = ExitDecision(action="HOLD", reason="No exit condition triggered", priority=_P_HOLD)

    def _update(candidate: ExitDecision) -> None:
        nonlocal best
        if candidate.priority < best.priority:
            best = candidate

    # ── L3: Cascade red line ──────────────────────────────────────────────────
    if state_4h == "Cascade":
        _update(
            ExitDecision(
                action="EXIT_FULL",
                reason="Cascade red line — immediate full exit",
                priority=_P_CASCADE,
            )
        )

    # ── L2: drawdown stop -2% ─────────────────────────────────────────────────
    pnl_pct = _unrealized_pnl_pct(direction, entry_price, mark_price)
    if pnl_pct <= S2_DRAWDOWN_STOP:
        _update(
            ExitDecision(
                action="EXIT_FULL",
                reason=f"Drawdown {pnl_pct:.2%} ≤ {S2_DRAWDOWN_STOP:.0%} stop",
                priority=_P_DRAWDOWN,
            )
        )

    # ── L2: CUSUM reverse trigger → full immediate exit ───────────────────────
    if cusum_trigger.triggered:
        reverse = (direction == "LONG" and cusum_trigger.direction == "SHORT") or (
            direction == "SHORT" and cusum_trigger.direction == "LONG"
        )
        if reverse:
            _update(
                ExitDecision(
                    action="EXIT_FULL",
                    reason=(f"CUSUM reverse: holding {direction}, triggered {cusum_trigger.direction}"),
                    priority=_P_CUSUM_REVERSE,
                )
            )

    # ── L2: new high SL (same direction accumulator created new peak) ─────────
    # SL fires when same-direction C creates a new max AFTER position was opened.
    # Proxy: if cusum_trigger.triggered in same direction, a new extreme is forming.
    if cusum_trigger.triggered and cusum_trigger.direction == direction:
        _update(
            ExitDecision(
                action="EXIT_FULL",
                reason=(
                    f"CUSUM new-high SL: same-direction trigger while holding "
                    f"{direction} — entry assumption invalidated"
                ),
                priority=_P_CUSUM_NEW_HIGH,
            )
        )

    # ── L2: CUSUM regression batch exit ──────────────────────────────────────
    if cusum_peak_since_entry > 0 and batches_triggered < 3:
        # Current C value in the entry direction
        current_c = cusum_trigger.cusum_positive if direction == "LONG" else cusum_trigger.cusum_negative
        decay_ratio = current_c / cusum_peak_since_entry

        next_batch = batches_triggered + 1
        thresholds = [S2_BATCH1_THRESHOLD, S2_BATCH2_THRESHOLD, S2_BATCH3_THRESHOLD]
        threshold = thresholds[batches_triggered]

        if decay_ratio <= threshold:
            if batches_triggered < 2:
                _update(
                    ExitDecision(
                        action="EXIT_BATCH_33",
                        reason=(
                            f"CUSUM regression batch {next_batch}/3: "
                            f"C={current_c:.3f} / peak={cusum_peak_since_entry:.3f} "
                            f"({decay_ratio:.0%} ≤ {threshold:.0%} threshold)"
                        ),
                        priority=_P_CUSUM_BATCH,
                        partial_fraction=1.0 / 3.0,
                    )
                )
            else:
                # batch 3 → final remaining position exits fully
                _update(
                    ExitDecision(
                        action="EXIT_FULL",
                        reason=(f"CUSUM regression batch 3/3: C={current_c:.3f} at baseline threshold"),
                        priority=_P_CUSUM_BATCH,
                    )
                )

    # ── L3: Time Stop 24h ─────────────────────────────────────────────────────
    holding_hours = (current_time - entry_time).total_seconds() / 3600
    if holding_hours >= S2_TIME_STOP_HOURS:
        _update(
            ExitDecision(
                action="EXIT_FULL",
                reason=f"Time stop: held {holding_hours:.1f}h ≥ {S2_TIME_STOP_HOURS}h",
                priority=_P_TIME_STOP,
            )
        )

    return best
