"""Unit tests for Strategy-2 Step 5 cross-exchange divergence (Wave S2C Part 3).

Pins the divergence bands: >0.5% aborts, <0.05% clean, mid-band discounts entry_confidence
to 0.7 (and scales size accordingly), and a missing feed (None) degrades — skip, no abort.
"""

from sel_v2.strategies.cusum_short import CUSUMTrigger
from sel_v2.strategies.inverse_vocab import AbsorptionSignal, SweepSignal
from sel_v2.strategies.strategy2_entry import Strategy2EntryFilter


def _warm_filter():
    f = Strategy2EntryFilter()
    for t in range(200):
        f.hawkes_tracker.add_event(float(t))
    for t in range(50):
        f.hawkes_threshold.add(float(t), 0.0)
    return f


def _eval(cross):
    f = _warm_filter()
    trig = CUSUMTrigger(
        triggered=True,
        direction="SHORT",
        cusum_positive=0.0,
        cusum_negative=3.0,
        threshold=2.0,
        intensity_coeff=1.5,
    )
    # Type B momentum path (ofi persistent, no absorption) so we reach Step 5/6.
    return f.evaluate(
        t=1e6,
        cusum_trigger=trig,
        state_4h="Surging",
        ofi_persistent_same_direction=True,
        absorption=AbsorptionSignal(present=False),
        sweep=SweepSignal(present=False),
        cross_spread_pct=cross,
    )


def test_no_feed_degrades_to_pass():
    d = _eval(None)
    assert d.action == "ENTER_SHORT" and d.entry_confidence == 1.0


def test_clean_spread_full_confidence():
    d = _eval(0.02)  # < 0.05%
    assert d.action == "ENTER_SHORT" and d.entry_confidence == 1.0


def test_mid_band_discounts_confidence_and_size():
    d = _eval(0.2)  # 0.05% .. 0.5%
    assert d.action == "ENTER_SHORT"
    assert d.entry_confidence == 0.7
    # base_size_pct = 0.10 * max(1, coeff=1.5) * 0.7 = 0.105
    assert abs(d.base_size_pct - 0.105) < 1e-9


def test_large_divergence_aborts():
    d = _eval(0.8)  # > 0.5%
    assert d.action == "ABORT" and d.step_reached == 5
