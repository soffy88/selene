"""Unit tests for the S2 dual-arm shadow recorder (Wave EXEC-S Part 2).

No database: the DB-driven paths run against a fake connection, so these execute
in CI (which has no Postgres) rather than only on a box with the live stack.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from sel_v2.shadow.shadow_exec import (
    CANCELLED,
    CLIP_A,
    CLIP_B,
    CROSSED,
    FILLED,
    INSIDE_ATR,
    MAKER_FEE,
    RESTING,
    TAKER_FEE,
    TIMEOUT_A_MIN,
    TIMEOUT_B_MIN,
    advance_resting,
    clip_delta,
    is_filled,
    limit_level,
    outcome,
    parse_entry_type,
    timeout_minutes,
)

TS = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


# ── frozen parameters ────────────────────────────────────────────────────────


def test_frozen_parameters_are_the_waves():
    assert (INSIDE_ATR, CLIP_A, CLIP_B) == (0.1, (0.15, 1.2), (0.15, 0.8))
    assert (TIMEOUT_A_MIN, TIMEOUT_B_MIN) == (120, 60)
    assert (TAKER_FEE, MAKER_FEE) == (0.0008, 0.0003)
    assert timeout_minutes("A") == 120 and timeout_minutes("B") == 60


# ── δ clipping ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "etype,raw,expect,clipped",
    [
        ("A", 2.0, 1.2, True),  # over the Type-A cap
        ("A", 0.05, 0.15, True),  # under the floor
        ("A", 0.6, 0.6, False),  # inside the band, untouched
        ("B", 1.0, 0.8, True),  # Type B's cap is tighter
        ("B", 0.15, 0.15, False),  # exactly on the floor is not "clipped"
        ("B", 0.8, 0.8, False),  # exactly on the cap
    ],
)
def test_clip_delta_respects_each_types_band(etype, raw, expect, clipped):
    got, was = clip_delta(raw, etype)
    assert got == pytest.approx(expect)
    assert was is clipped


def test_out_of_band_structural_level_is_pulled_in_not_accepted():
    """A sweep extreme 3×ATR away must not produce a 3×ATR limit — the cap wins."""
    level, delta, clipped = limit_level(
        signal_price=100.0, ref_price=70.0, atr=10.0, direction="LONG", entry_type="A"
    )
    assert clipped is True
    assert delta == pytest.approx(CLIP_A[1])  # 1.2
    assert level == pytest.approx(100.0 - 1.2 * 10.0)


def test_type_a_rests_inside_the_swept_extreme():
    """Type A pulls 0.1×ATR back toward the market from the extreme."""
    level, delta, clipped = limit_level(
        signal_price=100.0, ref_price=95.0, atr=10.0, direction="LONG", entry_type="A"
    )
    # raw level = 95 + 0.1*10 = 96 → δ = 0.4 ATR below the 100 signal
    assert clipped is False
    assert delta == pytest.approx(0.4)
    assert level == pytest.approx(96.0)


def test_long_rests_below_and_short_rests_above():
    long_level, _, _ = limit_level(100.0, 95.0, 10.0, "LONG", "B")
    short_level, _, _ = limit_level(100.0, 105.0, 10.0, "SHORT", "B")
    assert long_level < 100.0 < short_level


# ── strict-crossing fill rule ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "direction,level,extreme,filled",
    [
        ("LONG", 100.0, 99.99, True),
        ("LONG", 100.0, 100.0, False),  # touching L is NOT a fill
        ("LONG", 100.0, 100.01, False),
        ("SHORT", 100.0, 100.01, True),
        ("SHORT", 100.0, 100.0, False),  # touching L is NOT a fill
        ("SHORT", 100.0, 99.99, False),
    ],
)
def test_fill_needs_a_strict_crossing(direction, level, extreme, filled):
    assert is_filled(direction, level, extreme) is filled


# ── outcome accounting ───────────────────────────────────────────────────────


def test_unfilled_arm_books_exactly_zero():
    assert outcome("LONG", None, 120.0, MAKER_FEE) == 0.0


def test_outcome_signs_with_direction_and_subtracts_fee():
    long_o = outcome("LONG", 100.0, 110.0, 0.0)
    short_o = outcome("SHORT", 100.0, 110.0, 0.0)
    assert long_o == pytest.approx(0.10)
    assert short_o == pytest.approx(-0.10)
    assert outcome("LONG", 100.0, 110.0, MAKER_FEE) == pytest.approx(0.10 - MAKER_FEE)


def test_outcome_none_when_mark_missing():
    assert outcome("LONG", 100.0, None, MAKER_FEE) is None


# ── entry-type parsing (only lives in the free-text reason) ──────────────────


@pytest.mark.parametrize(
    "reason,expect",
    [
        ("Step 6: Entry approved — Type A (reversal) λ*=0.1", "A"),
        ("Step 6: Entry approved — Type B (momentum)", "B"),
        ("Step 3: ABORT — no type here", None),
        (None, None),
        ("Type C is not a thing", None),  # never guessed
    ],
)
def test_parse_entry_type(reason, expect):
    assert parse_entry_type(reason) == expect


# ── timeout handling, driven through a fake connection ───────────────────────


class FakeConn:
    """Minimal asyncpg stand-in: canned fetch results, recorded execute calls."""

    def __init__(self, rows, scalars=None):
        self._rows = rows
        self._scalars = scalars or {}
        self.executed: list[tuple] = []

    async def fetch(self, *_a, **_k):
        return self._rows

    async def fetchrow(self, query, *args, **_k):
        if "min(price)" in query or "max(price)" in query:
            return {"v": self._scalars.get("extreme")}
        if "ORDER BY timestamp DESC LIMIT 1" in query:
            return {"price": self._scalars.get("price")}
        return {"timestamp": self._scalars.get("fill_ts", TS)}

    async def execute(self, query, *args):
        self.executed.append((query, args))


def _resting_row(entry_type: str, deadline: datetime):
    return {
        "signal_ts": TS,
        "direction": "LONG",
        "entry_type": entry_type,
        "limit_price": 100.0,
        "deadline_ts": deadline,
        "signal_price": 101.0,
        "limit_status": RESTING,
    }


def test_type_a_timeout_cancels_and_does_not_chase():
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    conn = FakeConn([_resting_row("A", past)], {"extreme": 100.5})  # never crossed
    asyncio.run(advance_resting(conn))
    status = conn.executed[0][1][0]
    assert status == CANCELLED
    joined = " ".join(str(a) for _, args in conn.executed for a in args)
    assert CROSSED not in joined  # must not cross to market


def test_type_b_timeout_crosses_to_market():
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    conn = FakeConn([_resting_row("B", past)], {"extreme": 100.5, "price": 102.0})
    asyncio.run(advance_resting(conn))
    status, fill_ts, fill_px = (
        conn.executed[0][1][0],
        conn.executed[0][1][1],
        conn.executed[0][1][2],
    )
    assert status == CROSSED
    assert fill_ts == past  # booked at the deadline
    assert fill_px == 102.0  # real slippage, not the signal price


def test_crossing_before_deadline_fills_regardless_of_type():
    future = datetime.now(timezone.utc) + timedelta(minutes=30)
    conn = FakeConn([_resting_row("A", future)], {"extreme": 99.0})  # crossed 100
    asyncio.run(advance_resting(conn))
    assert conn.executed[0][1][0] == FILLED


def test_still_resting_when_untouched_and_inside_deadline():
    future = datetime.now(timezone.utc) + timedelta(minutes=30)
    conn = FakeConn([_resting_row("A", future)], {"extreme": 100.5})
    asyncio.run(advance_resting(conn))
    assert conn.executed == []  # no terminal transition yet


def test_recovery_reads_state_from_the_table_not_memory():
    """Crash recovery: resting arms are re-driven purely from persisted rows, so a
    fresh process with no in-memory state still closes them out."""
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    conn = FakeConn([_resting_row("A", past)], {"extreme": 100.5})
    asyncio.run(advance_resting(conn))  # no prior poll/bookkeeping call
    assert conn.executed and conn.executed[0][1][0] == CANCELLED
