"""Phase 5 wiring — perpetual funding folded into cost-adjusted Kelly sizing."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from services.portfolio.capital.kelly import CapitalAllocator


def _alloc():
    return CapitalAllocator(total_equity=100_000)


def test_no_funding_matches_base_cost():
    a = _alloc()
    s = a.compute_position_size("LONG_SETUP", 0.6, 2.0, 100.0, 95.0)
    assert abs(s["cost_with_funding"] - a.round_trip_cost) < 1e-9


def test_long_paying_funding_raises_cost_and_shrinks_kelly():
    a = _alloc()
    base = a.compute_position_size("LONG_SETUP", 0.6, 2.0, 100.0, 95.0)
    fund = a.compute_position_size("LONG_SETUP", 0.6, 2.0, 100.0, 95.0,
                                   funding_rate=0.01, side="LONG", hold_hours=72)
    assert fund["cost_with_funding"] > base["cost_with_funding"]
    assert fund["kelly_fraction"] <= base["kelly_fraction"]


def test_short_receiving_funding_not_credited():
    a = _alloc()
    base = a.compute_position_size("LONG_SETUP", 0.6, 2.0, 100.0, 95.0)
    sh = a.compute_position_size("LONG_SETUP", 0.6, 2.0, 100.0, 95.0,
                                 funding_rate=0.01, side="SHORT", hold_hours=72)
    # favorable funding must not be counted as negative cost (that would inflate Kelly)
    assert abs(sh["cost_with_funding"] - base["cost_with_funding"]) < 1e-9


def test_funding_drag_scales_with_hold():
    a = _alloc()
    short_hold = a.compute_position_size("LONG_SETUP", 0.6, 2.0, 100.0, 95.0,
                                         funding_rate=0.005, side="LONG", hold_hours=8)
    long_hold = a.compute_position_size("LONG_SETUP", 0.6, 2.0, 100.0, 95.0,
                                        funding_rate=0.005, side="LONG", hold_hours=72)
    assert long_hold["cost_with_funding"] > short_hold["cost_with_funding"]
