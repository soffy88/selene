"""Wave V22-A gate verdict report (v2.2 design draft, offline gate — Part 3).

Runs the Part-1 sub-state machine (sel_v2.offline.substate) and the Part-2 Branch B
pyramid simulator (sel_v2.offline.branch_b_sim) over the full BTC-USDT 4H history,
joining `v2_bars_4h` (OHLC) with the frozen `v2_state_annotation` state series
(Wave S2C) by timestamp. Writes sel_v2/reports/v22_offline_gate_v1.md and prints
the same numbers to stdout (raw). Reads only — never writes v2_state_history,
v2_trades, or the epoch; the run does not touch strategies/** or states/**.

Run:  python -m sel_v2.offline.v22_gate_report
"""

from __future__ import annotations

import asyncio
import logging
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import asyncpg
import numpy as np

from sel_v2.offline.branch_b_sim import simulate_branch_b
from sel_v2.offline.substate import compute_substates

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v22_gate_report")

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
REPORT_PATH = Path(__file__).resolve().parents[1] / "reports" / "v22_offline_gate_v1.md"

FALSE_POSITIVE_WINDOW_BARS = 10
H_V22_1_MIN_TRIGGERS = 20
H_V22_3_MAX_FALSE_POSITIVE_RATE = 0.40

YEAR_BUCKETS = [
    ("2024H2", "2024-07-01", "2025-01-01"),
    ("2025", "2025-01-01", "2026-01-01"),
    ("2026H1", "2026-01-01", "2026-07-01"),
    (
        "2026H2(partial)",
        "2026-07-01",
        "2100-01-01",
    ),  # not a spec'd bucket; shown, not dropped
]


async def _load(pool) -> tuple[list, np.ndarray, np.ndarray, np.ndarray, list[str]]:
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
            continue  # inner join: skip bars with no annotation (e.g. a trailing partial bar)
        times.append(r["time"])
        high.append(float(r["high"]))
        low.append(float(r["low"]))
        close.append(float(r["close"]))
        states.append(st)
    return times, np.array(high), np.array(low), np.array(close), states


def _year_bucket(ts) -> str:
    for label, start, end in YEAR_BUCKETS:
        if start <= ts.strftime("%Y-%m-%d") < end:
            return label
    return "unbucketed"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _false_positive_rate(events: list[tuple], high, low, close, window=FALSE_POSITIVE_WINDOW_BARS):
    """events: list of (direction, entry_bar, event_bar). Rate a break/flag as a false
    positive if price makes a NEW extreme beyond the leg's own prior extreme (from
    entry_bar..event_bar) within `window` bars after event_bar."""
    if not events:
        return None, 0, 0
    n = len(close)
    fp = 0
    for direction, entry_bar, event_bar in events:
        lo = max(0, entry_bar)
        hi = min(event_bar, n - 1)
        if direction == 1:
            prior_extreme = high[lo : hi + 1].max()
        else:
            prior_extreme = low[lo : hi + 1].min()
        window_end = min(event_bar + window, n - 1)
        recovered = False
        for j in range(event_bar + 1, window_end + 1):
            if direction == 1 and high[j] > prior_extreme:
                recovered = True
                break
            if direction == -1 and low[j] < prior_extreme:
                recovered = True
                break
        if recovered:
            fp += 1
    return fp / len(events), fp, len(events)


async def run() -> dict:
    dsn = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        times, high, low, close, states = await _load(pool)
        n = len(close)
        logger.info("loaded %d aligned bars for %s", n, SYMBOL)

        substates = compute_substates(high, low, close, states)
        records = simulate_branch_b(close, states, substates)

        legs = defaultdict(list)
        for r in records:
            legs[r.leg_id].append(r)

        # H-V22-1: first-leg trigger count (every leg_id, by construction, opened at k=1)
        n_legs = len(legs)

        # leg-level aggregates
        leg_pnl = {lid: sum(r.net_pnl for r in recs) for lid, recs in legs.items()}
        leg_direction = {lid: recs[0].direction for lid, recs in legs.items()}
        leg_entry_bar = {lid: min(r.entry_bar for r in recs) for lid, recs in legs.items()}
        leg_final_reason = {}
        leg_final_bar = {}
        for lid, recs in legs.items():
            finals = [r for r in recs if r.exit_reason != "terminal_50pct"]
            final = max(finals, key=lambda r: r.exit_bar) if finals else max(recs, key=lambda r: r.exit_bar)
            leg_final_reason[lid] = final.exit_reason
            leg_final_bar[lid] = final.exit_bar

        # H-V22-2: per-leg net expectancy, all batches merged, costs included
        pnl_values = list(leg_pnl.values())
        h2_expectancy = statistics.mean(pnl_values) if pnl_values else None

        # H-V22-3: STRUCTURE_BREAK false-positive rate (one evaluation per leg-closing break)
        break_events = [
            (leg_direction[lid], leg_entry_bar[lid], leg_final_bar[lid])
            for lid, reason in leg_final_reason.items()
            if reason == "stop_break"
        ]
        h3_rate, h3_fp, h3_total = _false_positive_rate(break_events, high, low, close)

        # exit reason distribution (final, per leg) + terminal-cut incidence
        exit_dist = Counter(leg_final_reason.values())
        n_terminal_cut = sum(1 for recs in legs.values() if any(r.exit_reason == "terminal_50pct" for r in recs))

        # terminal lead + its own false-positive rate
        terminal_bar = {}
        for lid, recs in legs.items():
            cuts = [r for r in recs if r.exit_reason == "terminal_50pct"]
            if cuts:
                terminal_bar[lid] = cuts[0].exit_bar
        terminal_leads = [leg_final_bar[lid] - terminal_bar[lid] for lid in terminal_bar]
        terminal_events = [(leg_direction[lid], leg_entry_bar[lid], terminal_bar[lid]) for lid in terminal_bar]
        term_fp_rate, term_fp, term_total = _false_positive_rate(terminal_events, high, low, close)

        # per-year split
        year_stats = defaultdict(list)
        for lid in legs:
            year_stats[_year_bucket(times[leg_entry_bar[lid]])].append(leg_pnl[lid])

        # long vs short split
        long_pnl = [leg_pnl[lid] for lid in legs if leg_direction[lid] == 1]
        short_pnl = [leg_pnl[lid] for lid in legs if leg_direction[lid] == -1]

        result = {
            "n_bars": n,
            "n_legs": n_legs,
            "h1_pass": n_legs >= H_V22_1_MIN_TRIGGERS,
            "h2_expectancy": h2_expectancy,
            "h2_pass": (h2_expectancy is not None) and h2_expectancy > 0,
            "h3_rate": h3_rate,
            "h3_fp": h3_fp,
            "h3_total": h3_total,
            "h3_pass": (h3_rate is not None) and h3_rate < H_V22_3_MAX_FALSE_POSITIVE_RATE,
            "exit_dist": dict(exit_dist),
            "n_terminal_cut": n_terminal_cut,
            "terminal_leads": terminal_leads,
            "term_fp_rate": term_fp_rate,
            "term_fp": term_fp,
            "term_total": term_total,
            "pnl_values": pnl_values,
            "year_stats": {k: v for k, v in year_stats.items()},
            "long_pnl": long_pnl,
            "short_pnl": short_pnl,
        }

        report = _build_report(result)
        print(report)
        try:
            REPORT_PATH.write_text(report)
            logger.info("report written to %s", REPORT_PATH)
        except OSError as exc:
            logger.warning("could not write report file (%s): %s", REPORT_PATH, exc)

        return result
    finally:
        await pool.close()


def _dist_line(vals: list[float]) -> str:
    if not vals:
        return "n/a (no legs)"
    arr = np.array(vals)
    return (
        f"p10={np.percentile(arr, 10):.4f} "
        f"p50={np.percentile(arr, 50):.4f} p90={np.percentile(arr, 90):.4f} "
        f"worst={arr.min():.4f} best={arr.max():.4f} n={len(arr)}"
    )


def _build_report(res: dict) -> str:
    if res["h2_expectancy"] is not None:
        h2_value = f"{res['h2_expectancy']:.5f}"
    else:
        h2_value = "n/a"
    if res["h3_rate"] is not None:
        h3_value = f"{_pct(res['h3_rate'])} ({res['h3_fp']}/{res['h3_total']})"
    else:
        h3_value = "n/a (0 stop_break exits)"

    lines = [
        "# v2.2 Offline Gate Verdict v1 (Wave V22-A)",
        "",
        f"Offline replay over {res['n_bars']} aligned BTC-USDT 4H bars "
        f"(`v2_bars_4h` joined to `v2_state_annotation` by timestamp). Branch B pyramid "
        "simulator (D1/D2/D4/D5), parameters frozen per the Wiki ruling — not retuned on "
        "these results.",
        "",
        "## Gate verdicts",
        "",
        "| gate | metric | threshold | value | pass |",
        "|---|---|---|---|---|",
        f"| H-V22-1 | first-leg trigger count | >= {H_V22_1_MIN_TRIGGERS} | {res['n_legs']} | {'PASS' if res['h1_pass'] else 'FAIL'} |",
        f"| H-V22-2 | per-leg net expectancy (all batches, w/ costs) | > 0 | {h2_value} | {'PASS' if res['h2_pass'] else 'FAIL'} |",
        f"| H-V22-3 | STRUCTURE_BREAK false-positive rate | < {_pct(H_V22_3_MAX_FALSE_POSITIVE_RATE)} | {h3_value} | {'PASS' if res['h3_pass'] else 'FAIL'} |",
        "",
        "## Exit reason distribution (per leg, final exit)",
        "",
        "| reason | legs |",
        "|---|---:|",
    ]
    for reason, cnt in sorted(res["exit_dist"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {reason} | {cnt} |")
    lines += [
        "",
        f"- Legs that hit a terminal 50% cut before their final exit: **{res['n_terminal_cut']} / {res['n_legs']}**",
        "",
        "## Per-leg net P&L distribution (quota-fraction units, all batches merged)",
        "",
        f"- {_dist_line(res['pnl_values'])}",
        "",
        "## Long vs short",
        "",
        f"- Long legs:  {_dist_line(res['long_pnl'])}",
        f"- Short legs: {_dist_line(res['short_pnl'])}",
        "",
        "## By year (regime robustness — not a re-tuning basis)",
        "",
        "| bucket | legs | net P&L dist |",
        "|---|---:|---|",
    ]
    for label, _s, _e in YEAR_BUCKETS:
        vals = res["year_stats"].get(label, [])
        lines.append(f"| {label} | {len(vals)} | {_dist_line(vals)} |")
    lines += [
        "",
        "## Terminal flag: lead time to leg end + its own false-positive rate",
        "",
        f"- Lead (bars from terminal_flag latch to the leg's final exit): "
        f"{_dist_line([float(x) for x in res['terminal_leads']])}",
        f"- False-positive rate (price makes a new extreme within "
        f"{FALSE_POSITIVE_WINDOW_BARS} bars after the cut, i.e. the cut was premature): "
        + (
            f"{_pct(res['term_fp_rate'])} ({res['term_fp']}/{res['term_total']})"
            if res["term_fp_rate"] is not None
            else "n/a (0 terminal cuts)"
        ),
        "",
        "## STOP condition checks",
        "",
        "- Accounting (leg weight in [0,1], never negative): enforced at simulation time "
        "by `branch_b_sim.AccountingError` — the run completed without raising, so no "
        "leg ever exceeded its quota or went negative.",
        "",
        "Regenerate: `python -m sel_v2.offline.v22_gate_report`.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(run())
