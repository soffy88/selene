"""
TDA1 Persistence Landscape calibration  (sel-language-v2.1-patches §12.1)

Implements offline TDA training:
  1. Takens embedding of BTC 4H log-returns
  2. Persistent Homology via ripser (Vietoris-Rips)
  3. Persistence Landscape L^1 norm time series
  4. Validation on historical Cascade events
  5. Rolling 90-day quantile threshold calibration

Output: analysis/tda_calibration_v1.md

Usage:
    python -m sel_v2.offline.tda_calibration [--data analysis/data/btc_4h.parquet]
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime

import numpy as np
import pandas as pd
from ripser import ripser
from scipy.spatial.distance import cdist

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Cascade events for validation
KNOWN_EVENTS = [
    ("2024-04-13", "Cascade", "Iran-Israel: BTC ~-15%"),
    ("2024-08-05", "Cascade", "Yen carry unwind: BTC flash crash"),
    ("2025-02-03", "Cascade", "Macro shock: BTC -20%"),
    ("2025-04-07", "Cascade", "Risk-off: BTC -12%"),
]

# Control events (should NOT trigger TDA signal)
CONTROL_EVENTS = [
    ("2024-03-14", "Normal", "BTC new ATH ~$73k, trend extension"),
    ("2024-10-16", "Normal", "BTC consolidation mid-range"),
    ("2025-01-15", "Normal", "Post-inauguration rally"),
    ("2025-06-01", "Normal", "Drifting-Calm period"),
]


# ── Takens Embedding ──────────────────────────────────────────────────────────

def takens_embed(x: np.ndarray, d: int = 4, tau: int = 1) -> np.ndarray:
    """
    Takens delay embedding.
    x: 1D time series
    d: embedding dimension
    tau: time delay (in bar steps)
    Returns: (N - (d-1)*tau) × d matrix of delay vectors.
    """
    n = len(x) - (d - 1) * tau
    if n <= 0:
        raise ValueError(f"Series too short for embedding: n={len(x)}, d={d}, tau={tau}")
    return np.stack([x[i * tau: i * tau + n] for i in range(d)], axis=1)


def estimate_tau(x: np.ndarray, max_lag: int = 20) -> int:
    """
    Estimate tau as first minimum of average mutual information (AMI).
    Approximate AMI via binned mutual information.
    """
    n_bins = max(10, int(np.sqrt(len(x))))
    x_disc = np.digitize(x, np.histogram_bin_edges(x, bins=n_bins)) - 1
    x_disc = np.clip(x_disc, 0, n_bins - 1)

    ami_vals = []
    for lag in range(1, max_lag + 1):
        x1 = x_disc[:-lag]
        x2 = x_disc[lag:]
        joint = np.bincount(x1 * n_bins + x2, minlength=n_bins ** 2).reshape(n_bins, n_bins)
        p_joint = joint / joint.sum()
        p1 = p_joint.sum(axis=1)
        p2 = p_joint.sum(axis=0)
        outer = np.outer(p1, p2)
        mask = (p_joint > 0) & (outer > 0)
        ami = float(np.sum(p_joint[mask] * np.log2(p_joint[mask] / outer[mask])))
        ami_vals.append(ami)

    # First local minimum
    for i in range(1, len(ami_vals) - 1):
        if ami_vals[i] < ami_vals[i - 1] and ami_vals[i] < ami_vals[i + 1]:
            return i + 1
    return 1  # fallback


# ── Persistence Landscape ─────────────────────────────────────────────────────

def persistence_diagram_to_landscape(
    dgm: np.ndarray,
    resolution: int = 100,
    x_min: float = 0.0,
    x_max: float = 2.0,
    k: int = 1,
) -> np.ndarray:
    """
    Convert a persistence diagram (N × 2 array of [birth, death]) to a
    persistence landscape Λ_k at a given resolution.

    Λ_k(t) = k-th largest value of {tent_p(t)} over all points p=(b,d).
    tent_p(t) = max(0, min(t-b, d-t)).
    """
    if len(dgm) == 0:
        return np.zeros(resolution)

    t_vals = np.linspace(x_min, x_max, resolution)
    tents = np.zeros((len(dgm), resolution))

    for i, (b, d) in enumerate(dgm):
        if not np.isfinite(d):
            d = x_max
        tent = np.maximum(0, np.minimum(t_vals - b, d - t_vals))
        tents[i] = tent

    # Sort descending at each t, take k-th
    tents_sorted = np.sort(tents, axis=0)[::-1]
    if k <= len(tents_sorted):
        return tents_sorted[k - 1]
    return np.zeros(resolution)


def compute_pl_l1(
    returns: np.ndarray,
    d: int = 4,
    tau: int = 1,
    max_dim: int = 1,
    k: int = 1,
    resolution: int = 100,
) -> float:
    """
    Compute Persistence Landscape L^1 norm for a window of returns.
    Returns 0.0 if computation fails (insufficient data).
    """
    try:
        cloud = takens_embed(returns, d=d, tau=tau)
        if len(cloud) < 5:
            return 0.0

        # Subsample for efficiency if too large
        if len(cloud) > 200:
            rng = np.random.default_rng(0)
            idx = rng.choice(len(cloud), 200, replace=False)
            cloud = cloud[idx]

        # Compute persistent homology
        dgms = ripser(cloud, maxdim=max_dim)["dgms"]
        dgm_h1 = dgms[1] if max_dim >= 1 and len(dgms) > 1 else np.empty((0, 2))

        if len(dgm_h1) == 0:
            return 0.0

        # Compute persistence landscape L^1 norm for H1
        # Clip infinite death values to finite max
        finite_mask = np.isfinite(dgm_h1[:, 1])
        dgm_finite = dgm_h1[finite_mask]
        if len(dgm_finite) == 0:
            return 0.0

        x_max = float(dgm_finite[:, 1].max()) * 1.1
        landscape = persistence_landscape_l1_norm = 0.0
        pl = persistence_diagram_to_landscape(
            dgm_finite, resolution=resolution, x_max=x_max, k=k
        )
        l1_norm = float(np.trapezoid(np.abs(pl), np.linspace(0, x_max, resolution)))
        return l1_norm

    except Exception as exc:
        logger.debug("TDA computation error: %s", exc)
        return 0.0


# ── Rolling TDA analysis ──────────────────────────────────────────────────────

def run_rolling_tda(
    returns: np.ndarray,
    window: int = 50,
    step: int = 5,
    d: int = 4,
    tau: int = 1,
) -> np.ndarray:
    """
    Compute rolling L^1 norm over the return series.
    Returns array of (n_windows,) L^1 values.
    """
    n = len(returns)
    n_windows = (n - window) // step + 1
    l1_series = np.zeros(n_windows)

    for wi in range(n_windows):
        start = wi * step
        seg = returns[start: start + window]
        l1_series[wi] = compute_pl_l1(seg, d=d, tau=tau)
        if wi % 20 == 0:
            logger.info("  TDA window %d/%d (index %d)", wi, n_windows, start)

    return l1_series


# ── Main calibration ──────────────────────────────────────────────────────────

def run_tda_calibration(
    data_path: str = "analysis/data/btc_4h.parquet",
    window: int = 50,
    step: int = 5,
    d: int = 4,
    tau: int = 1,
    output_path: str = "analysis/tda_calibration_v1.md",
    persist: bool = True,
    db_url: str | None = None,
) -> dict:
    logger.info("Loading data from %s", data_path)
    df = pd.read_parquet(data_path).sort_values("time").reset_index(drop=True)
    logger.info("Loaded %d bars", len(df))

    close = df["close"].values.astype(float)
    times = df["time"].values

    returns = np.diff(np.log(close))

    # Use log-price path (not returns) for Takens embedding.
    # Log-returns are stationary and don't form loops (H1 features) in phase space.
    # Log-prices are non-stationary and create meaningful trajectories/orbits
    # in the delay-embedding space, consistent with Gidea & Katz 2018 approach.
    log_prices = np.log(close)
    n = len(log_prices)

    # Estimate tau using AMI on the differenced series (stationarised)
    tau_est = estimate_tau(returns[:500])
    logger.info("Estimated tau: %d (using tau=%d as per d2.1 spec)", tau_est, tau)
    logger.info("Using log-price path for Takens embedding (not returns)")

    logger.info("Computing rolling TDA L^1 norm (window=%d, step=%d)...", window, step)
    l1_series = run_rolling_tda(log_prices, window=window, step=step, d=d, tau=tau)

    # Time index for l1_series (each point corresponds to the end of the window)
    window_times = []
    for wi in range(len(l1_series)):
        start = wi * step
        end_idx = min(start + window - 1, n - 1)
        window_times.append(times[end_idx])

    # Rolling 90-day quantile threshold calibration
    # 90 days = 90 * 6 = 540 4H bars, but after step reduction ~540/step windows
    q_window_bars = 540
    q_window_steps = max(1, q_window_bars // step)
    thresholds_90 = []
    thresholds_95 = []
    thresholds_97 = []
    for i in range(len(l1_series)):
        start = max(0, i - q_window_steps)
        segment = l1_series[start: i + 1]
        thresholds_90.append(np.quantile(segment, 0.90))
        thresholds_95.append(np.quantile(segment, 0.95))
        thresholds_97.append(np.quantile(segment, 0.97))

    # Evaluate on known Cascade events
    event_results = []
    for event_date_str, label, desc in KNOWN_EVENTS + CONTROL_EVENTS:
        event_dt = np.datetime64(event_date_str)
        diffs = np.abs(times.astype("datetime64[D]") - event_dt)
        bar_idx = int(np.argmin(diffs))
        # Find corresponding window
        if bar_idx < window:
            event_results.append({
                "date": event_date_str, "label": label, "desc": desc,
                "in_range": False,
            })
            continue

        win_idx = (bar_idx - window) // step
        if win_idx >= len(l1_series):
            event_results.append({
                "date": event_date_str, "label": label, "desc": desc,
                "in_range": False,
            })
            continue

        l1_val = l1_series[win_idx]
        q90 = thresholds_90[win_idx]
        q95 = thresholds_95[win_idx]

        event_results.append({
            "date": event_date_str,
            "label": label,
            "desc": desc,
            "in_range": True,
            "l1_norm": l1_val,
            "threshold_90": q90,
            "threshold_95": q95,
            "above_90": l1_val > q90,
            "above_95": l1_val > q95,
        })

    # Summary stats
    l1_mean = float(np.mean(l1_series[l1_series > 0]))
    l1_std = float(np.std(l1_series[l1_series > 0]))
    l1_q90 = float(np.quantile(l1_series, 0.90))
    l1_q95 = float(np.quantile(l1_series, 0.95))
    l1_q97 = float(np.quantile(l1_series, 0.97))

    result = {
        "n_bars": n,
        "coverage_start": str(df["time"].iloc[0]),
        "coverage_end": str(df["time"].iloc[-1]),
        "window": window,
        "step": step,
        "d": d,
        "tau": tau,
        "tau_estimated": tau_est,
        "l1_series": l1_series,
        "window_times": window_times,
        "thresholds_90": thresholds_90,
        "thresholds_95": thresholds_95,
        "thresholds_97": thresholds_97,
        "l1_mean": l1_mean,
        "l1_std": l1_std,
        "l1_q90": l1_q90,
        "l1_q95": l1_q95,
        "l1_q97": l1_q97,
        "event_results": event_results,
    }

    _write_report(result, output_path)

    if persist:
        _persist_to_db(result, db_url)

    return result


def _persist_to_db(result: dict, db_url: str | None) -> dict | None:
    """Write the TDA L^1 percentile thresholds into v2_strategy_params so the live
    Critical-state TDA condition uses the data-derived threshold instead of the
    hardcoded placeholder (sel_v2/states/schema.py tda_l1_threshold=0.000097). The
    p95 key is the one the live reader needs (replay.py: load_strategy_params('tda1',
    ['l1_threshold_p95'])); p90/p97 are persisted alongside for tuning context.

    Returns the persisted params, or None if the fit was degenerate or the write
    failed (logged, not raised — the markdown report is still produced and offline
    calibration must run without a DB)."""
    params = {
        "l1_threshold_p90": result.get("l1_q90"),
        "l1_threshold_p95": result.get("l1_q95"),
        "l1_threshold_p97": result.get("l1_q97"),
    }
    if any(v is None or not np.isfinite(v) for v in params.values()):
        logger.warning("Skipping DB persist: TDA L^1 thresholds not finite (%s)", params)
        return None
    try:
        from sel_v2.strategies.params_loader import save_strategy_params
        save_strategy_params(strategy="tda1", params=params, db_url=db_url)
        logger.info("Persisted TDA1 L^1 thresholds to v2_strategy_params "
                    "(p90=%.6f p95=%.6f p97=%.6f)",
                    params["l1_threshold_p90"], params["l1_threshold_p95"],
                    params["l1_threshold_p97"])
        return params
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to persist TDA1 thresholds to v2_strategy_params (%s). "
                     "The live TDA condition will keep using the placeholder.", exc)
        return None


def _write_report(result: dict, output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    l1_series = result["l1_series"]
    event_results = result["event_results"]

    lines = [
        "# TDA1 Persistence Landscape Calibration",
        "",
        f"**Date**: {pd.Timestamp.now().date()}  ",
        "**Method**: Takens Embedding → Vietoris-Rips → Persistent Homology (H1) → Landscape L^1 norm  ",
        f"**Library**: ripser  ",
        f"**Embedding**: d={result['d']}, τ={result['tau']} (estimated τ={result['tau_estimated']})  ",
        f"**Window**: {result['window']} 4H bars ({result['window'] * 4}h)  ",
        f"**Step**: {result['step']} bars  ",
        f"**Data**: BTC-USDT 4H log returns  ",
        f"**Coverage**: {result['coverage_start']} → {result['coverage_end']}  ",
        f"**N bars**: {result['n_bars']}  ",
        "",
        "## L^1 Norm Summary Statistics",
        "",
        "| Statistic | Value |",
        "|---|---|",
        f"| Mean | {result['l1_mean']:.6f} |",
        f"| Std | {result['l1_std']:.6f} |",
        f"| 90th percentile | {result['l1_q90']:.6f} |",
        f"| 95th percentile (v2.1 threshold) | {result['l1_q95']:.6f} |",
        f"| 97th percentile | {result['l1_q97']:.6f} |",
        "",
        "**v2.1 spec**: Critical entry condition = L^1 norm > 95th percentile + 12h monotone rise.",
        f"**Initial threshold** (static): {result['l1_q95']:.6f}  ",
        "**Production threshold**: rolling 90-day 95th percentile (recalculated per bar).  ",
        "",
        "## Cascade Event Validation",
        "",
        "| Date | Label | L^1 Norm | 90th %tile | 95th %tile | Above 90% | Above 95% |",
        "|---|---|---|---|---|---|---|",
    ]

    for ev in event_results:
        if not ev["in_range"]:
            lines.append(
                f"| {ev['date']} | {ev['label']} | N/A | N/A | N/A | N/A | N/A |"
            )
        else:
            a90 = "✅" if ev.get("above_90") else "❌"
            a95 = "✅" if ev.get("above_95") else "❌"
            lines.append(
                f"| {ev['date']} | {ev['label']} | {ev['l1_norm']:.6f} | "
                f"{ev['threshold_90']:.6f} | {ev['threshold_95']:.6f} | {a90} | {a95} |"
            )

    # Validation summary
    cascade_events = [ev for ev in event_results if ev.get("label") == "Cascade" and ev.get("in_range")]
    control_events = [ev for ev in event_results if ev.get("label") == "Normal" and ev.get("in_range")]
    cascade_tp = sum(1 for ev in cascade_events if ev.get("above_95", False))
    control_fp = sum(1 for ev in control_events if ev.get("above_95", False))

    lines += [
        "",
        f"**Cascade events above 95th %tile**: {cascade_tp}/{len(cascade_events)} "
        f"({'N/A — no in-range events' if not cascade_events else f'{cascade_tp/len(cascade_events)*100:.0f}% sensitivity'})",
        f"**Control events above 95th %tile (false positive rate)**: {control_fp}/{len(control_events)} "
        f"({'N/A' if not control_events else f'{control_fp/len(control_events)*100:.0f}%'})",
        "",
    ]

    lines += [
        "## Initial Threshold Parameters (for paper trading launch)",
        "",
        "```python",
        "# TDA1 Critical state condition (v2.1 spec, H2 eq)",
        "# Use rolling 90-day 95th percentile in production",
        f"TDA1_THRESHOLD_STATIC = {result['l1_q95']:.6f}  # initial, replace with rolling",
        "TDA1_QUANTILE = 0.95",
        "TDA1_ROLLING_WINDOW_BARS = 540  # 90 days × 6 bars/day",
        "",
        "# Embedding parameters",
        f"TDA1_D = {result['d']}",
        f"TDA1_TAU = {result['tau']}",
        f"TDA1_WINDOW = {result['window']}  # bars per computation",
        "```",
        "",
        "## Computational Performance",
        "",
        f"| Metric | Value |",
        "|---|---|",
        f"| Total windows computed | {len(l1_series)} |",
        f"| Window size | {result['window']} bars ({result['window'] * 4}h) |",
        f"| Step size | {result['step']} bars ({result['step'] * 4}h) |",
        "",
        "**Production latency estimate**: ~0.5-2s per 4H bar on CPU (100-point cloud, ripser). ",
        "Well within 4H decision window. Monitor in production.",
        "",
        "## Rolling Threshold Time Series (sample)",
        "",
        "| Date | L^1 Norm | 90th %tile | 95th %tile | 97th %tile |",
        "|---|---|---|---|---|",
    ]

    # Sample every 50th window to avoid huge table
    step_sample = max(1, len(l1_series) // 30)
    for i in range(0, len(l1_series), step_sample):
        t_str = str(result["window_times"][i])[:10]
        lines.append(
            f"| {t_str} | {l1_series[i]:.6f} | "
            f"{result['thresholds_90'][i]:.6f} | "
            f"{result['thresholds_95'][i]:.6f} | "
            f"{result['thresholds_97'][i]:.6f} |"
        )

    lines += [
        "",
        "## sel v2.0 Design Validation",
        "",
        _design_validation(cascade_tp, len(cascade_events), control_fp, len(control_events)),
        "",
        "## Limitations",
        "",
        "- **Cascade sample**: ~4 events. Sample size far too small for robust statistical validation.",
        "  (This is an expected limitation per v2.0 §18.3.)",
        "- **Temporal alignment**: TDA window ends at bar close; events are detected at bar open.",
        "  True lead time may differ from what appears here.",
        "- **Post-ETF validity**: Literature mostly uses 2018 data. BTC structure changed in 2024+.",
        "- **τ estimation**: AMI tau estimation on 4H bars is approximate.",
        "  Production should use persistent homology across τ ∈ {1,2,3} and take max.",
        "- **Dimension d=4**: Cao's method should be applied to confirm d. Not computed here.",
        "",
        "---",
        "*Generated by sel_v2/offline/tda_calibration.py — Wave 1 deliverable*",
    ]

    with open(output_path, "w") as f:
        f.write("\n".join(lines))
    logger.info("Report written to %s", output_path)


def _design_validation(
    cascade_tp: int,
    n_cascade: int,
    control_fp: int,
    n_control: int,
) -> str:
    lines = []

    if n_cascade == 0:
        lines.append(
            "⚠️ **Cascade events**: All historical cascade events fall outside the data range "
            "or before data coverage. Cannot validate TDA1 sensitivity. "
            "Re-run after 1+ Cascade events accumulate in paper data."
        )
    elif cascade_tp / n_cascade >= 0.75:
        lines.append(
            f"✅ **Cascade sensitivity**: {cascade_tp}/{n_cascade} Cascade events triggered TDA L^1 > 95th %tile. "
            "TDA1 shows promise as a Critical state indicator."
        )
    elif cascade_tp / n_cascade >= 0.5:
        lines.append(
            f"⚠️ **Moderate Cascade sensitivity**: {cascade_tp}/{n_cascade} events detected. "
            "Consider tuning to 90th percentile threshold or combining with σ-based condition."
        )
    else:
        lines.append(
            f"❌ **Low Cascade sensitivity**: {cascade_tp}/{n_cascade} events detected. "
            "TDA1 may not be reliable with current parameters. "
            "Recommended: reduce threshold to 90th percentile and re-evaluate. "
            "Or wait for more live paper data before relying on this condition."
        )

    if n_control > 0:
        if control_fp / n_control <= 0.25:
            lines.append(
                f"\n✅ **False positive rate**: {control_fp}/{n_control} control events "
                f"({control_fp/n_control*100:.0f}%) exceeded threshold. Acceptable specificity."
            )
        else:
            lines.append(
                f"\n⚠️ **High false positive rate**: {control_fp}/{n_control} control events "
                f"({control_fp/n_control*100:.0f}%) exceeded threshold. "
                "TDA1 may generate too many spurious Critical signals. "
                "Combine with Hawkes branching ratio (H2) per v2.1 spec."
            )

    lines.append(
        "\n**Production recommendation per v2.1 §2.1**: TDA L^1 > 95th %tile is ONE of two "
        "new main conditions. Combined logic requires either (σ-based full + one of TDA/Hawkes) "
        "OR (σ-based partial + both TDA and Hawkes). Single-tool false alarms are filtered."
    )

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="TDA1 Persistence Landscape calibration")
    parser.add_argument("--data", default="analysis/data/btc_4h.parquet")
    parser.add_argument("--window", type=int, default=50,
                        help="Sliding window size in bars")
    parser.add_argument("--step", type=int, default=5,
                        help="Step size between windows")
    parser.add_argument("--d", type=int, default=4,
                        help="Takens embedding dimension")
    parser.add_argument("--tau", type=int, default=1,
                        help="Takens time delay (bars)")
    parser.add_argument("--output", default="analysis/tda_calibration_v1.md")
    parser.add_argument("--no-persist", action="store_true",
                        help="Skip writing L^1 thresholds to v2_strategy_params")
    parser.add_argument("--db-url", default=None,
                        help="Postgres DSN override for param persistence")
    args = parser.parse_args()

    run_tda_calibration(
        data_path=args.data,
        window=args.window,
        step=args.step,
        d=args.d,
        tau=args.tau,
        output_path=args.output,
        persist=not args.no_persist,
        db_url=args.db_url,
    )


if __name__ == "__main__":
    main()
