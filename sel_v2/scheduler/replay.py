"""
Offline 4H bar replay — sel v2.0 / Wave 5 helixa integration.

Loads BTC-USDT 4H bars from the local parquet file (Wave 1 data),
precomputes all features (including helixa OI/funding/taker_flow when
available), runs the state machine bar-by-bar, and writes results to
v2_state_history.

Usage:
    python -m sel_v2.scheduler.replay --start 2024-01-01 --end 2026-04-29
    python -m sel_v2.scheduler.replay --dry-run          # no DB writes
    python -m sel_v2.scheduler.replay --reset            # truncate then replay
    python -m sel_v2.scheduler.replay --no-hawkes        # skip Hawkes (fast test)
    python -m sel_v2.scheduler.replay --no-tda           # skip TDA (fast test)
    python -m sel_v2.scheduler.replay --no-helixa        # skip helixa loaders

Output:
    v2_state_history table (≈5096 rows)
    sel_v2/analysis/wave3_replay_report.md
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from sel_v2.scheduler.bar_runner import BarRunner
from sel_v2.scheduler.derivatives_loader import load_oi_funding_series
from sel_v2.scheduler.taker_flow_loader import load_taker_flow_series

# Feature precomputation
from sel_v2.states.hawkes_critical import precompute_branching_ratios
from sel_v2.states.tda_critical import precompute_tda_l1
from sel_v2.strategies.db_writer import DBWriter
from sel_v2.strategies.params_loader import load_strategy_params

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_DEFAULT_PARQUET = os.path.join(os.path.dirname(__file__), "../../analysis/data/btc_4h.parquet")
_REPORT_PATH = os.path.join(os.path.dirname(__file__), "../../sel_v2/analysis/wave3_replay_report.md")
_SIGMA_WINDOW = 180


# ── Feature precomputation ────────────────────────────────────────────────────


def precompute_sigma_series(close: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Returns (sigma_series, sigma_pctile_series) each shape (n,).
    sigma = rolling std of log_returns over last SIGMA_WINDOW bars.
    sigma_pctile = rank of current sigma in last SIGMA_WINDOW sigma values.
    """
    n = len(close)
    log_returns = np.diff(np.log(close))  # length n-1
    sigma = np.full(n, np.nan)
    sigma_pctile = np.full(n, np.nan)

    for i in range(_SIGMA_WINDOW, n):
        seg = log_returns[i - _SIGMA_WINDOW : i]
        sigma[i] = float(np.std(seg))

    # Pctile: for each bar i, rank sigma[i] in sigma[i-WINDOW:i]
    for i in range(_SIGMA_WINDOW * 2, n):
        window = sigma[i - _SIGMA_WINDOW : i]
        valid = window[np.isfinite(window)]
        if len(valid) > 0:
            sigma_pctile[i] = float(np.mean(valid <= sigma[i]))

    return sigma, sigma_pctile


def precompute_tda_pctile_series(
    tda_l1: np.ndarray,
    pctile_window: int = 540,
    q: float = 0.95,
) -> np.ndarray:
    """
    For each bar i: rank of tda_l1[i] in the previous pctile_window values.
    Returns percentile rank in [0, 1].
    """
    n = len(tda_l1)
    pctile = np.full(n, np.nan)
    for i in range(pctile_window, n):
        seg = tda_l1[i - pctile_window : i]
        valid = seg[np.isfinite(seg) & (seg > 0)]
        if len(valid) >= 10:
            pctile[i] = float(np.mean(valid <= tda_l1[i]))
    return pctile


# ── Main replay orchestrator ──────────────────────────────────────────────────


async def run_replay(
    parquet_path: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    dry_run: bool = False,
    reset: bool = False,
    skip_hawkes: bool = False,
    skip_tda: bool = False,
    skip_helixa: bool = False,
    db_url: Optional[str] = None,
    helixa_db_url: Optional[str] = None,
) -> dict:
    t0 = time.time()
    logger.info("=== Wave 3 offline replay starting ===")

    # Load bars
    logger.info("Loading bars from %s", parquet_path)
    df = pd.read_parquet(parquet_path).sort_values("time").reset_index(drop=True)
    logger.info("Loaded %d bars, %s → %s", len(df), df["time"].iloc[0], df["time"].iloc[-1])

    # Filter date range
    if start_date:
        df = df[df["time"] >= pd.Timestamp(start_date, tz="UTC")].reset_index(drop=True)
    if end_date:
        df = df[df["time"] <= pd.Timestamp(end_date, tz="UTC")].reset_index(drop=True)
    logger.info("After date filter: %d bars", len(df))

    closes = df["close"].values.astype(float)
    n = len(closes)

    # Load thresholds from DB
    try:
        h2_params = load_strategy_params("h2", ["branching_ratio_threshold"])
        tda_params = load_strategy_params("tda1", ["l1_threshold_p95"])
        hawkes_threshold = float(h2_params["branching_ratio_threshold"])
        tda_threshold = float(tda_params["l1_threshold_p95"])
        logger.info("Loaded thresholds: hawkes=%.3f  tda=%.6f", hawkes_threshold, tda_threshold)
    except Exception as exc:
        logger.warning("Could not load params from DB (%s) — using hardcoded defaults", exc)
        hawkes_threshold = 0.85
        tda_threshold = 0.000097

    # Precompute σ series
    logger.info("Precomputing σ series...")
    sigma_series, sigma_pctile = precompute_sigma_series(closes)

    # Precompute Hawkes branching ratio
    if skip_hawkes:
        logger.info("Skipping Hawkes computation (--no-hawkes)")
        hawkes_br = np.full(n, np.nan)
    else:
        logger.info("Precomputing Hawkes branching ratio (rolling 90-day MLE)...")
        hawkes_br = precompute_branching_ratios(closes)
        valid_br = hawkes_br[np.isfinite(hawkes_br)]
        if len(valid_br) > 0:
            logger.info(
                "Hawkes BR: mean=%.4f std=%.4f median=%.4f frac>%.2f=%.1f%%",
                np.mean(valid_br),
                np.std(valid_br),
                np.median(valid_br),
                hawkes_threshold,
                np.mean(valid_br > hawkes_threshold) * 100,
            )

    # Precompute TDA L^1
    if skip_tda:
        logger.info("Skipping TDA computation (--no-tda)")
        tda_l1 = np.full(n, np.nan)
        tda_pctile = np.full(n, np.nan)
    else:
        logger.info("Precomputing TDA L^1 norms (rolling 50-bar windows)...")
        tda_l1 = precompute_tda_l1(closes)
        tda_pctile = precompute_tda_pctile_series(tda_l1)
        valid_l1 = tda_l1[np.isfinite(tda_l1)]
        if len(valid_l1) > 0:
            logger.info(
                "TDA L^1: mean=%.6f std=%.6f p95=%.6f threshold=%.6f",
                np.mean(valid_l1),
                np.std(valid_l1),
                float(np.quantile(valid_l1, 0.95)),
                tda_threshold,
            )

    # Load helixa OI / funding / taker-flow series
    oi_raw: Optional[np.ndarray] = None
    funding_raw: Optional[np.ndarray] = None
    ofi_proxy: Optional[np.ndarray] = None

    if not skip_helixa:
        logger.info("Loading helixa derivatives (OI + funding)...")
        try:
            bar_ts_list = list(df["time"])
            deriv = await load_oi_funding_series(bar_ts_list, symbol="BTC", db_url=helixa_db_url)
            oi_raw = deriv["open_interest"]
            funding_raw = deriv["funding_rate"]
            oi_valid = int(np.sum(~np.isnan(oi_raw)))
            logger.info(
                "helixa OI/funding: %d/%d bars covered (%.1f%%)",
                oi_valid,
                n,
                oi_valid / n * 100,
            )
        except Exception as exc:
            logger.warning("helixa derivatives load failed (%s) — skipping", exc)

        logger.info("Loading helixa taker flow (OFI proxy)...")
        try:
            flow = await load_taker_flow_series(bar_ts_list, symbol="BTC/USDT-SWAP", db_url=helixa_db_url)
            ofi_proxy = flow["net_taker_flow"]
            ofi_valid = int(np.sum(~np.isnan(ofi_proxy)))
            logger.info(
                "helixa taker flow: %d/%d bars covered (%.1f%%)",
                ofi_valid,
                n,
                ofi_valid / n * 100,
            )
        except Exception as exc:
            logger.warning("helixa taker_flow load failed (%s) — skipping", exc)
    else:
        logger.info("Skipping helixa loaders (--no-helixa)")

    # Build BarRunner
    runner = BarRunner.from_precomputed(
        df=df,
        sigma_series=sigma_series,
        sigma_pctile_series=sigma_pctile,
        hawkes_br_series=hawkes_br,
        tda_l1_series=tda_l1,
        tda_l1_pctile_series=tda_pctile,
        hawkes_br_threshold=hawkes_threshold,
        tda_l1_threshold=tda_threshold,
        oi_series=oi_raw,
        funding_series=funding_raw,
        ofi_proxy_series=ofi_proxy,
    )

    # Run state machine
    logger.info("Running state machine over %d bars...", n)
    records = runner.run_all()
    logger.info("State machine complete: %d records", len(records))

    # DB writes
    written = 0
    if not dry_run:
        writer = DBWriter(db_url=db_url)
        await writer.connect()

        if reset:
            logger.info("Resetting v2_state_history...")
            await writer.clear_state_history()

        logger.info("Writing %d records to v2_state_history...", len(records))
        written = await writer.write_states_bulk(records)
        logger.info("Written: %d rows", written)
        await writer.close()
    else:
        logger.info("DRY RUN — no DB writes")
        written = 0

    elapsed = time.time() - t0
    logger.info("Replay complete in %.1f seconds", elapsed)

    # Compile statistics
    stats = _compile_stats(records, df)
    stats["elapsed_seconds"] = elapsed
    stats["total_bars"] = n
    stats["written_rows"] = written
    stats["hawkes_threshold"] = hawkes_threshold
    stats["tda_threshold"] = tda_threshold
    stats["hawkes_br_mean"] = float(np.nanmean(hawkes_br)) if not skip_hawkes else None
    stats["tda_l1_mean"] = float(np.nanmean(tda_l1)) if not skip_tda else None
    stats["sigma_mean"] = float(np.nanmean(sigma_series))
    stats["start_date"] = str(df["time"].iloc[0])
    stats["end_date"] = str(df["time"].iloc[-1])
    stats["helixa_oi_covered"] = int(np.sum(~np.isnan(oi_raw))) if oi_raw is not None else 0
    stats["helixa_funding_covered"] = int(np.sum(~np.isnan(funding_raw))) if funding_raw is not None else 0
    stats["helixa_ofi_covered"] = int(np.sum(~np.isnan(ofi_proxy))) if ofi_proxy is not None else 0

    # Write report
    _write_report(stats, records, _REPORT_PATH)
    logger.info("Report written to %s", _REPORT_PATH)

    return stats


def _compile_stats(records, df) -> dict:
    from collections import Counter

    state_counts: Counter = Counter()
    transition_counts: Counter = Counter()
    illegal_transitions = 0
    cold_start_bars = 0
    critical_a_full_count = 0
    critical_a_partial_count = 0
    critical_b_count = 0
    critical_c_count = 0
    critical_ab_count = 0
    critical_ac_count = 0
    critical_abc_count = 0
    critical_path1_count = 0
    critical_path2_count = 0

    for r in records:
        state_counts[r.state.value] += 1
        if r.transition_via:
            transition_counts[r.transition_via] += 1
        if not r.is_legal:
            illegal_transitions += 1
        if r.cold_start:
            cold_start_bars += 1

        sf = r.state_features
        if sf.get("critical_a_full") is True:
            critical_a_full_count += 1
        if sf.get("critical_a_partial") is True:
            critical_a_partial_count += 1
        if sf.get("critical_b") is True:
            critical_b_count += 1
        if sf.get("critical_c") is True:
            critical_c_count += 1
        if sf.get("critical_a_partial") is True and sf.get("critical_b") is True:
            critical_ab_count += 1
        if sf.get("critical_a_partial") is True and sf.get("critical_c") is True:
            critical_ac_count += 1
        if sf.get("critical_a_partial") is True and sf.get("critical_b") is True and sf.get("critical_c") is True:
            critical_abc_count += 1
        if sf.get("critical_path1") is True:
            critical_path1_count += 1
        if sf.get("critical_path2") is True:
            critical_path2_count += 1

    total = len(records)
    state_pcts = {k: round(v / total * 100, 1) for k, v in state_counts.items()}

    return {
        "state_counts": dict(state_counts),
        "state_pcts": state_pcts,
        "transition_counts": dict(transition_counts),
        "illegal_transitions": illegal_transitions,
        "cold_start_bars": cold_start_bars,
        "critical_a_full_count": critical_a_full_count,
        "critical_a_partial_count": critical_a_partial_count,
        "critical_b_count": critical_b_count,
        "critical_c_count": critical_c_count,
        "critical_ab_count": critical_ab_count,
        "critical_ac_count": critical_ac_count,
        "critical_abc_count": critical_abc_count,
        "critical_path1_count": critical_path1_count,
        "critical_path2_count": critical_path2_count,
    }


def _write_report(stats: dict, records, output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    total = stats["total_bars"]
    lines = [
        "# Wave 3 Offline Replay Report",
        "",
        f"**Generated**: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}  ",
        f"**Elapsed**: {stats.get('elapsed_seconds', 0):.1f} s  ",
        "",
        "## 1. Replay Time Range",
        "",
        f"- Start: {stats.get('start_date', 'N/A')}",
        f"- End:   {stats.get('end_date', 'N/A')}",
        f"- Total bars: {total}",
        f"- Written to v2_state_history: {stats.get('written_rows', 0)}",
        "",
        "## 2. State Distribution",
        "",
        "| State | Bars | % | Target (§16.2) |",
        "|---|---|---|---|",
    ]

    targets = {
        "Coiling": "20-30%",
        "Drifting_Calm": "40-60%",
        "Surging": "10-20%",
        "Drifting_Charged": "—",
        "Critical": "1-5%",
        "Cascade": "<1%",
    }
    for state in ["Coiling", "Surging", "Drifting_Calm", "Drifting_Charged", "Critical", "Cascade"]:
        count = stats["state_counts"].get(state, 0)
        pct = stats["state_pcts"].get(state, 0.0)
        target = targets.get(state, "—")
        lines.append(f"| {state} | {count} | {pct:.1f}% | {target} |")

    lines += [
        "",
        "## 3. Transition Vocabulary Distribution",
        "",
        "| Transition | Count |",
        "|---|---|",
    ]
    for vocab in ["Release", "Exhaustion", "Decay", "Charging", "Stress", "Trigger", "Reset"]:
        count = stats["transition_counts"].get(vocab, 0)
        lines.append(f"| {vocab} | {count} |")
    lines += [
        "",
        f"**Illegal transitions**: {stats['illegal_transitions']}",
        "",
        "## 4. Critical Main Condition Statistics",
        "",
        "| Condition | Count | Bars (%) |",
        "|---|---|---|",
        f"| A partial (σ > 90th + monotone) | {stats['critical_a_partial_count']} | {stats['critical_a_partial_count'] / total * 100:.1f}% |",
        f"| A full (A_partial + entropy) | {stats['critical_a_full_count']} | {stats['critical_a_full_count'] / total * 100:.1f}% |",
        f"| B (Hawkes BR > threshold) | {stats['critical_b_count']} | {stats['critical_b_count'] / total * 100:.1f}% |",
        f"| C (TDA > 95th pctile + monotone) | {stats['critical_c_count']} | {stats['critical_c_count'] / total * 100:.1f}% |",
        f"| A_partial + B (path 2 candidate) | {stats['critical_ab_count']} | {stats['critical_ab_count'] / total * 100:.1f}% |",
        f"| A_partial + C | {stats['critical_ac_count']} | {stats['critical_ac_count'] / total * 100:.1f}% |",
        f"| A_partial + B + C (path 2 fires) | {stats['critical_abc_count']} | {stats['critical_abc_count'] / total * 100:.1f}% |",
        f"| Path 1 (A_full + B or C) | {stats['critical_path1_count']} | {stats['critical_path1_count'] / total * 100:.1f}% |",
        f"| Path 2 (A_partial + B + C) | {stats['critical_path2_count']} | {stats['critical_path2_count'] / total * 100:.1f}% |",
        "",
        "**Note**: A_full is always 0 in Wave 3 replay because A2 (LOB entropy) is STUB (None).",
        "All Critical entries via Path 2 (A_partial + B + C).",
        "",
        "## 5. Cold Start Period",
        "",
        f"- Cold-start bars (≤ {180} bar warmup): {stats['cold_start_bars']}",
        f"- Normal bars: {total - stats['cold_start_bars']}",
        "",
        "## 6. STUB Impact",
        "",
        "| Feature | Status | Affected States |",
        "|---|---|---|",
        "| LOB entropy | STUB (None) | Coiling A2, Drifting-Calm, Cascade cond1 |",
        "| OI change rate | STUB (None) | Coiling, Drifting-Charged, Surging |",
        "| Funding rate | STUB (None) | Coiling, Drifting-Charged |",
        "| LOB depth percentile | STUB (None) | Cascade condition 1 |",
        "| Liquidation pulse | STUB (None) | Cascade condition 2 |",
        "| Cross-exchange spread | STUB (None) | Cascade condition 3 |",
        "| OFI cumulative | STUB (None) | Surging, Strategy 2 |",
        "",
        "**Effect on state distribution**: Cascade = 0 (all 3 conditions require STUB data).",
        "Coiling and Surging may be under-counted because σ alone triggers without OI/OFI confirmation.",
        "",
        "## 7. Hawkes vs Wave 1 Calibration",
        "",
        "| | Wave 1 full-fit η | Wave 3 rolling mean |",
        "|---|---|---|",
        f"| η = α/β | 0.5537 | {stats.get('hawkes_br_mean', 'N/A'):.4f} |"
        if stats.get("hawkes_br_mean")
        else "| η = α/β | 0.5537 | N/A (skipped) |",
        f"| Critical threshold | 0.85 | {stats.get('hawkes_threshold', 0.85):.2f} |",
        "",
        "## 8. TDA L^1 vs Wave 1 Calibration",
        "",
        "| | Wave 1 p95 threshold | Wave 3 rolling mean |",
        "|---|---|---|",
        f"| L^1 norm | 0.000097 | {stats.get('tda_l1_mean', 'N/A'):.6f} |"
        if stats.get("tda_l1_mean")
        else "| L^1 norm | 0.000097 | N/A (skipped) |",
        f"| B threshold (v2_strategy_params) | {stats.get('tda_threshold', 0.000097):.6f} | — |",
        "",
        "## 9. Health Alerts (§16.2 targets)",
        "",
    ]

    alerts = []
    coiling_pct = stats["state_pcts"].get("Coiling", 0)
    drifting_calm_pct = stats["state_pcts"].get("Drifting_Calm", 0)
    surging_pct = stats["state_pcts"].get("Surging", 0)
    critical_pct = stats["state_pcts"].get("Critical", 0)
    cascade_pct = stats["state_pcts"].get("Cascade", 0)

    if not (20 <= coiling_pct <= 30):
        alerts.append(f"⚠️ Coiling={coiling_pct:.1f}% (target 20-30%). LOB/OI STUB may cause undercounting.")
    if not (40 <= drifting_calm_pct <= 60):
        alerts.append(f"⚠️ Drifting-Calm={drifting_calm_pct:.1f}% (target 40-60%).")
    if not (10 <= surging_pct <= 20):
        alerts.append(f"⚠️ Surging={surging_pct:.1f}% (target 10-20%). OFI STUB affects entry confirmation.")
    if not (1 <= critical_pct <= 5):
        alerts.append(f"⚠️ Critical={critical_pct:.1f}% (target 1-5%). Entropy STUB forces Path 2 only.")
    if cascade_pct > 1:
        alerts.append(f"🚨 Cascade={cascade_pct:.1f}% (target <1%).")

    if not alerts:
        lines.append("✅ All state distributions within §16.2 target ranges.")
    else:
        for alert in alerts:
            lines.append(f"- {alert}")

    lines += [
        "",
        "## Limitations",
        "",
        "- Cascade = 0%: All 3 Cascade conditions require STUB data (LOB depth / liquidation / spread).",
        "  This is an expected limitation per STUB_BOUNDARIES.md.",
        "- Critical A_full = 0%: LOB entropy (A2 sub-condition) is STUB. All Critical via Path 2.",
        "- Surging entry: OFI and OI acceleration are STUB; price+σ conditions only.",
        "  May undercount Surging bars that would have been blocked by missing OFI confirmation.",
        "- Drifting-Charged: OI and funding conditions both STUB. This state requires real data.",
        "",
        "---",
        "*Generated by sel_v2/scheduler/replay.py — Wave 3 deliverable*",
    ]

    with open(output_path, "w") as f:
        f.write("\n".join(lines))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="sel v2.0 offline 4H bar replay")
    p.add_argument("--start", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    p.add_argument("--end", default="2026-04-29", help="End date (YYYY-MM-DD)")
    p.add_argument("--parquet", default=_DEFAULT_PARQUET, help="Path to btc_4h.parquet")
    p.add_argument("--dry-run", action="store_true", help="Skip DB writes")
    p.add_argument("--reset", action="store_true", help="Truncate v2_state_history before replay")
    p.add_argument("--no-hawkes", action="store_true", help="Skip Hawkes computation (fast test)")
    p.add_argument("--no-tda", action="store_true", help="Skip TDA computation (fast test)")
    p.add_argument("--no-helixa", action="store_true", help="Skip helixa OI/funding/taker_flow loading")
    p.add_argument("--db-url", default=None, help="PostgreSQL DSN override")
    p.add_argument("--helixa-db-url", default=None, help="Helixa PostgreSQL DSN override")
    return p.parse_args()


async def main() -> None:
    args = _parse_args()
    stats = await run_replay(
        parquet_path=args.parquet,
        start_date=args.start,
        end_date=args.end,
        dry_run=args.dry_run,
        reset=args.reset,
        skip_hawkes=args.no_hawkes,
        skip_tda=args.no_tda,
        skip_helixa=args.no_helixa,
        db_url=args.db_url,
        helixa_db_url=args.helixa_db_url,
    )
    print("\n=== Replay Summary ===")
    print(f"Total bars : {stats['total_bars']}")
    print(f"Written    : {stats['written_rows']}")
    print(f"State dist : {stats['state_counts']}")
    print(f"Transitions: {stats['transition_counts']}")
    print(f"Elapsed    : {stats['elapsed_seconds']:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
