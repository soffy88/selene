"""Offline historical state annotation (Wave S2C Part 4).

Replays the *same* 4H state machine the live paper engine uses — BarRunner + the frozen
sel_v2/states/conditions.py recognizer (imported, never copied) — over the full v2_bars_4h
history, and records per bar: the state, the transition, which features actually participated
(non-None) vs were missing (None → degraded fallback path), into a SEPARATE table
v2_state_annotation. It NEVER writes v2_state_history (the live engine's own record) and
NEVER produces trades (pure state machine, no strategy/account code runs here).

Why: history predates the tick/LOB/liquidation feeds, so early bars decide on a reduced
feature set. This makes that explicit — a baseline distribution + dwell profile + degraded-
bar share the Wiki can read, and a guard against the v1.0 death mode (one state ≥95%).

Run:  python -m sel_v2.offline.state_annotator
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from pathlib import Path

import asyncpg
import numpy as np

from sel_v2.paper.paper_engine import PaperEngine
from sel_v2.paper.strategy_engine import PaperStrategyEngine

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("state_annotator")

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
REPORT_PATH = (
    Path(__file__).resolve().parents[1] / "reports" / "state_annotation_baseline_v1.md"
)
DEATH_MODE_THRESHOLD = (
    0.95  # a single state ≥ this share → STOP (v1.0 death-mode signal)
)


def _feature_split(features) -> tuple[list[str], list[str]]:
    """Split a BarFeatures instance into (available, missing) field-name lists by None-ness.
    Missing == the state machine took a degraded/None path for that input on this bar."""
    available, missing = [], []
    for f in dataclasses.fields(features):
        val = getattr(features, f.name)
        (missing if val is None else available).append(f.name)
    return available, missing


def _dwell_lengths(states: list[str]) -> dict[str, list[int]]:
    """Consecutive-run lengths per state (in bars)."""
    out: dict[str, list[int]] = {}
    if not states:
        return out
    cur, run = states[0], 1
    for s in states[1:]:
        if s == cur:
            run += 1
        else:
            out.setdefault(cur, []).append(run)
            cur, run = s, 1
    out.setdefault(cur, []).append(run)
    return out


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


async def annotate() -> dict:
    pe = PaperEngine()
    dsn = os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")
    pe._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3)
    try:
        await pe._load_strategy_params()
        df = await pe._load_bars_df()
        if df is None or len(df) < 30:
            raise SystemExit(
                f"not enough bars to annotate (have {0 if df is None else len(df)})"
            )

        try:
            import ripser  # noqa: F401

            skip_tda = False
        except Exception:
            skip_tda = True

        engine = PaperStrategyEngine(
            total_nav_usdt=100_000.0,
            instrument=SYMBOL,
            skip_tda=skip_tda,
            hawkes_threshold=pe._param_float("h2_branching_ratio_threshold", 0.85),
            tda_threshold=pe._param_float("tda1_l1_threshold_p95", 0.000097),
        )
        oi_series, funding_series = await pe._load_derivatives_series(df)
        ofi_series = pe._ofi_proxy(df)
        micro = await pe._load_microstructure_series(df)

        # Build the SAME runner the paper engine builds (identical precompute + thresholds).
        runner, _, _ = engine._build_runner(
            df,
            oi_series=oi_series,
            funding_series=funding_series,
            ofi_series=ofi_series,
            lob_depth_series=micro.get("lob_depth"),
            entropy_series=micro.get("entropy"),
        )

        n = len(df)
        rows = []
        states: list[str] = []
        degraded_count = 0
        for i in range(n):
            feats = runner.build_features(i)
            rec = runner.process_bar(i)  # advances internal dwell/prev-state, in order
            available, missing = _feature_split(feats)
            degraded = bool(rec.cold_start or rec.none_reason is not None)
            if degraded:
                degraded_count += 1
            # Clean label ("Drifting_Calm"), not the enum repr ("StateLabel.DRIFTING_CALM").
            state = getattr(rec.state, "value", None) or str(rec.state)
            states.append(state)
            rows.append(
                (
                    df["time"].iloc[i].to_pydatetime()
                    if hasattr(df["time"].iloc[i], "to_pydatetime")
                    else df["time"].iloc[i],
                    SYMBOL,
                    state,
                    rec.transition_via,
                    available,
                    missing,
                    degraded,
                )
            )

        # Persist to the SEPARATE annotation table (idempotent upsert on the PK).
        await pe._pool.executemany(
            """
            INSERT INTO v2_state_annotation
                (timestamp, symbol, state, transition_via,
                 features_available, features_missing, degraded)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (timestamp, symbol) DO UPDATE SET
                state = EXCLUDED.state, transition_via = EXCLUDED.transition_via,
                features_available = EXCLUDED.features_available,
                features_missing = EXCLUDED.features_missing,
                degraded = EXCLUDED.degraded, annotated_at = NOW()
            """,
            rows,
        )

        # ── distribution + dwell report ──
        from collections import Counter

        dist = Counter(states)
        total = len(states)
        dwell = _dwell_lengths(states)
        top_state, top_n = dist.most_common(1)[0]
        top_share = top_n / total

        report = _build_report(
            dist, total, dwell, degraded_count, top_state, top_share, n
        )
        print(report)  # always emit to stdout (raw), even if the repo path is read-only
        try:
            REPORT_PATH.write_text(report)
            logger.info("report written to %s", REPORT_PATH)
        except (
            OSError
        ) as exc:  # container mounts /app read-only — stdout still carries it
            logger.warning("could not write report file (%s): %s", REPORT_PATH, exc)

        result = {
            "bars": n,
            "distribution": dict(dist),
            "top_state": top_state,
            "top_share": top_share,
            "degraded": degraded_count,
        }
        if top_share >= DEATH_MODE_THRESHOLD:
            # STOP condition (spec): a single state ≥95% is the v1.0 death-mode signal.
            logger.error(
                "DEATH-MODE STOP: state '%s' is %s of bars (≥%s). Report written to %s — "
                "needs Wiki review before proceeding.",
                top_state,
                _pct(top_share),
                _pct(DEATH_MODE_THRESHOLD),
                REPORT_PATH,
            )
            raise SystemExit(2)
        return result
    finally:
        await pe._pool.close()


def _build_report(dist, total, dwell, degraded_count, top_state, top_share, n) -> str:
    lines = [
        "# State Annotation Baseline v1 (Wave S2C Part 4)",
        "",
        f"Offline replay of the live 4H state machine (BarRunner + frozen conditions.py) over "
        f"the full v2_bars_4h history. **{n} bars**, symbol BTC-USDT. Written to "
        f"`v2_state_annotation` (NOT `v2_state_history`).",
        "",
        "## State distribution",
        "",
        "| state | bars | share |",
        "|---|---:|---:|",
    ]
    for state, cnt in dist.most_common():
        lines.append(f"| {state} | {cnt} | {_pct(cnt / total)} |")
    lines += [
        "",
        "## Dwell duration (consecutive bars in state)",
        "",
        "| state | runs | p50 | p90 | max |",
        "|---|---:|---:|---:|---:|",
    ]
    for state, _cnt in dist.most_common():
        runs = dwell.get(state, [])
        if runs:
            arr = np.array(runs)
            lines.append(
                f"| {state} | {len(runs)} | {int(np.percentile(arr, 50))} | "
                f"{int(np.percentile(arr, 90))} | {int(arr.max())} |"
            )
    lines += [
        "",
        "## Feature completeness",
        "",
        f"- Degraded bars (cold_start or a None-reason forced a fallback): "
        f"**{degraded_count} / {total} = {_pct(degraded_count / total)}**",
        f"- Dominant state: **{top_state}** at **{_pct(top_share)}** "
        f"(death-mode STOP threshold = {_pct(DEATH_MODE_THRESHOLD)}).",
        "",
        "Degraded bars are historical bars predating the tick/LOB/liquidation feeds, so the "
        "OI/funding/entropy/LOB-gated conditions ran on a reduced feature set. This is expected "
        "and is exactly what the annotation makes explicit.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(annotate())
