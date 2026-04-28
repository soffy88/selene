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
    System approaching phase transition (CSD). doc §4.5.

    Cond1: lag-1 autocorr rising — autocorr_12h > autocorr_24h > autocorr_48h
    Cond2: σ(P) acceleration: sigma_p_d2 > 0 AND sigma_p_d2 > 80th pct
    Cond3: H change rate std_12h > 80th pct  [WIKI_REQUIRED; skip if None]
    Cond4: recovery time proxy — OI_hurst > 70th pct  [skip if None]
    Trigger: Cond1 AND Cond2 AND (Cond3 OR Cond4)
    """
    # Cond1: autocorr monotone rising (CSD core signal per doc §4.5)
    ac12 = fv.price_autocorr_12h
    ac24 = fv.price_autocorr_24h
    ac48 = fv.price_autocorr_48h
    if ac12 is None or ac24 is None or ac48 is None:
        return False, "", {}
    if not (ac12 > ac24 > ac48):
        return False, "", {}

    # Cond2: σ(P) second derivative > 0 AND > 80th pct
    sigma_d2_qr = qr.get("sigma_p_d2")
    if sigma_d2_qr is None or sigma_d2_qr < 0.80:
        return False, "", {}
    if fv.sigma_p_d2 is None or fv.sigma_p_d2 <= 0:
        return False, "", {}

    # Cond3: H erraticity > 80th pct (skip if H not running)
    h_cr_std_qr = qr.get("H_change_rate_std_12h")
    cond3 = h_cr_std_qr is not None and h_cr_std_qr >= 0.80

    # Cond4: OI persistence proxy (recovery time lengthening)
    oi_hurst_qr = qr.get("OI_hurst")
    cond4 = oi_hurst_qr is not None and oi_hurst_qr >= 0.70

    if not (cond3 or cond4):
        return False, "", {}

    used: dict = {
        "price_autocorr_12h": ac12,
        "price_autocorr_24h": ac24,
        "price_autocorr_48h": ac48,
        "sigma_p_d2": sigma_d2_qr,
    }
    parts = [
        f"autocorr({ac12:.3f}>{ac24:.3f}>{ac48:.3f})",
        f"sigma_d2@{sigma_d2_qr:.2f}",
    ]
    if cond3 and h_cr_std_qr is not None:
        used["H_change_rate_std_12h"] = h_cr_std_qr
        parts.append(f"H_cr_std@{h_cr_std_qr:.2f}")
    if cond4 and oi_hurst_qr is not None:
        used["OI_hurst"] = oi_hurst_qr
        parts.append(f"OI_hurst@{oi_hurst_qr:.2f}")

    reason = "CRITICAL:" + "+".join(parts)
    return True, reason, used


def check_coiling(fv: FeatureVector, qr: dict) -> tuple[bool, str, dict]:
    """
    Market winding up energy. doc §4.1.

    Cond1: σ(P)_24h < 30th pct  (low volatility)
    Cond2: H < 30th pct  (low entropy = concentrated orderbook)  [skip if None; WIKI_REQUIRED]
    Cond3: oi_change_rate_24h > 70th pct  (OI accumulating fast)  [short-circuit if None]
    Cond4: tf_dp_ratio_24h > 80th pct  (high absorption ratio)   [short-circuit if None]
    All 4 required; Cond2 relaxed when H unavailable; Cond3/4 require collector data.
    """
    # Cond1: low volatility
    sigma_qr = qr.get("sigma_p_24h")
    if sigma_qr is None or sigma_qr > 0.30:
        return False, "", {}

    # Cond2: low orderbook entropy (high H → disordered → NOT coiling)
    h_qr = qr.get("H")
    if h_qr is not None and h_qr > 0.30:
        return False, "", {}

    # Cond3: OI accumulating — short-circuit when OI collector data absent
    oi_cr_qr = qr.get("oi_change_rate_24h")
    if oi_cr_qr is None or oi_cr_qr < 0.70:
        return False, "", {}

    # Cond4: high absorption (|TF| / |ΔP| ratio) — short-circuit when TF absent
    tf_dp_qr = qr.get("tf_dp_ratio_24h")
    if tf_dp_qr is None or tf_dp_qr < 0.80:
        return False, "", {}

    used: dict = {
        "sigma_p_24h": sigma_qr,
        "oi_change_rate_24h": oi_cr_qr,
        "tf_dp_ratio_24h": tf_dp_qr,
    }
    parts = [
        f"sigma_p@{sigma_qr:.2f}",
        f"oi_cr@{oi_cr_qr:.2f}",
        f"tf_dp@{tf_dp_qr:.2f}",
    ]
    if h_qr is not None:
        used["H"] = h_qr
        parts.append(f"H@{h_qr:.2f}")

    reason = "COILING:" + "+".join(parts)
    return True, reason, used


def check_surging(fv: FeatureVector, qr: dict) -> tuple[bool, str, dict]:
    """
    Directional price movement with momentum. doc §4.2.

    Cond1: price_slope_6h > 80th pct  (strong 6H linear price trend)
    Cond2: |tf_directional_ratio_6h| > 70%  (TF is directionally aligned)  [short-circuit if None]
    Cond3: σ(P) rising over 12H  [skip sub-check when history unavailable]
           AND std(Δσ) over 6H < 50th pct  [skip sub-check when unavailable]
    Direction: sign(tf_directional_ratio_6h).
    Surging is NOT triggered when TF data is absent (Cond2 is hard requirement).
    Sigma rising lag = 12H; default pending empirical tuning.
    """
    # Cond1: 6H price regression slope above 80th pct
    slope_qr = qr.get("price_slope_6h")
    if slope_qr is None or slope_qr < 0.80:
        return False, "", {}

    # Cond2: TF directionally aligned — hard short-circuit when TF absent
    tf_dir = fv.tf_directional_ratio_6h
    if tf_dir is None or abs(tf_dir) <= 0.70:
        return False, "", {}

    direction = "UP" if tf_dir > 0 else "DOWN"

    # Cond3a: σ rising over 12H (skip if history insufficient)
    sigma_rising = fv.sigma_rising_12h
    if sigma_rising is not None and not sigma_rising:
        return False, "", {}

    # Cond3b: σ change rate stable (skip if unavailable)
    sigma_cr_std_qr = qr.get("sigma_change_rate_std_6h")
    if sigma_cr_std_qr is not None and sigma_cr_std_qr >= 0.50:
        return False, "", {}

    used: dict = {
        "price_slope_6h": slope_qr,
        "tf_directional_ratio_6h": abs(tf_dir),
    }
    parts = [
        f"slope_6h@{slope_qr:.2f}",
        f"tf_dir@{abs(tf_dir):.2f}",
    ]
    if sigma_rising is not None:
        used["sigma_rising_12h"] = float(sigma_rising)
        parts.append(f"sigma_rising={sigma_rising}")
    if sigma_cr_std_qr is not None:
        used["sigma_change_rate_std_6h"] = sigma_cr_std_qr
        parts.append(f"sigma_cr_std@{sigma_cr_std_qr:.2f}")

    reason = f"SURGING_{direction}:" + "+".join(parts)
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
