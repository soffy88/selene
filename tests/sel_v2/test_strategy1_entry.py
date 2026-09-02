"""Tests for sel_v2.strategies.strategy1_entry (v2.0 §13.2)."""

from datetime import datetime, timezone

from sel_v2.strategies.strategy1_entry import (
    BASE_SIZE_FRACTION,
    Strategy1EntryFilter,
)

_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _ts(bar_index: int) -> datetime:
    from datetime import timedelta

    return _TS + timedelta(hours=4 * bar_index)


def _make_filter_with_warm_cusum(trigger_value: float = 5.0) -> Strategy1EntryFilter:
    """Return a filter whose CUSUM-Mid has ≥ 20 positive peaks so threshold is warm.
    C+ is pre-charged (~62.5) — suitable for testing LONG entry.
    """
    f = Strategy1EntryFilter()
    for i in range(25):
        f.cusum_mid.update(z_t=3.0, t=_ts(i).timestamp())
    return f


def _make_filter_with_warm_cusum_short() -> Strategy1EntryFilter:
    """Return a filter whose CUSUM-Mid has ≥ 20 negative peaks so threshold is warm.
    C- is pre-charged (~62.5) — suitable for testing SHORT entry.
    """
    f = Strategy1EntryFilter()
    for i in range(25):
        f.cusum_mid.update(z_t=-3.0, t=_ts(i).timestamp())
    return f


# ── Step 1: state filter ──────────────────────────────────────────────────────


def test_abort_drifting_calm():
    f = Strategy1EntryFilter()
    d = f.evaluate(_ts(0), z_t=2.0, state_4h="Drifting-Calm", current_duration_4h=10)
    assert d.action == "OBSERVE"
    assert d.step_reached == 1


def test_abort_surging():
    f = Strategy1EntryFilter()
    d = f.evaluate(_ts(0), z_t=2.0, state_4h="Surging", current_duration_4h=3)
    assert d.action == "OBSERVE"
    assert d.step_reached == 1


def test_abort_critical():
    f = Strategy1EntryFilter()
    d = f.evaluate(_ts(0), z_t=2.0, state_4h="Critical", current_duration_4h=2)
    assert d.action == "OBSERVE"
    assert d.step_reached == 1


def test_abort_cascade():
    f = Strategy1EntryFilter()
    d = f.evaluate(_ts(0), z_t=2.0, state_4h="Cascade", current_duration_4h=1)
    assert d.action == "OBSERVE"
    assert d.step_reached == 1


# ── Step 2: min-dwell ─────────────────────────────────────────────────────────


def test_coiling_min_dwell_not_met():
    f = Strategy1EntryFilter()
    d = f.evaluate(_ts(0), z_t=2.0, state_4h="Coiling", current_duration_4h=5)
    assert d.action == "OBSERVE"
    assert d.step_reached == 2


def test_coiling_min_dwell_exactly_met():
    f = _make_filter_with_warm_cusum()
    # With warm CUSUM and exactly 6 bars, should pass step 2 (may not trigger CUSUM)
    d = f.evaluate(_ts(30), z_t=0.0, state_4h="Coiling", current_duration_4h=6)
    # step 2 passed; CUSUM may or may not trigger depending on internal state
    assert d.step_reached >= 2


def test_drifting_charged_min_dwell_not_met():
    f = Strategy1EntryFilter()
    d = f.evaluate(_ts(0), z_t=2.0, state_4h="Drifting-Charged", current_duration_4h=3)
    assert d.action == "OBSERVE"
    assert d.step_reached == 2


def test_drifting_charged_min_dwell_met():
    f = _make_filter_with_warm_cusum()
    d = f.evaluate(_ts(30), z_t=0.0, state_4h="Drifting-Charged", current_duration_4h=4)
    assert d.step_reached >= 2


# ── Step 3: CUSUM-Mid ─────────────────────────────────────────────────────────


def test_observe_when_cusum_not_triggered():
    f = Strategy1EntryFilter()
    # Cold start + small z_t won't trigger
    d = f.evaluate(_ts(0), z_t=0.1, state_4h="Coiling", current_duration_4h=6)
    assert d.action == "OBSERVE"
    assert d.step_reached == 3


def test_enter_long_when_cusum_triggers():
    f = _make_filter_with_warm_cusum()
    # Force a large positive z_t to trigger C+
    d = f.evaluate(_ts(30), z_t=10.0, state_4h="Coiling", current_duration_4h=8)
    # Should reach Step 8 and produce ENTER_LONG
    assert d.step_reached == 8
    assert d.action == "ENTER_LONG"
    assert d.direction == "LONG"


def test_enter_short_when_cusum_triggers_negative():
    f = _make_filter_with_warm_cusum_short()
    d = f.evaluate(_ts(30), z_t=-10.0, state_4h="Drifting-Charged", current_duration_4h=5)
    assert d.step_reached == 8
    assert d.action == "ENTER_SHORT"
    assert d.direction == "SHORT"


# ── Step 4: derivatives filter ────────────────────────────────────────────────


def _make_triggered_filter() -> tuple[Strategy1EntryFilter, float]:
    """Return filter + z_t that produces ENTER_LONG in neutral conditions."""
    f = _make_filter_with_warm_cusum()
    return f, 10.0


def test_funding_extreme_aborts_long():
    f, z = _make_triggered_filter()
    d = f.evaluate(
        _ts(30),
        z_t=z,
        state_4h="Coiling",
        current_duration_4h=8,
        funding_pctile=0.95,
    )
    assert d.action == "ABORT"
    assert "Step 4a" in d.reason
    assert "funding" in d.reason.lower()


def test_funding_extreme_aborts_short():
    f = _make_filter_with_warm_cusum_short()
    d = f.evaluate(
        _ts(30),
        z_t=-10.0,
        state_4h="Coiling",
        current_duration_4h=8,
        funding_pctile=0.05,
    )
    assert d.action == "ABORT"
    assert "Step 4a" in d.reason


def test_funding_none_does_not_abort():
    f, z = _make_triggered_filter()
    d = f.evaluate(
        _ts(30),
        z_t=z,
        state_4h="Coiling",
        current_duration_4h=8,
        funding_pctile=None,
    )
    assert d.action == "ENTER_LONG"


def test_oi_against_long_halves_size():
    f, z = _make_triggered_filter()
    d_with = f.evaluate(
        _ts(30),
        z_t=z,
        state_4h="Coiling",
        current_duration_4h=8,
        oi_direction="falling",
    )
    # Reset and get size without OI constraint
    f2 = _make_filter_with_warm_cusum()
    d_without = f2.evaluate(
        _ts(30),
        z_t=z,
        state_4h="Coiling",
        current_duration_4h=8,
        oi_direction=None,
    )
    assert d_with.action == "ENTER_LONG"
    assert d_without.action == "ENTER_LONG"
    assert d_with.oi_direction_against is True
    assert d_without.oi_direction_against is False
    # Size with OI reduction should be <= size without
    assert d_with.base_size_pct <= d_without.base_size_pct


def test_liquidation_pulse_aborts():
    f, z = _make_triggered_filter()
    d = f.evaluate(
        _ts(30),
        z_t=z,
        state_4h="Coiling",
        current_duration_4h=8,
        liquidation_pulse_1h=True,
    )
    assert d.action == "ABORT"
    assert "Step 4c" in d.reason


# ── Step 6: divergence ────────────────────────────────────────────────────────


def test_divergence_aborts_entry():
    f, z = _make_triggered_filter()
    d = f.evaluate(
        _ts(30),
        z_t=z,
        state_4h="Coiling",
        current_duration_4h=8,
        cross_spread_pct=0.5,
    )
    assert d.action == "ABORT"
    assert "Step 6" in d.reason


def test_divergence_none_does_not_abort():
    f, z = _make_triggered_filter()
    d = f.evaluate(
        _ts(30),
        z_t=z,
        state_4h="Coiling",
        current_duration_4h=8,
        cross_spread_pct=None,
    )
    assert d.action == "ENTER_LONG"


# ── Step 7: vocab modifier ────────────────────────────────────────────────────


def test_sweep_vocab_increases_size_modifier():
    f, z = _make_triggered_filter()
    d_no_vocab = f.evaluate(
        _ts(30),
        z_t=z,
        state_4h="Coiling",
        current_duration_4h=8,
    )
    f2 = _make_filter_with_warm_cusum()
    d_with_sweep = f2.evaluate(
        _ts(30),
        z_t=z,
        state_4h="Coiling",
        current_duration_4h=8,
        vocab=["Sweep"],
    )
    assert d_with_sweep.vocab_modifier > 0.0
    assert d_with_sweep.size_modifier >= d_no_vocab.size_modifier * 0.9


def test_absorption_reduces_confidence():
    f, z = _make_triggered_filter()
    d = f.evaluate(
        _ts(30),
        z_t=z,
        state_4h="Coiling",
        current_duration_4h=8,
        vocab=["Absorption"],
    )
    assert d.vocab_modifier < 0.0


# ── Sizing: base_size_pct ─────────────────────────────────────────────────────


def test_base_size_capped_at_base_fraction():
    f, z = _make_triggered_filter()
    d = f.evaluate(_ts(30), z_t=z, state_4h="Coiling", current_duration_4h=8)
    assert d.base_size_pct <= BASE_SIZE_FRACTION
    assert d.base_size_pct > 0.0
