"""
T1 Transfer Entropy causal map  (sel-language-v2.1-patches §6.1)

Implements Symbolic Transfer Entropy (STE) without external TE libraries.
STE is computed via ordinal-pattern encoding of time series, then
conditional entropy estimation from joint/marginal symbol distributions.

Reference: Schreiber 2000, Staniek & Lehnertz 2008 (symbolic TE).
Surrogate test: phase-randomisation with 100 surrogates, significance p < 0.05.

Usage:
    python -m sel_v2.offline.transfer_entropy [--data analysis/data/btc_4h.parquet]
"""
from __future__ import annotations

import argparse
import logging
import os
from itertools import permutations
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


# ── Ordinal encoding ──────────────────────────────────────────────────────────

def _ordinal_pattern(x: np.ndarray, d: int = 3) -> np.ndarray:
    """Encode x into ordinal patterns of order d (Permutation Entropy basis)."""
    n = len(x) - d + 1
    symbols = np.empty(n, dtype=np.int32)
    # Build lookup: permutation → integer rank
    perm_list = list(permutations(range(d)))
    perm_to_int = {p: i for i, p in enumerate(perm_list)}
    for i in range(n):
        w = x[i: i + d]
        rank = tuple(np.argsort(np.argsort(w)))
        symbols[i] = perm_to_int[rank]
    return symbols


# ── Entropy utilities ─────────────────────────────────────────────────────────

def _H(x: np.ndarray) -> float:
    """Shannon entropy in bits from integer symbol array."""
    counts = np.bincount(x)
    probs = counts[counts > 0] / len(x)
    return float(-np.sum(probs * np.log2(probs)))


def _joint_symbols(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Cantor-pair two integer arrays into a single joint symbol array."""
    n_x = int(x.max()) + 1
    return x * n_x + y


def symbolic_te(source: np.ndarray, target: np.ndarray, d: int = 3, lag: int = 1) -> float:
    """
    Symbolic Transfer Entropy from source → target (bits).

    STE(X→Y) = H(Y_t | Y_{t-1}^d) - H(Y_t | Y_{t-1}^d, X_{t-lag}^d)
             = H(Y_t, Y^d) + H(Y^d, X^d) - H(Y_t, Y^d, X^d) - H(Y^d)

    We use the conditional entropy form computed from joint distributions.
    """
    sx = _ordinal_pattern(source, d)
    sy = _ordinal_pattern(target, d)

    # Align: target needs past y symbols (starting at d-1) and past x (shifted by lag)
    n = min(len(sx), len(sy)) - lag
    if n <= 0:
        return 0.0

    # y_future: sy[d : d + n]  (y_t, the symbol to predict)
    # y_past:   sy[d - 1 : d - 1 + n]  (y_{t-1}'s pattern, simplified to 1-step)
    # x_past:   sx[d - 1 : d - 1 + n] shifted by lag

    # x_past must lag the target by `lag` steps. Previously this used sx[d-1:...]
    # regardless of lag (correct only at lag=1) — the lag was never applied (item #15).
    x_start = d - lag
    if x_start < 0:
        return 0.0   # lag exceeds the embedding offset; not enough history

    y_fut = sy[d: d + n]
    y_past = sy[d - 1: d - 1 + n]
    x_past = sx[x_start: x_start + n]

    if len(y_fut) != len(y_past) or len(y_fut) != len(x_past):
        min_len = min(len(y_fut), len(y_past), len(x_past))
        y_fut = y_fut[:min_len]
        y_past = y_past[:min_len]
        x_past = x_past[:min_len]

    # H(Y_t | Y_past) = H(Y_t, Y_past) - H(Y_past)
    jYtYp = _joint_symbols(y_fut, y_past)
    h_yt_yp = _H(jYtYp) - _H(y_past)

    # H(Y_t | Y_past, X_past)
    jYpXp = _joint_symbols(y_past, x_past)
    jAll = _joint_symbols(y_fut, jYpXp)
    h_yt_yp_xp = _H(jAll) - _H(jYpXp)

    return max(0.0, h_yt_yp - h_yt_yp_xp)


# ── Surrogate test ────────────────────────────────────────────────────────────

def phase_randomize(x: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Phase-randomise x preserving its power spectrum (null = no TE)."""
    n = len(x)
    ft = np.fft.rfft(x)
    phases = rng.uniform(0, 2 * np.pi, len(ft))
    ft_rand = np.abs(ft) * np.exp(1j * phases)
    return np.fft.irfft(ft_rand, n=n)


def te_pvalue(
    source: np.ndarray,
    target: np.ndarray,
    n_surrogates: int = 200,
    d: int = 3,
    lag: int = 1,
    seed: int = 42,
) -> tuple[float, float]:
    """
    Return (te_value, p_value) for source → target.
    p_value = fraction of surrogates with TE >= observed TE.
    """
    rng = np.random.default_rng(seed)
    te_obs = symbolic_te(source, target, d=d, lag=lag)
    surr_tes = [
        symbolic_te(phase_randomize(source, rng), target, d=d, lag=lag)
        for _ in range(n_surrogates)
    ]
    p_val = float(np.mean(np.array(surr_tes) >= te_obs))
    return te_obs, p_val


# ── Feature extraction from 4H bars ─────────────────────────────────────────

def build_signals(df: pd.DataFrame) -> dict[str, np.ndarray]:
    """
    Build signal arrays from raw 4H OHLCV.
    Uses price-derived signals only (data available from backfill).
    Derivative/OI signals would be added when real-time feeds are live.
    """
    close = df["close"].values.astype(float)
    volume = df["volume"].values.astype(float)
    n = len(close)

    # log returns
    ret = np.diff(np.log(close))

    # Rolling 24-bar (96h = 4 days) realised volatility
    window = 24
    rv = np.array([
        np.std(ret[max(0, i - window): i]) if i >= 2 else np.nan
        for i in range(1, n)
    ])

    # Price range (high-low normalised by close, proxy for liquidity stress)
    hl_range = ((df["high"] - df["low"]) / df["close"]).values[1:]

    # Volume change rate
    vol_ret = np.diff(np.log(np.maximum(volume, 1.0)))

    # Signed volume imbalance (rough OFI proxy: positive bar = buy pressure)
    bar_sign = np.sign(np.diff(close))
    vol_signed = bar_sign * volume[1:]

    # Cumulative sum of signed volume (trend proxy)
    cvoi = np.cumsum(vol_signed)

    # Align to same length
    min_len = min(len(ret), len(rv), len(hl_range), len(vol_ret), len(vol_signed), len(cvoi))
    signals = {
        "price_return": ret[:min_len],
        "realised_vol": rv[:min_len],
        "hl_range": hl_range[:min_len],
        "volume_change": vol_ret[:min_len],
        "signed_vol_flow": vol_signed[:min_len],
        "cum_vol_imbalance": cvoi[:min_len],
    }
    # Drop NaN rows
    mask = np.all([~np.isnan(v) for v in signals.values()], axis=0)
    return {k: v[mask] for k, v in signals.items()}


# ── Main analysis ─────────────────────────────────────────────────────────────

def run_causal_map(
    data_path: str = "analysis/data/btc_4h.parquet",
    n_surrogates: int = 200,
    d: int = 3,
    lag: int = 1,
    alpha: float = 0.05,
    output_path: str = "analysis/causal_map_v1.md",
) -> dict:
    """Run full T1 Transfer Entropy causal map analysis."""
    logger.info("Loading data from %s", data_path)
    df = pd.read_parquet(data_path).sort_values("time").reset_index(drop=True)
    logger.info("Loaded %d bars: %s → %s", len(df), df["time"].iloc[0], df["time"].iloc[-1])

    signals = build_signals(df)
    names = list(signals.keys())
    n_sig = len(names)
    logger.info("Running STE on %d signals × %d surrogates each pair...", n_sig, n_surrogates)

    # TE matrix and p-values
    te_matrix = np.zeros((n_sig, n_sig))
    pval_matrix = np.ones((n_sig, n_sig))

    pair_count = n_sig * (n_sig - 1)
    done = 0
    for i, src_name in enumerate(names):
        for j, tgt_name in enumerate(names):
            if i == j:
                continue
            te_val, p_val = te_pvalue(
                signals[src_name], signals[tgt_name],
                n_surrogates=n_surrogates, d=d, lag=lag,
            )
            te_matrix[i, j] = te_val
            pval_matrix[i, j] = p_val
            done += 1
            if done % 5 == 0:
                logger.info("  %d/%d pairs done", done, pair_count)

    # Significant edges
    significant = [
        (names[i], names[j], te_matrix[i, j], pval_matrix[i, j])
        for i in range(n_sig)
        for j in range(n_sig)
        if i != j and pval_matrix[i, j] < alpha
    ]
    significant.sort(key=lambda x: -x[2])

    result = {
        "signal_names": names,
        "te_matrix": te_matrix,
        "pval_matrix": pval_matrix,
        "significant_edges": significant,
        "n_bars": len(df),
        "coverage_start": str(df["time"].iloc[0]),
        "coverage_end": str(df["time"].iloc[-1]),
    }

    _write_report(result, alpha, d, lag, n_surrogates, output_path)
    return result


def _write_report(
    result: dict,
    alpha: float,
    d: int,
    lag: int,
    n_surrogates: int,
    output_path: str,
) -> None:
    names = result["signal_names"]
    te_matrix = result["te_matrix"]
    pval_matrix = result["pval_matrix"]
    significant = result["significant_edges"]

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    lines = [
        "# T1 Transfer Entropy Causal Map",
        "",
        f"**Date**: {pd.Timestamp.now().date()}  ",
        f"**Method**: Symbolic Transfer Entropy (STE), ordinal order d={d}, lag={lag}  ",
        f"**Significance**: surrogate test p < {alpha}, n_surrogates={n_surrogates}  ",
        f"**Data**: BTC-USDT 4H, {result['n_bars']} bars  ",
        f"**Coverage**: {result['coverage_start']} → {result['coverage_end']}  ",
        "",
        "## Signals Used",
        "",
        "Signals extracted from OHLCV only (no real-time feeds yet):",
        "",
        "| Signal | Description |",
        "|---|---|",
        "| price_return | Log return per 4H bar |",
        "| realised_vol | Rolling 24-bar realised volatility |",
        "| hl_range | (High-Low)/Close — liquidity stress proxy |",
        "| volume_change | Log-change in bar volume |",
        "| signed_vol_flow | Signed volume (+ = bullish bar) |",
        "| cum_vol_imbalance | Cumulative signed volume (CVOI / OFI proxy) |",
        "",
        "⚠️ **Data note**: Full v2.0 signal set (OFI, OI, funding, on-chain, cross-exchange spread)",
        "requires real-time collectors. This run uses OHLCV-derived proxies only.",
        "",
        "## STE Matrix (bits)",
        "",
        "Rows = source, Columns = target. Values are STE (bits).",
        "",
    ]

    # Header row
    header = "| → | " + " | ".join(names) + " |"
    sep = "|---|" + "|".join(["---"] * len(names)) + "|"
    lines += [header, sep]
    for i, src in enumerate(names):
        row_vals = []
        for j in range(len(names)):
            if i == j:
                row_vals.append("—")
            else:
                stars = "*" if pval_matrix[i, j] < alpha else ""
                row_vals.append(f"{te_matrix[i, j]:.4f}{stars}")
        lines.append(f"| **{src}** | " + " | ".join(row_vals) + " |")

    lines += [
        "",
        f"\\* = p < {alpha} (significant after surrogate test)",
        "",
        "## Significant Causal Edges (p < {alpha})".format(alpha=alpha),
        "",
        "| Source | Target | STE (bits) | p-value | Interpretation |",
        "|---|---|---|---|---|",
    ]

    for src, tgt, te_val, p_val in significant[:20]:
        interp = _interpret_edge(src, tgt)
        lines.append(f"| {src} | {tgt} | {te_val:.4f} | {p_val:.3f} | {interp} |")

    if not significant:
        lines.append("| — | — | — | — | No significant edges found |")

    lines += [
        "",
        "## Causal Map (Directed Graph Summary)",
        "",
        "```",
        "Significant edges (→ = significant causal direction):",
        "",
    ]
    for src, tgt, te_val, p_val in significant[:15]:
        lines.append(f"  {src} → {tgt}  (STE={te_val:.4f}, p={p_val:.3f})")
    if not significant:
        lines.append("  (none)")
    lines += ["```", ""]

    lines += [
        "## sel v2.0 Design Validation",
        "",
        _validate_design(significant),
        "",
        "## Limitations & Next Steps",
        "",
        "- **Signal set incomplete**: OFI, OI, funding, on-chain, cross-exchange spread missing.",
        "  Re-run after 30+ days of real-time collector data.",
        "- **4H lag choice**: lag=1 (4H) used per v2.0 design. Sensitivity to lag=2,3 not explored.",
        "- **Sample size**: ~5000 bars (2 years). STE estimation is borderline; prefer 6000+.",
        "- **Regime non-stationarity**: post-ETF (2024+) may differ from prior periods.",
        "  Consider regime-conditional TE (T2 rolling monitor) after paper launch.",
        "",
        "---",
        f"*Generated by sel_v2/offline/transfer_entropy.py — Wave 1 deliverable*",
    ]

    with open(output_path, "w") as f:
        f.write("\n".join(lines))
    logger.info("Report written to %s", output_path)


def _interpret_edge(src: str, tgt: str) -> str:
    mapping = {
        ("price_return", "realised_vol"): "Returns drive vol (ARCH effect)",
        ("realised_vol", "price_return"): "Vol feedback on returns",
        ("signed_vol_flow", "price_return"): "OFI proxy leads price (expected: strong)",
        ("price_return", "signed_vol_flow"): "Price return drives flow (execution impact)",
        ("cum_vol_imbalance", "price_return"): "CVOI pressure leads price",
        ("volume_change", "realised_vol"): "Volume shock → volatility",
        ("hl_range", "realised_vol"): "Range measure → vol (by construction, weak causal)",
        ("signed_vol_flow", "cum_vol_imbalance"): "Flow accumulates (trivially)",
    }
    return mapping.get((src, tgt), "—")


def _validate_design(significant: list) -> str:
    """Check if significant edges align with sel v2.0 design assumptions."""
    sig_set = {(s, t) for s, t, _, _ in significant}
    validations = []

    ofi_leads = ("signed_vol_flow", "price_return") in sig_set
    price_drives_vol = ("price_return", "realised_vol") in sig_set
    vol_feedback = ("realised_vol", "price_return") in sig_set

    if ofi_leads:
        validations.append(
            "✅ **OFI proxy → price**: Confirmed. sel v2.0 assumption that OFI leads price "
            "is supported by STE evidence."
        )
    else:
        validations.append(
            "⚠️ **OFI proxy → price**: Not significant. Either OFI proxy is too coarse "
            "(OHLCV volume is not true OFI), or the relationship is regime-dependent. "
            "Re-validate with real LOB OFI data from collectors."
        )

    if price_drives_vol:
        validations.append(
            "✅ **Returns → Vol**: ARCH-type feedback confirmed. Critical state σ-based "
            "conditions have information-theoretic basis."
        )

    if vol_feedback:
        validations.append(
            "✅ **Vol → Returns**: Volatility feedback on returns present. Consistent with "
            "v2.0 σ as both state-defining and decision-filtering signal."
        )

    if not validations:
        validations.append(
            "⚠️ No significant edges found. Possible causes: (1) surrogate threshold too strict, "
            "(2) OHLCV-only signals are too noisy, (3) 4H aggregation loses microstructure signal."
        )

    return "\n\n".join(validations)


def main() -> None:
    parser = argparse.ArgumentParser(description="T1 Transfer Entropy causal map")
    parser.add_argument("--data", default="analysis/data/btc_4h.parquet")
    parser.add_argument("--surrogates", type=int, default=200)
    parser.add_argument("--d", type=int, default=3,
                        help="Ordinal pattern order")
    parser.add_argument("--lag", type=int, default=1)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--output", default="analysis/causal_map_v1.md")
    args = parser.parse_args()

    run_causal_map(
        data_path=args.data,
        n_surrogates=args.surrogates,
        d=args.d,
        lag=args.lag,
        alpha=args.alpha,
        output_path=args.output,
    )


if __name__ == "__main__":
    main()
