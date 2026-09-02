"""
Shared Hawkes process MLE — used by H1 (Wave 2) and H2 (Wave 3).

Exponential kernel: λ(t) = μ + Σ_{t_i<t} α·exp(-β·(t-t_i))
Branching ratio: η = α/β

H1 (Wave 2): per-event second-scale intensity tracker for Strategy 2
H2 (Wave 3): 4H-bar rolling MLE for Critical state main condition B
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize


@dataclass
class HawkesParams:
    mu: float
    alpha: float
    beta: float

    @property
    def branching_ratio(self) -> float:
        return self.alpha / self.beta if self.beta > 0 else float("nan")


def hawkes_nll(params: np.ndarray, event_times: np.ndarray, T: float) -> float:
    """Negative log-likelihood of exponential Hawkes process."""
    log_mu, log_alpha, log_beta = params
    mu = np.exp(log_mu)
    alpha = np.exp(log_alpha)
    beta = np.exp(log_beta)

    n = len(event_times)
    if n < 2:
        return 1e10

    A = np.zeros(n)
    for i in range(1, n):
        A[i] = np.exp(-beta * (event_times[i] - event_times[i - 1])) * (1.0 + A[i - 1])

    lambdas = mu + alpha * A
    if np.any(lambdas <= 0):
        return 1e10

    log_sum = np.sum(np.log(lambdas))
    integral = mu * T + (alpha / beta) * np.sum(1.0 - np.exp(-beta * (T - event_times)))
    nll = -(log_sum - integral)
    return float(nll) if np.isfinite(nll) else 1e10


def fit_hawkes(
    event_times: np.ndarray,
    T: float,
    n_restarts: int = 3,
) -> dict:
    """
    Fit exponential Hawkes via MLE with multiple random restarts.
    Returns dict with mu, alpha, beta, branching_ratio, converged.
    """
    if len(event_times) < 5:
        return {"converged": False, "branching_ratio": float("nan")}

    best_result = None
    best_nll = np.inf
    rng = np.random.default_rng(42)

    for _ in range(n_restarts):
        log_mu0 = rng.uniform(-5, -1)
        log_alpha0 = rng.uniform(-5, -1)
        log_beta0 = rng.uniform(-3, 1)
        x0 = np.array([log_mu0, log_alpha0, log_beta0])

        try:
            result = minimize(
                hawkes_nll,
                x0,
                args=(event_times, T),
                method="Nelder-Mead",
                options={"maxiter": 2000, "xatol": 1e-6, "fatol": 1e-6},
            )
            if result.fun < best_nll:
                best_nll = result.fun
                best_result = result
        except Exception:
            continue

    if best_result is None:
        return {"converged": False, "branching_ratio": float("nan")}

    mu = np.exp(best_result.x[0])
    alpha = np.exp(best_result.x[1])
    beta = np.exp(best_result.x[2])
    br = alpha / beta

    return {
        "converged": best_result.success,
        "mu": float(mu),
        "alpha": float(alpha),
        "beta": float(beta),
        "branching_ratio": float(br) if np.isfinite(br) else float("nan"),
        "nll": float(best_nll),
    }
