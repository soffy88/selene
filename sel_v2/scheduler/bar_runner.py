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
  - STUB features (LOB/OI/funding) remain None throughout Wave 3
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
