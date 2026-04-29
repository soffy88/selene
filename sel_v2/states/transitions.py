"""
7 transition vocabulary — sel v2.0 §6.2.

Each vocabulary word identifies a LEGAL state transition by its
(from_state, to_state) pair and the triggering feature pattern.

Legal transitions and their vocabulary:
  Release    : Coiling/Drifting-Charged → Surging
  Exhaustion : Surging → Drifting-Calm / Drifting-Charged
  Decay      : Drifting-Charged → Drifting-Calm
  Charging   : Drifting-Calm → Drifting-Charged  or  Drifting-* → Coiling
  Stress     : Drifting-*/Coiling → Critical
  Trigger    : Critical → Cascade
  Reset      : Cascade → Drifting-Calm

All other (from, to) pairs are ILLEGAL — marked is_legal=False, transition_via=None.
"""
from __future__ import annotations

from typing import Optional

from sel_v2.states.schema import StateLabel, BarFeatures

# ── Legal transition map ───────────────────────────────────────────────────────

# Maps (from, to) → canonical vocabulary word
_LEGAL_TRANSITIONS: dict[tuple[StateLabel, StateLabel], str] = {
    # Release
    (StateLabel.COILING, StateLabel.SURGING): "Release",
    (StateLabel.DRIFTING_CHARGED, StateLabel.SURGING): "Release",

    # Exhaustion
    (StateLabel.SURGING, StateLabel.DRIFTING_CALM): "Exhaustion",
    (StateLabel.SURGING, StateLabel.DRIFTING_CHARGED): "Exhaustion",

    # Decay
    (StateLabel.DRIFTING_CHARGED, StateLabel.DRIFTING_CALM): "Decay",
    (StateLabel.COILING,          StateLabel.DRIFTING_CALM): "Decay",    # Coiling dissolves without breakout

    # Charging — multiple paths
    (StateLabel.DRIFTING_CALM, StateLabel.DRIFTING_CHARGED): "Charging",
    (StateLabel.DRIFTING_CALM, StateLabel.COILING): "Charging",
    (StateLabel.DRIFTING_CHARGED, StateLabel.COILING): "Charging",

    # Stress
    (StateLabel.DRIFTING_CALM, StateLabel.CRITICAL): "Stress",
    (StateLabel.DRIFTING_CHARGED, StateLabel.CRITICAL): "Stress",
    (StateLabel.COILING, StateLabel.CRITICAL): "Stress",
    (StateLabel.SURGING, StateLabel.CRITICAL): "Stress",  # rare but valid §5.3

    # Recoil
    (StateLabel.SURGING,  StateLabel.COILING):  "Recoil",   # post-surge re-compression (rare)

    # Trigger
    (StateLabel.CRITICAL, StateLabel.CASCADE): "Trigger",

    # Reset
    (StateLabel.CASCADE, StateLabel.DRIFTING_CALM): "Reset",
}

# Self-loops (state unchanged) — not a transition event, no vocabulary word needed
_SELF_LOOPS = {(s, s) for s in StateLabel}


def identify_transition(
    from_state: Optional[StateLabel],
    to_state: StateLabel,
    features: Optional[BarFeatures] = None,
) -> tuple[Optional[str], bool]:
    """
    Identify the transition vocabulary word and legality for a state change.

    Returns:
        (transition_via, is_legal)
        transition_via: one of the 7 vocabulary words, or None (no change / illegal)
        is_legal: True for legal transitions, False for illegal jumps
    """
    if from_state is None:
        # First state assignment — not a transition
        return None, True

    if from_state == to_state:
        # Self-loop — no transition event
        return None, True

    key = (from_state, to_state)
    if key in _LEGAL_TRANSITIONS:
        return _LEGAL_TRANSITIONS[key], True

    # Illegal transition — record it
    return None, False


def get_legal_exits(state: StateLabel) -> list[tuple[StateLabel, str]]:
    """Return list of (next_state, vocab_word) legal exits from this state."""
    return [
        (to_state, word)
        for (from_s, to_state), word in _LEGAL_TRANSITIONS.items()
        if from_s == state
    ]
