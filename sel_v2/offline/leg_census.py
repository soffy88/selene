"""Trend leg census (Wave V22-C, offline diagnostic — read-only, no strategy/backtest).

Answers one question: does the Wiki's ~36-leg trend model (10-35 day duration,
3-6 pushes) actually exist in the 2yr BTC-USDT 4H OHLC, and if so, how much of it
did the frozen parent state machine (v2_state_annotation) actually label Surging?

This is pure counting — no strategy parameters, no entry/exit gates, no P&L. Three
coarse zigzag thresholds are run independently and ALL THREE are reported; none are
picked after seeing results (frozen per the Wave's red line):

    coarse leg split : (a) 3x ATR(14)   (b) 5x ATR(14)   (c) fixed 8% price move
    fine push count  : 1.5x ATR(14) — identical parameter to sel_v2.offline.substate

Never imported by any live path; touches nothing under strategies/**, states/**, or
the epoch.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

import asyncpg
import numpy as np

from sel_v2.offline.substate import compute_atr

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("leg_census")

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
REPORT_PATH = Path(__file__).resolve().parents[1] / "reports" / "trend_leg_census_v1.md"

CONFIGS = [
    ("3xATR", 3.0),
    ("5xATR", 5.0),
    ("8pct", ("pct", 0.08)),
]

FINE_ZIGZAG_ATR_MULT = 1.5  # identical to sel_v2.offline.substate.ZIGZAG_ATR_MULT

WIKI_DURATION_DAYS = (10, 35)
WIKI_PUSH_RANGE = (3, 6)
BARS_PER_DAY = 6.0  # 4H bars

SURGING_STATE = "Surging"
CAPTURE_OVERLAP_THRESHOLD = 0.50

YEAR_BUCKETS = [
    ("2024H2", "2024-07-01", "2025-01-01"),
    ("2025", "2025-01-01", "2026-01-01"),
    ("2026H1", "2026-01-01", "2026-07-01"),
    ("2026H2(partial)", "2026-07-01", "2100-01-01"),
]


@dataclasses.dataclass
class CoarseLeg:
    direction: int  # +1 up, -1 down
    start_idx: int
    end_idx: int  # inclusive, the confirmed pivot bar
    duration_days: float
    net_pct: float
    max_adverse_pct: float
    push_count: int
    surging_overlap: float  # fraction of [start_idx, end_idx] bars labeled Surging


def _zigzag_legs(prices: np.ndarray, threshold_fn: Callable[[int, float], float]) -> list[tuple[int, int, int]]:
    """Generic causal zigzag: bootstrap the initial phase from the first non-flat
    move, then flip phase whenever price reverses by >= threshold_fn(bar, extreme)
    from the running extreme. Returns confirmed (start_idx, end_idx, direction)
    triples between consecutive pivots — the seed bar counts as the first pivot.
    A final in-progress swing (not yet confirmed) is NOT included."""
    n = len(prices)
    if n < 2:
        return []
    seed = 1
    while seed < n and prices[seed] == prices[0]:
        seed += 1
    if seed >= n:
        return []
    phase_up = prices[seed] > prices[0]
    ext_price, ext_idx = prices[0], 0
    last_pivot_idx = 0
    legs: list[tuple[int, int, int]] = []
    for i in range(1, n):
        p = prices[i]
        if phase_up:
            if p > ext_price:
                ext_price, ext_idx = p, i
            elif ext_price - p >= threshold_fn(i, ext_price):
                legs.append((last_pivot_idx, ext_idx, 1))
                last_pivot_idx = ext_idx
                phase_up = False
                ext_price, ext_idx = p, i
        else:
            if p < ext_price:
                ext_price, ext_idx = p, i
            elif p - ext_price >= threshold_fn(i, ext_price):
                legs.append((last_pivot_idx, ext_idx, -1))
                last_pivot_idx = ext_idx
                phase_up = True
                ext_price, ext_idx = p, i
    return legs


def _max_adverse_pct(close: np.ndarray, start_idx: int, end_idx: int, direction: int) -> float:
    """Largest intra-leg retracement against `direction`, as a fraction of the
    running best-so-far price. 0.0 for a leg that never pulls back at all."""
    best = close[start_idx]
    worst_retracement = 0.0
    for i in range(start_idx, end_idx + 1):
        p = close[i]
        if direction * (p - best) > 0:
            best = p
        retr = direction * (best - p)
        if best != 0:
            worst_retracement = max(worst_retracement, retr / best)
    return worst_retracement


def _push_count(close: np.ndarray, atr: np.ndarray, start_idx: int, end_idx: int, direction: int) -> int:
    """Count of confirmed fine (1.5x ATR) zigzag swings within the leg that run in
    the SAME direction as the coarse leg — i.e. how many times price pushed further
    in the trend direction, at the substate.py-identical fine-zigzag resolution."""
    if end_idx <= start_idx:
        return 0
    sub_close = close[start_idx : end_idx + 1]

    def thresh(i_local: int, _extreme: float) -> float:
        return FINE_ZIGZAG_ATR_MULT * atr[start_idx + i_local]

    fine_legs = _zigzag_legs(sub_close, thresh)
    return sum(1 for _s, _e, d in fine_legs if d == direction)


def _year_bucket(ts) -> str:
    for label, start, end in YEAR_BUCKETS:
        if start <= ts.strftime("%Y-%m-%d") < end:
            return label
    return "unbucketed"


def census(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    parent_state: list[str],
    mult_or_pct,
) -> tuple[list[CoarseLeg], int]:
    """Run one coarse-threshold config over the full series. `mult_or_pct` is either
    an ATR multiplier (float, e.g. 3.0) used with the 'x' unit, or a ('pct', 0.08)
    tuple for the fixed-percentage config. Returns (legs, tail_bars_unconfirmed)."""
    atr = compute_atr(high, low, close)

    if isinstance(mult_or_pct, tuple) and mult_or_pct[0] == "pct":
        pct = mult_or_pct[1]

        def thresh(i: int, extreme: float) -> float:
            return pct * extreme

    else:
        mult = float(mult_or_pct)

        def thresh(i: int, extreme: float) -> float:
            return mult * atr[i]

    raw_legs = _zigzag_legs(close, thresh)
    n = len(close)
    tail_bars = (n - 1) - raw_legs[-1][1] if raw_legs else n - 1

    legs = []
    for start_idx, end_idx, direction in raw_legs:
        duration_days = (end_idx - start_idx) / BARS_PER_DAY
        net_pct = direction * (close[end_idx] / close[start_idx] - 1.0)
        max_adverse = _max_adverse_pct(close, start_idx, end_idx, direction)
        push_count = _push_count(close, atr, start_idx, end_idx, direction)
        window = parent_state[start_idx : end_idx + 1]
        overlap = sum(1 for s in window if s == SURGING_STATE) / len(window)
        legs.append(
            CoarseLeg(
                direction=direction,
                start_idx=start_idx,
                end_idx=end_idx,
                duration_days=duration_days,
                net_pct=net_pct,
                max_adverse_pct=max_adverse,
                push_count=push_count,
                surging_overlap=overlap,
            )
        )
    return legs, tail_bars


# ── report ───────────────────────────────────────────────────────────────────


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _dist_line(vals: list[float]) -> str:
    if not vals:
        return "n/a (no legs)"
    arr = np.array(vals, dtype=float)
    return (
        f"p10={np.percentile(arr, 10):.2f} p50={np.percentile(arr, 50):.2f} "
        f"p90={np.percentile(arr, 90):.2f} n={len(arr)}"
    )


def _is_wiki_spec(leg: CoarseLeg) -> bool:
    lo, hi = WIKI_DURATION_DAYS
    plo, phi = WIKI_PUSH_RANGE
    return lo <= leg.duration_days <= hi and plo <= leg.push_count <= phi


def _median(vals: list[float]) -> Optional[float]:
    if not vals:
        return None
    return float(np.median(np.array(vals, dtype=float)))


def _build_config_section(name: str, legs: list[CoarseLeg], tail_bars: int, times, states) -> list[str]:
    lines = [f"## Threshold config: {name}", ""]

    n_up = sum(1 for leg in legs if leg.direction == 1)
    n_down = sum(1 for leg in legs if leg.direction == -1)
    lines += [
        "### 1. Leg counts",
        "",
        f"- Total legs: **{len(legs)}** (up: {n_up}, down: {n_down})",
        f"- Unconfirmed tail bars excluded from the census: {tail_bars}",
        "",
        "| year bucket | legs |",
        "|---|---:|",
    ]
    year_counts = Counter(_year_bucket(times[leg.start_idx]) for leg in legs)
    for label, _s, _e in YEAR_BUCKETS:
        lines.append(f"| {label} | {year_counts.get(label, 0)} |")
    lines.append("")

    lines += [
        "### 2. Duration / push / displacement distribution",
        "",
        f"- Duration (days): {_dist_line([leg.duration_days for leg in legs])}",
        f"- Push count: {_dist_line([float(leg.push_count) for leg in legs])}",
        f"- Net displacement (%): {_dist_line([leg.net_pct * 100 for leg in legs])}",
        f"- Max adverse excursion (%): {_dist_line([leg.max_adverse_pct * 100 for leg in legs])}",
        "",
    ]

    wiki_legs = [leg for leg in legs if _is_wiki_spec(leg)]
    wiki_share = len(wiki_legs) / len(legs) if legs else 0.0
    lines += [
        "### 3. Legs matching the Wiki spec "
        f"(duration {WIKI_DURATION_DAYS[0]}-{WIKI_DURATION_DAYS[1]}d, push {WIKI_PUSH_RANGE[0]}-{WIKI_PUSH_RANGE[1]})",
        "",
        f"- **{len(wiki_legs)} / {len(legs)} = {_pct(wiki_share)}**",
        "",
    ]

    captured = [leg for leg in legs if leg.surging_overlap >= CAPTURE_OVERLAP_THRESHOLD]
    missed = [leg for leg in legs if leg.surging_overlap < CAPTURE_OVERLAP_THRESHOLD]
    lines += [
        f"### 4. Parent-state-machine overlap (>= {_pct(CAPTURE_OVERLAP_THRESHOLD)} Surging bars = captured)",
        "",
        f"- Captured: **{len(captured)}** / Missed: **{len(missed)}** (of {len(legs)} total legs)",
        "",
    ]

    missed_state_bars: Counter = Counter()
    for leg in missed:
        for s in states[leg.start_idx : leg.end_idx + 1]:
            missed_state_bars[s] += 1
    total_missed_bars = sum(missed_state_bars.values())
    lines += ["### 5. Missed-leg profile", ""]
    if total_missed_bars:
        lines += [
            "- Bars in missed legs, by what the annotation actually called them:",
            "",
            "| state | bars | share |",
            "|---|---:|---:|",
        ]
        for s, cnt in missed_state_bars.most_common():
            lines.append(f"| {s} | {cnt} | {_pct(cnt / total_missed_bars)} |")
        lines.append("")
    else:
        lines += ["- No missed legs (or no legs at all) for this config.", ""]

    def _fmt(v, digits=2):
        return f"{v:.{digits}f}" if v is not None else "n/a"

    lines += [
        "- Missed vs captured legs (median):",
        "",
        "| | duration (d) | net displacement (%) | push count |",
        "|---|---:|---:|---:|",
        f"| missed | {_fmt(_median([leg.duration_days for leg in missed]))} | "
        f"{_fmt(_median([leg.net_pct * 100 for leg in missed]))} | "
        f"{_fmt(_median([float(leg.push_count) for leg in missed]), 1)} |",
        f"| captured | {_fmt(_median([leg.duration_days for leg in captured]))} | "
        f"{_fmt(_median([leg.net_pct * 100 for leg in captured]))} | "
        f"{_fmt(_median([float(leg.push_count) for leg in captured]), 1)} |",
        "",
    ]
    return lines


def _build_report(results: dict) -> str:
    lines = [
        "# Trend Leg Census v1 (Wave V22-C)",
        "",
        "Pure OHLC census — no strategy parameters, no entry/exit gates, no P&L. "
        "Three coarse zigzag thresholds run independently, all three reported "
        "(none picked after seeing results). Fine push-count layer uses the same "
        "1.5x ATR(14) as `sel_v2.offline.substate`.",
        "",
    ]
    for name, (legs, tail_bars, times, states) in results.items():
        lines += _build_config_section(name, legs, tail_bars, times, states)

    lines += [
        "## Verdict table (raw, no conclusion drawn)",
        "",
        "| config | Wiki-spec legs | of which captured (>=50% Surging) |",
        "|---|---:|---:|",
    ]
    for name, (legs, _tail, _times, _states) in results.items():
        wiki_legs = [leg for leg in legs if _is_wiki_spec(leg)]
        captured = [leg for leg in wiki_legs if leg.surging_overlap >= CAPTURE_OVERLAP_THRESHOLD]
        lines.append(f"| {name} | {len(wiki_legs)} | {len(captured)} |")
    lines += ["", "Regenerate: `python -m sel_v2.offline.leg_census`."]
    return "\n".join(lines)


async def _load(pool):
    bar_rows = await pool.fetch(
        "SELECT time, high, low, close FROM v2_bars_4h WHERE symbol=$1 ORDER BY time ASC",
        SYMBOL,
    )
    state_rows = await pool.fetch(
        "SELECT timestamp, state FROM v2_state_annotation WHERE symbol=$1 ORDER BY timestamp ASC",
        SYMBOL,
    )
    state_by_ts = {r["timestamp"]: r["state"] for r in state_rows}
    times, high, low, close, states = [], [], [], [], []
    for r in bar_rows:
        st = state_by_ts.get(r["time"])
        if st is None:
            continue
        times.append(r["time"])
        high.append(float(r["high"]))
        low.append(float(r["low"]))
        close.append(float(r["close"]))
        states.append(st)
    if not times:
        raise SystemExit("v2_bars_4h / v2_state_annotation unreadable or empty — STOP per Wave red line")
    return times, np.array(high), np.array(low), np.array(close), states


async def run() -> dict:
    dsn = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        times, high, low, close, states = await _load(pool)
        logger.info("loaded %d aligned bars for %s", len(close), SYMBOL)

        results = {}
        for name, cfg in CONFIGS:
            legs, tail_bars = census(high, low, close, states, cfg)
            results[name] = (legs, tail_bars, times, states)
            logger.info(
                "%s: %d confirmed legs, %d unconfirmed tail bars",
                name,
                len(legs),
                tail_bars,
            )

        report = _build_report(results)
        print(report)
        try:
            REPORT_PATH.write_text(report)
            logger.info("report written to %s", REPORT_PATH)
        except OSError as exc:
            logger.warning("could not write report file (%s): %s", REPORT_PATH, exc)

        return results
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run())
