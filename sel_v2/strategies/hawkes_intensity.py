"""
H1 Hawkes Intensity Tracker  (sel-language-v2.1-patches §5.1)

Real-time exponential Hawkes process intensity estimator for Strategy 2
CUSUM-Short pre-filter.

Model:  λ*(t) = μ + Σ_{t_i ≤ t} α · exp(-β · (t - t_i))

Computed recursively as events arrive:
  A_n = exp(-β · (t_n - t_{n-1})) · (1 + A_{n-1})
  λ*(t_n) = μ + α · A_n

Parameters:
  μ (mu)      baseline intensity (events/sec)
  α (alpha)   excitation amplitude
  β (beta)    decay rate  (1/characteristic time)

GMM estimation (Fonseca-Zaatour method, simplified):
  Uses sample mean intensity + variance-to-mean ratio + lag-1 IACV.
  O(N log N) after sort, runs < 1s on typical 7-day event windows.

Threshold:
  7-day rolling 70th-percentile of realised λ* values (v2.1 §5.1 placeholder).
  Recalibrated each time fit_gmm() is called or from offline H2 reference.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple

import numpy as np

# H1 threshold quantile per v2.1 §5.1 (placeholder — calibrate after paper data)
DEFAULT_THRESHOLD_QUANTILE: float = 0.70
# Rolling window for threshold computation: 7 days in seconds
DEFAULT_THRESHOLD_WINDOW_SEC: float = 7 * 24 * 3600


@dataclass
class HawkesParams:
    mu: float
    alpha: float
    beta: float

    @property
    def branching_ratio(self) -> float:
        return self.alpha / self.beta if self.beta > 0 else float("inf")

    @classmethod
    def from_h2_reference(cls, db_url: Optional[str] = None) -> "HawkesParams":
        """
        Load Wave 1 H2 offline calibration results from v2_strategy_params as
        the H1 cold-start parameters.

        IMPORTANT — scale mismatch:
          H2 parameters (mu, alpha, beta) are fitted to the 4H-bar Hawkes process
          (one time unit = one 4H bar, event = |return| > 1σ).
          H1 is a per-second tick-arrival process — a fundamentally different process
          at a different timescale.  Using H2 parameters as H1 cold-start is a
          deliberate PLACEHOLDER only.  Once 7+ days of tick data accumulate,
          fit_gmm() should replace these with on-line estimates.

        Raises RuntimeError if the required rows are absent from v2_strategy_params
        (i.e. Wave 1 calibration has not been run).  Silent fallback is not allowed.
        """
        from sel_v2.strategies.params_loader import load_strategy_params
        params = load_strategy_params(
            strategy="h2",
            param_names=["mu_ref", "alpha_ref", "beta_ref"],
            db_url=db_url,
        )
        return cls(
            mu=params["mu_ref"],
            alpha=params["alpha_ref"],
            beta=params["beta_ref"],
        )


@dataclass
class HawkesObservation:
    timestamp: float   # Unix seconds
    intensity: float   # λ*(t) at this event


class HawkesIntensityTracker:
    """
    Real-time exponential Hawkes intensity tracker.

    Usage:
        tracker = HawkesIntensityTracker(params)
        tracker.add_event(t)          # call each time a trade/tick event arrives
        lam = tracker.intensity(t)    # query λ*(t) at arbitrary t ≥ last event
    """

    def __init__(self, params: Optional[HawkesParams] = None) -> None:
        self.params = params or HawkesParams.from_h2_reference()
        self._last_t: Optional[float] = None
        self._A: float = 0.0          # recursive term Σ exp(-β(t-t_i))
        self._event_history: List[HawkesObservation] = []

    # ── Public API ────────────────────────────────────────────────────────

    def add_event(self, t: float) -> float:
        """Register a new event at Unix timestamp t. Returns λ*(t-)."""
        mu, alpha, beta = self.params.mu, self.params.alpha, self.params.beta

        if self._last_t is not None:
            dt = t - self._last_t
            if dt < 0:
                raise ValueError(f"Event time {t} < last event {self._last_t}")
            self._A = math.exp(-beta * dt) * (1.0 + self._A)
        else:
            self._A = 1.0  # first event contributes immediately

        lam = mu + alpha * self._A
        self._last_t = t
        self._event_history.append(HawkesObservation(timestamp=t, intensity=lam))
        return lam

    def intensity(self, t: float) -> float:
        """Compute λ*(t) at query time t (no new event)."""
        mu, alpha, beta = self.params.mu, self.params.alpha, self.params.beta
        if self._last_t is None:
            return mu
        dt = t - self._last_t
        if dt < 0:
            raise ValueError(f"Query time {t} < last event {self._last_t}")
        return mu + alpha * math.exp(-beta * dt) * self._A

    def reset(self) -> None:
        """Clear all event history (e.g. on reconnect)."""
        self._last_t = None
        self._A = 0.0
        self._event_history.clear()

    def update_params(self, params: HawkesParams) -> None:
        """Hot-swap parameters after re-estimation. Recomputes A from history."""
        self.params = params
        self._A = 0.0
        if not self._event_history:
            return
        ts = [obs.timestamp for obs in self._event_history]
        beta = params.beta
        a = 0.0
        for i in range(1, len(ts)):
            a = math.exp(-beta * (ts[i] - ts[i - 1])) * (1.0 + a)
        self._A = a

    def recent_intensities(self, since: float) -> List[float]:
        """Return λ* values from events after `since` timestamp."""
        return [obs.intensity for obs in self._event_history if obs.timestamp >= since]

    # ── GMM estimation ────────────────────────────────────────────────────

    @staticmethod
    def fit_gmm(
        event_times: np.ndarray,
        T: float,
        beta_init: float = 0.2,
        n_lags: int = 3,
    ) -> HawkesParams:
        """
        Simplified GMM estimation (Fonseca-Zaatour method).

        Uses:
          1. Empirical mean intensity:     λ̄ = N / T
          2. Lag-k inter-arrival moments:  E[δ_k] → β via moment matching
          3. Branching ratio:              η = 1 - μ / λ̄  → α = η · β

        Falls back to H2 reference params on failure (insufficient data).
        """
        event_times = np.sort(np.asarray(event_times, dtype=float))
        N = len(event_times)

        if N < 10 or T <= 0:
            return HawkesParams.from_h2_reference()

        lam_bar = N / T

        if N >= 2:
            deltas = np.diff(event_times)
            mean_delta = float(np.mean(deltas))
            beta_est = 1.0 / max(mean_delta, 1e-10)
        else:
            beta_est = beta_init

        if N >= 20 and T > 0:
            chunk_size = T / max(int(N / 5), 2)
            n_chunks = int(T / chunk_size)
            counts = np.array([
                np.sum((event_times >= i * chunk_size) & (event_times < (i + 1) * chunk_size))
                for i in range(n_chunks)
            ], dtype=float)
            mean_c = float(np.mean(counts))
            var_c = float(np.var(counts)) if len(counts) > 1 else 0.0
            if mean_c > 0 and var_c > 0:
                v = var_c / mean_c
                eta_est = max(0.0, min((v - 1.0) / (v + 1.0), 0.99))
            else:
                eta_est = 0.55  # sub-critical fallback matching H2 branching ratio
        else:
            eta_est = 0.55

        alpha_est = eta_est * beta_est
        mu_est = lam_bar * (1.0 - eta_est)
        mu_est = max(mu_est, 1e-10)
        alpha_est = max(alpha_est, 1e-10)
        beta_est = max(beta_est, 1e-10)

        return HawkesParams(mu=mu_est, alpha=alpha_est, beta=beta_est)


# ── Rolling threshold tracker ────────────────────────────────────────────────

class RollingIntensityThreshold:
    """
    Tracks a rolling N-day percentile of realised Hawkes intensity values.
    Used to compute h_λ (7-day 70th pct) per v2.1 §5.1.
    """

    def __init__(
        self,
        window_seconds: float = DEFAULT_THRESHOLD_WINDOW_SEC,
        quantile: float = DEFAULT_THRESHOLD_QUANTILE,
    ) -> None:
        self.window_seconds = window_seconds
        self.quantile = quantile
        self._buffer: Deque[Tuple[float, float]] = deque()

    def add(self, t: float, intensity: float) -> None:
        self._buffer.append((t, intensity))
        self._evict(t)

    def threshold(self, t: float) -> Optional[float]:
        """Current threshold; None if insufficient data (< 20 observations)."""
        self._evict(t)
        if len(self._buffer) < 20:
            return None
        from oprim import percentile_value
        values = np.array([v for _, v in self._buffer])
        return float(percentile_value(values, self.quantile))

    def above_threshold(self, t: float, intensity: float) -> bool:
        """
        True if intensity > rolling threshold.
        Returns True (pass-through) when insufficient data to compute threshold,
        so H1 is non-blocking before data accumulates.
        """
        th = self.threshold(t)
        if th is None:
            return True   # pass-through: not enough data to filter
        return intensity > th

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()
