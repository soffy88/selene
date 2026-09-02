"""
StateRecognizer — main entry point for the sel v2.0 state machine.

Called once per 4H bar with a BarFeatures vector.
Returns a StateRecord describing the new state.

Pipeline:
  1. Check all 6 conditions (returns ConditionResult per state)
  2. Priority arbitration (Cascade > Critical > Surging > Coiling > D-Charged > D-Calm)
  3. Min-dwell enforcement (Cascade bypasses, others respect min_dwell)
  4. Transition vocabulary identification
  5. Assemble and return StateRecord
"""

from __future__ import annotations

import logging
from typing import Optional

from sel_v2.states.conditions import (
    check_cascade,
    check_coiling,
    check_critical,
    check_drifting_calm,
    check_drifting_charged,
    check_surging,
)
from sel_v2.states.min_dwell import MinDwellTracker
from sel_v2.states.priority import arbitrate
from sel_v2.states.schema import BarFeatures, ConditionResult, StateLabel, StateRecord
from sel_v2.states.transitions import identify_transition

logger = logging.getLogger(__name__)


class StateRecognizer:
    """
    Stateful recognizer — holds current state and dwell counter across bars.
    One instance per symbol; call recognize() once per bar.
    """

    def __init__(self) -> None:
        self._dwell = MinDwellTracker()
        self._prev_state: Optional[StateLabel] = None
        # C3 (2026-07-12 user ruling): Surging direction. Close at leg entry —
        # sub_state derives from sign(close - entry_close), breakout direction
        # on the entry bar itself. None outside Surging.
        self._surging_entry_close: Optional[float] = None

    @property
    def current_state(self) -> Optional[StateLabel]:
        return self._dwell.current_state

    @property
    def duration_4h(self) -> int:
        return self._dwell.duration_4h

    def recognize(self, features: BarFeatures) -> StateRecord:
        """
        Process one 4H bar and return the resulting StateRecord.
        Updates internal state tracker.
        """
        # Cold-start: insufficient historical data for σ computation
        if features.cold_start:
            new_state = StateLabel.DRIFTING_CALM
            vocab, is_legal = identify_transition(self._prev_state, new_state)
            self._dwell.update(new_state)
            self._surging_entry_close = None
            rec = _make_record(
                features,
                new_state,
                self._prev_state,
                vocab,
                is_legal,
                self._dwell.duration_4h,
                cold_start=True,
                none_reason="cold_start",
                condition_results={},
            )
            self._prev_state = new_state
            return rec

        # Compute all 6 condition results
        results: dict[StateLabel, ConditionResult] = {
            StateLabel.CASCADE: check_cascade(features),
            StateLabel.CRITICAL: check_critical(features),
            StateLabel.SURGING: check_surging(features),
            StateLabel.COILING: check_coiling(features),
            StateLabel.DRIFTING_CHARGED: check_drifting_charged(features),
            StateLabel.DRIFTING_CALM: check_drifting_calm(features),
        }

        # Priority arbitration + min-dwell
        new_state, changed = arbitrate(
            results,
            current_state=self._dwell.current_state,
            current_duration_4h=self._dwell.duration_4h,
        )

        # If arbitration returns no confident state (all None/False), default to Drifting-Calm
        # (covers the case where σ is in transition zone and all STUB features missing)
        if new_state is None:
            new_state = StateLabel.DRIFTING_CALM

        # Transition vocabulary
        vocab, is_legal = identify_transition(self._prev_state, new_state, features)

        # cold_start is reserved for the warmup period (features.cold_start=True above).
        # STUB conditions are captured in none_reason for auditability but do NOT
        # set cold_start — that would conflate two different causes.
        all_none_tags: list[str] = []
        for state, res in results.items():
            if state == new_state:
                all_none_tags.extend(res.none_conditions)

        cold_start = False
        none_reason = ", ".join(all_none_tags) if all_none_tags else None

        sub_state = self._surging_sub_state(new_state, features)

        self._dwell.update(new_state)
        rec = _make_record(
            features,
            new_state,
            self._prev_state,
            vocab,
            is_legal,
            self._dwell.duration_4h,
            cold_start=cold_start,
            none_reason=none_reason,
            condition_results=results,
            sub_state=sub_state,
        )
        self._prev_state = new_state
        return rec

    def _surging_sub_state(self, new_state: StateLabel, features: BarFeatures) -> Optional[str]:
        """Direction of the current Surging leg — 'Up'/'Down' (C3, 2026-07-12
        user ruling; sub_state had been reserved-but-empty since v2.0, forcing
        every consumer to re-infer leg direction after the fact, V22 series).
        Causal: entry bar uses the breakout direction that fired Surging; later
        bars use sign(close - entry_close). None if the entry direction is
        unknowable (entry without a breakout flag)."""
        if new_state != StateLabel.SURGING:
            self._surging_entry_close = None
            return None
        if self._prev_state != StateLabel.SURGING or self._surging_entry_close is None:
            self._surging_entry_close = features.close
            if features.price_breakout_up:
                return "Up"
            if features.price_breakout_down:
                return "Down"
            return None
        return "Up" if features.close >= self._surging_entry_close else "Down"


def _make_record(
    features: BarFeatures,
    state: StateLabel,
    prev_state: Optional[StateLabel],
    transition_via: Optional[str],
    is_legal: bool,
    duration_4h: int,
    cold_start: bool,
    none_reason: Optional[str],
    condition_results: dict[StateLabel, ConditionResult],
    sub_state: Optional[str] = None,
) -> StateRecord:
    """Assemble a StateRecord from all components."""
    # Build state_features JSONB payload
    sf: dict = {
        "bar_index": features.bar_index,
        "sigma_4h": round(features.sigma_4h, 8),
        "sigma_pctile": round(features.sigma_pctile, 4),
        "sigma_monotone_3bar": features.sigma_monotone_3bar,
        "price_breakout_up": features.price_breakout_up,
        "price_breakout_down": features.price_breakout_down,
        "hawkes_br": round(features.hawkes_br, 6) if features.hawkes_br is not None else None,
        "hawkes_br_threshold": features.hawkes_br_threshold,
        "tda_l1": round(features.tda_l1, 8) if features.tda_l1 is not None else None,
        "tda_l1_pctile": round(features.tda_l1_pctile, 4) if features.tda_l1_pctile is not None else None,
        "tda_l1_threshold": features.tda_l1_threshold,
        "tda_l1_monotone_3bar": features.tda_l1_monotone_3bar,
        "cold_start": cold_start,
        "none_reason": none_reason,
        "is_legal": is_legal,
        # STUB tags
        "entropy_4h": features.entropy_4h,
        "oi_change_rate": features.oi_change_rate,
        "funding_rate": features.funding_rate,
        "lob_depth_pctile": features.lob_depth_pctile,
        "liquidation_pulse": features.liquidation_pulse,
    }

    # Append per-state condition met results
    for label, res in condition_results.items():
        sf[f"cond_{label.value.lower()}"] = res.met

    # Critical sub-conditions (always include for auditability)
    if StateLabel.CRITICAL in condition_results:
        cr = condition_results[StateLabel.CRITICAL]
        sf.update(
            {
                "critical_a1": cr.details.get("a1"),
                "critical_a2": cr.details.get("a2"),
                "critical_a_full": cr.details.get("a_full"),
                "critical_a_partial": cr.details.get("a_partial"),
                "critical_b": cr.details.get("b"),
                "critical_c": cr.details.get("c"),
                "critical_path1": cr.details.get("path1"),
                "critical_path2": cr.details.get("path2"),
            }
        )

    return StateRecord(
        timestamp=features.timestamp,
        state=state,
        sub_state=sub_state,
        state_features=sf,
        transition_from=prev_state,
        transition_via=transition_via,
        duration_4h=duration_4h,
        cold_start=cold_start,
        none_reason=none_reason,
        is_legal=is_legal,
    )
