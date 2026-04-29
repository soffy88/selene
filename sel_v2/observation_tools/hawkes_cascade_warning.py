"""
H3 — Hawkes multi-variate Cascade early-warning  (v2.1 §5.3)

Multi-variate Hawkes process on three event streams:
  N_buy(t)   market buy arrivals
  N_sell(t)  market sell arrivals
  N_liq(t)   liquidation arrivals

STUB conditions:
  - liquidation events: v2_liquidations table is empty until collector is live.
    proxy = large negative returns as liquidation signature (coarse).
  - buy/sell: derived from bar-level price direction + volume (coarse proxy).

With current data (4H bars only) the tool runs in "degraded" mode using
bar-level proxies. True multi-variate intensities require tick-level data.

SIGNAL fires when:
  λ_liq_proxy(t) > 95-pct rolling 7-day AND
  buy/sell imbalance > BUY_SELL_IMBALANCE_THRESHOLD  (or < 1/threshold)
  for the current bar

Output label: "CASCADE_WARNING" | "NORMAL"

v2.1 §5.3 Cascade early-warning thresholds (all placeholders):
  - IMBALANCE_THRESHOLD = 5.0  (buy/sell ratio > 5 or < 0.2)
  - WINDOW_BARS = 42           (7 days × 6 bars/day)
"""
from __future__ import annotations

from collections import deque
from typing import Optional

import numpy as np

from sel_v2.observation_tools.base import BarFeatures, ObservationResult, ObservationTool

WINDOW_BARS = 42          # 7-day rolling window for intensity baseline
MIN_BARS = 14             # minimum before percentile is meaningful
LIQ_PCTILE = 0.95         # λ_liq must exceed 95-pct to trigger (v2.1 §5.3)
IMBALANCE_THRESHOLD = 5.0 # buy/sell ratio threshold (v2.1 §5.3 placeholder)


def _bar_to_intensity_proxy(bar: BarFeatures) -> tuple[float, float, float]:
    """
    Extract (λ_buy_proxy, λ_sell_proxy, λ_liq_proxy) from a 4H bar.

    STUB: uses bar-level proxies only, not tick-level event streams.
    - λ_buy / λ_sell: derived from price direction + volume
    - λ_liq:          uses explicit liquidation_usd if available, else
                      infers from large negative returns (< -3σ)
    """
    vol = max(bar.volume, 1e-12)
    ret = bar.log_return

    # Buy/sell split heuristic: positive return → more buys, negative → more sells
    if ret >= 0:
        buy_frac = 0.5 + min(abs(ret) * 20, 0.45)
    else:
        buy_frac = 0.5 - min(abs(ret) * 20, 0.45)
    buy_frac = float(np.clip(buy_frac, 0.05, 0.95))

    lambda_buy = vol * buy_frac
    lambda_sell = vol * (1.0 - buy_frac)

    if bar.liquidation_usd is not None:
        lambda_liq = float(bar.liquidation_usd)
    else:
        # Proxy: treat large-negative bars as liquidation signal
        lambda_liq = float(vol * max(-ret, 0.0) * 10.0)

    return lambda_buy, lambda_sell, lambda_liq


class HawkesCascadeWarning(ObservationTool):
    """
    H3 — multi-variate Hawkes Cascade early-warning  (v2.1 §5.3).

    STUB: operates on 4H bar-level proxies until liquidation collector is
    live and tick-level data is available.  With current data this tool
    will rarely trigger — this is expected behaviour, not a bug.
    """
    tool_id = "H3"

    def __init__(
        self,
        window: int = WINDOW_BARS,
        liq_pctile: float = LIQ_PCTILE,
        imbalance_threshold: float = IMBALANCE_THRESHOLD,
    ) -> None:
        self._window = window
        self._liq_pctile = liq_pctile
        self._imbalance = imbalance_threshold

        self._liq_history: deque[float] = deque(maxlen=window)
        self._buy_history: deque[float] = deque(maxlen=window)
        self._sell_history: deque[float] = deque(maxlen=window)
        self._has_real_liq_data: bool = False

    def is_ready(self) -> bool:
        return len(self._liq_history) >= MIN_BARS

    def update(self, bar: BarFeatures) -> ObservationResult:
        lambda_buy, lambda_sell, lambda_liq = _bar_to_intensity_proxy(bar)

        if bar.liquidation_usd is not None and bar.liquidation_usd > 0:
            self._has_real_liq_data = True

        self._buy_history.append(lambda_buy)
        self._sell_history.append(lambda_sell)
        self._liq_history.append(lambda_liq)

        if not self.is_ready():
            return ObservationResult(
                tool_id=self.tool_id,
                timestamp=bar.timestamp,
                signal=False,
                value=0.0,
                threshold=0.0,
                label="WARMING",
                confidence=0.0,
            )

        liq_arr = np.array(self._liq_history)
        liq_threshold = float(np.quantile(liq_arr, self._liq_pctile))

        # Compute current imbalance
        b = lambda_buy
        s = lambda_sell
        if s < 1e-12:
            imbalance = self._imbalance + 1.0
        else:
            imbalance = b / s

        liq_elevated = lambda_liq > liq_threshold and liq_threshold > 0
        imbalance_extreme = (
            imbalance > self._imbalance or imbalance < (1.0 / self._imbalance)
        )
        signal = liq_elevated and imbalance_extreme

        label = "CASCADE_WARNING" if signal else "NORMAL"
        confidence = 0.0
        if signal:
            liq_excess = (lambda_liq - liq_threshold) / max(liq_threshold, 1e-12)
            confidence = float(np.clip(liq_excess, 0.0, 1.0))

        return ObservationResult(
            tool_id=self.tool_id,
            timestamp=bar.timestamp,
            signal=signal,
            value=float(lambda_liq),
            threshold=float(liq_threshold),
            label=label,
            confidence=confidence,
            metadata={
                "lambda_buy": float(lambda_buy),
                "lambda_sell": float(lambda_sell),
                "lambda_liq": float(lambda_liq),
                "buy_sell_imbalance": float(imbalance),
                "liq_pctile_threshold": float(liq_threshold),
                "has_real_liq_data": self._has_real_liq_data,
                "stub": not self._has_real_liq_data,
            },
        )

    def reset(self) -> None:
        self._liq_history.clear()
        self._buy_history.clear()
        self._sell_history.clear()
        self._has_real_liq_data = False
