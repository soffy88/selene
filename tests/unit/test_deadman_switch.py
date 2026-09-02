"""Dead-man's-switch staleness logic (optimization item #10)."""

from services.healthcheck.main import DEADMAN_STALE_S, deadman_age


def test_fresh_heartbeat_not_stale():
    age = deadman_age({"ts": 1000.0, "open_orders": 2}, 1000.0 + 30)
    assert age == 30
    assert age <= DEADMAN_STALE_S


def test_stale_heartbeat_detected():
    age = deadman_age({"ts": 1000.0}, 1000.0 + 600)
    assert age == 600
    assert age > DEADMAN_STALE_S


def test_missing_ts_treated_as_ancient():
    # No ts field → age == now_epoch (huge) → always stale.
    age = deadman_age({}, 5000.0)
    assert age == 5000.0
    assert age > DEADMAN_STALE_S
