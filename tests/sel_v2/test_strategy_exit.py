"""Tests for sel_v2.strategies.strategy_exit (v2.0 §13.3 + §14.3)."""
import pytest
from datetime import datetime, timezone, timedelta

from sel_v2.strategies.cusum_short import CUSUMTrigger
from sel_v2.strategies.strategy_exit import (
    check_strategy1_exit,
    check_strategy2_exit,
    ExitDecision,
    S1_DRAWDOWN_STOP,
    S2_DRAWDOWN_STOP,
    S1_TIME_STOP_HOURS,
    S2_TIME_STOP_HOURS,
)

_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_ENTRY = datetime(2025, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

_ENTRY_PRICE = 50_000.0


def _no_trigger() -> CUSUMTrigger:
    return CUSUMTrigger(
        triggered=False, direction=None,
        cusum_positive=0.5, cusum_negative=0.3,
        threshold=2.0, intensity_coeff=0.0,
    )


def _long_trigger() -> CUSUMTrigger:
    return CUSUMTrigger(
        triggered=True, direction="LONG",
        cusum_positive=3.0, cusum_negative=0.0,
        threshold=2.0, intensity_coeff=1.5,
    )


def _short_trigger() -> CUSUMTrigger:
    return CUSUMTrigger(
        triggered=True, direction="SHORT",
        cusum_positive=0.0, cusum_negative=3.0,
        threshold=2.0, intensity_coeff=1.5,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Strategy 1 exit tests
# ══════════════════════════════════════════════════════════════════════════════

def test_s1_hold_when_no_condition_triggered():
    d = check_strategy1_exit(
        direction="LONG",
        entry_price=_ENTRY_PRICE,
        entry_time=_ENTRY,
        mark_price=51_000.0,     # +2% — fine
        current_time=_NOW,
        state_4h="Surging",
        cusum_trigger=_no_trigger(),
    )
    assert d.action == "HOLD"


def test_s1_cascade_exits_immediately():
    d = check_strategy1_exit(
        direction="LONG",
        entry_price=_ENTRY_PRICE,
        entry_time=_ENTRY,
        mark_price=51_000.0,
        current_time=_NOW,
        state_4h="Cascade",
        cusum_trigger=_no_trigger(),
    )
    assert d.action == "EXIT_FULL"
    assert "Cascade" in d.reason
    assert d.priority == 1


def test_s1_drawdown_3pct_triggers_exit():
    mark = _ENTRY_PRICE * (1 + S1_DRAWDOWN_STOP)  # exactly -3%
    d = check_strategy1_exit(
        direction="LONG",
        entry_price=_ENTRY_PRICE,
        entry_time=_ENTRY,
        mark_price=mark,
        current_time=_NOW,
        state_4h="Coiling",
        cusum_trigger=_no_trigger(),
    )
    assert d.action == "EXIT_FULL"
    assert "Drawdown" in d.reason


def test_s1_cusum_reverse_long_to_short_exits():
    d = check_strategy1_exit(
        direction="LONG",
        entry_price=_ENTRY_PRICE,
        entry_time=_ENTRY,
        mark_price=51_000.0,
        current_time=_NOW,
        state_4h="Coiling",
        cusum_trigger=_short_trigger(),
    )
    assert d.action == "EXIT_FULL"
    assert "CUSUM reverse" in d.reason


def test_s1_cusum_same_direction_does_not_exit():
    d = check_strategy1_exit(
        direction="LONG",
        entry_price=_ENTRY_PRICE,
        entry_time=_ENTRY,
        mark_price=51_000.0,
        current_time=_NOW,
        state_4h="Coiling",
        cusum_trigger=_long_trigger(),
    )
    assert d.action == "HOLD"


def test_s1_state_exit_surging_to_drifting():
    d = check_strategy1_exit(
        direction="LONG",
        entry_price=_ENTRY_PRICE,
        entry_time=_ENTRY,
        mark_price=51_000.0,
        current_time=_NOW,
        state_4h="Drifting-Calm",
        cusum_trigger=_no_trigger(),
        prev_state_4h="Surging",
    )
    assert d.action == "EXIT_FULL"
    assert "Surging" in d.reason


def test_s1_critical_reduce_50_once():
    d = check_strategy1_exit(
        direction="LONG",
        entry_price=_ENTRY_PRICE,
        entry_time=_ENTRY,
        mark_price=51_000.0,
        current_time=_NOW,
        state_4h="Critical",
        cusum_trigger=_no_trigger(),
        prev_state_4h="Coiling",
        critical_already_reduced=False,
    )
    assert d.action == "REDUCE_50"
    assert d.partial_fraction == 0.50


def test_s1_critical_does_not_reduce_twice():
    d = check_strategy1_exit(
        direction="LONG",
        entry_price=_ENTRY_PRICE,
        entry_time=_ENTRY,
        mark_price=51_000.0,
        current_time=_NOW,
        state_4h="Critical",
        cusum_trigger=_no_trigger(),
        prev_state_4h="Coiling",
        critical_already_reduced=True,
    )
    assert d.action == "HOLD"


def test_s1_time_stop_7d():
    entry_old = _NOW - timedelta(hours=S1_TIME_STOP_HOURS)
    d = check_strategy1_exit(
        direction="LONG",
        entry_price=_ENTRY_PRICE,
        entry_time=entry_old,
        mark_price=51_000.0,
        current_time=_NOW,
        state_4h="Coiling",
        cusum_trigger=_no_trigger(),
    )
    assert d.action == "EXIT_FULL"
    assert "Time stop" in d.reason


def test_s1_cascade_beats_drawdown():
    """Cascade priority 1 must beat drawdown priority 2."""
    mark = _ENTRY_PRICE * (1 + S1_DRAWDOWN_STOP)
    d = check_strategy1_exit(
        direction="LONG",
        entry_price=_ENTRY_PRICE,
        entry_time=_ENTRY,
        mark_price=mark,
        current_time=_NOW,
        state_4h="Cascade",
        cusum_trigger=_no_trigger(),
    )
    assert d.action == "EXIT_FULL"
    assert d.priority == 1


# ══════════════════════════════════════════════════════════════════════════════
# Strategy 2 exit tests
# ══════════════════════════════════════════════════════════════════════════════

def _s2_hold(**kwargs) -> ExitDecision:
    defaults = dict(
        direction="LONG",
        entry_price=_ENTRY_PRICE,
        entry_time=_ENTRY,
        mark_price=51_000.0,
        current_time=_NOW,
        state_4h="Coiling",
        cusum_trigger=_no_trigger(),
        cusum_peak_since_entry=0,  # no peak → batch exit branch skipped
        batches_triggered=0,
    )
    defaults.update(kwargs)
    return check_strategy2_exit(**defaults)


def test_s2_hold_when_no_condition():
    d = _s2_hold()
    assert d.action == "HOLD"


def test_s2_cascade_exits():
    d = _s2_hold(state_4h="Cascade")
    assert d.action == "EXIT_FULL"
    assert d.priority == 1


def test_s2_drawdown_2pct():
    mark = _ENTRY_PRICE * (1 + S2_DRAWDOWN_STOP)
    d = _s2_hold(mark_price=mark)
    assert d.action == "EXIT_FULL"
    assert "Drawdown" in d.reason


def test_s2_cusum_reverse_exits_full():
    d = _s2_hold(cusum_trigger=_short_trigger())
    assert d.action == "EXIT_FULL"
    assert "CUSUM reverse" in d.reason


def test_s2_new_high_sl_same_direction():
    """Same-direction CUSUM trigger while holding = new-high SL."""
    d = _s2_hold(cusum_trigger=_long_trigger())
    assert d.action == "EXIT_FULL"
    assert "new-high" in d.reason.lower()


def test_s2_batch1_at_50pct_decay():
    cusum_trigger = CUSUMTrigger(
        triggered=False, direction=None,
        cusum_positive=1.5, cusum_negative=0.0,  # 1.5 / 3.0 = 50%
        threshold=2.0, intensity_coeff=0.0,
    )
    d = _s2_hold(
        cusum_trigger=cusum_trigger,
        cusum_peak_since_entry=3.0,
        batches_triggered=0,
    )
    assert d.action == "EXIT_BATCH_33"
    assert "batch 1/3" in d.reason


def test_s2_batch2_at_25pct_decay():
    cusum_trigger = CUSUMTrigger(
        triggered=False, direction=None,
        cusum_positive=0.75, cusum_negative=0.0,  # 0.75 / 3.0 = 25%
        threshold=2.0, intensity_coeff=0.0,
    )
    d = _s2_hold(
        cusum_trigger=cusum_trigger,
        cusum_peak_since_entry=3.0,
        batches_triggered=1,
    )
    assert d.action == "EXIT_BATCH_33"
    assert "batch 2/3" in d.reason


def test_s2_batch3_at_baseline():
    cusum_trigger = CUSUMTrigger(
        triggered=False, direction=None,
        cusum_positive=0.0, cusum_negative=0.0,
        threshold=2.0, intensity_coeff=0.0,
    )
    d = _s2_hold(
        cusum_trigger=cusum_trigger,
        cusum_peak_since_entry=3.0,
        batches_triggered=2,
    )
    assert d.action == "EXIT_FULL"
    assert "batch 3/3" in d.reason


def test_s2_batch_not_retriggered_when_already_done():
    cusum_trigger = CUSUMTrigger(
        triggered=False, direction=None,
        cusum_positive=0.0, cusum_negative=0.0,
        threshold=2.0, intensity_coeff=0.0,
    )
    d = _s2_hold(
        cusum_trigger=cusum_trigger,
        cusum_peak_since_entry=3.0,
        batches_triggered=3,  # all batches done
    )
    assert d.action == "HOLD"


def test_s2_time_stop_24h():
    entry_old = _NOW - timedelta(hours=S2_TIME_STOP_HOURS)
    d = _s2_hold(entry_time=entry_old)
    assert d.action == "EXIT_FULL"
    assert "Time stop" in d.reason
