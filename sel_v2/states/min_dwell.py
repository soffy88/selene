"""
Minimum dwell time tracking for sel v2.0 §5.

Each state has a minimum number of 4H bars it must be held before the machine
can switch away. Cascade has no lower limit (can exit immediately).

The MinDwellTracker is a simple counter that increments each bar while in
the same state and resets on any state change.
"""

from __future__ import annotations

from typing import Optional

from sel_v2.states.schema import MIN_DWELL_BARS, StateLabel


class MinDwellTracker:
    """Track how long we've been in the current state."""

    def __init__(self) -> None:
        self._state: Optional[StateLabel] = None
        self._count: int = 0

    @property
    def current_state(self) -> Optional[StateLabel]:
        return self._state

    @property
    def duration_4h(self) -> int:
        """Number of 4H bars spent in current state."""
        return self._count

    def update(self, new_state: StateLabel) -> None:
        """Update tracker with new bar's state. Call once per bar."""
        if new_state == self._state:
            self._count += 1
        else:
            self._state = new_state
            self._count = 1

    def can_leave(self) -> bool:
        """
        True if the current state has been held long enough to switch away.
        Cascade can always be left (no min-dwell applies on exit from Cascade).
        """
        if self._state is None:
            return True
        if self._state == StateLabel.CASCADE:
            return True  # Cascade has no minimum exit dwell
        min_dwell = MIN_DWELL_BARS.get(self._state, 0)
        return self._count >= min_dwell
