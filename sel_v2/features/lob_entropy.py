"""LOB entropy variance feature (GL1 T0.1).

``entropy_4h`` (mean Shannon entropy of the LOB per 4H bar) is already collected:
``v2_lob_snapshots.entropy`` is written per-snapshot by the iris md-adapter
(P3 collector consolidation), and ``paper_engine._load_microstructure_series``
already aggregates it into a per-bar mean. What was still STUB
(``states/critical_logic.py`` A2, ``STATE_STUB_BOUNDARIES.md``) is the *trend* of
that series — rolling variance, and whether it's rising — which is what this
module computes.

Granularity note (GL1 D2 default describes a snapshot-level window: "24h /
~1440 快照 at 60s polling"). This module instead computes the rolling variance
over the bar-level ``entropy_4h`` series (window = 6 bars = 24h at the 4H bar
cadence), for two reasons:
  1. Every other rolling-window feature in this state machine (sigma_pctile,
     tda_l1_pctile, oi_change_rate_pctile, funding_pctile, ofi_cumulative_pctile,
     lob_depth_pctile, entropy_pctile itself) is computed over per-bar arrays
     inside BarRunner — introducing a second, snapshot-level rolling-window
     mechanism (with its own persistence/backfill machinery) for this one
     feature alone would be an inconsistent, higher-blast-radius design.
  2. Raw per-snapshot LOB depth (bids/asks beyond top-5) isn't persisted in
     ``v2_lob_snapshots`` post P3-migration (the md-adapter writes '[]'
     placeholders — see collector-consolidation memory); only the already-
     aggregated ``entropy`` column is available, so a snapshot-level variance
     would be computed over the same 60s-cadence entropy series with no extra
     signal, at higher query cost.
  Bar-level variance also has zero cold-start delay relative to entropy_4h
  itself: as soon as ``entropy_4h`` has ``ENTROPY_VARIANCE_MIN_BARS`` finite
  values, entropy_variance (and _rising) become available — no separate
  backfill job needed.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

ENTROPY_VARIANCE_WINDOW_BARS = 6  # 24h at 4H bar cadence (GL1 D2 default window)
ENTROPY_VARIANCE_MIN_BARS = (
    4  # adaptive-window floor (mirrors BarRunner._precompute_rolling_pctile)
)


def rolling_entropy_variance(
    entropy_4h: np.ndarray,
    window: int = ENTROPY_VARIANCE_WINDOW_BARS,
    min_bars: int = ENTROPY_VARIANCE_MIN_BARS,
) -> np.ndarray:
    """Rolling variance of the per-bar mean LOB entropy, trailing `window` bars
    (inclusive of the current bar). NaN until `min_bars` finite observations are
    available in the window."""
    n = len(entropy_4h)
    out = np.full(n, np.nan)
    for i in range(n):
        lo = max(0, i + 1 - window)
        seg = entropy_4h[lo : i + 1]
        valid = seg[np.isfinite(seg)]
        if len(valid) >= min_bars:
            out[i] = float(np.var(valid))
    return out


def is_rising_3bar(series: np.ndarray, i: int) -> Optional[bool]:
    """True if series[i-2] < series[i-1] < series[i] (strictly increasing),
    mirroring the sigma_monotone_3bar / tda_l1_monotone_3bar convention used
    elsewhere in BarRunner. None if fewer than 3 finite observations."""
    if i < 2:
        return None
    a, b, c = series[i - 2], series[i - 1], series[i]
    if not (np.isfinite(a) and np.isfinite(b) and np.isfinite(c)):
        return None
    return bool(a < b < c)
