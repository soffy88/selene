"""
v2.0 §5.8 Priority arbitration.

Priority order: Cascade > Critical > Surging > Coiling > Drifting-Charged > Drifting-Calm

Rules:
- Walk states in priority order (high → low).
- Cascade overrides everything, ignores min_dwell (§5.7 red line).
- For other states: if condition met=True, assign if min_dwell allows.
- If no state with met=True is assignable, fall back to Drifting-Calm.
- met=None is treated as "not confirmed" — skip (conservative).

The caller is responsible for passing the ConditionResult for each state.
"""
from __future__ import annotations

from typing import Optional

from sel_v2.states.schema import (
    BarFeatures, ConditionResult, StateLabel, STATE_PRIORITY, MIN_DWELL_BARS,
)


def arbitrate(
    results: dict[StateLabel, ConditionResult],
    current_state: Optional[StateLabel],
    current_duration_4h: int,
) -> tuple[StateLabel, bool]:
    """
    Select the highest-priority state whose condition is met=True and whose
    min_dwell constraint (relative to current state) is satisfied.

    Returns (selected_state, changed) where changed=True if different from current.

    Min-dwell constraint: the CURRENT state must have been held for at least
    MIN_DWELL_BARS[current_state] before switching away (except for Cascade which
    can always enter).
    """
    # Cascade is always checked first — instant switch, no dwell check
    cascade_result = results.get(StateLabel.CASCADE)
    if cascade_result and cascade_result.met is True:
        return StateLabel.CASCADE, (current_state != StateLabel.CASCADE)

    # Check if current state has satisfied its min dwell
    if current_state is not None and current_state != StateLabel.CASCADE:
        min_dwell = MIN_DWELL_BARS.get(current_state, 0)
        can_switch = current_duration_4h >= min_dwell
    else:
        can_switch = True  # no current state or just left Cascade

    # Walk priority order (excluding Cascade already checked)
    priority_order = sorted(
        [s for s in StateLabel if s != StateLabel.CASCADE],
        key=lambda s: STATE_PRIORITY[s],
        reverse=True,
    )

    for state in priority_order:
        res = results.get(state)
        if res is None or res.met is not True:
            continue  # skip: not met or uncertain

        if state == current_state:
            # staying in same state — always valid
            return state, False

        # Switching to a different state — min_dwell must be satisfied for current
        if can_switch:
            return state, True

        # Min-dwell not met: if target is higher priority than current, allow Critical/Cascade override
        # Per §5.1: "红线即时切换: Cascade 触发时立即切换" — already handled above.
        # For all others, respect min_dwell.
        if current_state is not None and STATE_PRIORITY[state] > STATE_PRIORITY.get(current_state, 0):
            # Higher priority state entered — allow switching even under dwell constraint
            # (design intent: risk states override dwell)
            if state in (StateLabel.CRITICAL,):
                return state, True

        # If we can't switch, keep looking (maybe same state matches)
        if state == current_state:
            return state, False

    # Fallback: no state with met=True is assignable
    # Return current state if still valid, else Drifting-Calm
    if current_state is not None:
        return current_state, False

    return StateLabel.DRIFTING_CALM, True
