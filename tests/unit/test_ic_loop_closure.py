"""IC-loop closure tests (optimization item #4).

Previously ICTracker.record_outcome was never called, so the IC-decay throttle
was permanently inert. SignalService.record_signal_outcome is the dispatch the
order.lifecycle consumer calls when a position closes.
"""

from services.signal.main import SignalService


def test_outcome_backfills_originating_tracker():
    svc = SignalService()
    t = svc._get_ic("BTCUSDT")
    t.record_signal("sig1", 0.8, 100.0)
    assert svc.record_signal_outcome("sig1", 110.0) is True
    assert len(t._records) == 1
    score, ret, _ = t._records[0]
    assert score == 0.8
    assert abs(ret - 0.10) < 1e-9
    assert "sig1" not in t._pending  # consumed


def test_unknown_signal_is_noop():
    svc = SignalService()
    svc._get_ic("BTCUSDT").record_signal("sig1", 0.5, 100.0)
    assert svc.record_signal_outcome("does-not-exist", 110.0) is False


def test_bad_exit_price_rejected():
    svc = SignalService()
    t = svc._get_ic("BTCUSDT")
    t.record_signal("sig1", 0.5, 100.0)
    assert svc.record_signal_outcome("sig1", 0.0) is False
    assert svc.record_signal_outcome("sig1", None) is False
    assert "sig1" in t._pending  # not consumed by a bad outcome


def test_dispatch_finds_correct_symbol_tracker():
    svc = SignalService()
    bt = svc._get_ic("BTCUSDT")
    et = svc._get_ic("ETHUSDT")
    bt.record_signal("b1", 0.7, 200.0)
    et.record_signal("e1", 0.3, 50.0)
    assert svc.record_signal_outcome("e1", 55.0) is True
    assert len(et._records) == 1
    assert len(bt._records) == 0  # untouched
    assert "b1" in bt._pending


def test_calc_ic_activates_after_enough_outcomes():
    svc = SignalService()
    t = svc._get_ic("BTCUSDT")
    # Feed 12 signal→outcome pairs so calc_ic crosses its n>=10 threshold.
    for i in range(12):
        sid = f"s{i}"
        t.record_signal(sid, 0.1 * i, 100.0)
        svc.record_signal_outcome(sid, 100.0 + i)  # monotonic → positive IC
    stats = t.calc_ic()
    assert stats["ic"] is not None
    assert stats["n"] == 12
