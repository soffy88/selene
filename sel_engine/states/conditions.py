"""
State condition check functions for the sel state recognition layer.

Each function receives a FeatureVector and a pre-computed quantile rank dict,
and returns (matched: bool, reason: str, used_quantiles: dict).

All threshold values are marked PLACEHOLDER — calibrate with v1.0.md when available.
"""
from typing import Optional
from sel_engine.features.schema import FeatureVector


def check_cascade(fv: FeatureVector, qr: dict) -> tuple[bool, str, dict]:
    """
    Market regime collapse. doc §4.6.

    Cond1 (primary gate): |ΔP|/P > 97th pct  [abs_delta_p_pct]
    Cond2: volume > 95th pct — volume not in FeatureVector; always False
    Cond3: LV > 95th pct
    Cond4: |ΔH| > 95th pct  [delta_H = |H_current - H_previous|]
    Trigger: Cond1 AND (Cond3 OR Cond4)   (Cond2 permanently short-circuited)
    """
    abs_dp_qr = qr.get("abs_delta_p_pct")
    if abs_dp_qr is None or abs_dp_qr < 0.97:
        return False, "", {}

    secondary = []

    # Cond3: LV > 95th pct
    lv_qr = qr.get("LV")
    if lv_qr is not None and lv_qr >= 0.95:
        secondary.append(f"LV@{lv_qr:.2f}")

    # Cond4: |ΔH| > 95th pct (volume/Cond2 not implemented)
    delta_h_qr = qr.get("delta_H")
    if delta_h_qr is not None and delta_h_qr >= 0.95:
        secondary.append(f"|dH|@{delta_h_qr:.2f}")

    if not secondary:
        return False, "", {}

    used: dict = {"abs_delta_p_pct": abs_dp_qr}
    if lv_qr is not None:
        used["LV"] = lv_qr
    if delta_h_qr is not None:
        used["delta_H"] = delta_h_qr
    reason = f"CASCADE:|delta_p|@{abs_dp_qr:.2f}+" + "+".join(secondary)
    return True, reason, used


def check_critical(fv: FeatureVector, qr: dict) -> tuple[bool, str, dict]:
    """
    System approaching phase transition (physics of critical state diagrams).

    Condition 1: σ(P)_d2 quantile rank ≥ 0.80        # PLACEHOLDER — calibrate with v1.0.md when available
    Condition 2: OI_hurst quantile rank ≥ 0.70        # PLACEHOLDER — calibrate with v1.0.md when available
                 (skipped if OI_hurst is None)
    Condition 3: LV quantile rank ≥ 0.70              # PLACEHOLDER — calibrate with v1.0.md when available
                 OR price_autocorr_24h quantile rank ≤ 0.20  # PLACEHOLDER — calibrate with v1.0.md when available

    If OI_hurst unavailable, relax to: cond1 + cond3.
    """
    sigma_d2_qr = qr.get("sigma_p_d2")
    if sigma_d2_qr is None or sigma_d2_qr < 0.80:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    oi_hurst_qr = qr.get("OI_hurst")
    lv_qr = qr.get("LV")
    autocorr_24h_qr = qr.get("price_autocorr_24h")

    # Condition 3: liquidity deteriorating OR autocorr breakdown
    cond3_lv = lv_qr is not None and lv_qr >= 0.70          # PLACEHOLDER — calibrate with v1.0.md when available
    cond3_ac = autocorr_24h_qr is not None and autocorr_24h_qr <= 0.20  # PLACEHOLDER — calibrate with v1.0.md when available
    cond3 = cond3_lv or cond3_ac

    # No sub-condition available at all for cond3 — cannot evaluate
    if lv_qr is None and autocorr_24h_qr is None:
        return False, "", {}

    if not cond3:
        return False, "", {}

    # OI_hurst available → require it; unavailable → relax (cond1 + cond3 sufficient)
    if oi_hurst_qr is not None and oi_hurst_qr < 0.70:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    used: dict = {"sigma_p_d2": sigma_d2_qr}
    parts = [f"sigma_p_d2@{sigma_d2_qr:.2f}"]

    if oi_hurst_qr is not None:
        used["OI_hurst"] = oi_hurst_qr
        parts.append(f"OI_hurst@{oi_hurst_qr:.2f}")

    if cond3_lv and lv_qr is not None:
        used["LV"] = lv_qr
        parts.append(f"LV@{lv_qr:.2f}")
    if cond3_ac and autocorr_24h_qr is not None:
        used["price_autocorr_24h"] = autocorr_24h_qr
        parts.append(f"autocorr_24h@{autocorr_24h_qr:.2f}")

    reason = "CRITICAL:" + "+".join(parts)
    return True, reason, used


def check_coiling(fv: FeatureVector, qr: dict) -> tuple[bool, str, dict]:
    """
    Market winding up energy (low vol + high entropy + trending OI + positive autocorr).

    σ(P)_24h quantile rank ≤ 0.30           # PLACEHOLDER — calibrate with v1.0.md when available
    H quantile rank ≥ 0.70                  # PLACEHOLDER — calibrate with v1.0.md when available (skipped if None)
    price_autocorr_24h quantile rank ≥ 0.60 # PLACEHOLDER — calibrate with v1.0.md when available
    OI quantile rank ≥ 0.50                 # PLACEHOLDER — calibrate with v1.0.md when available
    """
    sigma_qr = qr.get("sigma_p_24h")
    if sigma_qr is None or sigma_qr > 0.30:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    autocorr_qr = qr.get("price_autocorr_24h")
    if autocorr_qr is None or autocorr_qr < 0.60:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    oi_qr = qr.get("OI")
    if oi_qr is None or oi_qr < 0.50:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    h_qr = qr.get("H")
    # H is WIKI_REQUIRED — skip if unavailable
    if h_qr is not None and h_qr < 0.70:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    used: dict = {
        "sigma_p_24h": sigma_qr,
        "price_autocorr_24h": autocorr_qr,
        "OI": oi_qr,
    }
    parts = [
        f"sigma_p@{sigma_qr:.2f}",
        f"autocorr_24h@{autocorr_qr:.2f}",
        f"OI@{oi_qr:.2f}",
    ]
    if h_qr is not None:
        used["H"] = h_qr
        parts.append(f"H@{h_qr:.2f}")

    reason = "COILING:" + "+".join(parts)
    return True, reason, used


def check_surging(fv: FeatureVector, qr: dict) -> tuple[bool, str, dict]:
    """
    Directional price movement with momentum.

    |ΔP/P| quantile rank ≥ 0.70            # PLACEHOLDER — calibrate with v1.0.md when available
    σ(P)_24h quantile rank ≥ 0.60          # PLACEHOLDER — calibrate with v1.0.md when available
    price_autocorr_12h quantile rank ≥ 0.60 # PLACEHOLDER — calibrate with v1.0.md when available

    Returns SURGING_UP if ΔP/P > 0, SURGING_DOWN if ΔP/P < 0.
    Direction determined from raw FeatureVector value (not quantile).
    """
    abs_dp_qr = qr.get("abs_delta_p_pct")
    if abs_dp_qr is None or abs_dp_qr < 0.70:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    sigma_qr = qr.get("sigma_p_24h")
    if sigma_qr is None or sigma_qr < 0.60:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    autocorr_12h_qr = qr.get("price_autocorr_12h")
    if autocorr_12h_qr is None or autocorr_12h_qr < 0.60:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    # Direction from the raw delta_p_pct value (positive = up, negative = down)
    # When delta_p_pct is 0 or None, default to Surging_Up (edge case)
    direction = "UP"
    if fv.delta_p_pct is not None and fv.delta_p_pct < 0:
        direction = "DOWN"

    used = {
        "abs_delta_p_pct": abs_dp_qr,
        "sigma_p_24h": sigma_qr,
        "price_autocorr_12h": autocorr_12h_qr,
    }
    reason = (
        f"SURGING_{direction}:|delta_p|@{abs_dp_qr:.2f}"
        f"+sigma_p@{sigma_qr:.2f}+autocorr_12h@{autocorr_12h_qr:.2f}"
    )
    return True, reason, used


def check_drifting_charged(fv: FeatureVector, qr: dict) -> tuple[bool, str, dict]:
    """
    Quiet price, elevated derivatives positioning.

    σ(P)_24h quantile rank ≤ 0.50           # PLACEHOLDER — calibrate with v1.0.md when available
    OI quantile rank ≥ 0.70                 # PLACEHOLDER — calibrate with v1.0.md when available (skipped if None)
    |funding_rate| quantile rank ≥ 0.60     # PLACEHOLDER — calibrate with v1.0.md when available (skipped if None)
    """
    sigma_qr = qr.get("sigma_p_24h")
    if sigma_qr is None or sigma_qr > 0.50:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    oi_qr = qr.get("OI")
    funding_qr = qr.get("abs_funding_rate")

    # OI condition (skipped if unavailable)
    if oi_qr is not None and oi_qr < 0.70:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    # Funding condition (skipped if unavailable)
    if funding_qr is not None and funding_qr < 0.60:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    # Need at least one derivative condition to have triggered (even if relaxed)
    # Both unavailable → can't confirm "charged" positioning
    if oi_qr is None and funding_qr is None:
        return False, "", {}

    used: dict = {"sigma_p_24h": sigma_qr}
    parts = [f"sigma_p@{sigma_qr:.2f}"]

    if oi_qr is not None:
        used["OI"] = oi_qr
        parts.append(f"OI@{oi_qr:.2f}")
    if funding_qr is not None:
        used["abs_funding_rate"] = funding_qr
        parts.append(f"|funding|@{funding_qr:.2f}")

    reason = "DRIFTING_CHARGED:" + "+".join(parts)
    return True, reason, used


def check_drifting_calm(fv: FeatureVector, qr: dict) -> tuple[bool, str, dict]:
    """
    Catch-all quiet state.

    σ(P)_24h quantile rank ≤ 0.50           # PLACEHOLDER — calibrate with v1.0.md when available
    Falls through when all higher-priority states fail.
    If σ(P)_24h is None, still triggers as the final fallback.
    """
    sigma_qr = qr.get("sigma_p_24h")

    if sigma_qr is not None and sigma_qr > 0.50:  # PLACEHOLDER — calibrate with v1.0.md when available
        return False, "", {}

    used: dict = {}
    if sigma_qr is not None:
        used["sigma_p_24h"] = sigma_qr
        reason = f"DRIFTING_CALM:sigma_p@{sigma_qr:.2f}"
    else:
        reason = "DRIFTING_CALM:fallback(sigma_p_unavailable)"

    return True, reason, used
