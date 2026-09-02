"""
Backtest harness for the REAL sel_v2 strategies (P1-1).

The WFO engine (backtest/engine.py) only ever optimises/validates the built-in
RSI/funding/regime rules (default_signal_fn) — the strategies the system actually
deploys (Strategy 1 trend, Strategy 2 intraday) were never backtested, so they had
zero out-of-sample evidence.

This harness closes that gap by running the deployed PaperStrategyEngine over a
historical bar frame (it already replays the full history deterministically and
produces costed closed positions) and feeding the resulting trade PnL through the
statistically-correct metrics in backtest/metrics.py (daily-bucketed Sharpe, PSR,
DSR, bootstrap CI, net-equity drawdown). Trade PnL is already net of fees, slippage
and funding (sub_account, P0-4), so the metrics reflect realistic net performance.

`oos_start_ms` slices the metrics to an out-of-sample window (e.g. the period after
the calibration window), which is the evidence that must exist before any live
capital decision.

`n_trials` is the number of independent strategy configurations effectively searched
when the deployed strategies were calibrated — the Deflated Sharpe (Bailey & López de
Prado) must deflate for that selection. It defaults NOT to 1 (which would make DSR
collapse to PSR-vs-0 and ignore selection bias entirely — "guilty until proven
innocent" requires the opposite) but to the product of the calibration knobs the
strategies were tuned over (see CALIBRATION_KNOBS). Callers who know their exact
search can override it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from backtest import metrics as M

# Gate thresholds mirror backtest/engine.py's WFO pass criteria so the real
# strategies are held to the same bar as the built-in rules.
MIN_TRADES = 30
MIN_WIN_RATE = 0.50
MIN_SHARPE = 1.0
MAX_DRAWDOWN = 0.15
MIN_DSR = 0.90

# Independent calibration knobs the deployed sel_v2 config was *selected* over. The
# Deflated Sharpe deflates for the number of trials = the product of these candidate
# counts (a config is one point in this grid). A documented, conservative floor — real
# tuning explores at least this much — grounded in the actual code/defaults:
CALIBRATION_KNOBS = {
    "tda_l1_percentile": 3,  # {p90, p95, p97} computed side by side (tda_calibration.py:258-260)
    "cusum_threshold_quantile": 3,  # {0.90, 0.92, 0.95}; DEFAULT_THRESHOLD_QUANTILE=0.95 (cusum_short.py:31), tuned per §28
    "hawkes_br_threshold": 3,  # {0.80, 0.85, 0.90}; schema default 0.85 (schema.py:102)
    "state_entry_sigma_pctile": 3,  # {0.85, 0.90, 0.95} state-machine entry σ-percentile threshold
}


def effective_calibration_trials() -> int:
    """n_trials for the Deflated Sharpe: the number of strategy configurations effectively
    searched during calibration = product of each tunable knob's candidate count. A
    conservative floor (see CALIBRATION_KNOBS); never 1, because the deployed config was
    selected, not handed down."""
    n = 1
    for c in CALIBRATION_KNOBS.values():
        n *= c
    return n


DEFAULT_N_TRIALS = effective_calibration_trials()


@dataclass
class _TradeView:
    """Adapter to the metrics.py trade contract (.exit_time unix-ms, .pnl_usd)."""

    exit_time: int
    pnl_usd: float
    strategy: str
    direction: str
    entry_time_ms: int


def _to_ms(dt) -> int:
    if isinstance(dt, datetime):
        return int(dt.timestamp() * 1000)
    return int(dt)


def collect_trades(engine) -> list[_TradeView]:
    """Flatten both sub-accounts' closed positions into metric-ready trades,
    sorted by exit time (drawdown/equity-curve order)."""
    out: list[_TradeView] = []
    for acct in (engine.accounts.subaccount_1, engine.accounts.subaccount_2):
        for c in acct.closed_positions:
            out.append(
                _TradeView(
                    exit_time=_to_ms(c.exit_time),
                    pnl_usd=c.pnl_usdt,
                    strategy=c.position.strategy,
                    direction=c.position.direction,
                    entry_time_ms=_to_ms(c.position.entry_time),
                )
            )
    out.sort(key=lambda t: t.exit_time)
    return out


def evaluate_trades(trades: list[_TradeView], initial_capital: float, n_trials: int = DEFAULT_N_TRIALS) -> dict:
    """Compute the performance/robustness metric panel for a set of trades."""
    n = len(trades)
    if n == 0:
        return {
            "n_trades": 0,
            "win_rate": 0.0,
            "total_pnl_usd": 0.0,
            "return_pct": 0.0,
            "sharpe": 0.0,
            "psr": 0.0,
            "dsr": 0.0,
            "bootstrap_sharpe_p5": 0.0,
            "bootstrap_sharpe_p50": 0.0,
            "bootstrap_sharpe_p95": 0.0,
            "max_drawdown": 0.0,
            "n_days": 0,
        }
    daily = M.daily_returns_from_trades(trades, initial_capital)
    p5, p50, p95 = M.bootstrap_sharpe_ci(daily)
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    total = sum(t.pnl_usd for t in trades)
    return {
        "n_trades": n,
        "win_rate": wins / n,
        "total_pnl_usd": total,
        "return_pct": total / initial_capital if initial_capital else 0.0,
        "sharpe": M.sharpe_ratio(daily),
        "psr": M.probabilistic_sharpe_ratio(daily),
        "dsr": M.deflated_sharpe_ratio(daily, n_trials=n_trials),
        "bootstrap_sharpe_p5": p5,
        "bootstrap_sharpe_p50": p50,
        "bootstrap_sharpe_p95": p95,
        "max_drawdown": M.max_drawdown_net(trades, initial_capital),
        "n_days": len(daily),
    }


class BacktestRejected(Exception):
    """Raised when a strategy fails (or lacks) out-of-sample validation. Makes the
    pass/fail verdict *binding*: code that gates a deploy on validation must either pass
    the gate or handle this — the verdict can no longer be computed and silently ignored."""


def enforce_oos_gate(result: dict) -> dict:
    """Guilty-until-proven-innocent enforcement (audit P0-5).

    A backtest verdict is worthless if nothing consumes it. Call this anywhere a strategy
    would be promoted toward live: it RAISES BacktestRejected unless the result carries an
    out-of-sample slice that PASSED the gate. Returns the result unchanged on pass so it
    can be used inline. Absent OOS (`run_v2_backtest` called without oos_start_ms) is a
    rejection, not a pass — no evidence is not innocence."""
    if "oos" not in result or "oos_passed" not in result:
        raise BacktestRejected(
            "no out-of-sample evaluation present — run run_v2_backtest(..., oos_start_ms=...) "
            "to produce binding OOS evidence"
        )
    if not result["oos_passed"]:
        raise BacktestRejected("out-of-sample validation FAILED: " + "; ".join(result.get("oos_fail_reasons", [])))
    return result


def check_pass(metrics: dict) -> tuple[bool, list[str]]:
    """Apply the WFO-parity pass gate. Returns (passed, failed_reasons)."""
    fails = []
    if metrics["n_trades"] < MIN_TRADES:
        fails.append(f"n_trades {metrics['n_trades']} < {MIN_TRADES}")
    if metrics["win_rate"] < MIN_WIN_RATE:
        fails.append(f"win_rate {metrics['win_rate']:.2f} < {MIN_WIN_RATE}")
    if metrics["sharpe"] < MIN_SHARPE:
        fails.append(f"sharpe {metrics['sharpe']:.2f} < {MIN_SHARPE}")
    if metrics["max_drawdown"] > MAX_DRAWDOWN:
        fails.append(f"max_drawdown {metrics['max_drawdown']:.2f} > {MAX_DRAWDOWN}")
    if metrics["dsr"] < MIN_DSR:
        fails.append(f"dsr {metrics['dsr']:.2f} < {MIN_DSR}")
    return (not fails), fails


def run_v2_backtest(
    df,
    *,
    initial_capital: float = 100_000.0,
    oi_series=None,
    funding_series=None,
    ofi_series=None,
    tick_times=None,
    micro=None,
    oos_start_ms: Optional[int] = None,
    hawkes_params=None,
    skip_hawkes: bool = False,
    skip_tda: bool = False,
    instrument: str = "BTC-USDT",
    n_trials: int = DEFAULT_N_TRIALS,
) -> dict:
    """Run the deployed sel_v2 engine over `df` and evaluate its trades.

    Returns a dict with overall metrics ('all'), per-strategy breakdown, the raw
    engine summary, and — when oos_start_ms is given — an out-of-sample ('oos')
    slice plus its pass/fail gate.
    """
    from sel_v2.paper.strategy_engine import PaperStrategyEngine

    engine = PaperStrategyEngine(
        total_nav_usdt=initial_capital,
        instrument=instrument,
        skip_hawkes=skip_hawkes,
        skip_tda=skip_tda,
        hawkes_params=hawkes_params,
    )
    summary = engine.process_frame(
        df,
        oi_series=oi_series,
        funding_series=funding_series,
        ofi_series=ofi_series,
        tick_times=tick_times,
        micro=micro,
    )

    trades = collect_trades(engine)
    result = {
        "engine_summary": summary,
        "initial_capital": initial_capital,
        "all": evaluate_trades(trades, initial_capital, n_trials),
        "by_strategy": {
            "strategy_1": evaluate_trades([t for t in trades if t.strategy == "strategy_1"], initial_capital, n_trials),
            "strategy_2": evaluate_trades([t for t in trades if t.strategy == "strategy_2"], initial_capital, n_trials),
        },
    }
    if oos_start_ms is not None:
        oos = evaluate_trades([t for t in trades if t.exit_time >= oos_start_ms], initial_capital, n_trials)
        passed, reasons = check_pass(oos)
        result["oos"] = oos
        result["oos_passed"] = passed
        result["oos_fail_reasons"] = reasons
    return result
