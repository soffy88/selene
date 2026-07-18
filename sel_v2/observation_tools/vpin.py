"""VPIN (Volume-Synchronized Probability of Informed Trading) — ICT-1 (v2.2 pool).

Streaming Easley-López de Prado-O'Hara calculator over v2_ticks:

  1. volume buckets of V_bucket (30-day avg daily volume / 50, bootstrapped by the
     caller); a trade straddling a boundary is split pro-rata across buckets
  2. per bucket, buy/sell volume — PRIMARY classification uses the exchange-truth
     `side` column (v2_ticks has it); BVC (bulk volume classification, Φ(z) of the
     z-scored per-bucket log price change) is computed in parallel as the
     validation control for venues without a side field
  3. VPIN = Σ|V_buy − V_sell| / Σ V_total over the last 50 buckets
  4. rolling 90/95/97 percentiles, available after a 100-bucket warmup, computed
     over a trailing 30-day window of VPIN points

Deterministic given the same tick stream (restart recovery = replay ticks; no
persisted state). Observation-only: never feeds any strategy decision path.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import datetime, timedelta
from typing import Optional

N_BUCKETS = 50  # VPIN window (candidate-pool definition)
WARMUP_BUCKETS = 100  # percentiles unavailable before this many completed buckets
BVC_Z_WINDOW = 50  # trailing buckets used to z-score the price change
PCTILE_TRAIL_DAYS = 30  # percentile window (trailing, by bucket close time)


@dataclasses.dataclass
class Bucket:
    close_ts: datetime  # timestamp of the tick that completed the bucket
    open_price: float
    close_price: float
    v_buy: float  # side-classified
    v_sell: float
    duration_s: float
    bvc_buy_frac: Optional[float] = None  # filled once the z-window has data

    @property
    def v_total(self) -> float:
        return self.v_buy + self.v_sell


def _phi(z: float) -> float:
    """Standard normal CDF via erf (no scipy needed in the live path)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


class VPINCalculator:
    def __init__(
        self,
        v_bucket: float,
        n_buckets: int = N_BUCKETS,
        warmup_buckets: int = WARMUP_BUCKETS,
    ) -> None:
        if not v_bucket > 0:
            raise ValueError("v_bucket must be positive")
        self.v_bucket = float(v_bucket)
        self.n_buckets = n_buckets
        self.warmup_buckets = warmup_buckets
        self._buckets: list[Bucket] = []  # completed, most recent last
        self._dprices: list[float] = []  # per-bucket log price changes (BVC z-window)
        self._vpin_history: list[tuple[datetime, float]] = []  # (close_ts, vpin)
        # current (in-progress) bucket accumulators
        self._cur_vol = 0.0
        self._cur_buy = 0.0
        self._cur_sell = 0.0
        self._cur_open_price: Optional[float] = None
        self._cur_open_ts: Optional[datetime] = None
        self._completed_total = 0

    # ── ingestion ──────────────────────────────────────────────────────────

    def on_tick(
        self, ts: datetime, price: float, size: float, side: str
    ) -> list[Bucket]:
        """Feed one trade. Returns the list of buckets COMPLETED by this trade
        (empty most of the time; >1 if a single trade spans several buckets)."""
        completed: list[Bucket] = []
        remaining = float(size)
        is_buy = side == "buy"
        while remaining > 0:
            if self._cur_open_price is None:
                self._cur_open_price = float(price)
                self._cur_open_ts = ts
            room = self.v_bucket - self._cur_vol
            fill = min(remaining, room)
            self._cur_vol += fill
            if is_buy:
                self._cur_buy += fill
            else:
                self._cur_sell += fill
            remaining -= fill
            if self._cur_vol >= self.v_bucket:
                completed.append(self._seal_bucket(ts, float(price)))
        return completed

    def _seal_bucket(self, ts: datetime, price: float) -> Bucket:
        dur = (
            (ts - self._cur_open_ts).total_seconds()
            if self._cur_open_ts is not None
            else 0.0
        )
        # BVC price change is bucket-close to bucket-close (standard Easley/LdP/
        # O'Hara), NOT open-to-close within the bucket — a bucket completed by a
        # single large trade would otherwise always have dp = 0.
        prev_close = self._buckets[-1].close_price if self._buckets else None
        dp = (
            math.log(price / prev_close)
            if prev_close and prev_close > 0 and price > 0
            else 0.0
        )
        bucket = Bucket(
            close_ts=ts,
            open_price=float(self._cur_open_price),
            close_price=price,
            v_buy=self._cur_buy,
            v_sell=self._cur_sell,
            duration_s=dur,
        )
        # BVC: Φ(z) of this bucket's log price change against the trailing window
        if len(self._dprices) >= 2:
            arr = self._dprices[-BVC_Z_WINDOW:]
            mean = sum(arr) / len(arr)
            var = sum((x - mean) ** 2 for x in arr) / len(arr)
            std = math.sqrt(var)
            bucket.bvc_buy_frac = _phi((dp - mean) / std) if std > 0 else 0.5
        self._dprices.append(dp)
        self._buckets.append(bucket)
        if len(self._buckets) > max(self.n_buckets, BVC_Z_WINDOW):
            self._buckets.pop(0)
        self._completed_total += 1
        v = self.vpin
        if v is not None:
            self._vpin_history.append((ts, v))
            cutoff = ts - timedelta(days=PCTILE_TRAIL_DAYS)
            while self._vpin_history and self._vpin_history[0][0] < cutoff:
                self._vpin_history.pop(0)
        # reset accumulators (any overspill continues in the caller's while loop)
        self._cur_vol = self._cur_buy = self._cur_sell = 0.0
        self._cur_open_price = None
        self._cur_open_ts = None
        return bucket

    # ── readings ───────────────────────────────────────────────────────────

    @property
    def completed_buckets(self) -> int:
        return self._completed_total

    @property
    def vpin(self) -> Optional[float]:
        """Side-classified VPIN over the last n_buckets buckets; None while the
        window is still filling."""
        if len(self._buckets) < self.n_buckets:
            return None
        window = self._buckets[-self.n_buckets :]
        total = sum(b.v_total for b in window)
        if total <= 0:
            return None
        return sum(abs(b.v_buy - b.v_sell) for b in window) / total

    @property
    def bvc_vpin(self) -> Optional[float]:
        """BVC-classified VPIN over the same window (validation control)."""
        if len(self._buckets) < self.n_buckets:
            return None
        window = [
            b for b in self._buckets[-self.n_buckets :] if b.bvc_buy_frac is not None
        ]
        if len(window) < self.n_buckets:
            return None
        total = sum(b.v_total for b in window)
        if total <= 0:
            return None
        return (
            sum(abs((2.0 * b.bvc_buy_frac - 1.0) * b.v_total) for b in window) / total
        )

    def percentile(self, q: float) -> Optional[float]:
        """Trailing-30d VPIN percentile; None until warmup_buckets completed."""
        if self._completed_total < self.warmup_buckets or not self._vpin_history:
            return None
        vals = sorted(v for _ts, v in self._vpin_history)
        if len(vals) == 1:
            return vals[0]
        pos = (q / 100.0) * (len(vals) - 1)
        lo = int(pos)
        frac = pos - lo
        hi = min(lo + 1, len(vals) - 1)
        return vals[lo] * (1 - frac) + vals[hi] * frac
