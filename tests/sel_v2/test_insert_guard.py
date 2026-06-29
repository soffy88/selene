"""Tests for the collector InsertGuard (optimization item #2)."""
import pytest

from sel_v2.data.insert_guard import InsertGuard, InsertFailureLimitExceeded


def test_isolated_failures_do_not_trip():
    g = InsertGuard("t", max_consecutive=3)
    for _ in range(10):
        g.fail(ValueError("x"))
        g.ok()  # success resets between failures
    assert g.consecutive == 0
    assert g.total_failed == 10


def test_consecutive_failures_raise():
    g = InsertGuard("t", max_consecutive=3)
    g.fail(ValueError("a"))
    g.fail(ValueError("b"))
    with pytest.raises(InsertFailureLimitExceeded):
        g.fail(ValueError("c"))


def test_success_resets_counter():
    g = InsertGuard("t", max_consecutive=3)
    g.fail(ValueError("a"))
    g.fail(ValueError("b"))
    g.ok()
    assert g.consecutive == 0
    # two more failures should not yet trip (counter was reset)
    g.fail(ValueError("c"))
    g.fail(ValueError("d"))
    assert g.consecutive == 2


def test_counts_track():
    g = InsertGuard("t", max_consecutive=100)
    for _ in range(5):
        g.ok()
    for _ in range(3):
        g.fail(ValueError("x"))
    assert g.total_ok == 5
    assert g.total_failed == 3
