"""
Tests for sel v2.0 §6.2 — 7 transition vocabulary.

Covers:
- All 7 legal transition vocabulary words
- Illegal transition detection (is_legal=False)
- Self-loops (no vocabulary word)
- StateRecognizer integration for transition_via field
"""

from datetime import datetime, timezone

import pytest

from sel_v2.states.schema import BarFeatures, StateLabel
from sel_v2.states.transitions import _LEGAL_TRANSITIONS, get_legal_exits, identify_transition

_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


# ── All 7 legal transitions ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "from_s, to_s, expected_vocab",
    [
        (StateLabel.COILING, StateLabel.SURGING, "Release"),
        (StateLabel.DRIFTING_CHARGED, StateLabel.SURGING, "Release"),
        (StateLabel.SURGING, StateLabel.DRIFTING_CALM, "Exhaustion"),
        (StateLabel.SURGING, StateLabel.DRIFTING_CHARGED, "Exhaustion"),
        (StateLabel.DRIFTING_CHARGED, StateLabel.DRIFTING_CALM, "Decay"),
        (StateLabel.COILING, StateLabel.DRIFTING_CALM, "Decay"),
        (StateLabel.CRITICAL, StateLabel.DRIFTING_CALM, "Decay"),
        (StateLabel.DRIFTING_CALM, StateLabel.DRIFTING_CHARGED, "Charging"),
        (StateLabel.DRIFTING_CALM, StateLabel.COILING, "Charging"),
        (StateLabel.DRIFTING_CHARGED, StateLabel.COILING, "Charging"),
        (StateLabel.DRIFTING_CALM, StateLabel.CRITICAL, "Stress"),
        (StateLabel.DRIFTING_CHARGED, StateLabel.CRITICAL, "Stress"),
        (StateLabel.COILING, StateLabel.CRITICAL, "Stress"),
        (StateLabel.SURGING, StateLabel.CRITICAL, "Stress"),
        (StateLabel.SURGING, StateLabel.COILING, "Recoil"),
        (StateLabel.CRITICAL, StateLabel.CASCADE, "Trigger"),
        (StateLabel.CASCADE, StateLabel.DRIFTING_CALM, "Reset"),
    ],
)
def test_legal_transition(from_s, to_s, expected_vocab):
    vocab, is_legal = identify_transition(from_s, to_s)
    assert vocab == expected_vocab
    assert is_legal is True


# ── Self-loops ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", list(StateLabel))
def test_self_loop_no_vocab(state):
    vocab, is_legal = identify_transition(state, state)
    assert vocab is None
    assert is_legal is True


# ── First assignment ───────────────────────────────────────────────────────────


def test_first_assignment_no_vocab():
    vocab, is_legal = identify_transition(None, StateLabel.DRIFTING_CALM)
    assert vocab is None
    assert is_legal is True


# ── Illegal transitions ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "from_s, to_s",
    [
        # Skip Cascade — must go via Critical → Cascade or Reset
        (StateLabel.DRIFTING_CALM, StateLabel.CASCADE),
        (StateLabel.COILING, StateLabel.CASCADE),
        (StateLabel.SURGING, StateLabel.CASCADE),
        (StateLabel.DRIFTING_CHARGED, StateLabel.CASCADE),
        # Skip Critical — must go via Stress; Critical exits only to Cascade or DCalm
        (StateLabel.CRITICAL, StateLabel.COILING),
        (StateLabel.CRITICAL, StateLabel.SURGING),
        (StateLabel.CRITICAL, StateLabel.DRIFTING_CHARGED),
        # Cascade can only → Drifting-Calm
        (StateLabel.CASCADE, StateLabel.COILING),
        (StateLabel.CASCADE, StateLabel.SURGING),
        (StateLabel.CASCADE, StateLabel.CRITICAL),
        (StateLabel.CASCADE, StateLabel.DRIFTING_CHARGED),
    ],
)
def test_illegal_transitions(from_s, to_s):
    vocab, is_legal = identify_transition(from_s, to_s)
    assert vocab is None
    assert is_legal is False


# ── Legal exits enumeration ────────────────────────────────────────────────────


def test_coiling_legal_exits():
    exits = {to: vocab for to, vocab in get_legal_exits(StateLabel.COILING)}
    assert StateLabel.SURGING in exits
    assert StateLabel.CRITICAL in exits
    assert StateLabel.DRIFTING_CALM in exits
    assert exits[StateLabel.SURGING] == "Release"
    assert exits[StateLabel.CRITICAL] == "Stress"
    assert exits[StateLabel.DRIFTING_CALM] == "Decay"


def test_critical_legal_exits():
    exits = {to: vocab for to, vocab in get_legal_exits(StateLabel.CRITICAL)}
    assert StateLabel.CASCADE in exits
    assert StateLabel.DRIFTING_CALM in exits
    assert exits[StateLabel.CASCADE] == "Trigger"
    assert exits[StateLabel.DRIFTING_CALM] == "Decay"  # soft-landing (v2.0 §5.6)


def test_cascade_legal_exits():
    exits = {to: vocab for to, vocab in get_legal_exits(StateLabel.CASCADE)}
    assert StateLabel.DRIFTING_CALM in exits
    assert exits[StateLabel.DRIFTING_CALM] == "Reset"
    assert len(exits) == 1


# ── All legal transitions covered ─────────────────────────────────────────────


def test_all_vocabulary_words_appear():
    all_vocabs = set(_LEGAL_TRANSITIONS.values())
    expected = {"Release", "Exhaustion", "Decay", "Charging", "Stress", "Trigger", "Reset", "Recoil"}
    assert all_vocabs == expected


# ── Recognizer integration: transition_via in StateRecord ─────────────────────


def test_recognizer_assigns_transition_via():
    """End-to-end: recognizer should assign 'Stress' when transitioning Coiling→Critical."""
    from sel_v2.states.recognizer import StateRecognizer
    from sel_v2.states.schema import StateLabel

    recognizer = StateRecognizer()

    # Bar 1: force Coiling (low sigma)
    def _feat(sigma_pctile, sigma_monotone=None, hawkes_br=None, tda_pctile=None, tda_monotone=None, bar_index=600):
        return BarFeatures(
            timestamp=_TS,
            bar_index=bar_index,
            close=50000.0,
            log_price=10.8,
            sigma_4h=0.01,
            sigma_pctile=sigma_pctile,
            sigma_monotone_3bar=sigma_monotone,
            price_breakout_up=False,
            price_breakout_down=False,
            hawkes_br=hawkes_br,
            hawkes_br_threshold=0.85,
            tda_l1=None,
            tda_l1_pctile=tda_pctile,
            tda_l1_threshold=0.000097,
            tda_l1_monotone_3bar=tda_monotone,
            cold_start=False,
        )

    # Stay in Coiling for 6 bars (min_dwell=6)
    for i in range(6):
        recognizer.recognize(_feat(sigma_pctile=0.20, bar_index=i))

    # Now transition to Critical (should produce "Stress")
    r_critical = recognizer.recognize(
        _feat(sigma_pctile=0.96, sigma_monotone=True, hawkes_br=0.90, tda_pctile=0.98, tda_monotone=True, bar_index=7)
    )

    if r_critical.state == StateLabel.CRITICAL:
        assert r_critical.transition_via == "Stress"
        assert r_critical.is_legal is True
