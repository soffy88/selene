"""
Wave 3 — DwellFilter, CascadeCooling, LegalityChecker.

All transition tables and dwell times are PLACEHOLDERS pending spec finalisation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .schema import StateLabel, StateNoneReason, StateRecord

# ── Minimum dwell times (1H bars) ────────────────────────────────────────────
# PLACEHOLDER — verify with v1.0.md when available
DWELL_TIMES: dict[Optional[StateLabel], int] = {
    StateLabel.COILING: 6,  # PLACEHOLDER — verify with v1.0.md when available
    StateLabel.SURGING_UP: 3,  # PLACEHOLDER — verify with v1.0.md when available
    StateLabel.SURGING_DOWN: 3,  # PLACEHOLDER — verify with v1.0.md when available
    StateLabel.DRIFTING_CALM: 12,  # PLACEHOLDER — verify with v1.0.md when available
    StateLabel.DRIFTING_CHARGED: 6,  # PLACEHOLDER — verify with v1.0.md when available
    StateLabel.CRITICAL: 1,  # PLACEHOLDER — verify with v1.0.md when available
    StateLabel.CASCADE: 1,  # PLACEHOLDER — verify with v1.0.md when available
}

# ── Legal transition table ────────────────────────────────────────────────────
# PLACEHOLDER — verify with v1.0.md section 7.1 when available
_ALL = set(StateLabel)

LEGAL_TRANSITIONS: dict[Optional[StateLabel], set[StateLabel]] = {
    None: _ALL,  # cold start: any state is legal
    StateLabel.COILING: {  # doc §7.1
        StateLabel.SURGING_UP,
        StateLabel.SURGING_DOWN,
        StateLabel.CRITICAL,
        StateLabel.CASCADE,
        # DRIFTING_CALM removed: doc §7.1 marks Coiling→Drifting-Calm as illegal
    },
    StateLabel.SURGING_UP: {  # doc §7.1
        StateLabel.DRIFTING_CALM,
        StateLabel.DRIFTING_CHARGED,
        StateLabel.CRITICAL,
        StateLabel.CASCADE,
        # COILING removed: doc §7.1 marks Surging→Coiling as illegal
    },
    StateLabel.SURGING_DOWN: {  # doc §7.1
        StateLabel.DRIFTING_CALM,
        StateLabel.DRIFTING_CHARGED,
        StateLabel.CRITICAL,
        StateLabel.CASCADE,
        # COILING removed: doc §7.1 marks Surging→Coiling as illegal
    },
    StateLabel.DRIFTING_CALM: {  # doc §7.1
        StateLabel.COILING,
        StateLabel.CRITICAL,
        StateLabel.CASCADE,
        StateLabel.DRIFTING_CHARGED,
        # SURGING_UP/DOWN removed: doc §7.1 marks Drifting-Calm→Surging as illegal
    },
    StateLabel.DRIFTING_CHARGED: {  # PLACEHOLDER — verify with v1.0.md section 7.1 when available
        StateLabel.SURGING_UP,
        StateLabel.SURGING_DOWN,
        StateLabel.CRITICAL,
        StateLabel.CASCADE,
        StateLabel.DRIFTING_CALM,
        StateLabel.COILING,
    },
    StateLabel.CRITICAL: {  # PLACEHOLDER — verify with v1.0.md section 7.1 when available
        StateLabel.CASCADE,
        StateLabel.SURGING_UP,
        StateLabel.SURGING_DOWN,
        StateLabel.DRIFTING_CALM,
        StateLabel.COILING,
    },
    StateLabel.CASCADE: {  # PLACEHOLDER — verify with v1.0.md section 7.1 when available
        StateLabel.DRIFTING_CALM,
        StateLabel.COILING,
    },
}


class DwellFilter:
    """
    Enforces minimum dwell times before confirming a state change.

    The recognizer's raw output is passed through this filter.  A state is only
    *confirmed* (emitted) once N consecutive bars all show the same raw state.
    Until confirmation, the PREVIOUS confirmed state is re-emitted (hysteresis).
    """

    def __init__(self) -> None:
        self._candidate: Optional[StateLabel] = None
        self._count: int = 0
        self._last_confirmed: Optional[StateLabel] = None

    def apply(
        self,
        raw: StateRecord,
        last_confirmed: Optional[StateLabel],
    ) -> StateRecord:
        """
        Args:
            raw:            StateRecord produced by StateRecognizer.
            last_confirmed: last confirmed (non-cold-start) state from StateEngine.

        Returns:
            A (possibly modified) StateRecord — same object fields cloned as needed.
        """
        # Cold-start bars pass through unchanged; dwell filter is irrelevant.
        if raw.cold_start or raw.state is None:
            self._candidate = None
            self._count = 0
            return raw

        raw_state = raw.state
        required = DWELL_TIMES.get(raw_state, 1)

        if raw_state == self._candidate:
            self._count += 1
        else:
            self._candidate = raw_state
            self._count = 1

        if self._count >= required:
            # Confirmed — update internal tracker and emit raw record as-is.
            self._last_confirmed = raw_state
            return raw
        else:
            # Hysteresis: re-emit previous confirmed state (or DRIFTING_CALM if none).
            held_state = last_confirmed
            if held_state is None:
                # No prior confirmed state yet; emit raw unchanged (best-effort).
                return raw
            # Return a clone with state replaced by the held state.
            return StateRecord(
                time=raw.time,
                symbol=raw.symbol,
                state=held_state,
                reason=f"DWELL_HELD:{held_state.value}(waiting {self._count}/{required} for {raw_state.value})",
                feature_quantiles=raw.feature_quantiles,
                feature_vector=raw.feature_vector,
                cold_start=False,
                is_legal_transition=raw.is_legal_transition,
                transition_from=raw.transition_from,
                none_reason=StateNoneReason.NOT_APPLICABLE,
            )


class CascadeCooling:
    """
    After a CASCADE is confirmed, suppresses CRITICAL detections for `cooldown_bars` bars.
    """

    def __init__(self, cooldown_bars: int = 6) -> None:
        self._cooldown_bars = cooldown_bars
        self._cascade_end_time: Optional[datetime] = None
        self.cooling_events: int = 0

    def apply(
        self,
        record: StateRecord,
        last_confirmed: Optional[StateLabel],
    ) -> StateRecord:
        """
        If a CASCADE is just confirmed, arm the cooldown window.
        If we are inside a cooldown window and the record is CRITICAL, suppress it.
        """
        if record.cold_start or record.state is None:
            return record

        # Arm cooldown when we see a CASCADE.
        if record.state == StateLabel.CASCADE:
            self._cascade_end_time = record.time + timedelta(hours=self._cooldown_bars)
            return record

        # Suppress CRITICAL within cooling window.
        if (
            record.state == StateLabel.CRITICAL
            and self._cascade_end_time is not None
            and record.time < self._cascade_end_time
        ):
            self.cooling_events += 1
            # Per v1.0 §7.5: Critical suppression during cascade cooling enters
            # "undefined" state, not DRIFTING_CALM. When no prior state exists,
            # return state=None to avoid synthetic state pollution.
            if last_confirmed is None:
                return StateRecord(
                    time=record.time,
                    symbol=record.symbol,
                    state=None,
                    reason=(
                        f"CASCADE_COOLING_SUPPRESSED_CRITICAL_NO_PRIOR"
                        f"(cooling until {self._cascade_end_time.isoformat()})"
                    ),
                    feature_quantiles=record.feature_quantiles,
                    feature_vector=record.feature_vector,
                    cold_start=False,
                    is_legal_transition=record.is_legal_transition,
                    transition_from=record.transition_from,
                    none_reason=StateNoneReason.NO_MATCH,
                )
            return StateRecord(
                time=record.time,
                symbol=record.symbol,
                state=last_confirmed,
                reason=(
                    f"CASCADE_COOLING_HOLD_PRIOR:{last_confirmed.value}"
                    f"(critical suppressed until {self._cascade_end_time.isoformat()})"
                ),
                feature_quantiles=record.feature_quantiles,
                feature_vector=record.feature_vector,
                cold_start=False,
                is_legal_transition=record.is_legal_transition,
                transition_from=record.transition_from,
                none_reason=StateNoneReason.NOT_APPLICABLE,
            )

        return record


class LegalityChecker:
    """
    Checks whether a state transition is legal per LEGAL_TRANSITIONS.
    Does NOT suppress illegal transitions — only annotates them.
    """

    def __init__(self) -> None:
        self.illegal_transition_count: int = 0
        self._illegal_types: dict[str, int] = {}

    @property
    def illegal_transition_types(self) -> dict[str, int]:
        return dict(self._illegal_types)

    def check(
        self,
        record: StateRecord,
        last_confirmed: Optional[StateLabel],
    ) -> StateRecord:
        """
        Annotates record.is_legal_transition and record.transition_from.
        Always returns the record (never suppresses).
        """
        if record.cold_start or record.state is None:
            return record

        # Only check when the state actually changes (or starts from cold).
        new_state = record.state
        from_state = last_confirmed

        # No change — same state continuing; always legal (dwell continuation).
        if new_state == from_state:
            return StateRecord(
                time=record.time,
                symbol=record.symbol,
                state=record.state,
                reason=record.reason,
                feature_quantiles=record.feature_quantiles,
                feature_vector=record.feature_vector,
                cold_start=record.cold_start,
                is_legal_transition=True,
                transition_from=from_state,
                none_reason=record.none_reason,
            )

        legal_set = LEGAL_TRANSITIONS.get(from_state, _ALL)
        is_legal = new_state in legal_set

        if not is_legal:
            self.illegal_transition_count += 1
            key = f"{from_state.value if from_state else 'None'}->{new_state.value}"
            self._illegal_types[key] = self._illegal_types.get(key, 0) + 1

        return StateRecord(
            time=record.time,
            symbol=record.symbol,
            state=record.state,
            reason=record.reason,
            feature_quantiles=record.feature_quantiles,
            feature_vector=record.feature_vector,
            cold_start=record.cold_start,
            is_legal_transition=is_legal,
            transition_from=from_state,
            none_reason=record.none_reason,
        )
