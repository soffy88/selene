"""
B1 — Bayesian HMM regime classifier  (v2.1 §8.1)

2-state Gaussian HMM on 4H log-returns.  Intended implementation: pymc/numpyro
Bayesian HMM.  Downgrade applied: custom frequentist EM (Baum-Welch) because
neither pymc nor hmmlearn is available in this environment.

STUB: model is not retrained online — parameters are re-estimated from the
rolling window of the last WINDOW_BARS bars each update call (expensive but
correct for paper-trading latency tolerance).

States:
  0 = LOW_VOL  (calm / drifting regime)
  1 = HIGH_VOL (volatile / trend / pre-cascade regime)

Output label: "LOW_VOL" | "HIGH_VOL"
"""

from __future__ import annotations

from collections import deque

import numpy as np

from sel_v2.observation_tools.base import BarFeatures, ObservationResult, ObservationTool

WINDOW_BARS = 60  # 60 × 4H = 10 days of history
MIN_BARS = 20  # minimum before HMM is meaningful
MAX_ITER = 50  # Baum-Welch EM iterations
TOL = 1e-4  # convergence tolerance


class _GaussianHMM:
    """
    Two-state Gaussian HMM fitted via Baum-Welch EM.

    Parameters
    ----------
    n_iter : max EM iterations
    tol    : log-likelihood convergence tolerance
    """

    def __init__(self, n_iter: int = MAX_ITER, tol: float = TOL) -> None:
        self.n_iter = n_iter
        self.tol = tol
        self.n_states = 2

        # Initial parameters (will be re-estimated)
        self._pi = np.array([0.5, 0.5])
        self._A = np.array([[0.95, 0.05], [0.10, 0.90]])
        self._mu = np.array([0.0, 0.0])
        self._sigma = np.array([0.01, 0.03])

    # ── Gaussian emission density ──────────────────────────────────────────

    def _emit(self, x: np.ndarray) -> np.ndarray:
        """(T, K) emission probabilities for each state."""
        T = len(x)
        B = np.empty((T, self.n_states))
        for k in range(self.n_states):
            std = max(self._sigma[k], 1e-8)
            B[:, k] = np.exp(-0.5 * ((x - self._mu[k]) / std) ** 2) / (std * np.sqrt(2 * np.pi))
        return np.clip(B, 1e-300, None)

    # ── Forward-Backward ──────────────────────────────────────────────────

    def _forward(self, B: np.ndarray):
        T = len(B)
        alpha = np.zeros((T, self.n_states))
        scale = np.zeros(T)
        alpha[0] = self._pi * B[0]
        scale[0] = alpha[0].sum()
        alpha[0] /= max(scale[0], 1e-300)
        for t in range(1, T):
            alpha[t] = (alpha[t - 1] @ self._A) * B[t]
            scale[t] = alpha[t].sum()
            alpha[t] /= max(scale[t], 1e-300)
        return alpha, scale

    def _backward(self, B: np.ndarray, scale: np.ndarray):
        T = len(B)
        beta = np.zeros((T, self.n_states))
        beta[-1] = 1.0
        for t in range(T - 2, -1, -1):
            beta[t] = (self._A * B[t + 1] * beta[t + 1]).sum(axis=1)
            beta[t] /= max(scale[t + 1], 1e-300)
        return beta

    # ── EM fit ────────────────────────────────────────────────────────────

    def fit(self, x: np.ndarray) -> None:
        """Fit HMM parameters to observation sequence x via Baum-Welch."""
        # Initialise with k-means-style split on variance
        med = np.median(np.abs(x))
        self._mu = np.array([-med * 0.5, med * 0.5])
        self._sigma = np.array([np.std(x[np.abs(x) <= med]) + 1e-8, np.std(x[np.abs(x) > med]) + 1e-8])

        prev_ll = -np.inf
        for _ in range(self.n_iter):
            B = self._emit(x)
            alpha, scale = self._forward(B)
            beta = self._backward(B, scale)

            gamma = alpha * beta
            gamma /= gamma.sum(axis=1, keepdims=True) + 1e-300

            T = len(x)
            xi_sum = np.zeros((self.n_states, self.n_states))
            for t in range(T - 1):
                xi_t = alpha[t, :, None] * self._A * B[t + 1] * beta[t + 1]
                xi_t /= max(xi_t.sum(), 1e-300)
                xi_sum += xi_t

            # Update parameters
            self._pi = gamma[0] / max(gamma[0].sum(), 1e-300)
            self._pi /= self._pi.sum()
            row_sum = xi_sum.sum(axis=1, keepdims=True) + 1e-300
            self._A = xi_sum / row_sum

            for k in range(self.n_states):
                w = gamma[:, k]
                w_sum = w.sum() + 1e-300
                self._mu[k] = (w * x).sum() / w_sum
                self._sigma[k] = np.sqrt((w * (x - self._mu[k]) ** 2).sum() / w_sum) + 1e-8

            ll = np.sum(np.log(scale + 1e-300))
            if abs(ll - prev_ll) < self.tol:
                break
            prev_ll = ll

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        """Return (T, 2) smoothed state posteriors via forward-backward."""
        B = self._emit(x)
        alpha, scale = self._forward(B)
        beta = self._backward(B, scale)
        gamma = alpha * beta
        gamma /= gamma.sum(axis=1, keepdims=True) + 1e-300
        return gamma

    def predict(self, x: np.ndarray) -> np.ndarray:
        """Viterbi decoding — returns (T,) integer state sequence."""
        B = self._emit(x)
        T = len(x)
        viterbi = np.zeros((T, self.n_states))
        psi = np.zeros((T, self.n_states), dtype=int)
        viterbi[0] = np.log(self._pi + 1e-300) + np.log(B[0] + 1e-300)
        log_A = np.log(self._A + 1e-300)
        for t in range(1, T):
            trans = viterbi[t - 1, :, None] + log_A
            psi[t] = trans.argmax(axis=0)
            viterbi[t] = trans.max(axis=0) + np.log(B[t] + 1e-300)
        states = np.empty(T, dtype=int)
        states[-1] = viterbi[-1].argmax()
        for t in range(T - 2, -1, -1):
            states[t] = psi[t + 1, states[t + 1]]
        return states


def _canonical_high_vol_state(hmm: _GaussianHMM) -> int:
    """State index with higher absolute mean deviation (HIGH_VOL state)."""
    return int(np.argmax(np.abs(hmm._sigma)))


class BayesianHMM(ObservationTool):
    """
    B1 — rolling 2-state Gaussian HMM  (v2.1 §8.1).

    Classifies the current 4H bar as LOW_VOL or HIGH_VOL and returns the
    posterior probability of the HIGH_VOL state.

    STUB: pymc / hmmlearn unavailable → custom EM Baum-Welch.
    """

    tool_id = "B1"

    def __init__(self, window: int = WINDOW_BARS, high_vol_threshold: float = 0.60) -> None:
        self._window = window
        self._high_vol_threshold = high_vol_threshold
        self._buffer: deque[float] = deque(maxlen=window)
        self._last_proba: float = 0.0

    def is_ready(self) -> bool:
        return len(self._buffer) >= MIN_BARS

    def update(self, bar: BarFeatures) -> ObservationResult:
        self._buffer.append(bar.log_return)
        if not self.is_ready():
            return ObservationResult(
                tool_id=self.tool_id,
                timestamp=bar.timestamp,
                signal=False,
                value=0.0,
                threshold=self._high_vol_threshold,
                label="WARMING",
                confidence=0.0,
            )
        x = np.array(self._buffer)
        hmm = _GaussianHMM()
        hmm.fit(x)
        posteriors = hmm.predict_proba(x)
        high_state = _canonical_high_vol_state(hmm)
        p_high_raw = float(posteriors[-1, high_state])
        # Guard against NaN from degenerate (constant) input
        p_high = 0.5 if np.isnan(p_high_raw) else float(np.clip(p_high_raw, 0.0, 1.0))
        self._last_proba = p_high

        is_high = p_high >= self._high_vol_threshold
        label = "HIGH_VOL" if is_high else "LOW_VOL"
        conf = p_high if is_high else (1.0 - p_high)
        return ObservationResult(
            tool_id=self.tool_id,
            timestamp=bar.timestamp,
            signal=is_high,
            value=p_high,
            threshold=self._high_vol_threshold,
            label=label,
            confidence=float(np.clip(conf, 0.0, 1.0)),
            metadata={
                "mu_low": float(np.nan_to_num(hmm._mu[1 - high_state])),
                "mu_high": float(np.nan_to_num(hmm._mu[high_state])),
                "sigma_low": float(np.nan_to_num(hmm._sigma[1 - high_state])),
                "sigma_high": float(np.nan_to_num(hmm._sigma[high_state])),
            },
        )

    def reset(self) -> None:
        self._buffer.clear()
        self._last_proba = 0.0

    @property
    def last_high_vol_proba(self) -> float:
        return self._last_proba
