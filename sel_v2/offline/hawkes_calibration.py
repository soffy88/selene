"""
H2 Hawkes Branching Ratio offline calibration  (sel-language-v2.1-patches §5)

Implements offline Hawkes calibration:
  1. Define events: 4H bars where |log_return| > 1-sigma threshold
  2. Fit univariate exponential Hawkes via MLE (scipy)
  3. Rolling 90-day branching ratio α/β time series
  4. Validate: α/β > 0.85 at Cascade events (Critical state condition)
  5. Report calibration parameters and rolling threshold

Note: Wave 1 uses 4H OHLCV bars as event proxy (|return| spikes).
True tick-level Hawkes fitting is deferred to Wave 3 when LOB data is available.

Output: analysis/hawkes_calibration_v1.md

Usage:
    python -m sel_v2.offline.hawkes_calibration [--data analysis/data/btc_4h.parquet]
"""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

KNOWN_EVENTS = [
    ("2024-04-13", "Cascade", "Iran-Israel: BTC ~-15%"),
    ("2024-08-05", "Cascade", "Yen carry unwind: BTC flash crash"),
    ("2025-02-03", "Cascade", "Macro shock: BTC -20%"),
    ("2025-04-07", "Cascade", "Risk-off: BTC -12%"),
]

CONTROL_EVENTS = [
    ("2024-03-14", "Normal", "BTC new ATH ~$73k, trend extension"),
    ("2024-10-16", "Normal", "BTC consolidation mid-range"),
    ("2025-01-15", "Normal", "Post-inauguration rally"),
    ("2025-06-01", "Normal", "Drifting-Calm period"),
]

# v2.1 §5.2 Critical state condition: branching ratio > CRITICAL_THRESHOLD
CRITICAL_THRESHOLD = 0.85

# A full-sample fit with branching ratio above this is treated as a diverged MLE
# (matches the rolling-stats MAX_BR clip) and is not persisted to the live params.
_MAX_BRANCHING_PERSIST = 10.0


# ── Hawkes log-likelihood ─────────────────────────────────────────────────────

from sel_v2.hawkes.mle import hawkes_nll, fit_hawkes  # canonical implementation


# ── Rolling calibration ───────────────────────────────────────────────────────

def run_rolling_hawkes(
    event_flags: np.ndarray,
    bar_indices: np.ndarray,
    window: int = 540,
    step: int = 6,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Rolling Hawkes calibration over the event series.

    event_flags: boolean array, True = event at this bar
    bar_indices: integer indices (0..N-1)
    window: rolling window in bars (default 540 = 90 days × 6 bars/day)
    step: step between calibration points (default 6 = 1 day)

    Returns:
        window_ends: bar index of each calibration window end
        branching_ratios: α/β at each calibration point
    """
    n = len(event_flags)
    n_windows = (n - window) // step + 1

    window_ends = []
    branching_ratios = []

    for wi in range(n_windows):
        start = wi * step
        end = start + window
        seg_flags = event_flags[start:end]
        event_times = np.where(seg_flags)[0].astype(float)

        if wi % 10 == 0:
            logger.info("  Hawkes window %d/%d (end bar %d)", wi, n_windows, end)

        if len(event_times) < 5:
            window_ends.append(end - 1)
            branching_ratios.append(np.nan)
            continue

        T = float(window - 1)
        fit = fit_hawkes(event_times, T)
        window_ends.append(end - 1)
        branching_ratios.append(fit["branching_ratio"])

    return np.array(window_ends), np.array(branching_ratios)


# ── Main calibration ──────────────────────────────────────────────────────────

def run_hawkes_calibration(
    data_path: str = "analysis/data/btc_4h.parquet",
    event_sigma_threshold: float = 1.0,
    window: int = 540,
    step: int = 6,
    output_path: str = "analysis/hawkes_calibration_v1.md",
    persist: bool = True,
    db_url: str | None = None,
) -> dict:
    logger.info("Loading data from %s", data_path)
    df = pd.read_parquet(data_path).sort_values("time").reset_index(drop=True)
    logger.info("Loaded %d bars", len(df))

    close = df["close"].values.astype(float)
    times = df["time"].values
    returns = np.diff(np.log(close))
    times_r = times[1:]  # aligned with returns

    # Event definition: |return| > event_sigma_threshold * σ
    sigma = float(np.std(returns))
    threshold = event_sigma_threshold * sigma
    event_flags = np.abs(returns) > threshold
    n_events_total = int(event_flags.sum())
    event_rate = float(n_events_total / len(returns))

    logger.info(
        "Event definition: |return| > %.4f (%.1f sigma). Events: %d / %d bars (%.1f%%)",
        threshold, event_sigma_threshold, n_events_total, len(returns), event_rate * 100,
    )

    logger.info("Running rolling Hawkes calibration (window=%d bars, step=%d bars)...", window, step)
    window_ends, branching_ratios = run_rolling_hawkes(event_flags, np.arange(len(returns)), window, step)

    # Map window_ends bar indices back to timestamps
    window_times = times_r[np.minimum(window_ends, len(times_r) - 1)]

    # Filter NaN and clip optimizer artifacts (α/β > 10 = MLE divergence)
    MAX_BR = 10.0
    valid_br = branching_ratios[np.isfinite(branching_ratios)]
    valid_br = np.clip(valid_br, 0, MAX_BR)
    br_mean = float(np.mean(valid_br)) if len(valid_br) > 0 else np.nan
    br_std = float(np.std(valid_br)) if len(valid_br) > 0 else np.nan
    br_q50 = float(np.median(valid_br)) if len(valid_br) > 0 else np.nan
    br_q90 = float(np.quantile(valid_br, 0.90)) if len(valid_br) > 0 else np.nan
    br_q95 = float(np.quantile(valid_br, 0.95)) if len(valid_br) > 0 else np.nan
    br_frac_critical = float(np.mean(valid_br > CRITICAL_THRESHOLD)) if len(valid_br) > 0 else np.nan

    logger.info("Branching ratio: mean=%.3f std=%.3f q90=%.3f fraction_critical=%.1f%%",
                br_mean, br_std, br_q90, br_frac_critical * 100)

    # Evaluate on known Cascade and control events
    event_results = []
    for event_date_str, label, desc in KNOWN_EVENTS + CONTROL_EVENTS:
        event_dt = np.datetime64(event_date_str)
        diffs = np.abs(times_r.astype("datetime64[D]") - event_dt)
        bar_idx = int(np.argmin(diffs))

        if bar_idx < window:
            event_results.append({"date": event_date_str, "label": label, "in_range": False})
            continue

        # Find closest calibration window ending before this bar
        eligible = np.where(window_ends <= bar_idx)[0]
        if len(eligible) == 0:
            event_results.append({"date": event_date_str, "label": label, "in_range": False})
            continue

        win_idx = eligible[-1]
        br_val = branching_ratios[win_idx]

        event_results.append({
            "date": event_date_str,
            "label": label,
            "desc": desc,
            "in_range": True,
            "branching_ratio": br_val,
            "above_threshold": bool(np.isfinite(br_val) and br_val > CRITICAL_THRESHOLD),
        })

    # Overall fit on full dataset (for static threshold)
    full_event_times = np.where(event_flags)[0].astype(float)
    T_full = float(len(returns) - 1)
    logger.info("Fitting Hawkes on full dataset (%d events)...", len(full_event_times))
    full_fit = fit_hawkes(full_event_times, T_full, n_restarts=8)
    logger.info("Full fit: mu=%.5f alpha=%.5f beta=%.5f eta=%.4f converged=%s",
                full_fit.get("mu", np.nan), full_fit.get("alpha", np.nan),
                full_fit.get("beta", np.nan), full_fit.get("branching_ratio", np.nan),
                full_fit.get("converged", False))

    result = {
        "n_bars": len(df),
        "coverage_start": str(df["time"].iloc[0]),
        "coverage_end": str(df["time"].iloc[-1]),
        "event_sigma_threshold": event_sigma_threshold,
        "sigma": sigma,
        "event_threshold": threshold,
        "n_events": n_events_total,
        "event_rate": event_rate,
        "window": window,
        "step": step,
        "window_times": window_times,
        "branching_ratios": branching_ratios,
        "br_mean": br_mean,
        "br_std": br_std,
        "br_q50": br_q50,
        "br_q90": br_q90,
        "br_q95": br_q95,
        "br_frac_critical": br_frac_critical,
        "event_results": event_results,
        "full_fit": full_fit,
    }

    _write_report(result, output_path)

    if persist:
        _persist_to_db(full_fit, db_url)

    return result


def _persist_to_db(full_fit: dict, db_url: str | None) -> dict | None:
    """Write the H2 reference parameters into v2_strategy_params so the live
    strategies can load them. Without this, HawkesParams.from_h2_reference() raises
    and Strategy 2 silently disables itself (it caught the RuntimeError and ran S1
    only). The keys match what load_strategy_params('h2', [...]) expects:
    h2_mu_ref / h2_alpha_ref / h2_beta_ref / h2_branching_ratio_threshold.

    Returns the persisted params, or None on a degenerate fit / write failure
    (logged, not raised: the markdown report is still produced, and offline
    calibration must remain runnable in environments without Postgres)."""
    mu = full_fit.get("mu")
    alpha = full_fit.get("alpha")
    beta = full_fit.get("beta")
    if None in (mu, alpha, beta) or not all(np.isfinite(x) for x in (mu, alpha, beta)):
        logger.warning("Skipping DB persist: full-dataset fit did not converge "
                       "(mu=%s alpha=%s beta=%s)", mu, alpha, beta)
        return None
    # Reject degenerate fits: beta must be > 0 (else the H1 kernel never decays and
    # branching_ratio blows up), and the branching ratio must be in a sane range.
    # A diverged MLE (seen on too-few/noisy events) would otherwise poison the live
    # Strategy-2 intensity with beta≈0 / α/β≈inf.
    branching = alpha / beta if beta > 0 else float("inf")
    if beta <= 0 or not (0.0 < branching <= _MAX_BRANCHING_PERSIST):
        logger.warning("Skipping DB persist: degenerate Hawkes fit "
                       "(mu=%s alpha=%s beta=%s branching=%s)", mu, alpha, beta, branching)
        return None
    params = {
        "mu_ref": mu,
        "alpha_ref": alpha,
        "beta_ref": beta,
        "branching_ratio_threshold": CRITICAL_THRESHOLD,
    }
    try:
        from sel_v2.strategies.params_loader import save_strategy_params
        save_strategy_params(strategy="h2", params=params, db_url=db_url)
        logger.info("Persisted H2 reference params to v2_strategy_params "
                    "(mu=%.6f alpha=%.6f beta=%.6f threshold=%.2f)",
                    mu, alpha, beta, CRITICAL_THRESHOLD)
        return params
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to persist H2 params to v2_strategy_params (%s). "
                     "Strategy 2 will stay disabled until these rows exist.", exc)
        return None


def _write_report(result: dict, output_path: str) -> None:
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    br = result["branching_ratios"]
    full_fit = result["full_fit"]

    lines = [
        "# H2 Hawkes Branching Ratio Calibration",
        "",
        f"**Date**: {pd.Timestamp.now().date()}  ",
        "**Method**: Exponential Hawkes MLE → Branching ratio α/β  ",
        "**Event definition**: |4H log-return| > 1σ of historical returns  ",
        f"**Event threshold**: {result['event_threshold']:.5f} ({result['event_sigma_threshold']:.1f}σ)  ",
        f"**Data**: BTC-USDT 4H OHLCV (return-spike proxy for tick arrivals)  ",
        f"**Coverage**: {result['coverage_start']} → {result['coverage_end']}  ",
        f"**N bars**: {result['n_bars']}  ",
        f"**Events**: {result['n_events']} bars ({result['event_rate']*100:.1f}% of bars)  ",
        "",
        "> **Note**: Wave 1 uses OHLCV-proxy events. True tick-level Hawkes (Wave 3) "
        "requires LOB trade arrival data. Results here are directional only.",
        "",
        "## Full-Dataset Hawkes Fit",
        "",
        "| Parameter | Value |",
        "|---|---|",
        f"| μ (baseline) | {full_fit.get('mu', float('nan')):.6f} events/bar |",
        f"| α (excitation) | {full_fit.get('alpha', float('nan')):.6f} |",
        f"| β (decay) | {full_fit.get('beta', float('nan')):.6f} |",
        f"| **Branching ratio η = α/β** | **{full_fit.get('branching_ratio', float('nan')):.4f}** |",
        f"| Critical threshold (v2.1 §5.2) | {CRITICAL_THRESHOLD:.2f} |",
        f"| Above critical threshold | {'✅ YES' if full_fit.get('branching_ratio', 0) > CRITICAL_THRESHOLD else '❌ NO'} |",
        f"| Converged | {full_fit.get('converged', False)} |",
        "",
        "## Rolling Branching Ratio Summary (90-day windows, 1-day step)",
        "",
        "| Statistic | Value |",
        "|---|---|",
        f"| Mean α/β | {result['br_mean']:.4f} |",
        f"| Std α/β | {result['br_std']:.4f} |",
        f"| Median α/β | {result['br_q50']:.4f} |",
        f"| 90th percentile α/β | {result['br_q90']:.4f} |",
        f"| 95th percentile α/β | {result['br_q95']:.4f} |",
        f"| Fraction windows α/β > {CRITICAL_THRESHOLD} | {result['br_frac_critical']*100:.1f}% |",
        "",
        "**v2.1 spec**: H2 Critical state condition = α/β > 0.85 in rolling 90-day window.",
        "",
        "## Cascade Event Validation",
        "",
        "| Date | Label | α/β | Above 0.85 |",
        "|---|---|---|---|",
    ]

    for ev in result["event_results"]:
        if not ev.get("in_range"):
            lines.append(f"| {ev['date']} | {ev['label']} | N/A | N/A |")
        else:
            br_val = ev.get("branching_ratio", float("nan"))
            flag = "✅" if ev.get("above_threshold") else "❌"
            br_str = f"{br_val:.4f}" if np.isfinite(br_val) else "NaN"
            lines.append(f"| {ev['date']} | {ev['label']} | {br_str} | {flag} |")

    cascade_evs = [ev for ev in result["event_results"] if ev.get("label") == "Cascade" and ev.get("in_range")]
    control_evs = [ev for ev in result["event_results"] if ev.get("label") == "Normal" and ev.get("in_range")]
    cascade_tp = sum(1 for ev in cascade_evs if ev.get("above_threshold", False))
    control_fp = sum(1 for ev in control_evs if ev.get("above_threshold", False))

    lines += [
        "",
        f"**Cascade events above 0.85**: {cascade_tp}/{len(cascade_evs)} "
        f"({'N/A' if not cascade_evs else f'{cascade_tp/len(cascade_evs)*100:.0f}% sensitivity'})",
        f"**Control events above 0.85 (false positive rate)**: {control_fp}/{len(control_evs)} "
        f"({'N/A' if not control_evs else f'{control_fp/len(control_evs)*100:.0f}%'})",
        "",
        "## Initial Threshold Parameters (for paper trading launch)",
        "",
        "```python",
        "# H2 Hawkes Critical state condition (v2.1 §5.2)",
        f"HAWKES_BRANCHING_RATIO_THRESHOLD = {CRITICAL_THRESHOLD:.2f}  # α/β > this = Critical",
        f"HAWKES_WINDOW_BARS = {result['window']}  # 90 days × 6 bars/day rolling window",
        f"HAWKES_EVENT_SIGMA = {result['event_sigma_threshold']:.1f}  # event = |return| > N sigma",
        "",
        "# Full-dataset fit parameters (reference)",
        f"HAWKES_MU_REF = {full_fit.get('mu', float('nan')):.6f}",
        f"HAWKES_ALPHA_REF = {full_fit.get('alpha', float('nan')):.6f}",
        f"HAWKES_BETA_REF = {full_fit.get('beta', float('nan')):.6f}",
        f"HAWKES_ETA_REF = {full_fit.get('branching_ratio', float('nan')):.4f}  # α/β",
        "```",
        "",
        "## Rolling Branching Ratio Time Series (sample)",
        "",
        "| Date | α/β | Critical (>0.85) |",
        "|---|---|---|",
    ]

    step_sample = max(1, len(br) // 30)
    for i in range(0, len(br), step_sample):
        t_str = str(result["window_times"][i])[:10]
        br_val = br[i]
        if not np.isfinite(br_val):
            lines.append(f"| {t_str} | NaN | — |")
        elif br_val > 10.0:
            lines.append(f"| {t_str} | >10 (overflow) | ✅ |")
        else:
            crit = "✅" if br_val > CRITICAL_THRESHOLD else "—"
            lines.append(f"| {t_str} | {br_val:.4f} | {crit} |")

    lines += [
        "",
        "## Design Validation",
        "",
        _design_validation(cascade_tp, len(cascade_evs), control_fp, len(control_evs),
                           result["br_frac_critical"]),
        "",
        "## Limitations",
        "",
        "- **Proxy events**: Using |return| > 1σ as Hawkes event surrogate. "
        "True Hawkes requires trade-level LOB data (Wave 3).",
        "- **4H discretisation**: Hawkes is a continuous-time model. "
        "Discrete 4H bins underestimate inter-event timing.",
        "- **Cascade sample**: 4 events — too small for robust validation.",
        "- **Regime stationarity**: BTC structure changed post-ETF approval (Jan 2024). "
        "Full-dataset fit may blend different regimes.",
        "- **MLE sensitivity**: Hawkes NLL surface can be flat; Nelder-Mead with restarts "
        "may not converge to global optimum for all windows.",
        "",
        "---",
        "*Generated by sel_v2/offline/hawkes_calibration.py — Wave 1 deliverable*",
    ]

    with open(output_path, "w") as f:
        f.write("\n".join(lines))
    logger.info("Report written to %s", output_path)


def _design_validation(
    cascade_tp: int,
    n_cascade: int,
    control_fp: int,
    n_control: int,
    br_frac_critical: float,
) -> str:
    lines = []

    if n_cascade == 0:
        lines.append(
            "⚠️ **Cascade events**: No in-range Cascade events for validation. "
            "Cannot assess H2 sensitivity."
        )
    elif cascade_tp / n_cascade >= 0.75:
        lines.append(
            f"✅ **Cascade sensitivity**: {cascade_tp}/{n_cascade} Cascade events "
            f"triggered α/β > {CRITICAL_THRESHOLD}. H2 shows strong Cascade discrimination."
        )
    elif cascade_tp / n_cascade >= 0.5:
        lines.append(
            f"⚠️ **Moderate sensitivity**: {cascade_tp}/{n_cascade} Cascade events detected. "
            "Consider lower threshold or combining with TDA1."
        )
    else:
        lines.append(
            f"❌ **Low Cascade sensitivity**: {cascade_tp}/{n_cascade} events detected. "
            "H2 proxy (OHLCV-based) may not separate Cascade from Normal states well. "
            "Wave 3 tick-level Hawkes required for better performance."
        )

    if n_control > 0:
        if control_fp / n_control <= 0.25:
            lines.append(
                f"\n✅ **False positive rate**: {control_fp}/{n_control} control events "
                f"({control_fp/n_control*100:.0f}%) exceeded threshold."
            )
        else:
            lines.append(
                f"\n⚠️ **High false positive rate**: {control_fp}/{n_control} control events "
                f"({control_fp/n_control*100:.0f}%) exceeded threshold."
            )

    lines.append(
        f"\n**Baseline critical rate**: {br_frac_critical*100:.1f}% of rolling 90-day windows "
        f"have α/β > {CRITICAL_THRESHOLD}. "
        "In production, this rate should be elevated near Cascade events."
    )

    lines.append(
        "\n**Production recommendation per v2.1 §2.1**: H2 branching ratio is ONE of two "
        "new main conditions (alongside TDA1 L^1). Combined logic: "
        "(σ-based full + one of TDA/Hawkes) OR (σ-based partial + both TDA and Hawkes)."
    )

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="H2 Hawkes branching ratio calibration")
    parser.add_argument("--data", default="analysis/data/btc_4h.parquet")
    parser.add_argument("--sigma", type=float, default=1.0,
                        help="Event threshold in sigma units of returns")
    parser.add_argument("--window", type=int, default=540,
                        help="Rolling window in bars (default 540 = 90 days)")
    parser.add_argument("--step", type=int, default=6,
                        help="Step between calibration points (default 6 = 1 day)")
    parser.add_argument("--output", default="analysis/hawkes_calibration_v1.md")
    parser.add_argument("--no-persist", action="store_true",
                        help="Skip writing H2 reference params to v2_strategy_params")
    parser.add_argument("--db-url", default=None,
                        help="Postgres DSN override for param persistence")
    args = parser.parse_args()

    run_hawkes_calibration(
        data_path=args.data,
        event_sigma_threshold=args.sigma,
        window=args.window,
        step=args.step,
        output_path=args.output,
        persist=not args.no_persist,
        db_url=args.db_url,
    )


if __name__ == "__main__":
    main()
