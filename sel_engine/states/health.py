"""
Wave 3 — HealthMonitor + HealthReport.

Expected state rate ranges are PLACEHOLDERS pending spec finalisation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .schema import StateLabel, StateNoneReason, StateRecord

# ── Expected "healthy" rate ranges (fraction of active bars) ──────────────────
# PLACEHOLDER — verify expected ranges with v1.0.md §10.5 when available
EXPECTED_RATE_RANGES: dict[str, tuple[float, float]] = {
    "Coiling": (0.10, 0.25),  # PLACEHOLDER — verify with v1.0.md §10.5 when available
    "Surging_Up": (0.05, 0.15),  # PLACEHOLDER — verify with v1.0.md §10.5 when available
    "Surging_Down": (0.05, 0.15),  # PLACEHOLDER — verify with v1.0.md §10.5 when available
    "Drifting_Calm": (0.20, 0.45),  # PLACEHOLDER — verify with v1.0.md §10.5 when available
    "Drifting_Charged": (0.05, 0.20),  # PLACEHOLDER — verify with v1.0.md §10.5 when available
    "Critical": (0.02, 0.10),  # PLACEHOLDER — verify with v1.0.md §10.5 when available
    "Cascade": (0.001, 0.03),  # PLACEHOLDER — verify with v1.0.md §10.5 when available
}


@dataclass
class HealthReport:
    period_start: datetime
    period_end: datetime
    total_bars: int
    cold_start_bars: int
    missing_data_bars: int  # WIKI_REQUIRED feature absent, post-warmup
    no_match_bars: int  # all data present but no condition matched
    active_bars: int  # bars with a confirmed state (not None)
    state_counts: dict[str, int]
    state_rates: dict[str, float]  # fraction of active_bars
    legal_transition_rate: float  # fraction of state-change events that are legal
    illegal_transition_types: dict[str, int]  # e.g. {"CASCADE->SURGING_UP": 3}
    avg_dwell_times: dict[str, float]  # mean consecutive bars spent in each state
    cascade_cooling_events: int  # how many times Critical was suppressed by cooling
    in_healthy_range: bool  # True if all state rates match EXPECTED_RATE_RANGES
    health_warnings: list[str]  # human-readable issues


class HealthMonitor:
    """
    Accumulates StateRecords and produces HealthReport for arbitrary time windows.
    """

    def __init__(self) -> None:
        self._records: list[StateRecord] = []
        self._cascade_cooling_events: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, sr: StateRecord) -> None:
        """Append a StateRecord to internal history."""
        self._records.append(sr)
        if sr.reason.startswith("CASCADE_COOLING:"):
            self._cascade_cooling_events += 1

    def set_cooling_events(self, count: int) -> None:
        """Allow StateEngine to sync the cascade-cooling event counter."""
        self._cascade_cooling_events = count

    def generate(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> HealthReport:
        """
        Build a HealthReport for records whose time falls in [start, end].
        If start/end is None, the full history is used.
        """
        records = self._filter(start, end)

        if not records:
            _now = datetime.now(tz=timezone.utc)
            return HealthReport(
                period_start=start or _now,
                period_end=end or _now,
                total_bars=0,
                cold_start_bars=0,
                missing_data_bars=0,
                no_match_bars=0,
                active_bars=0,
                state_counts={lbl.value: 0 for lbl in StateLabel},
                state_rates={lbl.value: 0.0 for lbl in StateLabel},
                legal_transition_rate=1.0,
                illegal_transition_types={},
                avg_dwell_times={lbl.value: 0.0 for lbl in StateLabel},
                cascade_cooling_events=self._cascade_cooling_events,
                in_healthy_range=False,
                health_warnings=["No records in period"],
            )

        period_start = records[0].time
        period_end = records[-1].time
        total_bars = len(records)

        cold_start_bars = sum(1 for r in records if r.none_reason == StateNoneReason.COLD_START)
        missing_data_bars = sum(1 for r in records if r.none_reason == StateNoneReason.MISSING_DATA)
        no_match_bars = sum(1 for r in records if r.none_reason == StateNoneReason.NO_MATCH)
        active_bars = total_bars - cold_start_bars - missing_data_bars - no_match_bars

        # State counts
        state_counts: dict[str, int] = {lbl.value: 0 for lbl in StateLabel}
        for r in records:
            if r.state is not None and not r.cold_start:
                state_counts[r.state.value] += 1

        # State rates
        state_rates: dict[str, float] = {}
        for lbl_str, cnt in state_counts.items():
            state_rates[lbl_str] = cnt / active_bars if active_bars > 0 else 0.0

        # Transition legality
        transitions: list[StateRecord] = []
        prev_state: Optional[StateLabel] = None
        for r in records:
            if r.state is not None and not r.cold_start:
                if r.state != prev_state:
                    transitions.append(r)
                    prev_state = r.state

        total_transitions = len(transitions)
        illegal_transitions = [t for t in transitions if not t.is_legal_transition]
        illegal_count = len(illegal_transitions)
        legal_count = total_transitions - illegal_count
        legal_transition_rate = legal_count / total_transitions if total_transitions > 0 else 1.0

        illegal_transition_types: dict[str, int] = {}
        for t in illegal_transitions:
            from_lbl = t.transition_from.value if t.transition_from else "None"
            key = f"{from_lbl}->{t.state.value}"
            illegal_transition_types[key] = illegal_transition_types.get(key, 0) + 1

        # Average dwell times (mean run-length for each state)
        avg_dwell_times = self._compute_avg_dwell(records)

        # Cascade cooling events in window
        cooling_events = sum(1 for r in records if r.reason.startswith("CASCADE_COOLING:"))

        # Health warnings
        health_warnings: list[str] = []
        in_healthy_range = True

        if active_bars < 720:
            health_warnings.append(f"[INSUFFICIENT_DATA: warmup] active_bars={active_bars} < 720")
            in_healthy_range = False

        if total_bars > 0:
            missing_data_pct = missing_data_bars / total_bars
            if missing_data_pct > 0.1:
                health_warnings.append(f"[DEGRADED: collector_data_missing {missing_data_pct:.1%}]")
                in_healthy_range = False

        for lbl_str, (lo, hi) in EXPECTED_RATE_RANGES.items():
            rate = state_rates.get(lbl_str, 0.0)
            if rate < lo:
                health_warnings.append(f"{lbl_str} rate {rate:.4f} below expected [{lo}, {hi}]")
                in_healthy_range = False
            elif rate > hi:
                health_warnings.append(f"{lbl_str} rate {rate:.4f} above expected [{lo}, {hi}]")
                in_healthy_range = False

        return HealthReport(
            period_start=period_start,
            period_end=period_end,
            total_bars=total_bars,
            cold_start_bars=cold_start_bars,
            missing_data_bars=missing_data_bars,
            no_match_bars=no_match_bars,
            active_bars=active_bars,
            state_counts=state_counts,
            state_rates=state_rates,
            legal_transition_rate=legal_transition_rate,
            illegal_transition_types=illegal_transition_types,
            avg_dwell_times=avg_dwell_times,
            cascade_cooling_events=cooling_events,
            in_healthy_range=in_healthy_range,
            health_warnings=health_warnings,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _filter(
        self,
        start: Optional[datetime],
        end: Optional[datetime],
    ) -> list[StateRecord]:
        result = self._records
        if start is not None:
            result = [r for r in result if r.time >= start]
        if end is not None:
            result = [r for r in result if r.time <= end]
        return result

    @staticmethod
    def _compute_avg_dwell(records: list[StateRecord]) -> dict[str, float]:
        """Compute mean run-length (consecutive bar count) for each state."""
        run_lengths: dict[str, list[int]] = {lbl.value: [] for lbl in StateLabel}

        current_state: Optional[StateLabel] = None
        run = 0
        for r in records:
            if r.cold_start or r.state is None:
                if current_state is not None:
                    run_lengths[current_state.value].append(run)
                    current_state = None
                    run = 0
                continue
            if r.state == current_state:
                run += 1
            else:
                if current_state is not None:
                    run_lengths[current_state.value].append(run)
                current_state = r.state
                run = 1

        if current_state is not None:
            run_lengths[current_state.value].append(run)

        avg: dict[str, float] = {}
        for lbl_str, lengths in run_lengths.items():
            avg[lbl_str] = sum(lengths) / len(lengths) if lengths else 0.0
        return avg
