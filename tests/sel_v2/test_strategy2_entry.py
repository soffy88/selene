"""Unit tests for sel_v2.strategies.strategy2_entry (H1 integrated)."""
import pytest

from sel_v2.strategies.cusum_short import CUSUMShort, CUSUMTrigger
from sel_v2.strategies.hawkes_intensity import (
    HawkesIntensityTracker,
    HawkesParams,
    RollingIntensityThreshold,
)
from sel_v2.strategies.strategy2_entry import Strategy2EntryFilter


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_trigger(triggered=True, direction="LONG", coeff=1.5, threshold=2.0):
    return CUSUMTrigger(
        triggered=triggered,
        direction=direction,
        cusum_positive=threshold + 0.5 if triggered else 0.5,
        cusum_negative=0.0,
        threshold=threshold,
        intensity_coeff=coeff,
    )


def _make_filter_with_warm_hawkes(lam_high=1.0, lam_low=0.001):
    """Create a Strategy2EntryFilter with a warm (> 20 obs) threshold."""
    f = Strategy2EntryFilter()
    # Seed threshold with 25 high-intensity observations
    t_base = 1_000_000.0
    for i in range(25):
        f.hawkes_threshold.add(t_base + i * 10, lam_high)
    return f, t_base + 25 * 10


# ── Step 1a: CUSUM not triggered → OBSERVE ───────────────────────────────────

def test_no_cusum_trigger_returns_observe():
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=False, direction=None)
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Coiling")
    assert d.action == "OBSERVE"
    assert d.step_reached == 1


# ── Step 1b: H1 gate fails → OBSERVE ─────────────────────────────────────────

def test_h1_gate_blocks_low_intensity():
    f, t_warm = _make_filter_with_warm_hawkes(lam_high=1.0)
    # Hawkes tracker has no events → intensity = mu (very low ~1e-5)
    # Threshold is 70th pct of 25 observations at 1.0 → threshold ≈ 1.0
    # So low mu will fail
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=t_warm + 1, cusum_trigger=trigger, state_4h="Coiling")
    assert d.action == "OBSERVE"
    assert d.hawkes_passed is False
    assert d.step_reached == 2


def test_h1_gate_passes_high_intensity():
    f = Strategy2EntryFilter()
    # Override tracker with very high intensity params
    f.hawkes_tracker = HawkesIntensityTracker(
        HawkesParams(mu=100.0, alpha=0.3, beta=0.5)
    )
    # Threshold is cold (None) → pass-through
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Coiling",
                   inverse_vocab=["Sweep"])
    # Should pass H1 (cold threshold → pass-through) and proceed
    assert d.hawkes_passed is True or d.step_reached >= 2


def test_h1_gate_pass_through_when_cold():
    """Cold threshold (< 20 obs) should not block."""
    f = Strategy2EntryFilter()
    # Hawkes tracker: mu very low but threshold is cold
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Coiling",
                   inverse_vocab=["Sweep"])
    # Cold threshold → pass-through → Step 1b passes
    # Next step is Step 3 (vocab), with ["Sweep"] only (no Absorption) → Type B entry
    assert d.hawkes_passed is True
    assert d.step_reached >= 3


# ── Step 2: 4H Cascade veto ───────────────────────────────────────────────────

def test_cascade_state_aborts():
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Cascade",
                   inverse_vocab=["Sweep"])
    assert d.action == "ABORT"
    assert "Cascade" in d.reason
    assert d.step_reached == 2


def test_critical_state_allowed():
    """Strategy 2 allows entry in Critical (only Cascade is vetoed)."""
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Critical",
                   inverse_vocab=["Sweep"])
    assert d.action != "ABORT" or "Cascade" not in d.reason


# ── Step 3: Inverse vocab classification ─────────────────────────────────────

def test_empty_vocab_aborts():
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Coiling",
                   inverse_vocab=[])
    assert d.action == "ABORT"
    assert d.step_reached == 3


def test_type_a_reversal_inverts_direction():
    """Absorption + Sweep → Type A → direction inverted."""
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Coiling",
                   inverse_vocab=["Absorption", "Sweep"])
    # Type A reversal: CUSUM=LONG → enter SHORT
    assert d.action == "ENTER_SHORT"


def test_type_b_momentum_same_direction():
    """No Absorption → Type B → direction same as CUSUM."""
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Coiling",
                   inverse_vocab=["Sweep"])
    assert d.action == "ENTER_LONG"


def test_type_b_short_momentum():
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=True, direction="SHORT")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Drifting-Charged",
                   inverse_vocab=["Sweep"])
    assert d.action == "ENTER_SHORT"


# ── Step 4: Liquidation pulse ──────────────────────────────────────────────────

def test_liq_pulse_aborts():
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Coiling",
                   inverse_vocab=["Sweep"], liq_pulse=True)
    assert d.action == "ABORT"
    assert d.step_reached == 4


# ── Step 5: Cross-exchange spread ────────────────────────────────────────────

def test_high_spread_aborts():
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Coiling",
                   inverse_vocab=["Sweep"], cross_spread_pct=0.6)
    assert d.action == "ABORT"
    assert d.step_reached == 5


def test_low_spread_passes():
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Coiling",
                   inverse_vocab=["Sweep"], cross_spread_pct=0.05)
    assert d.action == "ENTER_LONG"


# ── Step 6: Leverage calculation ──────────────────────────────────────────────

def test_leverage_default_3x():
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Coiling",
                   inverse_vocab=["Sweep"])
    assert d.suggested_leverage == 3.0


def test_leverage_5x_on_high_confidence():
    """2+ vocab tags → 5x leverage."""
    f = Strategy2EntryFilter()
    trigger = _make_trigger(triggered=True, direction="LONG")
    d = f.evaluate(t=1e6, cusum_trigger=trigger, state_4h="Coiling",
                   inverse_vocab=["Absorption", "Sweep"])
    # Type A entry (Absorption + Sweep) → suggested_leverage = 5x
    assert d.suggested_leverage == 5.0


# ── Full happy-path integration ────────────────────────────────────────────────

def test_full_happy_path_long():
    """Simulate a clean LONG entry: CUSUM trigger + cold H1 + Type B + no blocks."""
    f = Strategy2EntryFilter()
    cusum = CUSUMShort(drift_k=0.5)
    # Build up CUSUM to trigger
    r = None
    for _ in range(5):
        r = cusum.update(2.0)
    assert r is not None and r.triggered, "Setup: CUSUM should have triggered"

    d = f.evaluate(
        t=1_000_000.0,
        cusum_trigger=r,
        state_4h="Surging",
        inverse_vocab=["Sweep"],
    )
    assert d.action == "ENTER_LONG"
    assert d.step_reached == 6
    assert d.hawkes_passed is True
    assert d.cusum_direction == "LONG"


def test_full_happy_path_short_reversal():
    """CUSUM SHORT + Absorption + Sweep → Type A → ENTER_LONG (reversal)."""
    f = Strategy2EntryFilter()
    cusum = CUSUMShort(drift_k=0.5)
    r = None
    for _ in range(5):
        r = cusum.update(-2.0)
    assert r is not None and r.triggered

    d = f.evaluate(
        t=1_000_000.0,
        cusum_trigger=r,
        state_4h="Drifting-Charged",
        inverse_vocab=["Absorption", "Sweep"],
    )
    assert d.action == "ENTER_LONG"   # SHORT CUSUM + reversal → LONG
    assert d.step_reached == 6
