"""
StateRecognizer: applies state conditions in priority order to produce StateRecord.
Also provides compute_state_distribution for quantile history validation.
"""
from datetime import datetime
from typing import Optional

from sel_engine.features.schema import FeatureVector
from .schema import StateLabel, StateRecord
from .thresholds import RollingQuantileCalculator
from .conditions import (
    check_cascade,
    check_critical,
    check_coiling,
    check_surging,
    check_drifting_charged,
    check_drifting_calm,
)

# All feature names whose raw values enter the rolling quantile windows
QUANTILE_FEATURES = [
    "close",
    "sigma_p_24h",
    "delta_p_pct",
    "LV",
    "TF",
    "sigma_p_d2",
    "OI_hurst",
    "price_autocorr_24h",
    "H",
    "price_autocorr_12h",
    "OI",
    "funding_rate",
    "H_change_rate_std_12h",  # doc §4.5 Cond3 Critical
    "delta_H",                 # doc §4.6 Cond4 Cascade: |ΔH| this bar
    # P1 features (doc §4.1–4.4)
    "oi_change_rate_24h",      # §4.1 Cond3, §4.3 Cond4
    "tf_dp_ratio_24h",         # §4.1 Cond4; WIKI_REQUIRED
    "price_slope_6h",          # §4.2 Cond1 (already abs value)
    "sigma_change_rate_std_6h",  # §4.2 Cond3 stability
    "H_24h_mean",              # §4.3 Cond2, §4.4 Cond2; WIKI_REQUIRED
    "abs_tf_24h_sum",          # §4.3 Cond3, §4.4 Cond3; WIKI_REQUIRED
]


class StateRecognizer:
    """
    Identifies the current sel market state from a FeatureVector.
    Maintains rolling quantile windows internally.
    """

    def __init__(self):
        self.calc = RollingQuantileCalculator()
        self._bar_count = 0

    def recognize(self, fv: FeatureVector) -> StateRecord:
        self._bar_count += 1

        # Compute quantile ranks BEFORE adding current bar to windows (strictly causal)
        qr = self._compute_quantile_ranks(fv)

        # Update windows with current values AFTER computing ranks
        self._update_windows(fv)

        # Cold start: not enough history for reliable quantile estimates
        if self._bar_count < RollingQuantileCalculator.WINDOW:
            return StateRecord(
                time=fv.time,
                symbol=fv.symbol,
                state=None,
                reason="COLD_START",
                feature_quantiles=qr,
                feature_vector=fv,
                cold_start=True,
            )

        # Apply states in priority order (highest priority first)
        checks = [
            (check_cascade,          StateLabel.CASCADE,          None),
            (check_critical,         StateLabel.CRITICAL,         None),
            (check_coiling,          StateLabel.COILING,          None),
            (check_surging,          None,                        None),   # special: direction from reason
            (check_drifting_charged, StateLabel.DRIFTING_CHARGED, None),
            (check_drifting_calm,    StateLabel.DRIFTING_CALM,    None),
        ]

        for check_fn, label, _ in checks:
            matched, reason, used = check_fn(fv, qr)
            if not matched:
                continue

            # Surging needs direction resolution
            if check_fn is check_surging:
                if "SURGING_UP" in reason:
                    label = StateLabel.SURGING_UP
                else:
                    label = StateLabel.SURGING_DOWN

            return StateRecord(
                time=fv.time,
                symbol=fv.symbol,
                state=label,
                reason=reason,
                feature_quantiles=used,
                feature_vector=fv,
                cold_start=False,
            )

        # No state matched — expected when WIKI_REQUIRED data (H, TF, OI) is not yet available.
        # Returns state=None with cold_start=False; decision engine maps this to NO_ACTION.
        return StateRecord(
            time=fv.time,
            symbol=fv.symbol,
            state=None,
            reason="NO_STATE_MATCHED",
            feature_quantiles=qr,
            feature_vector=fv,
            cold_start=False,
        )

    def _compute_quantile_ranks(self, fv: FeatureVector) -> dict:
        """Compute quantile ranks for all relevant features (current bar not yet in window)."""
        qr: dict = {}

        for name in QUANTILE_FEATURES:
            val = getattr(fv, name, None)
            qr[name] = self.calc.quantile_rank(name, val)

        # Derived absolute-value features (need separate window keys)
        qr["abs_delta_p_pct"] = self.calc.quantile_rank(
            "abs_delta_p_pct",
            abs(fv.delta_p_pct) if fv.delta_p_pct is not None else None,
        )
        qr["abs_TF"] = self.calc.quantile_rank(
            "abs_TF",
            abs(fv.TF) if fv.TF is not None else None,
        )
        qr["abs_funding_rate"] = self.calc.quantile_rank(
            "abs_funding_rate",
            abs(fv.funding_rate) if fv.funding_rate is not None else None,
        )
        # |OI 24H change rate| for Drifting-Calm Cond4 (doc §4.3)
        qr["abs_oi_change_rate_24h"] = self.calc.quantile_rank(
            "abs_oi_change_rate_24h",
            abs(fv.oi_change_rate_24h) if fv.oi_change_rate_24h is not None else None,
        )

        return qr

    def _update_windows(self, fv: FeatureVector) -> None:
        """Update rolling windows with current bar values (called AFTER quantile_rank)."""
        for name in QUANTILE_FEATURES:
            self.calc.add(name, getattr(fv, name, None))

        self.calc.add(
            "abs_delta_p_pct",
            abs(fv.delta_p_pct) if fv.delta_p_pct is not None else None,
        )
        self.calc.add(
            "abs_TF",
            abs(fv.TF) if fv.TF is not None else None,
        )
        self.calc.add(
            "abs_funding_rate",
            abs(fv.funding_rate) if fv.funding_rate is not None else None,
        )
        self.calc.add(
            "abs_oi_change_rate_24h",
            abs(fv.oi_change_rate_24h) if fv.oi_change_rate_24h is not None else None,
        )


def compute_state_distribution(state_records: list[StateRecord]) -> dict:
    """
    Returns summary statistics over a list of StateRecord objects.
    Useful for verifying quantile history and state frequency.
    """
    total = len(state_records)
    cold = sum(1 for r in state_records if r.cold_start or r.state is None)
    non_cold = total - cold

    counts: dict[str, int] = {}
    for label in StateLabel:
        counts[label.value] = 0

    for r in state_records:
        if r.state is not None:
            counts[r.state.value] = counts.get(r.state.value, 0) + 1

    rates: dict[str, float] = {}
    for label_str, n in counts.items():
        rates[label_str] = n / non_cold if non_cold > 0 else 0.0

    return {
        "total_bars": total,
        "cold_start_bars": cold,
        "state_counts": counts,
        "state_rates": rates,
    }
