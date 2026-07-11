"""Tests for sel_v2.offline.substate (Wave V22-A, offline gate — v2.2 design draft D3).

Synthetic OHLC paths built with a constant high/low band (close +/- 8) so True Range
(and therefore ATR-14) is a known constant (16) throughout — this makes the 1.5x/0.5x/
1.0x ATR thresholds land on round numbers (24 / 8 / 16) that are easy to hand-verify
against the exact close values chosen below. `leg_path` walks the close series from one
target extreme to the next in unit steps, so every threshold crossing is observed on
its own bar rather than jumped over.

Coverage: RE_PUSH counting, higher-low tracking + stop ratchet, STRUCTURE_BREAK
detection (and leg reset), both TERMINAL_FLAG paths, and the short-side mirror.
"""

from __future__ import annotations

import numpy as np

from sel_v2.offline.substate import Event, SubState, _Structure, compute_substates


def _leg_path(start: float, targets: list[float], step: float = 1.0) -> list[float]:
    """Walk from `start` toward each target in turn, in unit steps."""
    closes = []
    cur = start
    for tgt in targets:
        d = 1 if tgt > cur else -1
        while abs(tgt - cur) > 1e-9:
            nxt = cur + d * step
            if (d > 0 and nxt > tgt) or (d < 0 and nxt < tgt):
                nxt = tgt
            closes.append(nxt)
            cur = nxt
    return closes


def _bars(closes: list[float], band: float = 8.0):
    """high/low/close arrays with a constant band => True Range constant at 2*band."""
    c = np.array(closes, dtype=float)
    return c + band, c - band, c


def _run(pre_pad: list[float], seg_closes: list[float]):
    """Build a Drifting_Calm lead-in + one Surging segment, run compute_substates."""
    full_closes = pre_pad + seg_closes
    high, low, close = _bars(full_closes)
    parent = ["Drifting_Calm"] * len(pre_pad) + ["Surging"] * len(seg_closes)
    return compute_substates(high, low, close, parent), len(pre_pad)


# ── RE_PUSH counting + higher-low stop ratchet ──────────────────────────────


def test_re_push_counts_and_stop_ratchets_to_a_higher_low():
    # ATR stabilizes at 16 (band=8): zigzag=24, pullback=8. Cycle 1: push 1010->1037,
    # pull to 1005 (confirms, retr=32), repush past 1037 -> RE_PUSH #1, stop=1005.
    # Cycle 2: push ->1076, pull to 1040 (retr=36, a HIGHER low than 1005), repush
    # past 1076 -> RE_PUSH #2, stop ratchets 1005 -> 1040.
    seg = _leg_path(1010, [1037, 1005, 1040, 1076, 1040, 1077])
    out, pre_n = _run([1010] * 5, seg)

    first_repush = next(b for b in out[pre_n:] if Event.RE_PUSH in b.events)
    assert first_repush.push_count == 1
    assert first_repush.stop_level == 1005.0

    second_repush_idx = [i for i, b in enumerate(out) if Event.RE_PUSH in b.events][1]
    second_repush = out[second_repush_idx]
    assert second_repush.push_count == 2
    assert second_repush.stop_level == 1040.0
    assert (
        second_repush.stop_level > first_repush.stop_level
    )  # ratcheted UP, never down


# ── STRUCTURE_BREAK detection + leg reset ───────────────────────────────────


def test_structure_break_fires_on_close_below_stop_and_resets_the_leg():
    # Same cycle-1 setup (RE_PUSH #1, stop=1005), then a crash to 990 (< stop).
    seg = _leg_path(1010, [1037, 1005, 1040]) + _leg_path(1040, [990])[1:]
    out, pre_n = _run([1010] * 5, seg)

    break_bar = next(b for b in out[pre_n:] if Event.STRUCTURE_BREAK in b.events)
    assert break_bar.push_count == 0  # leg reset, not just flagged
    assert break_bar.stop_level is None

    # nothing after the break re-arms push_count without a fresh RE_PUSH
    assert all(b.push_count == 0 for b in out[out.index(break_bar) :])


# ── Short-side mirror ────────────────────────────────────────────────────────


def test_short_segment_mirrors_direction_and_stop_as_a_lower_high():
    # Declining lead-in => direction=-1. Push down 990->963, pull up to 995
    # (retr=32, confirms), repush past 963 (i.e. below it) -> RE_PUSH #1,
    # stop = 995 (the confirmed lower high), not a "higher low".
    seg = _leg_path(990, [963, 995, 960])
    out, pre_n = _run([1010, 1005, 1000, 995, 990], seg)

    assert out[pre_n].direction == -1
    repush = next(b for b in out[pre_n:] if Event.RE_PUSH in b.events)
    assert repush.push_count == 1
    assert repush.stop_level == 995.0


# ── TERMINAL_FLAG — path A: 2 consecutive declining push legs ───────────────


def test_terminal_flag_path_a_two_consecutive_declining_pushes():
    # Push legs confirm at sizes 60, 35, 30 (strictly decreasing) with each leg's
    # peak BELOW the prior peak, so RE_PUSH (and therefore any stop) never fires —
    # isolates the terminal-magnitude check from the stop/structure-break path.
    closes = _leg_path(1000, [1060, 1010, 1035, 1045, 1000, 1025, 1030, 1005])
    st = _Structure(direction=1, base_price=1000.0, base_idx=0)
    fired_at = None
    for i, c in enumerate(closes, start=1):
        _, events = st.step(i, c, atr_i=16.0)
        if Event.TERMINAL_FLAG in events:
            fired_at = i
            break

    assert fired_at is not None
    assert st.push_leg_sizes == [60.0, 35.0, 30.0]
    assert st.terminal_flag is True


# ── TERMINAL_FLAG — path B: pullback > 1.5x median(prior pullbacks) ─────────


def test_terminal_flag_path_b_pullback_exceeds_1_5x_median():
    # Pullback legs confirm at sizes ~30, ~35, then a 145-point pullback —
    # 145 > 1.5 * median(30, 35) = 48.75. Pushes stay below each prior peak
    # throughout, so no RE_PUSH/stop fires here either.
    closes = _leg_path(1000, [1060, 1030, 1055, 1050, 1020, 1045, 1040, 900, 930])
    st = _Structure(direction=1, base_price=1000.0, base_idx=0)
    fired_at = None
    for i, c in enumerate(closes, start=1):
        _, events = st.step(i, c, atr_i=16.0)
        if Event.TERMINAL_FLAG in events:
            fired_at = i
            break

    assert fired_at is not None
    assert len(st.pullback_leg_sizes) == 3
    prior_median = float(np.median(st.pullback_leg_sizes[:-1]))
    assert st.pullback_leg_sizes[-1] > 1.5 * prior_median
    assert st.terminal_flag is True


# ── Outside Surging: no-op contract ─────────────────────────────────────────


def test_outside_surging_all_fields_are_none_or_default():
    seg = [1000.0, 1001.0, 1002.0]
    out, pre_n = _run([1000.0] * 5, seg)
    for b in out[:pre_n]:
        assert b.sub_state is None
        assert b.direction is None
        assert b.push_count == 0
        assert b.stop_level is None
        assert b.terminal_flag is False
        assert b.events == []


def test_consolidation_classified_on_tight_six_bar_range():
    # A flat run (range ~0 << 1.5xATR=24 over 6 bars) inside Surging classifies
    # as Consolidation once the 6-bar window is filled, not Impulse.
    seg = [1000.0] * 10
    out, pre_n = _run([1000.0] * 5, seg)
    assert out[pre_n + 5].sub_state == SubState.CONSOLIDATION
