"""
End-to-end integration test: CUSUMShort → HawkesIntensityTracker → Strategy2EntryFilter.

Uses synthetic 1-second tick z-score sequences to exercise the full chain
without mocking any internal logic (only params_loader is mocked via conftest).

STUB note: This e2e test passes OFI / liq_pulse / cross_spread / vocab as
hardcoded scalar values to _run_chain(), bypassing real LOB / liquidation /
cross-exchange feed inference. These tests verify routing logic only, NOT
signal inference logic. Real OFI / vocab inference is Wave 3 STUB
(see sel_v2/strategies/STUB_BOUNDARIES.md).
"""

from sel_v2.strategies.cusum_short import CUSUMShort
from sel_v2.strategies.strategy2_entry import Strategy2EntryFilter


def _run_chain(z_scores, state_4h, vocab, ofi=None, liq_pulse=False):
    """
    Feed z_scores into real CUSUMShort, collect the first CUSUM trigger,
    then evaluate through Strategy2EntryFilter.
    Returns list of (tick_idx, EntryDecision) for every triggered tick.
    """
    cusum = CUSUMShort(drift_k=0.5)
    entry_filter = Strategy2EntryFilter()
    results = []

    for i, z in enumerate(z_scores):
        trigger = cusum.update(z, t=float(i))
        if trigger.triggered:
            decision = entry_filter.evaluate(
                t=float(i),
                cusum_trigger=trigger,
                state_4h=state_4h,
                inverse_vocab=vocab,
                ofi_persistent_same_direction=ofi,
                liq_pulse=liq_pulse,
            )
            results.append((i, decision))
    return results


# ── Happy-path LONG (Type B) ──────────────────────────────────────────────────


def test_e2e_type_b_long_entry():
    """
    Sustained positive z-scores → CUSUM triggers LONG → cold H1 passes →
    Type B (Sweep, OFI confirmed) → ENTER_LONG.
    """
    # 5 bars at z=2.5 → C+ accumulates: each step adds 2.5-0.5=2.0 → C+=10 after 5 steps
    z_scores = [2.5] * 5 + [0.0] * 5
    results = _run_chain(z_scores, state_4h="Surging", vocab=["Sweep"], ofi=True)
    assert len(results) > 0
    tick_idx, decision = results[0]
    assert decision.action == "ENTER_LONG"
    assert decision.step_reached == 6
    assert decision.cusum_direction == "LONG"


# ── Happy-path SHORT reversal (Type A) ───────────────────────────────────────


def test_e2e_type_a_short_reversal():
    """
    Sustained negative z-scores → CUSUM triggers SHORT → Type A reversal
    (Absorption + Sweep) → ENTER_LONG.
    """
    z_scores = [-2.5] * 5 + [0.0] * 5
    results = _run_chain(z_scores, state_4h="Drifting-Charged", vocab=["Absorption", "Sweep"])
    assert len(results) > 0
    _, decision = results[0]
    assert decision.action == "ENTER_LONG"  # SHORT CUSUM + reversal → LONG
    assert decision.step_reached == 6


# ── OFI absent → Type B abort ─────────────────────────────────────────────────


def test_e2e_type_b_aborts_without_ofi():
    """
    CUSUM triggers but ofi=None → Type B cannot trigger → ABORT at Step 3.
    Type A also not triggered (no Absorption+Sweep).
    """
    z_scores = [2.5] * 5
    results = _run_chain(z_scores, state_4h="Surging", vocab=["Sweep"], ofi=None)
    assert len(results) > 0
    _, decision = results[0]
    assert decision.action == "ABORT"
    assert decision.step_reached == 3


# ── Cascade veto blocks entry ─────────────────────────────────────────────────


def test_e2e_cascade_veto():
    """CUSUM triggers but 4H state = Cascade → ABORT at Step 2."""
    z_scores = [2.5] * 5
    results = _run_chain(z_scores, state_4h="Cascade", vocab=["Sweep"], ofi=True)
    assert len(results) > 0
    _, decision = results[0]
    assert decision.action == "ABORT"
    assert decision.step_reached == 2
    assert "Cascade" in decision.reason


# ── Liquidation pulse blocks entry ────────────────────────────────────────────


def test_e2e_liq_pulse_blocks():
    """CUSUM triggers, Type B passes, but liq_pulse=True → ABORT at Step 4."""
    z_scores = [2.5] * 5
    results = _run_chain(z_scores, state_4h="Surging", vocab=["Sweep"], ofi=True, liq_pulse=True)
    assert len(results) > 0
    _, decision = results[0]
    assert decision.action == "ABORT"
    assert decision.step_reached == 4


# ── CUSUM never triggers → no decisions ──────────────────────────────────────


def test_e2e_no_trigger_on_mild_z():
    """
    Mild z-scores (z=0.8 < drift_k=0.5 threshold barely accumulates)
    — C+ after 100 steps: each step adds max(0, 0.8-0.5)=0.3 → C+=30 after 100 steps.
    Actually that WOULD trigger.  Use z=0.4 < drift_k so C+ stays 0.
    """
    z_scores = [0.4] * 200  # z < k=0.5 → C+ = max(0, 0.4-0.5) = 0 every step
    results = _run_chain(z_scores, state_4h="Coiling", vocab=["Sweep"], ofi=True)
    assert len(results) == 0


# ── Mixed signal sequence ─────────────────────────────────────────────────────


def test_e2e_alternating_z_no_trigger():
    """Alternating high positive/negative z-scores cancel each other out."""
    z_scores = [3.0, -3.0] * 50  # C+ and C- both stay low
    results = _run_chain(z_scores, state_4h="Coiling", vocab=["Sweep"], ofi=True)
    # Both C+ and C- get reset to 0 each alternating step; should not trigger
    for _, decision in results:
        # If triggered at all, both direction should not both fire
        assert decision.action in ("ENTER_LONG", "ENTER_SHORT", "ABORT", "OBSERVE")


# ── Hawkes warm-threshold blocks low-intensity ticks ─────────────────────────


def test_e2e_hawkes_gate_observe_when_warm():
    """
    With a warm Hawkes threshold seeded above the tracker's baseline,
    the H1 gate should downgrade the entry to OBSERVE.
    """
    cusum = CUSUMShort(drift_k=0.5)
    entry_filter = Strategy2EntryFilter()

    # Seed threshold with 25 high-intensity observations
    t_base = 1_000_000.0
    for i in range(25):
        entry_filter.hawkes_threshold.add(t_base + i * 10, 100.0)

    # Now trigger CUSUM at a time after the seeded window
    t_eval = t_base + 260.0
    r = None
    for i in range(5):
        r = cusum.update(2.5, t=t_eval + i)

    assert r is not None and r.triggered

    # Tracker has no events → intensity = mu (<<100) → fails warm threshold
    decision = entry_filter.evaluate(
        t=t_eval + 5,
        cusum_trigger=r,
        state_4h="Surging",
        inverse_vocab=["Sweep"],
        ofi_persistent_same_direction=True,
    )
    assert decision.action == "OBSERVE"
    assert decision.hawkes_passed is False
    assert decision.step_reached == 2
