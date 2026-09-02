"""Tests for sel_v2.offline.branch_b_sim (Wave V22-A, offline gate — v2.2 D1/D2/D4/D5).

Drives simulate_branch_b with hand-built BarSubState sequences (bypassing
sel_v2.offline.substate, which has its own test coverage) so each exit rule and
the pyramid sizing/terminal-cut accounting can be exercised in isolation.
"""

from __future__ import annotations

import pytest

from sel_v2.offline.branch_b_sim import (
    BATCH_WEIGHTS,
    ROUND_TRIP_COST_RATE,
    simulate_branch_b,
)
from sel_v2.offline.substate import BarSubState, Event, SubState

SURGING = "Surging"
CALM = "Drifting_Calm"
CASCADE = "Cascade"


def _bar(push_count=0, stop_level=None, terminal_flag=False, events=None, direction=1):
    return BarSubState(
        sub_state=SubState.IMPULSE,
        direction=direction,
        push_count=push_count,
        stop_level=stop_level,
        terminal_flag=terminal_flag,
        events=events or [],
    )


def _fragment_pnl(direction, weight, entry, exit_):
    gross = weight * direction * (exit_ / entry - 1.0)
    return gross - weight * ROUND_TRIP_COST_RATE


# ── full pyramid + normal exit ──────────────────────────────────────────────


def test_full_pyramid_sizes_30_45_25_and_closes_on_left_surging():
    close = [100.0, 110.0, 120.0, 130.0]
    parent = [SURGING, SURGING, SURGING, CALM]
    subs = [
        _bar(push_count=1, events=[Event.RE_PUSH]),
        _bar(push_count=2, events=[Event.RE_PUSH]),
        _bar(push_count=3, events=[Event.RE_PUSH]),
        _bar(push_count=3),  # default BarSubState would be used in practice; irrelevant here
    ]
    records = simulate_branch_b(close, parent, subs)

    assert len(records) == 3
    by_k = {r.batch_k: r for r in records}
    assert by_k[1].weight == pytest.approx(BATCH_WEIGHTS[1])
    assert by_k[2].weight == pytest.approx(BATCH_WEIGHTS[2])
    assert by_k[3].weight == pytest.approx(BATCH_WEIGHTS[3])
    assert sum(r.weight for r in records) == pytest.approx(1.0)
    for r in records:
        assert r.exit_reason == "left_surging"
        assert r.exit_price == 130.0
    assert by_k[1].net_pnl == pytest.approx(_fragment_pnl(1, BATCH_WEIGHTS[1], 100.0, 130.0))


def test_k_four_and_beyond_never_adds():
    close = [100.0, 110.0, 120.0, 125.0, 130.0]
    parent = [SURGING] * 5
    subs = [
        _bar(push_count=1, events=[Event.RE_PUSH]),
        _bar(push_count=2, events=[Event.RE_PUSH]),
        _bar(push_count=3, events=[Event.RE_PUSH]),
        _bar(push_count=4, events=[Event.RE_PUSH]),  # must NOT add a 4th batch
        _bar(push_count=5, events=[Event.RE_PUSH]),
    ]
    records = simulate_branch_b(close, parent, subs)
    # still in Surging the whole time (no break/terminal/drawdown/time-stop) -> the
    # only closure is the end-of-data force-close, and only batches 1-3 ever opened.
    assert all(r.exit_reason == "data_end" for r in records)
    assert {r.batch_k for r in records} == {1, 2, 3}
    assert sum(r.weight for r in records) == pytest.approx(1.0)
    # force a real exit to double-check the same accumulation independent of data_end
    parent2 = parent[:-1] + [CALM]
    records2 = simulate_branch_b(close, parent2, subs)
    assert {r.batch_k for r in records2} == {1, 2, 3}
    assert sum(r.weight for r in records2) == pytest.approx(1.0)


# ── terminal 50% cut, then final exit on the remaining half ────────────────


def test_terminal_flag_cuts_half_then_final_exit_closes_the_remainder():
    close = [100.0, 110.0, 120.0, 125.0, 90.0]
    parent = [SURGING] * 5
    subs = [
        _bar(push_count=1, events=[Event.RE_PUSH]),
        _bar(push_count=2, events=[Event.RE_PUSH]),
        _bar(push_count=3, events=[Event.RE_PUSH]),
        _bar(push_count=3, terminal_flag=True, events=[Event.TERMINAL_FLAG]),
        _bar(
            push_count=3,
            terminal_flag=True,
            events=[Event.STRUCTURE_BREAK],
            stop_level=95.0,
        ),
    ]
    records = simulate_branch_b(close, parent, subs)

    cuts = [r for r in records if r.exit_reason == "terminal_50pct"]
    finals = [r for r in records if r.exit_reason == "stop_break"]
    assert {r.batch_k for r in cuts} == {1, 2, 3}
    assert {r.batch_k for r in finals} == {1, 2, 3}
    for k, orig in BATCH_WEIGHTS.items():
        cut = next(r for r in cuts if r.batch_k == k)
        final = next(r for r in finals if r.batch_k == k)
        assert cut.weight == pytest.approx(orig / 2)
        assert final.weight == pytest.approx(orig / 2)
        assert cut.exit_price == 125.0
        assert final.exit_price == 90.0


def test_terminal_flag_blocks_further_adds_even_within_k_1_to_3():
    # D4: once terminal_flag latches, NO further adds — not just a k>=4 cap.
    # Here k=2 arrives only AFTER the terminal cut, so it must never be added.
    close = [100.0, 110.0, 125.0, 130.0]
    parent = [SURGING] * 4
    subs = [
        _bar(push_count=1, events=[Event.RE_PUSH]),
        _bar(push_count=1, terminal_flag=True, events=[Event.TERMINAL_FLAG]),
        _bar(push_count=2, events=[Event.RE_PUSH]),  # must be ignored
        _bar(push_count=2),
    ]
    records = simulate_branch_b(close, parent[:3] + ["Drifting_Calm"], subs)
    assert {r.batch_k for r in records} == {1}
    assert sum(r.weight for r in records) == pytest.approx(BATCH_WEIGHTS[1])


# ── structure break on a single-batch leg ───────────────────────────────────


def test_structure_break_closes_a_single_batch_leg():
    close = [100.0, 95.0]
    parent = [SURGING, SURGING]
    subs = [
        _bar(push_count=1, events=[Event.RE_PUSH]),
        _bar(push_count=0, events=[Event.STRUCTURE_BREAK]),
    ]
    records = simulate_branch_b(close, parent, subs)
    assert len(records) == 1
    assert records[0].exit_reason == "stop_break"
    assert records[0].weight == pytest.approx(BATCH_WEIGHTS[1])
    assert records[0].exit_price == 95.0


# ── drawdown stop ────────────────────────────────────────────────────────────


def test_drawdown_stop_fires_at_minus_3_pct():
    close = [100.0, 96.9]  # -3.1%, single batch so weight cancels out of the pct calc
    parent = [SURGING, SURGING]
    subs = [_bar(push_count=1, events=[Event.RE_PUSH]), _bar()]
    records = simulate_branch_b(close, parent, subs)
    assert len(records) == 1
    assert records[0].exit_reason == "drawdown"


def test_no_drawdown_exit_above_threshold():
    close = [100.0, 98.0]  # -2%, should NOT trigger the -3% drawdown stop
    parent = [SURGING, SURGING]
    subs = [_bar(push_count=1, events=[Event.RE_PUSH]), _bar()]
    records = simulate_branch_b(close, parent, subs)
    # only closure is the end-of-data force-close, never a drawdown exit
    assert all(r.exit_reason != "drawdown" for r in records)


# ── time stop ────────────────────────────────────────────────────────────────


def test_time_stop_after_180_bars():
    n = 182
    close = [100.0] * n
    parent = [SURGING] * n
    subs = [_bar(push_count=1, events=[Event.RE_PUSH])] + [_bar()] * (n - 1)
    records = simulate_branch_b(close, parent, subs)
    assert len(records) == 1
    assert records[0].exit_reason == "time_stop"
    assert records[0].bars_held == 180


# ── cascade exit reason ──────────────────────────────────────────────────────


def test_cascade_exit_reason_tagged_distinctly_from_left_surging():
    close = [100.0, 105.0]
    parent = [SURGING, CASCADE]
    subs = [_bar(push_count=1, events=[Event.RE_PUSH]), _bar()]
    records = simulate_branch_b(close, parent, subs)
    assert len(records) == 1
    assert records[0].exit_reason == "cascade"


# ── accounting invariant across multiple legs ───────────────────────────────


def test_leg_weight_never_exceeds_full_quota_across_repeated_cycles():
    close = [100.0, 110.0, 120.0, 90.0, 95.0, 105.0, 115.0, 125.0, 100.0]
    parent = [SURGING] * 9
    subs = [
        _bar(push_count=1, events=[Event.RE_PUSH]),
        _bar(push_count=2, events=[Event.RE_PUSH]),
        _bar(push_count=3, events=[Event.RE_PUSH]),
        _bar(push_count=0, events=[Event.STRUCTURE_BREAK]),  # leg 1 dies, resets
        _bar(push_count=1, events=[Event.RE_PUSH]),  # leg 2 starts
        _bar(push_count=2, events=[Event.RE_PUSH]),
        _bar(push_count=3, events=[Event.RE_PUSH]),
        _bar(push_count=3),
        _bar(push_count=0, events=[Event.STRUCTURE_BREAK]),
    ]
    records = simulate_branch_b(close, parent, subs)
    assert {r.leg_id for r in records} == {1, 2}
    for leg_id in (1, 2):
        leg_records = [r for r in records if r.leg_id == leg_id]
        assert sum(r.weight for r in leg_records) == pytest.approx(1.0)
        assert all(r.weight >= 0 for r in leg_records)
