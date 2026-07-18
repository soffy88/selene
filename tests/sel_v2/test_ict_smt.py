"""Tests for sel_v2.offline.ict_smt (ICT-8 SMT divergence, preregistered)."""

import numpy as np

from sel_v2.offline.ict_smt import detect_smt
from sel_v2.offline.lens_common import compute_atr


def _path(legs, base=100.0, leg_len=8, step=2.0):
    """Build a close path from a list of per-leg net moves (in steps)."""
    out = [base]
    for mult in legs:
        d = step * mult / leg_len
        for _ in range(leg_len):
            out.append(out[-1] + d)
    return np.array(out)


def _atr(close):
    return compute_atr(close * 1.001, close * 0.999, close)


def test_bearish_smt_when_one_asset_fails_to_confirm_hh():
    # A: up, down, HIGHER high, down  → HH at second top
    a = _path([+8, -4, +10, -4])
    # B: up, down, LOWER high, down   → LH at its second top (same bar grid)
    b = _path([+8, -4, +2, -4])
    events = detect_smt(a, _atr(a), b, _atr(b))
    bear = [e for e in events if e.direction == -1]
    assert bear, "HH in A + LH in B must produce a bearish SMT"
    # causal: the event is knowable only after both second tops CONFIRMED,
    # i.e. strictly after the second top's pivot bar (index 16 area)
    assert all(e.idx > 16 for e in bear)


def test_no_smt_when_both_confirm():
    a = _path([+8, -4, +10, -4])
    b = _path([+8, -4, +9, -4])  # B also makes a higher high
    events = detect_smt(a, _atr(a), b, _atr(b))
    assert [e for e in events if e.direction == -1] == []


def test_bullish_smt_at_lows_mirror():
    a = _path([-8, +4, -10, +4])  # lower low
    b = _path([-8, +4, -2, +4])  # higher low → bullish SMT
    events = detect_smt(a, _atr(a), b, _atr(b))
    assert [e for e in events if e.direction == 1]


def test_symmetry_leader_roles():
    # swap A/B — the same divergence must still be found (leader flips)
    a = _path([+8, -4, +10, -4])
    b = _path([+8, -4, +2, -4])
    e1 = detect_smt(a, _atr(a), b, _atr(b))
    e2 = detect_smt(b, _atr(b), a, _atr(a))
    assert len(e1) == len(e2)
    assert {(e.idx, e.direction) for e in e1} == {(e.idx, e.direction) for e in e2}
