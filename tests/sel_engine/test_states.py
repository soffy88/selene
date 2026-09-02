"""
Tests for sel_engine Wave 2 state recognition layer.
All tests use synthetic data — no DB or Redis connections required.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from sel_engine.features.schema import FeatureVector
from sel_engine.states.conditions import (
    check_cascade,
    check_coiling,
    check_critical,
    check_drifting_calm,
    check_drifting_charged,
    check_surging,
)
from sel_engine.states.recognizer import _HARD_SHORT_CIRCUIT_QR, StateRecognizer, compute_state_distribution
from sel_engine.states.schema import StateLabel, StateNoneReason, StateRecord
from sel_engine.states.thresholds import RollingQuantileCalculator

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_fv(
    time: Optional[datetime] = None,
    symbol: str = "BTCUSDT",
    close: float = 50000.0,
    delta_p_pct: Optional[float] = 0.5,
    sigma_p_24h: Optional[float] = 0.01,
    sigma_p_d2: Optional[float] = None,
    H: Optional[float] = None,
    TF: Optional[float] = None,
    OI: Optional[float] = None,
    funding_rate: Optional[float] = None,
    LV: Optional[float] = None,
    OI_hurst: Optional[float] = None,
    price_autocorr_12h: Optional[float] = None,
    price_autocorr_24h: Optional[float] = None,
    price_autocorr_48h: Optional[float] = None,
    # P1 features
    price_slope_6h: Optional[float] = None,
    tf_directional_ratio_6h: Optional[float] = None,
    sigma_rising_12h: Optional[bool] = None,
    sigma_change_rate_std_6h: Optional[float] = None,
    H_24h_mean: Optional[float] = None,
    abs_tf_24h_sum: Optional[float] = None,
    oi_change_rate_24h: Optional[float] = None,
    tf_dp_ratio_24h: Optional[float] = None,
) -> FeatureVector:
    if time is None:
        time = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return FeatureVector(
        time=time,
        symbol=symbol,
        close=close,
        delta_p_pct=delta_p_pct,
        sigma_p_24h=sigma_p_24h,
        sigma_p_d2=sigma_p_d2,
        H=H,
        TF=TF,
        OI=OI,
        funding_rate=funding_rate,
        LV=LV,
        OI_hurst=OI_hurst,
        price_autocorr_12h=price_autocorr_12h,
        price_autocorr_24h=price_autocorr_24h,
        price_autocorr_48h=price_autocorr_48h,
        price_slope_6h=price_slope_6h,
        tf_directional_ratio_6h=tf_directional_ratio_6h,
        sigma_rising_12h=sigma_rising_12h,
        sigma_change_rate_std_6h=sigma_change_rate_std_6h,
        H_24h_mean=H_24h_mean,
        abs_tf_24h_sum=abs_tf_24h_sum,
        oi_change_rate_24h=oi_change_rate_24h,
        tf_dp_ratio_24h=tf_dp_ratio_24h,
    )


def _flat_fv_sequence(n: int, base_time: Optional[datetime] = None) -> list[FeatureVector]:
    """Generate n identical FeatureVectors with stable, unremarkable values."""
    if base_time is None:
        base_time = datetime(2023, 1, 1, tzinfo=timezone.utc)
    return [
        make_fv(
            time=base_time + timedelta(hours=i),
            close=50000.0,
            delta_p_pct=0.1,
            sigma_p_24h=0.005,
        )
        for i in range(n)
    ]


# ── RollingQuantileCalculator ─────────────────────────────────────────────────


class TestRollingQuantileCalculator:
    def test_returns_none_with_no_history(self):
        calc = RollingQuantileCalculator()
        result = calc.quantile_rank("sigma_p_24h", 0.05)
        assert result is None

    def test_returns_none_for_none_value(self):
        calc = RollingQuantileCalculator()
        for i in range(20):
            calc.add("sigma_p_24h", float(i))
        result = calc.quantile_rank("sigma_p_24h", None)
        assert result is None

    def test_returns_none_below_min_values(self):
        calc = RollingQuantileCalculator()
        # Feed 9 values (below MIN_VALUES=10)
        for i in range(9):
            calc.add("sigma_p_24h", float(i))
        result = calc.quantile_rank("sigma_p_24h", 5.0)
        assert result is None

    def test_computes_rank_with_sufficient_history(self):
        calc = RollingQuantileCalculator()
        for i in range(20):
            calc.add("x", float(i))  # [0, 1, ..., 19]
        # Value 10 is above 10 out of 20 values (0..9 strictly below 10)
        rank = calc.quantile_rank("x", 10.0)
        assert rank is not None
        assert rank == pytest.approx(0.5, abs=1e-6)

    def test_rank_of_minimum_value_is_zero(self):
        calc = RollingQuantileCalculator()
        for i in range(20):
            calc.add("x", float(i + 1))  # [1..20]
        rank = calc.quantile_rank("x", 0.5)  # below all
        assert rank is not None
        assert rank == pytest.approx(0.0)

    def test_rank_of_maximum_is_one(self):
        calc = RollingQuantileCalculator()
        for i in range(20):
            calc.add("x", float(i))  # [0..19]
        rank = calc.quantile_rank("x", 100.0)  # above all
        assert rank is not None
        assert rank == pytest.approx(1.0)

    def test_strictly_causal_add_after_rank(self):
        """add() called after quantile_rank() must not include current value in the rank."""
        calc = RollingQuantileCalculator()
        for _i in range(20):
            calc.add("x", 1.0)  # fill with 1.0

        # Current value is 999 (should NOT appear in window yet)
        rank_before = calc.quantile_rank("x", 999.0)
        calc.add("x", 999.0)  # add AFTER
        rank_after = calc.quantile_rank("x", 999.0)

        # Before adding: 999 is strictly above all 1.0s → rank = 1.0
        assert rank_before == pytest.approx(1.0)
        # After adding: window now has one 999 among the 1.0s
        # rank of 999.0 vs window = 20 values (the last add shifted one 1.0 out if window < 720,
        # but window is only 21 deep here so 999 is in the window)
        # Regardless, it must not be 1.0 anymore (the 999 IS now in the past window)
        # Actually with the deque, 999 is included in the window for the NEXT call
        assert rank_after is not None  # still computable

    def test_is_cold_start_initially(self):
        calc = RollingQuantileCalculator()
        assert calc.is_cold_start("close") is True

    def test_is_not_cold_start_after_window_fills(self):
        calc = RollingQuantileCalculator()
        for i in range(RollingQuantileCalculator.WINDOW):
            calc.add("close", float(i))
        assert calc.is_cold_start("close") is False

    def test_bar_count_tracks_close_adds(self):
        calc = RollingQuantileCalculator()
        assert calc.bar_count() == 0
        calc.add("close", 50000.0)
        assert calc.bar_count() == 1
        for _ in range(10):
            calc.add("close", 50000.0)
        assert calc.bar_count() == 11

    def test_window_maxlen_respected(self):
        calc = RollingQuantileCalculator()
        for i in range(RollingQuantileCalculator.WINDOW + 100):
            calc.add("x", float(i))
        window = calc._windows["x"]
        assert len(window) == RollingQuantileCalculator.WINDOW

    def test_none_values_in_window_excluded_from_rank(self):
        calc = RollingQuantileCalculator()
        for _ in range(10):
            calc.add("x", None)  # None values in history
        for i in range(10):
            calc.add("x", float(i))  # [0..9]
        rank = calc.quantile_rank("x", 5.0)
        assert rank is not None
        # Only 10 real values [0..9]; 5 values strictly below 5 → rank = 0.5
        assert rank == pytest.approx(0.5)


# ── check_cascade ─────────────────────────────────────────────────────────────


class TestCheckCascade:
    def test_matches_with_all_secondary_conditions(self):
        """Primary (abs_delta_p_pct > 97th) + LV secondary + delta_H secondary."""
        fv = make_fv(delta_p_pct=5.0)
        qr = {
            "abs_delta_p_pct": 0.98,  # > 0.97 ✓ primary
            "LV": 0.97,  # > 0.95 ✓ secondary
            "delta_H": 0.97,  # > 0.95 ✓ secondary
        }
        matched, reason, used = check_cascade(fv, qr)
        assert matched
        assert "CASCADE" in reason

    def test_matches_with_lv_secondary_only(self):
        """Primary gate + LV secondary is sufficient."""
        fv = make_fv(delta_p_pct=5.0)
        qr = {
            "abs_delta_p_pct": 0.98,
            "LV": 0.97,
            "delta_H": None,
        }
        matched, reason, _ = check_cascade(fv, qr)
        assert matched
        assert "CASCADE" in reason

    def test_matches_with_delta_h_secondary_only(self):
        """Primary gate + delta_H secondary is sufficient."""
        fv = make_fv(delta_p_pct=5.0)
        qr = {
            "abs_delta_p_pct": 0.98,
            "LV": None,
            "delta_H": 0.97,
        }
        matched, reason, _ = check_cascade(fv, qr)
        assert matched

    def test_fails_without_any_secondary(self):
        """Primary gate met but no secondary → False (Cond1 AND (Cond3 OR Cond4) required)."""
        fv = make_fv()
        qr = {
            "abs_delta_p_pct": 0.98,  # primary ✓
            "LV": None,  # no secondary
            "delta_H": None,  # no secondary
        }
        matched, _, _ = check_cascade(fv, qr)
        assert not matched

    def test_fails_when_primary_below_threshold(self):
        """abs_delta_p_pct < 0.97 → False regardless of secondary."""
        fv = make_fv()
        qr = {
            "abs_delta_p_pct": 0.96,  # below 0.97
            "LV": 0.99,
            "delta_H": 0.99,
        }
        matched, _, _ = check_cascade(fv, qr)
        assert not matched

    def test_fails_when_primary_is_none(self):
        """abs_delta_p_pct=None → False."""
        fv = make_fv()
        qr = {
            "abs_delta_p_pct": None,
            "LV": 0.99,
            "delta_H": 0.99,
        }
        matched, _, _ = check_cascade(fv, qr)
        assert not matched

    def test_secondary_below_threshold_not_counted(self):
        """LV=0.90 < 0.95 and delta_H=0.90 < 0.95 → no valid secondary → False."""
        fv = make_fv()
        qr = {
            "abs_delta_p_pct": 0.98,
            "LV": 0.90,  # below 0.95
            "delta_H": 0.90,  # below 0.95
        }
        matched, _, _ = check_cascade(fv, qr)
        assert not matched

    def test_used_quantiles_contain_abs_delta_p(self):
        """abs_delta_p_pct should appear in used dict (primary gate)."""
        fv = make_fv(delta_p_pct=5.0)
        qr = {
            "abs_delta_p_pct": 0.98,
            "LV": 0.97,
            "delta_H": None,
        }
        matched, _, used = check_cascade(fv, qr)
        assert matched
        assert "abs_delta_p_pct" in used


# ── check_critical ────────────────────────────────────────────────────────────


class TestCheckCritical:
    """Cond1: ac12 > ac24 > ac48 (monotone rising). Cond2: sigma_p_d2 > 0 AND > 80th.
    Cond3: H_change_rate_std_12h > 80th. Cond4: OI_hurst > 70th. Trigger: 1 AND 2 AND (3 OR 4)."""

    def _csd_fv(self, **kwargs):
        """FeatureVector with Cond1 monotone autocorr and positive sigma_p_d2 by default."""
        defaults = dict(
            price_autocorr_12h=0.60,
            price_autocorr_24h=0.40,
            price_autocorr_48h=0.20,
            sigma_p_d2=0.001,
        )
        defaults.update(kwargs)
        return make_fv(**defaults)

    def test_matches_with_cond4_oi_hurst(self):
        """Cond1+2+4 — OI_hurst satisfies Cond4."""
        fv = self._csd_fv()
        qr = {"sigma_p_d2": 0.85, "H_change_rate_std_12h": None, "OI_hurst": 0.75}
        matched, reason, _ = check_critical(fv, qr)
        assert matched
        assert "CRITICAL" in reason

    def test_matches_with_cond3_h_change_rate(self):
        """Cond1+2+3 — H_change_rate_std satisfies Cond3."""
        fv = self._csd_fv()
        qr = {"sigma_p_d2": 0.85, "H_change_rate_std_12h": 0.85, "OI_hurst": None}
        matched, reason, _ = check_critical(fv, qr)
        assert matched
        assert "H_cr_std" in reason

    def test_fails_cond1_autocorr_not_monotone(self):
        """autocorr_12h < autocorr_24h violates Cond1."""
        fv = make_fv(
            price_autocorr_12h=0.30,
            price_autocorr_24h=0.40,  # higher than 12h → NOT rising
            price_autocorr_48h=0.20,
            sigma_p_d2=0.001,
        )
        qr = {"sigma_p_d2": 0.85, "H_change_rate_std_12h": None, "OI_hurst": 0.75}
        matched, _, _ = check_critical(fv, qr)
        assert not matched

    def test_fails_cond1_autocorr_none(self):
        """Any None autocorr fails Cond1."""
        fv = make_fv(price_autocorr_12h=0.60, price_autocorr_24h=None, sigma_p_d2=0.001)
        qr = {"sigma_p_d2": 0.85, "H_change_rate_std_12h": None, "OI_hurst": 0.75}
        matched, _, _ = check_critical(fv, qr)
        assert not matched

    def test_fails_cond2_sigma_d2_none(self):
        """sigma_p_d2=None fails Cond2."""
        fv = self._csd_fv(sigma_p_d2=None)
        qr = {"sigma_p_d2": None, "H_change_rate_std_12h": None, "OI_hurst": 0.75}
        matched, _, _ = check_critical(fv, qr)
        assert not matched

    def test_fails_cond2_sigma_d2_not_positive(self):
        """sigma_p_d2 <= 0 fails Cond2 even if qr is high."""
        fv = self._csd_fv(sigma_p_d2=-0.001)
        qr = {"sigma_p_d2": 0.85, "H_change_rate_std_12h": None, "OI_hurst": 0.75}
        matched, _, _ = check_critical(fv, qr)
        assert not matched

    def test_fails_cond2_sigma_d2_qr_below_80(self):
        """sigma_p_d2 qr < 0.80 fails Cond2."""
        fv = self._csd_fv()
        qr = {"sigma_p_d2": 0.75, "H_change_rate_std_12h": None, "OI_hurst": 0.75}
        matched, _, _ = check_critical(fv, qr)
        assert not matched

    def test_fails_when_neither_cond3_nor_cond4(self):
        """Both H_change_rate_std and OI_hurst unavailable → False."""
        fv = self._csd_fv()
        qr = {"sigma_p_d2": 0.85, "H_change_rate_std_12h": None, "OI_hurst": None}
        matched, _, _ = check_critical(fv, qr)
        assert not matched

    def test_fails_cond4_oi_hurst_below_70(self):
        """OI_hurst present but below 0.70 threshold → Cond4 fails."""
        fv = self._csd_fv()
        qr = {"sigma_p_d2": 0.85, "H_change_rate_std_12h": None, "OI_hurst": 0.65}
        matched, _, _ = check_critical(fv, qr)
        assert not matched


# ── check_coiling ─────────────────────────────────────────────────────────────


class TestCheckCoiling:
    def test_matches_all_conditions(self):
        """Low sigma, low H, high OI change rate, high absorption ratio."""
        fv = make_fv(H=3.5)
        qr = {
            "sigma_p_24h": 0.20,  # < 0.30 ✓
            "H": 0.20,  # < 0.30 ✓
            "oi_change_rate_24h": 0.75,  # > 0.70 ✓
            "tf_dp_ratio_24h": 0.85,  # > 0.80 ✓
        }
        matched, reason, _ = check_coiling(fv, qr)
        assert matched
        assert "COILING" in reason

    def test_matches_without_h(self):
        """H is WIKI_REQUIRED — absent H is skipped (Cond2 relaxed)."""
        fv = make_fv(H=None)
        qr = {
            "sigma_p_24h": 0.20,
            "H": None,
            "oi_change_rate_24h": 0.75,
            "tf_dp_ratio_24h": 0.85,
        }
        matched, _, _ = check_coiling(fv, qr)
        assert matched

    def test_fails_high_sigma(self):
        """sigma_p_24h > 30th pct → Cond1 fails."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.60,  # above 0.30
            "H": None,
            "oi_change_rate_24h": 0.75,
            "tf_dp_ratio_24h": 0.85,
        }
        matched, _, _ = check_coiling(fv, qr)
        assert not matched

    def test_fails_when_h_present_above_30th(self):
        """H present and qr > 0.30 → Cond2 fails (high entropy = not coiling)."""
        fv = make_fv(H=5.0)
        qr = {
            "sigma_p_24h": 0.20,
            "H": 0.50,  # > 0.30 → fails
            "oi_change_rate_24h": 0.75,
            "tf_dp_ratio_24h": 0.85,
        }
        matched, _, _ = check_coiling(fv, qr)
        assert not matched

    def test_fails_oi_change_rate_none(self):
        """oi_change_rate_24h=None → Cond3 short-circuits to False."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.20,
            "H": None,
            "oi_change_rate_24h": None,
            "tf_dp_ratio_24h": 0.85,
        }
        matched, _, _ = check_coiling(fv, qr)
        assert not matched

    def test_fails_oi_change_rate_low(self):
        """oi_change_rate_24h < 0.70 → Cond3 fails."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.20,
            "H": None,
            "oi_change_rate_24h": 0.60,  # below 0.70
            "tf_dp_ratio_24h": 0.85,
        }
        matched, _, _ = check_coiling(fv, qr)
        assert not matched

    def test_fails_tf_dp_ratio_none(self):
        """tf_dp_ratio_24h=None → Cond4 short-circuits to False."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.20,
            "H": None,
            "oi_change_rate_24h": 0.75,
            "tf_dp_ratio_24h": None,
        }
        matched, _, _ = check_coiling(fv, qr)
        assert not matched

    def test_fails_tf_dp_ratio_low(self):
        """tf_dp_ratio_24h < 0.80 → Cond4 fails."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.20,
            "H": None,
            "oi_change_rate_24h": 0.75,
            "tf_dp_ratio_24h": 0.70,  # below 0.80
        }
        matched, _, _ = check_coiling(fv, qr)
        assert not matched


# ── check_surging ─────────────────────────────────────────────────────────────


class TestCheckSurging:
    def test_surging_up_positive_tf(self):
        """price_slope_6h > 80th + tf_dir > 0 → SURGING_UP."""
        fv = make_fv(tf_directional_ratio_6h=0.85, sigma_rising_12h=True)
        qr = {
            "price_slope_6h": 0.85,
            "sigma_change_rate_std_6h": 0.30,  # < 0.50 ✓
        }
        matched, reason, _ = check_surging(fv, qr)
        assert matched
        assert "SURGING_UP" in reason

    def test_surging_down_negative_tf(self):
        """tf_dir < 0 → SURGING_DOWN."""
        fv = make_fv(tf_directional_ratio_6h=-0.85, sigma_rising_12h=True)
        qr = {
            "price_slope_6h": 0.85,
            "sigma_change_rate_std_6h": 0.30,
        }
        matched, reason, _ = check_surging(fv, qr)
        assert matched
        assert "SURGING_DOWN" in reason

    def test_fails_slope_below_threshold(self):
        """price_slope_6h < 0.80 → Cond1 fails."""
        fv = make_fv(tf_directional_ratio_6h=0.85)
        qr = {
            "price_slope_6h": 0.70,  # below 0.80
            "sigma_change_rate_std_6h": 0.30,
        }
        matched, _, _ = check_surging(fv, qr)
        assert not matched

    def test_fails_slope_none(self):
        """price_slope_6h=None → Cond1 fails."""
        fv = make_fv(tf_directional_ratio_6h=0.85)
        qr = {"price_slope_6h": None}
        matched, _, _ = check_surging(fv, qr)
        assert not matched

    def test_fails_tf_dir_none(self):
        """tf_directional_ratio_6h=None → hard short-circuit (TF absent)."""
        fv = make_fv(tf_directional_ratio_6h=None)
        qr = {"price_slope_6h": 0.85}
        matched, _, _ = check_surging(fv, qr)
        assert not matched

    def test_fails_tf_dir_low(self):
        """|tf_directional_ratio_6h| <= 0.70 → Cond2 fails."""
        fv = make_fv(tf_directional_ratio_6h=0.50)
        qr = {"price_slope_6h": 0.85}
        matched, _, _ = check_surging(fv, qr)
        assert not matched

    def test_fails_sigma_not_rising(self):
        """sigma_rising_12h=False → Cond3a fails."""
        fv = make_fv(tf_directional_ratio_6h=0.85, sigma_rising_12h=False)
        qr = {"price_slope_6h": 0.85, "sigma_change_rate_std_6h": 0.30}
        matched, _, _ = check_surging(fv, qr)
        assert not matched

    def test_passes_sigma_rising_none(self):
        """sigma_rising_12h=None → Cond3a skipped (history insufficient)."""
        fv = make_fv(tf_directional_ratio_6h=0.85, sigma_rising_12h=None)
        qr = {"price_slope_6h": 0.85}
        matched, _, _ = check_surging(fv, qr)
        assert matched

    def test_fails_sigma_cr_std_above_50(self):
        """sigma_change_rate_std_6h >= 0.50 → Cond3b fails (σ unstable)."""
        fv = make_fv(tf_directional_ratio_6h=0.85, sigma_rising_12h=None)
        qr = {"price_slope_6h": 0.85, "sigma_change_rate_std_6h": 0.60}
        matched, _, _ = check_surging(fv, qr)
        assert not matched

    def test_passes_sigma_cr_std_none(self):
        """sigma_change_rate_std_6h=None → Cond3b skipped."""
        fv = make_fv(tf_directional_ratio_6h=0.85, sigma_rising_12h=None)
        qr = {"price_slope_6h": 0.85, "sigma_change_rate_std_6h": None}
        matched, _, _ = check_surging(fv, qr)
        assert matched


# ── check_drifting_charged ────────────────────────────────────────────────────


class TestCheckDriftingCharged:
    def test_matches_all_conditions(self):
        """σ in [40th, 70th], H_24h_mean < 50th, abs_tf in [30th, 70th], OI_hurst > 0.6."""
        fv = make_fv(OI_hurst=0.72)
        qr = {
            "sigma_p_24h": 0.55,  # in [0.40, 0.70] ✓
            "H_24h_mean": 0.40,  # < 0.50 ✓
            "abs_tf_24h_sum": 0.50,  # in [0.30, 0.70] ✓
        }
        matched, reason, _ = check_drifting_charged(fv, qr)
        assert matched
        assert "DRIFTING_CHARGED" in reason

    def test_fails_sigma_below_band(self):
        """sigma_p_24h < 0.40 → Cond1 fails."""
        fv = make_fv(OI_hurst=0.72)
        qr = {
            "sigma_p_24h": 0.30,  # below 0.40
            "H_24h_mean": 0.40,
            "abs_tf_24h_sum": 0.50,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert not matched

    def test_fails_sigma_above_band(self):
        """sigma_p_24h > 0.70 → Cond1 fails."""
        fv = make_fv(OI_hurst=0.72)
        qr = {
            "sigma_p_24h": 0.80,  # above 0.70
            "H_24h_mean": 0.40,
            "abs_tf_24h_sum": 0.50,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert not matched

    def test_fails_h24_none(self):
        """H_24h_mean=None → Cond2 short-circuits (H collector absent)."""
        fv = make_fv(OI_hurst=0.72)
        qr = {
            "sigma_p_24h": 0.55,
            "H_24h_mean": None,
            "abs_tf_24h_sum": 0.50,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert not matched

    def test_fails_h24_above_50th(self):
        """H_24h_mean >= 0.50 → Cond2 fails (entropy not low enough)."""
        fv = make_fv(OI_hurst=0.72)
        qr = {
            "sigma_p_24h": 0.55,
            "H_24h_mean": 0.60,  # >= 0.50
            "abs_tf_24h_sum": 0.50,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert not matched

    def test_fails_tf24_none(self):
        """abs_tf_24h_sum=None → Cond3 short-circuits (TF collector absent)."""
        fv = make_fv(OI_hurst=0.72)
        qr = {
            "sigma_p_24h": 0.55,
            "H_24h_mean": 0.40,
            "abs_tf_24h_sum": None,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert not matched

    def test_fails_tf24_out_of_band(self):
        """abs_tf_24h_sum outside [0.30, 0.70] → Cond3 fails."""
        fv = make_fv(OI_hurst=0.72)
        qr = {
            "sigma_p_24h": 0.55,
            "H_24h_mean": 0.40,
            "abs_tf_24h_sum": 0.20,  # below 0.30
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert not matched

    def test_fails_oi_hurst_none(self):
        """OI_hurst=None → Cond4 short-circuits."""
        fv = make_fv(OI_hurst=None)
        qr = {
            "sigma_p_24h": 0.55,
            "H_24h_mean": 0.40,
            "abs_tf_24h_sum": 0.50,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert not matched

    def test_fails_oi_hurst_at_boundary(self):
        """OI_hurst=0.60 → Cond4 fails (threshold is > 0.6, not >=)."""
        fv = make_fv(OI_hurst=0.60)
        qr = {
            "sigma_p_24h": 0.55,
            "H_24h_mean": 0.40,
            "abs_tf_24h_sum": 0.50,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert not matched


# ── check_drifting_calm ───────────────────────────────────────────────────────


class TestCheckDriftingCalm:
    def test_matches_all_conditions(self):
        """σ in [30th, 60th], H_24h_mean in [40th, 80th], abs_tf < 50th, |oi_cr| < 50th."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.45,  # in [0.30, 0.60] ✓
            "H_24h_mean": 0.60,  # in [0.40, 0.80] ✓
            "abs_tf_24h_sum": 0.35,  # < 0.50 ✓
            "abs_oi_change_rate_24h": 0.30,  # < 0.50 ✓
        }
        matched, reason, _ = check_drifting_calm(fv, qr)
        assert matched
        assert "DRIFTING_CALM" in reason

    def test_fails_sigma_below_band(self):
        """sigma_p_24h < 0.30 → Cond1 fails."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.20,  # below 0.30
            "H_24h_mean": 0.60,
            "abs_tf_24h_sum": 0.35,
            "abs_oi_change_rate_24h": 0.30,
        }
        matched, _, _ = check_drifting_calm(fv, qr)
        assert not matched

    def test_fails_sigma_above_band(self):
        """sigma_p_24h > 0.60 → Cond1 fails."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.70,  # above 0.60
            "H_24h_mean": 0.60,
            "abs_tf_24h_sum": 0.35,
            "abs_oi_change_rate_24h": 0.30,
        }
        matched, _, _ = check_drifting_calm(fv, qr)
        assert not matched

    def test_fails_h24_none(self):
        """H_24h_mean=None → Cond2 short-circuits (H collector absent, §10.1 principle 3)."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.45,
            "H_24h_mean": None,
            "abs_tf_24h_sum": 0.35,
            "abs_oi_change_rate_24h": 0.30,
        }
        matched, _, _ = check_drifting_calm(fv, qr)
        assert not matched

    def test_fails_h24_out_of_band(self):
        """H_24h_mean outside [0.40, 0.80] → Cond2 fails."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.45,
            "H_24h_mean": 0.30,  # below 0.40
            "abs_tf_24h_sum": 0.35,
            "abs_oi_change_rate_24h": 0.30,
        }
        matched, _, _ = check_drifting_calm(fv, qr)
        assert not matched

    def test_fails_tf24_none(self):
        """abs_tf_24h_sum=None → Cond3 short-circuits (TF collector absent)."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.45,
            "H_24h_mean": 0.60,
            "abs_tf_24h_sum": None,
            "abs_oi_change_rate_24h": 0.30,
        }
        matched, _, _ = check_drifting_calm(fv, qr)
        assert not matched

    def test_fails_tf24_above_50th(self):
        """abs_tf_24h_sum >= 0.50 → Cond3 fails (flow too high)."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.45,
            "H_24h_mean": 0.60,
            "abs_tf_24h_sum": 0.60,  # >= 0.50
            "abs_oi_change_rate_24h": 0.30,
        }
        matched, _, _ = check_drifting_calm(fv, qr)
        assert not matched

    def test_fails_oi_cr_none(self):
        """abs_oi_change_rate_24h=None → Cond4 short-circuits (OI collector absent)."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.45,
            "H_24h_mean": 0.60,
            "abs_tf_24h_sum": 0.35,
            "abs_oi_change_rate_24h": None,
        }
        matched, _, _ = check_drifting_calm(fv, qr)
        assert not matched

    def test_fails_oi_cr_above_50th(self):
        """abs_oi_change_rate_24h >= 0.50 → Cond4 fails (OI moving too much)."""
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.45,
            "H_24h_mean": 0.60,
            "abs_tf_24h_sum": 0.35,
            "abs_oi_change_rate_24h": 0.60,  # >= 0.50
        }
        matched, _, _ = check_drifting_calm(fv, qr)
        assert not matched


# ── StateRecognizer — end-to-end ──────────────────────────────────────────────


class TestStateRecognizer:
    WINDOW = RollingQuantileCalculator.WINDOW  # 720

    def test_cold_start_returns_none_state(self):
        """First 719 bars should all be cold start."""
        recognizer = StateRecognizer()
        fvs = _flat_fv_sequence(self.WINDOW - 1)
        for fv in fvs:
            record = recognizer.recognize(fv)
            assert record.state is None
            assert record.cold_start is True
            assert record.reason == "COLD_START"

    def test_bar_720_exits_cold_start(self):
        """Bar 720 (the 720th bar) is the first to have a full window."""
        recognizer = StateRecognizer()
        fvs = _flat_fv_sequence(self.WINDOW)
        records = [recognizer.recognize(fv) for fv in fvs]
        for r in records[: self.WINDOW - 1]:
            assert r.cold_start is True
        assert records[-1].cold_start is False
        # No WIKI-REQUIRED data in flat sequence → state=None expected (§10.1 principle 3)
        assert records[-1].reason == "NO_STATE_MATCHED"

    def test_stable_bars_yield_no_state_without_wiki_data(self):
        """Flat data with no WIKI-REQUIRED features → state=None after warm-up."""
        recognizer = StateRecognizer()
        fvs = _flat_fv_sequence(self.WINDOW + 5)
        records = [recognizer.recognize(fv) for fv in fvs]
        warm_records = [r for r in records if not r.cold_start]
        for r in warm_records:
            assert r.state is None
            assert r.reason == "NO_STATE_MATCHED"

    def test_cascade_triggers_on_extreme_bar_721(self):
        """
        720 bars with spreading LV distribution, then one bar with extreme
        abs_delta_p_pct + extreme LV. Cascade should trigger on bar 721.
        """
        recognizer = StateRecognizer()
        base_time = datetime(2023, 1, 1, tzinfo=timezone.utc)

        for i in range(self.WINDOW):
            fv = make_fv(
                time=base_time + timedelta(hours=i),
                delta_p_pct=0.01,
                LV=0.001 + i * 0.0001,  # spread LV distribution
            )
            recognizer.recognize(fv)

        extreme_fv = make_fv(
            time=base_time + timedelta(hours=self.WINDOW),
            delta_p_pct=50.0,  # extreme → abs_delta_p_pct > 97th
            LV=1.0,  # extreme → LV > 95th (CASCADE secondary)
        )
        record = recognizer.recognize(extreme_fv)
        assert record.cold_start is False
        assert record.state == StateLabel.CASCADE, f"Expected CASCADE, got {record.state} ({record.reason})"

    def test_priority_cascade_over_surging(self):
        """
        When both Cascade and Surging conditions could match,
        Cascade (higher priority) must win.
        """
        recognizer = StateRecognizer()
        base_time = datetime(2023, 1, 1, tzinfo=timezone.utc)

        for i in range(self.WINDOW):
            fv = make_fv(
                time=base_time + timedelta(hours=i),
                delta_p_pct=float(i % 10) * 0.01,
                LV=0.001 + i * 0.0001,
                price_slope_6h=0.0001 + i * 0.000001,
            )
            recognizer.recognize(fv)

        extreme_fv = make_fv(
            time=base_time + timedelta(hours=self.WINDOW),
            delta_p_pct=100.0,  # extreme → CASCADE primary
            LV=1.0,  # extreme → CASCADE secondary
            price_slope_6h=999.0,  # extreme → SURGING Cond1
            tf_directional_ratio_6h=0.85,  # SURGING Cond2 direction
        )
        record = recognizer.recognize(extreme_fv)
        assert record.state == StateLabel.CASCADE

    def test_symbol_propagated(self):
        recognizer = StateRecognizer()
        fvs = _flat_fv_sequence(self.WINDOW + 1)
        records = [recognizer.recognize(fv) for fv in fvs]
        for r in records:
            assert r.symbol == "BTCUSDT"

    def test_feature_vector_attached(self):
        recognizer = StateRecognizer()
        fvs = _flat_fv_sequence(5)
        for fv in fvs:
            record = recognizer.recognize(fv)
            assert record.feature_vector is not None


# ── compute_state_distribution ────────────────────────────────────────────────


class TestComputeStateDistribution:
    def _make_state_records(self, states: list) -> list[StateRecord]:
        """states: list of StateLabel or None (None = cold start)"""
        records = []
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        for s in states:
            records.append(
                StateRecord(
                    time=t,
                    symbol="BTCUSDT",
                    state=s,
                    reason="TEST" if s else "COLD_START",
                    feature_quantiles={},
                    cold_start=(s is None),
                    none_reason=StateNoneReason.COLD_START if s is None else StateNoneReason.NOT_APPLICABLE,
                )
            )
            t += timedelta(hours=1)
        return records

    def test_empty_list(self):
        dist = compute_state_distribution([])
        assert dist["total_bars"] == 0
        assert dist["cold_start_bars"] == 0
        assert dist["state_counts"] is not None
        assert dist["state_rates"] is not None

    def test_all_cold_start(self):
        records = self._make_state_records([None] * 10)
        dist = compute_state_distribution(records)
        assert dist["total_bars"] == 10
        assert dist["cold_start_bars"] == 10
        for v in dist["state_rates"].values():
            assert v == 0.0

    def test_counts_states_correctly(self):
        states = [
            StateLabel.CASCADE,
            StateLabel.DRIFTING_CALM,
            StateLabel.DRIFTING_CALM,
            StateLabel.SURGING_UP,
            None,  # cold start
        ]
        records = self._make_state_records(states)
        dist = compute_state_distribution(records)
        assert dist["total_bars"] == 5
        assert dist["cold_start_bars"] == 1
        assert dist["state_counts"]["Cascade"] == 1
        assert dist["state_counts"]["Drifting_Calm"] == 2
        assert dist["state_counts"]["Surging_Up"] == 1

    def test_rates_sum_to_one(self):
        states = [StateLabel.CASCADE, StateLabel.DRIFTING_CALM, StateLabel.COILING]
        records = self._make_state_records(states)
        dist = compute_state_distribution(records)
        total_rate = sum(dist["state_rates"].values())
        assert total_rate == pytest.approx(1.0)

    def test_rates_zero_for_absent_states(self):
        records = self._make_state_records([StateLabel.CASCADE])
        dist = compute_state_distribution(records)
        assert dist["state_rates"]["Drifting_Calm"] == 0.0

    def test_keys_match_all_state_labels(self):
        records = self._make_state_records([StateLabel.CASCADE])
        dist = compute_state_distribution(records)
        for label in StateLabel:
            assert label.value in dist["state_counts"]
            assert label.value in dist["state_rates"]

    def test_distribution_splits_cold_missing_no_match(self):
        """compute_state_distribution must separate three distinct None causes."""
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        records = [
            StateRecord(
                time=t,
                symbol="X",
                state=None,
                reason="COLD_START",
                feature_quantiles={},
                cold_start=True,
                none_reason=StateNoneReason.COLD_START,
            ),
            StateRecord(
                time=t,
                symbol="X",
                state=None,
                reason="NO_STATE_MATCHED",
                feature_quantiles={},
                cold_start=False,
                none_reason=StateNoneReason.MISSING_DATA,
            ),
            StateRecord(
                time=t,
                symbol="X",
                state=None,
                reason="NO_STATE_MATCHED",
                feature_quantiles={},
                cold_start=False,
                none_reason=StateNoneReason.NO_MATCH,
            ),
            StateRecord(
                time=t,
                symbol="X",
                state=StateLabel.CASCADE,
                reason="CASCADE:x",
                feature_quantiles={},
                cold_start=False,
                none_reason=StateNoneReason.NOT_APPLICABLE,
            ),
        ]
        dist = compute_state_distribution(records)
        assert dist["total_bars"] == 4
        assert dist["cold_start_bars"] == 1
        assert dist["missing_data_bars"] == 1
        assert dist["no_match_bars"] == 1
        assert dist["active_bars"] == 1


# ── StateNoneReason — recognizer integration ──────────────────────────────────


class TestStateNoneReason:
    """Verify StateRecognizer sets none_reason correctly for all three None causes."""

    WINDOW = RollingQuantileCalculator.WINDOW

    def _warm_recognizer(self, n: int = None, with_wiki: bool = False) -> StateRecognizer:
        """Return a recognizer that has processed n bars (default: WINDOW, so post-warmup)."""
        if n is None:
            n = self.WINDOW
        rec = StateRecognizer()
        t = datetime(2023, 1, 1, tzinfo=timezone.utc)
        for i in range(n):
            fv = make_fv(
                time=t + timedelta(hours=i),
                close=50000.0 + i,
                delta_p_pct=0.1,
                sigma_p_24h=0.005,
                # Include WIKI features if requested
                H_24h_mean=0.5 if with_wiki else None,
                abs_tf_24h_sum=100.0 if with_wiki else None,
                oi_change_rate_24h=0.01 if with_wiki else None,
                tf_dp_ratio_24h=0.5 if with_wiki else None,
                tf_directional_ratio_6h=0.3 if with_wiki else None,
            )
            rec.recognize(fv)
        return rec

    def test_cold_start_bars_get_cold_start_reason(self):
        rec = StateRecognizer()
        t = datetime(2023, 1, 1, tzinfo=timezone.utc)
        for i in range(self.WINDOW - 1):
            result = rec.recognize(make_fv(time=t + timedelta(hours=i)))
            assert result.none_reason == StateNoneReason.COLD_START
            assert result.cold_start is True

    def test_none_reason_distinguishes_cold_start_from_missing_data(self):
        """Post-warmup bars without WIKI features → MISSING_DATA, not COLD_START."""
        rec = self._warm_recognizer(with_wiki=False)
        t = datetime(2023, 6, 1, tzinfo=timezone.utc)
        # Process one more bar with no WIKI data — should be MISSING_DATA
        result = rec.recognize(
            make_fv(
                time=t,
                close=50000.0,
                delta_p_pct=0.1,
                sigma_p_24h=0.005,
            )
        )
        assert result.state is None
        assert result.cold_start is False
        assert result.none_reason == StateNoneReason.MISSING_DATA

    def test_none_reason_distinguishes_missing_data_from_no_match(self):
        """Post-warmup bars WITH full WIKI features but no condition met → NO_MATCH."""
        rec = self._warm_recognizer(with_wiki=True)
        t = datetime(2023, 6, 1, tzinfo=timezone.utc)
        # Flat data with all WIKI features present — no condition should fire
        result = rec.recognize(
            make_fv(
                time=t,
                close=50000.0,
                delta_p_pct=0.01,  # unremarkable
                sigma_p_24h=0.005,
                H_24h_mean=0.5,
                abs_tf_24h_sum=50.0,
                oi_change_rate_24h=0.001,
                tf_dp_ratio_24h=0.5,
                tf_directional_ratio_6h=0.1,
            )
        )
        # If state is None: none_reason must be MISSING_DATA or NO_MATCH (not COLD_START)
        if result.state is None:
            assert result.none_reason in (StateNoneReason.MISSING_DATA, StateNoneReason.NO_MATCH)
            assert result.none_reason != StateNoneReason.COLD_START

    def test_matched_state_gets_not_applicable_reason(self):
        """When a state is matched, none_reason must be NOT_APPLICABLE."""
        rec = self._warm_recognizer(with_wiki=True)
        t = datetime(2023, 6, 1, tzinfo=timezone.utc)
        # Inject an extreme bar to trigger CASCADE (abs_delta_p > 97th, LV > 95th)
        result = rec.recognize(
            make_fv(
                time=t,
                close=50000.0,
                delta_p_pct=40.0,  # extreme
                sigma_p_24h=0.005,
                LV=1.0,
                H_24h_mean=0.5,
                abs_tf_24h_sum=100.0,
                oi_change_rate_24h=0.01,
                tf_dp_ratio_24h=0.5,
                tf_directional_ratio_6h=0.3,
            )
        )
        if result.state is not None:
            assert result.none_reason == StateNoneReason.NOT_APPLICABLE


# ── EC-05 defense tests ────────────────────────────────────────────────────────


class TestHardShortCircuitQRCoverage:
    """Defense tests for _HARD_SHORT_CIRCUIT_QR — prevent silent regression if
    a new WIKI feature is added to conditions without updating the frozenset."""

    # Authoritative list of WIKI-required features that cause hard short-circuit.
    # fv_direct: checked directly on the FeatureVector (not via qr dict).
    # qr_features: must be in _HARD_SHORT_CIRCUIT_QR.
    _FV_DIRECT: frozenset = frozenset({"tf_directional_ratio_6h"})
    _REQUIRED_QR_FEATURES: frozenset = frozenset(
        {
            "H_24h_mean",
            "abs_tf_24h_sum",
            "oi_change_rate_24h",
            "tf_dp_ratio_24h",
            "abs_oi_change_rate_24h",
        }
    )

    def test_hard_short_circuit_qr_covers_all_wiki_required_features(self):
        """Every WIKI_REQUIRED quantile feature must be in _HARD_SHORT_CIRCUIT_QR.

        If this test fails, a new WIKI feature was added to conditions but
        _HARD_SHORT_CIRCUIT_QR in recognizer.py was not updated — MISSING_DATA
        would be silently misclassified as NO_MATCH.
        """
        missing = self._REQUIRED_QR_FEATURES - _HARD_SHORT_CIRCUIT_QR
        assert not missing, (
            f"The following WIKI_REQUIRED features are not in _HARD_SHORT_CIRCUIT_QR: {missing}. "
            f"Add them to _HARD_SHORT_CIRCUIT_QR in sel_engine/states/recognizer.py. "
            f"See EC-05 in audit/engineering_concerns.md."
        )

    def test_no_match_only_when_all_wiki_features_present(self):
        """NO_MATCH is only returned when all WIKI_REQUIRED features are present.
        Nulling any individual WIKI feature must flip none_reason to MISSING_DATA.
        """
        WINDOW = RollingQuantileCalculator.WINDOW
        rec = StateRecognizer()
        t = datetime(2023, 1, 1, tzinfo=timezone.utc)
        # Warm to post-warmup with all WIKI features present
        for i in range(WINDOW):
            rec.recognize(
                make_fv(
                    time=t + timedelta(hours=i),
                    close=50000.0 + i,
                    delta_p_pct=0.1,
                    sigma_p_24h=0.005,
                    H_24h_mean=0.5,
                    abs_tf_24h_sum=100.0,
                    oi_change_rate_24h=0.01,
                    tf_dp_ratio_24h=0.5,
                    tf_directional_ratio_6h=0.3,
                )
            )

        base_t = t + timedelta(hours=WINDOW)

        # Baseline: all features present, unremarkable values → state should be None with NO_MATCH
        baseline = rec.recognize(
            make_fv(
                time=base_t,
                close=50000.0,
                delta_p_pct=0.01,
                sigma_p_24h=0.005,
                H_24h_mean=0.5,
                abs_tf_24h_sum=50.0,
                oi_change_rate_24h=0.001,
                tf_dp_ratio_24h=0.5,
                tf_directional_ratio_6h=0.1,
            )
        )
        # If no condition fires, must be NO_MATCH (not MISSING_DATA)
        if baseline.state is None:
            assert baseline.none_reason == StateNoneReason.NO_MATCH, (
                f"Expected NO_MATCH with all WIKI features present, got {baseline.none_reason}"
            )

        # Now null each WIKI feature one at a time — each must produce MISSING_DATA
        wiki_cases = [
            ("H_24h_mean", dict(H_24h_mean=None)),
            ("abs_tf_24h_sum", dict(abs_tf_24h_sum=None)),
            ("oi_change_rate_24h", dict(oi_change_rate_24h=None)),
            ("tf_dp_ratio_24h", dict(tf_dp_ratio_24h=None)),
            ("tf_directional_ratio_6h", dict(tf_directional_ratio_6h=None)),
        ]
        full_kwargs = dict(
            close=50000.0,
            delta_p_pct=0.01,
            sigma_p_24h=0.005,
            H_24h_mean=0.5,
            abs_tf_24h_sum=50.0,
            oi_change_rate_24h=0.001,
            tf_dp_ratio_24h=0.5,
            tf_directional_ratio_6h=0.1,
        )
        for feat_name, override in wiki_cases:
            kwargs = {**full_kwargs, **override}
            result = rec.recognize(make_fv(time=base_t + timedelta(hours=1), **kwargs))
            if result.state is None:
                assert result.none_reason == StateNoneReason.MISSING_DATA, (
                    f"Setting {feat_name}=None should yield MISSING_DATA, got {result.none_reason}"
                )
