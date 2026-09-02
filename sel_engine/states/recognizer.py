"""
StateRecognizer: applies state conditions in priority order to produce StateRecord.
Also provides compute_state_distribution for quantile history validation.
"""

from sel_engine.features.schema import FeatureVector

from .conditions import (
    check_cascade,
    check_coiling,
    check_critical,
    check_drifting_calm,
    check_drifting_charged,
    check_surging,
)
from .schema import StateLabel, StateNoneReason, StateRecord
from .thresholds import RollingQuantileCalculator

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
    "delta_H",  # doc §4.6 Cond4 Cascade: |ΔH| this bar
    # P1 features (doc §4.1–4.4)
    "oi_change_rate_24h",  # §4.1 Cond3, §4.3 Cond4
    "tf_dp_ratio_24h",  # §4.1 Cond4; WIKI_REQUIRED
    "price_slope_6h",  # §4.2 Cond1 (already abs value)
    "sigma_change_rate_std_6h",  # §4.2 Cond3 stability
    "H_24h_mean",  # §4.3 Cond2, §4.4 Cond2; WIKI_REQUIRED
    "abs_tf_24h_sum",  # §4.3 Cond3, §4.4 Cond3; WIKI_REQUIRED
]


# WARNING: This frozenset MUST be kept in sync with all WIKI-required features.
# When adding a new condition that reads a feature dependent on collector data
# (orderbook / trade_flow / oi_persister), add the feature name here.
# See EC-05 in audit/engineering_concerns.md for the design rationale.
# See test_hard_short_circuit_qr_covers_all_wiki_required_features for enforcement.
_HARD_SHORT_CIRCUIT_QR: frozenset = frozenset(
    {
        "H_24h_mean",  # Coiling §4.1 Cond2, Drifting-Calm §4.3 Cond2, Drifting-Charged §4.4 Cond2
        "abs_tf_24h_sum",  # Drifting-Calm §4.3 Cond3, Drifting-Charged §4.4 Cond3
        "oi_change_rate_24h",  # Coiling §4.1 Cond3, Drifting-Calm §4.3 Cond4
        "tf_dp_ratio_24h",  # Coiling §4.1 Cond4
        "abs_oi_change_rate_24h",  # Drifting-Calm §4.3 Cond4
    }
)


def _none_reason_for_no_match(qr: dict, fv: FeatureVector) -> StateNoneReason:
    """Return MISSING_DATA if any WIKI_REQUIRED feature is absent, else NO_MATCH."""
    if fv.tf_directional_ratio_6h is None:
        return StateNoneReason.MISSING_DATA
    for feat in _HARD_SHORT_CIRCUIT_QR:
        if qr.get(feat) is None:
            return StateNoneReason.MISSING_DATA
    return StateNoneReason.NO_MATCH


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
                none_reason=StateNoneReason.COLD_START,
            )

        # Apply states in priority order (highest priority first)
        checks = [
            (check_cascade, StateLabel.CASCADE, None),
            (check_critical, StateLabel.CRITICAL, None),
            (check_coiling, StateLabel.COILING, None),
            (check_surging, None, None),  # special: direction from reason
            (check_drifting_charged, StateLabel.DRIFTING_CHARGED, None),
            (check_drifting_calm, StateLabel.DRIFTING_CALM, None),
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
                none_reason=StateNoneReason.NOT_APPLICABLE,
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
            none_reason=_none_reason_for_no_match(qr, fv),
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
    cold_start_bars = sum(1 for r in state_records if r.none_reason == StateNoneReason.COLD_START)
    missing_data_bars = sum(1 for r in state_records if r.none_reason == StateNoneReason.MISSING_DATA)
    no_match_bars = sum(1 for r in state_records if r.none_reason == StateNoneReason.NO_MATCH)
    active_bars = total - cold_start_bars - missing_data_bars - no_match_bars

    counts: dict[str, int] = {}
    for label in StateLabel:
        counts[label.value] = 0

    for r in state_records:
        if r.state is not None:
            counts[r.state.value] = counts.get(r.state.value, 0) + 1

    rates: dict[str, float] = {}
    for label_str, n in counts.items():
        rates[label_str] = n / active_bars if active_bars > 0 else 0.0

    return {
        "total_bars": total,
        "cold_start_bars": cold_start_bars,
        "missing_data_bars": missing_data_bars,
        "no_match_bars": no_match_bars,
        "active_bars": active_bars,
        "state_counts": counts,
        "state_rates": rates,
    }
