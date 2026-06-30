"""
BarRunner — per-bar feature computation + state recognition.

Responsible for:
  1. Computing BarFeatures from precomputed series at a given bar index
  2. Calling StateRecognizer.recognize()
  3. Returning the StateRecord

Designed for offline replay; does NOT connect to OKX or any real-time feed.
Real-time integration (Wave 4+) wraps this in an async event loop that fires
on each 4H bar close.

Feature computation strategy:
  - Precompute rolling σ, σ-percentile, Hawkes BR, TDA L^1 once for all bars
  - Per-bar: look up precomputed values by index, check monotone/breakout
  - Wave 5: helixa OI/funding/taker_flow series are optional; NaN → None in BarFeatures
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from sel_v2.states.schema import BarFeatures, StateRecord
from sel_v2.states.recognizer import StateRecognizer

logger = logging.getLogger(__name__)

# Rolling window sizes (§16.1)
_SIGMA_WINDOW = 180       # 30 days × 6 bars/day
_SIGMA_PCTILE_WINDOW = 180
_BREAKOUT_WINDOW = 6      # 24h = 6 × 4H bars
_TDA_PCTILE_WINDOW = 540  # 90 days for percentile baseline

# Helixa-derived rolling windows
_OI_PCTILE_WINDOW = 360   # 60 days
_FUNDING_PCTILE_WINDOW = 360
_OFI_PCTILE_WINDOW = 42   # 7 days
_LOB_DEPTH_PCTILE_WINDOW = 42   # 7 days (schema: "LOB depth rank in 7-day history")
_ENTROPY_PCTILE_WINDOW = 180    # 30 days (schema: "entropy rank in 30-day history")
# Min finite observations before a rolling percentile is emitted (adaptive window, A2):
# lets recently-started feeds (OI/funding/entropy) produce a percentile instead of staying
# null until their full window is filled.
_PCTILE_MIN_BARS = 30
_FUNDING_PERSIST_BARS = 6  # same sign 6 consecutive bars


class BarRunner:
    """
    Orchestrates per-bar feature computation and state recognition.

    Usage (offline replay):
        runner = BarRunner.from_parquet("analysis/data/btc_4h.parquet")
        records = runner.run_all()
    """

    def __init__(
        self,
        closes: np.ndarray,
        timestamps: np.ndarray,
        sigma_series: np.ndarray,          # rolling σ, shape (n,)
        sigma_pctile_series: np.ndarray,   # rolling pctile of σ, shape (n,)
        hawkes_br_series: np.ndarray,      # rolling Hawkes BR, shape (n,)
        tda_l1_series: np.ndarray,         # rolling TDA L^1, shape (n,)
        tda_l1_pctile_series: np.ndarray,  # rolling 95th pctile of TDA, shape (n,)
        hawkes_br_threshold: float = 0.85,
        tda_l1_threshold: float = 0.000097,
        # Wave 5: helixa-derived optional series (all shape (n,), NaN where no data)
        oi_series: Optional[np.ndarray] = None,
        funding_series: Optional[np.ndarray] = None,
        ofi_proxy_series: Optional[np.ndarray] = None,
        lob_depth_series: Optional[np.ndarray] = None,   # total top-of-book depth (bid+ask) per bar
        entropy_series: Optional[np.ndarray] = None,     # LOB Shannon entropy per bar (from v2_lob_snapshots)
    ) -> None:
        self._closes = closes
        self._timestamps = timestamps
        self._sigma = sigma_series
        self._sigma_pctile = sigma_pctile_series
        self._hawkes_br = hawkes_br_series
        self._tda_l1 = tda_l1_series
        self._tda_l1_pctile = tda_l1_pctile_series
        self._hawkes_threshold = hawkes_br_threshold
        self._tda_threshold = tda_l1_threshold
        self._recognizer = StateRecognizer()
        self._n = len(closes)

        # Precompute helixa-derived feature series
        self._oi_raw = oi_series
        self._funding_raw = funding_series
        self._ofi_raw = ofi_proxy_series
        self._oi_change_rate = self._precompute_oi_change_rate(oi_series)
        self._oi_change_rate_pctile = self._precompute_rolling_pctile(
            np.abs(self._oi_change_rate) if self._oi_change_rate is not None else None,
            _OI_PCTILE_WINDOW,
        )
        self._oi_acceleration = self._precompute_oi_acceleration(self._oi_change_rate)
        self._funding_pctile = self._precompute_rolling_pctile(
            np.abs(funding_series) if funding_series is not None else None,
            _FUNDING_PCTILE_WINDOW,
        )
        self._funding_persistent = self._precompute_funding_persistent(funding_series)
        self._ofi_cumulative_pctile = self._precompute_rolling_pctile(
            ofi_proxy_series, _OFI_PCTILE_WINDOW
        )
        # Rolling rank of top-of-book depth: a LOW percentile = a thin/exhausted book,
        # which (with σ extreme) is Cascade condition 1 (§5.7). Previously always None →
        # Cascade cond-1 was permanently unreachable (audit P1-3).
        self._lob_depth_pctile = self._precompute_rolling_pctile(
            lob_depth_series, _LOB_DEPTH_PCTILE_WINDOW
        )
        # LOB entropy is collected (v2_lob_snapshots.entropy) but was never aggregated into a
        # bar feature, so entropy_pctile was 100% null and Coiling (which needs entropy_low)
        # could never confirm (audit follow-up B). Wire it through as a rolling rank.
        self._entropy_raw = entropy_series
        self._entropy_pctile = self._precompute_rolling_pctile(
            entropy_series, _ENTROPY_PCTILE_WINDOW
        )

    # ── Helixa feature precomputation ─────────────────────────────────────────

    def _precompute_oi_change_rate(
        self, oi: Optional[np.ndarray]
    ) -> Optional[np.ndarray]:
        if oi is None:
            return None
        n = len(oi)
        rate = np.full(n, np.nan)
        for i in range(1, n):
            if np.isfinite(oi[i]) and np.isfinite(oi[i - 1]) and oi[i - 1] != 0:
                rate[i] = (oi[i] - oi[i - 1]) / abs(oi[i - 1])
        return rate

    def _precompute_rolling_pctile(
        self, series: Optional[np.ndarray], window: int, min_bars: int = _PCTILE_MIN_BARS
    ) -> Optional[np.ndarray]:
        """Rolling rank of `series[i]` within its trailing `window`.

        Adaptive window (audit follow-up A2): a percentile is produced as soon as there are
        `min_bars` finite observations in the trailing window, rather than waiting for the bar
        index to reach `window`. This matters when a feed (OI/funding/entropy) only started
        recently: with a 360-bar (60-day) window but ~90 bars of real data, the old
        `range(window, n)` never emitted a value, so oi_change_rate_pctile / funding_pctile /
        entropy_pctile were 100% null and Coiling/Drifting-Charged were unreachable. Now the
        last ~(valid−min_bars) bars get a (necessarily thinner) percentile so those states can
        fire on recent data; backfilling the feed history restores the full-window statistics.
        """
        if series is None:
            return None
        n = len(series)
        pctile = np.full(n, np.nan)
        for i in range(1, n):
            lo = max(0, i - window)
            seg = series[lo:i]
            valid = seg[np.isfinite(seg)]
            if len(valid) >= min_bars and np.isfinite(series[i]):
                pctile[i] = float(np.mean(valid <= series[i]))
        return pctile

    def _precompute_oi_acceleration(
        self, rate: Optional[np.ndarray]
    ) -> Optional[np.ndarray]:
        if rate is None:
            return None
        n = len(rate)
        accel = np.full(n, np.nan)
        for i in range(1, n):
            if np.isfinite(rate[i]) and np.isfinite(rate[i - 1]):
                accel[i] = 1.0 if rate[i] > rate[i - 1] else 0.0
        return accel

    def _precompute_funding_persistent(
        self, funding: Optional[np.ndarray]
    ) -> Optional[np.ndarray]:
        if funding is None:
            return None
        n = len(funding)
        persist = np.full(n, np.nan)
        for i in range(_FUNDING_PERSIST_BARS - 1, n):
            window = funding[i - _FUNDING_PERSIST_BARS + 1: i + 1]
            if np.all(np.isfinite(window)):
                signs = np.sign(window)
                persist[i] = 1.0 if np.all(signs == signs[0]) and signs[0] != 0 else 0.0
        return persist

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def from_precomputed(
        cls,
        df: pd.DataFrame,
        sigma_series: np.ndarray,
        sigma_pctile_series: np.ndarray,
        hawkes_br_series: np.ndarray,
        tda_l1_series: np.ndarray,
        tda_l1_pctile_series: np.ndarray,
        hawkes_br_threshold: float = 0.85,
        tda_l1_threshold: float = 0.000097,
        oi_series: Optional[np.ndarray] = None,
        funding_series: Optional[np.ndarray] = None,
        ofi_proxy_series: Optional[np.ndarray] = None,
        lob_depth_series: Optional[np.ndarray] = None,
        entropy_series: Optional[np.ndarray] = None,
    ) -> "BarRunner":
        closes = df["close"].values.astype(float)
        timestamps = df["time"].values
        return cls(
            closes=closes,
            timestamps=timestamps,
            sigma_series=sigma_series,
            sigma_pctile_series=sigma_pctile_series,
            hawkes_br_series=hawkes_br_series,
            tda_l1_series=tda_l1_series,
            tda_l1_pctile_series=tda_l1_pctile_series,
            hawkes_br_threshold=hawkes_br_threshold,
            tda_l1_threshold=tda_l1_threshold,
            oi_series=oi_series,
            funding_series=funding_series,
            ofi_proxy_series=ofi_proxy_series,
            lob_depth_series=lob_depth_series,
            entropy_series=entropy_series,
        )

    def build_features(self, i: int) -> BarFeatures:
        """Build BarFeatures for bar at index i."""
        ts = self._timestamps[i]
        if isinstance(ts, np.datetime64):
            ts = pd.Timestamp(ts).to_pydatetime()
        close = float(self._closes[i])
        log_price = float(np.log(close))

        # Cold start: insufficient data for σ computation
        if i < _SIGMA_WINDOW or not np.isfinite(self._sigma[i]):
            return BarFeatures(
                timestamp=ts,
                bar_index=i,
                close=close,
                log_price=log_price,
                sigma_4h=0.0,
                sigma_pctile=0.5,
                sigma_monotone_3bar=None,
                price_breakout_up=None,
                price_breakout_down=None,
                cold_start=True,
            )

        sigma = float(self._sigma[i])
        sigma_pctile = float(self._sigma_pctile[i]) if np.isfinite(self._sigma_pctile[i]) else 0.5

        # σ monotone 3 bars: bars i-2, i-1, i all have increasing σ
        sigma_monotone: Optional[bool] = None
        if i >= 2 and np.isfinite(self._sigma[i - 2]) and np.isfinite(self._sigma[i - 1]):
            sigma_monotone = bool(self._sigma[i - 2] < self._sigma[i - 1] < self._sigma[i])

        # Price breakout vs last 6 bars (24h)
        price_breakout_up: Optional[bool] = None
        price_breakout_down: Optional[bool] = None
        if i >= _BREAKOUT_WINDOW:
            window_closes = self._closes[i - _BREAKOUT_WINDOW: i]
            price_breakout_up = bool(close > float(np.max(window_closes)))
            price_breakout_down = bool(close < float(np.min(window_closes)))

        # Hawkes BR
        hawkes_br: Optional[float] = None
        br_val = self._hawkes_br[i]
        if np.isfinite(br_val):
            hawkes_br = float(br_val)

        # TDA L^1
        tda_l1: Optional[float] = None
        tda_pctile: Optional[float] = None
        l1_val = self._tda_l1[i]
        if np.isfinite(l1_val):
            tda_l1 = float(l1_val)
        p_val = self._tda_l1_pctile[i]
        if np.isfinite(p_val):
            tda_pctile = float(p_val)

        # TDA L^1 monotone 3 bars
        tda_monotone: Optional[bool] = None
        if tda_l1 is not None and i >= 2:
            l1_prev2 = self._tda_l1[i - 2]
            l1_prev1 = self._tda_l1[i - 1]
            if np.isfinite(l1_prev2) and np.isfinite(l1_prev1):
                tda_monotone = bool(float(l1_prev2) < float(l1_prev1) < tda_l1)

        # ── Helixa-derived features (NaN → None) ──────────────────────────────
        def _f(arr: Optional[np.ndarray]) -> Optional[float]:
            if arr is None:
                return None
            v = arr[i]
            return float(v) if np.isfinite(v) else None

        def _b(arr: Optional[np.ndarray]) -> Optional[bool]:
            if arr is None:
                return None
            v = arr[i]
            return bool(v) if np.isfinite(v) else None

        oi_change_rate = _f(self._oi_change_rate)
        oi_change_rate_pctile = _f(self._oi_change_rate_pctile)
        oi_acceleration: Optional[bool] = _b(self._oi_acceleration)
        funding_rate = _f(self._funding_raw)
        funding_pctile = _f(self._funding_pctile)
        funding_persistent: Optional[bool] = _b(self._funding_persistent)
        ofi_cumulative_pctile = _f(self._ofi_cumulative_pctile)
        lob_depth_pctile = _f(self._lob_depth_pctile)
        entropy_4h = _f(self._entropy_raw)
        entropy_pctile = _f(self._entropy_pctile)

        return BarFeatures(
            timestamp=ts,
            bar_index=i,
            close=close,
            log_price=log_price,
            sigma_4h=sigma,
            sigma_pctile=sigma_pctile,
            sigma_monotone_3bar=sigma_monotone,
            price_breakout_up=price_breakout_up,
            price_breakout_down=price_breakout_down,
            hawkes_br=hawkes_br,
            hawkes_br_threshold=self._hawkes_threshold,
            tda_l1=tda_l1,
            tda_l1_pctile=tda_pctile,
            tda_l1_threshold=self._tda_threshold,
            tda_l1_monotone_3bar=tda_monotone,
            cold_start=False,
            oi_change_rate=oi_change_rate,
            oi_change_rate_pctile=oi_change_rate_pctile,
            oi_acceleration=oi_acceleration,
            funding_rate=funding_rate,
            funding_pctile=funding_pctile,
            funding_persistent=funding_persistent,
            ofi_cumulative_pctile=ofi_cumulative_pctile,
            lob_depth_pctile=lob_depth_pctile,
            entropy_4h=entropy_4h,
            entropy_pctile=entropy_pctile,
        )

    def process_bar(self, i: int) -> StateRecord:
        """Process bar i and return StateRecord."""
        features = self.build_features(i)
        return self._recognizer.recognize(features)

    def run_all(self, start: int = 0, end: Optional[int] = None) -> list[StateRecord]:
        """Run state machine for bars [start, end). Returns all StateRecords."""
        end = end or self._n
        records = []
        for i in range(start, end):
            rec = self.process_bar(i)
            records.append(rec)
            if i % 500 == 0:
                logger.info(
                    "BarRunner: bar %d/%d  state=%s", i, self._n, rec.state.value
                )
        return records
