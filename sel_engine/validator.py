"""Feature quality validator: missing rate, outlier rate, distribution stats."""
import logging
from dataclasses import dataclass, field, fields
from typing import Optional

import numpy as np

from sel_engine.features.schema import FeatureVector

logger = logging.getLogger(__name__)

NUMERIC_FIELDS = [
    "close", "delta_p_pct", "sigma_p_24h",
    "H", "top_5_bid_size", "top_5_ask_size", "total_depth", "spread_bps",
    "TF", "OI", "funding_rate",
    "LV", "absorption_ratio",
    "price_autocorr_12h", "price_autocorr_24h", "price_autocorr_48h",
    "sigma_p_d2", "H_change_rate_std_12h", "OI_hurst",
]

MISSING_RATE_THRESHOLD = 0.20
ZSCORE_OUTLIER_THRESHOLD = 4.0


@dataclass
class FeatureStats:
    name: str
    count: int = 0
    missing_count: int = 0
    missing_rate: float = 0.0
    outlier_count: int = 0
    outlier_rate: float = 0.0
    mean: Optional[float] = None
    std: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    p1: Optional[float] = None
    p99: Optional[float] = None
    flag_missing: bool = False   # True if missing_rate > threshold


@dataclass
class ValidationReport:
    total_vectors: int = 0
    per_feature: dict = field(default_factory=dict)
    flagged_features: list = field(default_factory=list)


class FeatureValidator:
    def validate(self, vectors: list[FeatureVector]) -> ValidationReport:
        """Run quality checks across a list of FeatureVectors. Returns ValidationReport."""
        report = ValidationReport(total_vectors=len(vectors))
        if not vectors:
            return report

        for fname in NUMERIC_FIELDS:
            vals = []
            missing = 0
            for fv in vectors:
                v = getattr(fv, fname, None)
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    missing += 1
                else:
                    vals.append(float(v))

            stats = FeatureStats(name=fname, count=len(vectors), missing_count=missing)
            stats.missing_rate = missing / len(vectors)
            stats.flag_missing = stats.missing_rate > MISSING_RATE_THRESHOLD

            if vals:
                arr = np.array(vals)
                stats.mean = float(np.mean(arr))
                stats.std = float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0
                stats.min = float(arr.min())
                stats.max = float(arr.max())
                stats.p1 = float(np.percentile(arr, 1))
                stats.p99 = float(np.percentile(arr, 99))

                if stats.std and stats.std > 0:
                    z = np.abs((arr - stats.mean) / stats.std)
                    stats.outlier_count = int((z > ZSCORE_OUTLIER_THRESHOLD).sum())
                    stats.outlier_rate = stats.outlier_count / len(vals)

            report.per_feature[fname] = stats
            if stats.flag_missing:
                report.flagged_features.append(fname)

        return report

    def summary(self, report: ValidationReport) -> str:
        lines = [f"ValidationReport: {report.total_vectors} vectors"]
        for fname, stats in report.per_feature.items():
            flag = " [MISSING>20%]" if stats.flag_missing else ""
            lines.append(
                f"  {fname}: missing={stats.missing_rate:.1%} "
                f"outliers={stats.outlier_rate:.1%} "
                f"mean={stats.mean} std={stats.std}{flag}"
            )
        return "\n".join(lines)
