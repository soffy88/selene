"""
backtest/metrics.py — statistically-correct performance metrics for the WFO backtester.

Addresses audit findings on backtest/engine.py:
  - Sharpe annualization was applied to *per-trade* PnL with sqrt(252), which assumes
    one IID observation per "day" and inflates Sharpe several-fold for intraday strategies.
    Here we aggregate per-trade PnL into a *daily* return series first, then annualize.
  - Max drawdown was computed on cumulative PnL starting at 0 (so the `peak > 0` guard
    skipped early losses and understated risk). Here we use the *net equity* curve
    (initial_capital + cumulative PnL).
  - Adds Probabilistic Sharpe Ratio (PSR) and Deflated Sharpe Ratio (DSR) per
    Bailey & López de Prado (2014), and a bootstrap Sharpe confidence interval —
    the legitimate significance test the trade-order-shuffle Monte Carlo never provided.

Crypto trades 24/7, so annualization uses 365 calendar days, not 252.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

TRADING_DAYS_PER_YEAR = 365
EULER_MASCHERONI = 0.5772156649015329


def _phi(x: float) -> float:
    """Standard-normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _phi_inv(p: float) -> float:
    """Standard-normal inverse CDF (Acklam's rational approximation)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


def daily_returns_from_trades(trades, initial_capital: float) -> list[float]:
    """Aggregate per-trade PnL (USD) into a daily return series (fraction of capital).

    Each trade is bucketed by its UTC exit day; daily return = sum(PnL that day)/capital.
    Calendar days between the first and last trading day with no closed trade contribute 0.
    `trades` items must expose `.exit_time` (unix ms) and `.pnl_usd`.
    """
    if not trades or initial_capital <= 0:
        return []
    by_day: dict = {}
    for t in trades:
        day = datetime.fromtimestamp(t.exit_time / 1000, tz=timezone.utc).date()
        by_day[day] = by_day.get(day, 0.0) + t.pnl_usd
    start, end = min(by_day), max(by_day)
    out: list[float] = []
    d = start
    while d <= end:
        out.append(by_day.get(d, 0.0) / initial_capital)
        d += timedelta(days=1)
    return out


def sharpe_ratio(returns, periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Annualized Sharpe of a per-period (e.g. daily) return series. Uses sample std (n-1)."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return mean / std * math.sqrt(periods_per_year)


def max_drawdown_net(trades, initial_capital: float) -> float:
    """Max drawdown of the net equity curve (initial_capital + cumulative PnL). Fraction in [0,1]."""
    equity = peak = float(initial_capital)
    max_dd = 0.0
    for t in trades:
        equity += t.pnl_usd
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd


def _moments(returns):
    n = len(returns)
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / n
    std = math.sqrt(var)
    if std == 0:
        return mean, 0.0, 0.0, 3.0
    skew = (sum((r - mean) ** 3 for r in returns) / n) / std ** 3
    kurt = (sum((r - mean) ** 4 for r in returns) / n) / std ** 4
    return mean, std, skew, kurt


def probabilistic_sharpe_ratio(returns, benchmark_sr: float = 0.0,
                               periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """P(true Sharpe > benchmark) given observed SR, skew, kurtosis and sample size.

    Bailey & López de Prado (2014). `benchmark_sr` is the annualized hurdle Sharpe.
    """
    n = len(returns)
    if n < 3:
        return 0.0
    _, std, skew, kurt = _moments(returns)
    if std == 0:
        return 0.0
    sr_ann = sharpe_ratio(returns, periods_per_year)
    # Convert annualized Sharpe to per-observation for the PSR formula.
    sr = sr_ann / math.sqrt(periods_per_year)
    sr_b = benchmark_sr / math.sqrt(periods_per_year)
    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr ** 2
    if denom <= 0:
        return 0.0
    z = (sr - sr_b) * math.sqrt(n - 1) / math.sqrt(denom)
    return _phi(z)


def deflated_sharpe_ratio(returns, n_trials: int, sr_trials_std: float | None = None,
                          periods_per_year: int = TRADING_DAYS_PER_YEAR) -> float:
    """Deflated Sharpe Ratio: PSR against the expected maximum Sharpe under `n_trials`
    independent backtest configurations. Guards against selection bias / multiple testing.

    `sr_trials_std` is the std of (annualized) Sharpe across the trials; if unknown we fall
    back to the analytic SR sampling std ~ sqrt((1 - skew*SR + (kurt-1)/4*SR^2)/(n-1)).
    """
    n = len(returns)
    if n < 3 or n_trials < 1:
        return 0.0
    sr_ann = sharpe_ratio(returns, periods_per_year)
    sr = sr_ann / math.sqrt(periods_per_year)
    _, _, skew, kurt = _moments(returns)
    if sr_trials_std is None:
        denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr ** 2
        var_sr = max(denom, 1e-12) / (n - 1)
        std_sr_ann = math.sqrt(var_sr) * math.sqrt(periods_per_year)
    else:
        std_sr_ann = sr_trials_std
    if std_sr_ann <= 0:
        return probabilistic_sharpe_ratio(returns, 0.0, periods_per_year)
    # Expected max of N standard normals (Gumbel approximation).
    e_max = ((1 - EULER_MASCHERONI) * _phi_inv(1 - 1.0 / n_trials)
             + EULER_MASCHERONI * _phi_inv(1 - 1.0 / (n_trials * math.e)))
    sr_star_ann = std_sr_ann * e_max
    return probabilistic_sharpe_ratio(returns, sr_star_ann, periods_per_year)


def bootstrap_sharpe_ci(returns, n_boot: int = 1000, seed: int = 42,
                        periods_per_year: int = TRADING_DAYS_PER_YEAR):
    """Bootstrap CI for annualized Sharpe by resampling daily returns with replacement.

    Returns (p5, p50, p95). This is the legitimate edge-significance / stability test that
    the trade-order shuffle (which preserves the return *set* and only permutes order, leaving
    Sharpe almost unchanged) never provided.
    """
    n = len(returns)
    if n < 2:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    sharpes = []
    for _ in range(n_boot):
        sample = [returns[rng.randrange(n)] for _ in range(n)]
        sharpes.append(sharpe_ratio(sample, periods_per_year))
    sharpes.sort()
    return (sharpes[int(0.05 * n_boot)],
            sharpes[int(0.50 * n_boot)],
            sharpes[int(0.95 * n_boot)])


def funding_cost_pct(funding_rates: dict, entry_time_ms: int, exit_time_ms: int,
                     side: str) -> float:
    """Signed funding cost over a holding period, as a fraction of notional.

    `funding_rates` maps funding_time (unix ms) -> rate. Longs pay when the rate is positive;
    shorts receive it. Positive return value = cost to the position.
    """
    if not funding_rates or exit_time_ms <= entry_time_ms:
        return 0.0
    total = 0.0
    for ft, rate in funding_rates.items():
        if entry_time_ms < ft <= exit_time_ms:
            total += rate
    sign = 1.0 if side == "LONG" else -1.0
    return sign * total
