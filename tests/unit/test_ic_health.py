"""IC-decay closed-loop sizing multiplier (Phase 6)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from services.signal.ic_health import ic_health_scalar


class TestICHealthScalar:
    def test_neutral_before_min_trades(self):
        # Even a terrible IC must not gate until enough outcomes exist.
        assert ic_health_scalar(-0.5, n=5, min_trades=20) == 1.0
        assert ic_health_scalar(None, n=100) == 1.0

    def test_full_size_when_ic_good(self):
        assert ic_health_scalar(0.08, n=50, good=0.05) == 1.0
        assert ic_health_scalar(0.05, n=50, good=0.05) == 1.0

    def test_floor_when_ic_at_or_below_floor(self):
        assert ic_health_scalar(0.0, n=50, floor=0.0, min_scale=0.25) == 0.25
        assert ic_health_scalar(-0.3, n=50, floor=0.0, min_scale=0.25) == 0.25

    def test_never_returns_zero(self):
        # No feedback deadlock: trading is throttled, never fully stopped.
        for ic in (-1.0, -0.1, 0.0, 0.01):
            assert ic_health_scalar(ic, n=50, min_scale=0.25) >= 0.25

    def test_monotonic_ramp_between_floor_and_good(self):
        lo = ic_health_scalar(0.01, n=50, floor=0.0, good=0.05, min_scale=0.25)
        mid = ic_health_scalar(0.025, n=50, floor=0.0, good=0.05, min_scale=0.25)
        hi = ic_health_scalar(0.04, n=50, floor=0.0, good=0.05, min_scale=0.25)
        assert 0.25 < lo < mid < hi < 1.0

    def test_midpoint_value(self):
        # IC exactly halfway -> halfway between min_scale and 1.0
        v = ic_health_scalar(0.025, n=50, floor=0.0, good=0.05, min_scale=0.25)
        assert abs(v - (0.25 + 0.75 * 0.5)) < 1e-6
