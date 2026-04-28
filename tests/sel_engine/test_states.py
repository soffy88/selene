"""
Tests for sel_engine Wave 2 state recognition layer.
All tests use synthetic data — no DB or Redis connections required.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

import pytest

from sel_engine.features.schema import FeatureVector
from sel_engine.states.schema import StateLabel, StateRecord
from sel_engine.states.thresholds import RollingQuantileCalculator
from sel_engine.states.conditions import (
    check_cascade,
    check_critical,
    check_coiling,
    check_surging,
    check_drifting_charged,
    check_drifting_calm,
)
from sel_engine.states.recognizer import StateRecognizer, compute_state_distribution


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
        for i in range(20):
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
        for i in range(10):
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
        fv = make_fv(sigma_p_24h=0.1, delta_p_pct=5.0)
        qr = {
            "sigma_p_24h": 0.98,
            "abs_delta_p_pct": 0.96,
            "LV": 0.97,
            "abs_TF": 0.98,
        }
        matched, reason, used = check_cascade(fv, qr)
        assert matched
        assert "CASCADE" in reason

    def test_matches_with_only_abs_delta_p(self):
        fv = make_fv(sigma_p_24h=0.1, delta_p_pct=5.0)
        qr = {
            "sigma_p_24h": 0.98,
            "abs_delta_p_pct": 0.96,
            "LV": None,
            "abs_TF": None,
        }
        matched, reason, _ = check_cascade(fv, qr)
        assert matched
        assert "CASCADE" in reason

    def test_fails_without_secondary(self):
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.98,
            "abs_delta_p_pct": None,
            "LV": None,
            "abs_TF": None,
        }
        matched, _, _ = check_cascade(fv, qr)
        assert not matched

    def test_fails_when_sigma_below_threshold(self):
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.90,  # below 0.97
            "abs_delta_p_pct": 0.99,
            "LV": None,
            "abs_TF": None,
        }
        matched, _, _ = check_cascade(fv, qr)
        assert not matched

    def test_fails_when_sigma_is_none(self):
        fv = make_fv(sigma_p_24h=None)
        qr = {
            "sigma_p_24h": None,
            "abs_delta_p_pct": 0.99,
            "LV": 0.99,
            "abs_TF": 0.99,
        }
        matched, _, _ = check_cascade(fv, qr)
        assert not matched

    def test_secondary_below_threshold_not_counted(self):
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.98,
            "abs_delta_p_pct": 0.80,  # below 0.95
            "LV": None,
            "abs_TF": None,
        }
        matched, _, _ = check_cascade(fv, qr)
        assert not matched

    def test_used_quantiles_contain_sigma(self):
        fv = make_fv(delta_p_pct=5.0)
        qr = {
            "sigma_p_24h": 0.98,
            "abs_delta_p_pct": 0.96,
            "LV": None,
            "abs_TF": None,
        }
        matched, _, used = check_cascade(fv, qr)
        assert matched
        assert "sigma_p_24h" in used


# ── check_critical ────────────────────────────────────────────────────────────

class TestCheckCritical:
    def test_matches_full_conditions(self):
        fv = make_fv()
        qr = {
            "sigma_p_d2": 0.85,
            "OI_hurst": 0.75,
            "LV": 0.80,
            "price_autocorr_24h": 0.50,
        }
        matched, reason, _ = check_critical(fv, qr)
        assert matched
        assert "CRITICAL" in reason

    def test_fails_when_sigma_d2_none(self):
        fv = make_fv()
        qr = {
            "sigma_p_d2": None,
            "OI_hurst": 0.75,
            "LV": 0.80,
            "price_autocorr_24h": 0.10,
        }
        matched, _, _ = check_critical(fv, qr)
        assert not matched

    def test_relaxes_when_oi_hurst_none(self):
        """Without OI_hurst, should still match if sigma_d2 + cond3 met."""
        fv = make_fv()
        qr = {
            "sigma_p_d2": 0.85,
            "OI_hurst": None,
            "LV": 0.80,
            "price_autocorr_24h": None,
        }
        matched, _, _ = check_critical(fv, qr)
        assert matched

    def test_fails_when_oi_hurst_below_threshold(self):
        fv = make_fv()
        qr = {
            "sigma_p_d2": 0.85,
            "OI_hurst": 0.50,  # below 0.70
            "LV": 0.80,
            "price_autocorr_24h": None,
        }
        matched, _, _ = check_critical(fv, qr)
        assert not matched

    def test_cond3_via_autocorr_low(self):
        """Low autocorr_24h (≤0.20) satisfies cond3."""
        fv = make_fv()
        qr = {
            "sigma_p_d2": 0.85,
            "OI_hurst": 0.75,
            "LV": None,
            "price_autocorr_24h": 0.10,
        }
        matched, reason, _ = check_critical(fv, qr)
        assert matched
        assert "autocorr_24h" in reason

    def test_fails_when_cond3_unavailable(self):
        fv = make_fv()
        qr = {
            "sigma_p_d2": 0.85,
            "OI_hurst": 0.75,
            "LV": None,
            "price_autocorr_24h": None,
        }
        matched, _, _ = check_critical(fv, qr)
        assert not matched


# ── check_coiling ─────────────────────────────────────────────────────────────

class TestCheckCoiling:
    def test_matches_all_conditions(self):
        fv = make_fv(H=3.5)
        qr = {
            "sigma_p_24h": 0.20,
            "H": 0.80,
            "price_autocorr_24h": 0.70,
            "OI": 0.60,
        }
        matched, reason, _ = check_coiling(fv, qr)
        assert matched
        assert "COILING" in reason

    def test_matches_without_h(self):
        """H is WIKI_REQUIRED — should match even if H is None."""
        fv = make_fv(H=None)
        qr = {
            "sigma_p_24h": 0.20,
            "H": None,
            "price_autocorr_24h": 0.70,
            "OI": 0.60,
        }
        matched, reason, _ = check_coiling(fv, qr)
        assert matched

    def test_fails_high_sigma(self):
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.60,  # above 0.30
            "H": 0.80,
            "price_autocorr_24h": 0.70,
            "OI": 0.60,
        }
        matched, _, _ = check_coiling(fv, qr)
        assert not matched

    def test_fails_low_autocorr(self):
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.20,
            "H": 0.80,
            "price_autocorr_24h": 0.40,  # below 0.60
            "OI": 0.60,
        }
        matched, _, _ = check_coiling(fv, qr)
        assert not matched

    def test_fails_low_oi(self):
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.20,
            "H": 0.80,
            "price_autocorr_24h": 0.70,
            "OI": 0.30,  # below 0.50
        }
        matched, _, _ = check_coiling(fv, qr)
        assert not matched

    def test_fails_when_h_present_but_low(self):
        """If H is available but below threshold, should fail (not relaxed)."""
        fv = make_fv(H=1.0)
        qr = {
            "sigma_p_24h": 0.20,
            "H": 0.40,  # below 0.70
            "price_autocorr_24h": 0.70,
            "OI": 0.60,
        }
        matched, _, _ = check_coiling(fv, qr)
        assert not matched


# ── check_surging ─────────────────────────────────────────────────────────────

class TestCheckSurging:
    def test_surging_up_positive_delta(self):
        fv = make_fv(delta_p_pct=3.0)
        qr = {
            "abs_delta_p_pct": 0.80,
            "sigma_p_24h": 0.70,
            "price_autocorr_12h": 0.65,
        }
        matched, reason, _ = check_surging(fv, qr)
        assert matched
        assert "SURGING_UP" in reason

    def test_surging_down_negative_delta(self):
        fv = make_fv(delta_p_pct=-3.0)
        qr = {
            "abs_delta_p_pct": 0.80,
            "sigma_p_24h": 0.70,
            "price_autocorr_12h": 0.65,
        }
        matched, reason, _ = check_surging(fv, qr)
        assert matched
        assert "SURGING_DOWN" in reason

    def test_fails_low_abs_delta(self):
        fv = make_fv(delta_p_pct=0.1)
        qr = {
            "abs_delta_p_pct": 0.50,  # below 0.70
            "sigma_p_24h": 0.70,
            "price_autocorr_12h": 0.65,
        }
        matched, _, _ = check_surging(fv, qr)
        assert not matched

    def test_fails_low_sigma(self):
        fv = make_fv(delta_p_pct=3.0)
        qr = {
            "abs_delta_p_pct": 0.80,
            "sigma_p_24h": 0.40,  # below 0.60
            "price_autocorr_12h": 0.65,
        }
        matched, _, _ = check_surging(fv, qr)
        assert not matched

    def test_fails_low_autocorr(self):
        fv = make_fv(delta_p_pct=3.0)
        qr = {
            "abs_delta_p_pct": 0.80,
            "sigma_p_24h": 0.70,
            "price_autocorr_12h": 0.30,  # below 0.60
        }
        matched, _, _ = check_surging(fv, qr)
        assert not matched

    def test_fails_when_any_none(self):
        fv = make_fv(delta_p_pct=3.0)
        qr = {
            "abs_delta_p_pct": None,
            "sigma_p_24h": 0.70,
            "price_autocorr_12h": 0.65,
        }
        matched, _, _ = check_surging(fv, qr)
        assert not matched


# ── check_drifting_charged ────────────────────────────────────────────────────

class TestCheckDriftingCharged:
    def test_matches_with_both_derivatives(self):
        fv = make_fv(OI=1000.0, funding_rate=0.001)
        qr = {
            "sigma_p_24h": 0.30,
            "OI": 0.75,
            "abs_funding_rate": 0.65,
        }
        matched, reason, _ = check_drifting_charged(fv, qr)
        assert matched
        assert "DRIFTING_CHARGED" in reason

    def test_matches_with_only_oi(self):
        fv = make_fv(OI=1000.0, funding_rate=None)
        qr = {
            "sigma_p_24h": 0.30,
            "OI": 0.75,
            "abs_funding_rate": None,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert matched

    def test_matches_with_only_funding(self):
        fv = make_fv(OI=None, funding_rate=0.001)
        qr = {
            "sigma_p_24h": 0.30,
            "OI": None,
            "abs_funding_rate": 0.65,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert matched

    def test_fails_both_derivatives_unavailable(self):
        fv = make_fv()
        qr = {
            "sigma_p_24h": 0.30,
            "OI": None,
            "abs_funding_rate": None,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert not matched

    def test_fails_high_sigma(self):
        fv = make_fv(OI=1000.0, funding_rate=0.001)
        qr = {
            "sigma_p_24h": 0.70,  # above 0.50
            "OI": 0.75,
            "abs_funding_rate": 0.65,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert not matched

    def test_fails_oi_below_threshold(self):
        fv = make_fv(OI=100.0, funding_rate=None)
        qr = {
            "sigma_p_24h": 0.30,
            "OI": 0.50,  # below 0.70
            "abs_funding_rate": None,
        }
        matched, _, _ = check_drifting_charged(fv, qr)
        assert not matched


# ── check_drifting_calm ───────────────────────────────────────────────────────

class TestCheckDriftingCalm:
    def test_matches_with_low_sigma(self):
        fv = make_fv()
        qr = {"sigma_p_24h": 0.30}
        matched, reason, _ = check_drifting_calm(fv, qr)
        assert matched
        assert "DRIFTING_CALM" in reason

    def test_matches_when_sigma_none(self):
        fv = make_fv(sigma_p_24h=None)
        qr = {"sigma_p_24h": None}
        matched, reason, _ = check_drifting_calm(fv, qr)
        assert matched
        assert "fallback" in reason.lower() or "DRIFTING_CALM" in reason

    def test_fails_high_sigma(self):
        fv = make_fv()
        qr = {"sigma_p_24h": 0.80}  # above 0.50
        matched, _, _ = check_drifting_calm(fv, qr)
        assert not matched

    def test_matches_at_boundary(self):
        fv = make_fv()
        qr = {"sigma_p_24h": 0.50}
        matched, _, _ = check_drifting_calm(fv, qr)
        assert matched  # ≤ 0.50 includes 0.50


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
        # First 719 are cold start
        for r in records[: self.WINDOW - 1]:
            assert r.cold_start is True
        # 720th bar exits cold start
        assert records[-1].cold_start is False
        assert records[-1].state is not None

    def test_stable_bars_yield_drifting_calm(self):
        """Flat, low-vol data → should resolve to Drifting_Calm after warm-up."""
        recognizer = StateRecognizer()
        fvs = _flat_fv_sequence(self.WINDOW + 5)
        records = [recognizer.recognize(fv) for fv in fvs]
        warm_records = [r for r in records if not r.cold_start]
        # All warm records should be drifting calm (constant series → sigma at bottom)
        for r in warm_records:
            assert r.state == StateLabel.DRIFTING_CALM, (
                f"Expected DRIFTING_CALM, got {r.state} ({r.reason})"
            )

    def test_cascade_triggers_on_extreme_bar_721(self):
        """
        Feed 720 bars of normal data, then one bar with extreme sigma_p_24h,
        high abs_delta_p_pct. Cascade should trigger.
        """
        recognizer = StateRecognizer()
        base_time = datetime(2023, 1, 1, tzinfo=timezone.utc)

        # 720 flat bars (use strictly increasing sigma to create quantile spread)
        for i in range(self.WINDOW):
            fv = make_fv(
                time=base_time + timedelta(hours=i),
                sigma_p_24h=0.001 + i * 0.00001,  # tiny increasing trend
                delta_p_pct=0.01,
            )
            recognizer.recognize(fv)

        # Bar 721: extreme values well above 97th percentile
        extreme_fv = make_fv(
            time=base_time + timedelta(hours=self.WINDOW),
            sigma_p_24h=100.0,      # extreme sigma → top of distribution
            delta_p_pct=50.0,       # extreme delta
        )
        record = recognizer.recognize(extreme_fv)
        assert record.cold_start is False
        assert record.state == StateLabel.CASCADE, (
            f"Expected CASCADE, got {record.state} ({record.reason})"
        )

    def test_priority_cascade_over_surging(self):
        """
        When both Cascade and Surging conditions could match,
        Cascade (higher priority) must win.
        """
        recognizer = StateRecognizer()
        base_time = datetime(2023, 1, 1, tzinfo=timezone.utc)

        # Build window with varying data so quantile ranks are meaningful
        for i in range(self.WINDOW):
            fv = make_fv(
                time=base_time + timedelta(hours=i),
                sigma_p_24h=0.001 + i * 0.00001,
                delta_p_pct=float(i % 10) * 0.01,
                price_autocorr_12h=0.5 + (i % 5) * 0.01,
            )
            recognizer.recognize(fv)

        # Extreme bar that satisfies both Cascade and Surging conditions
        extreme_fv = make_fv(
            time=base_time + timedelta(hours=self.WINDOW),
            sigma_p_24h=500.0,   # extreme → Cascade primary
            delta_p_pct=100.0,   # extreme positive → both Cascade secondary + Surging
            price_autocorr_12h=1.0,
        )
        record = recognizer.recognize(extreme_fv)
        # CASCADE must beat SURGING_UP due to priority ordering
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
            records.append(StateRecord(
                time=t,
                symbol="BTCUSDT",
                state=s,
                reason="TEST" if s else "COLD_START",
                feature_quantiles={},
                cold_start=(s is None),
            ))
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
