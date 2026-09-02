"""
CryptoWatch v4 — Backtest Engine with Walk-Forward Optimization (WFO)
Solves v3 D2: gives unbiased Out-of-Sample performance estimates.

WFO Protocol:
  - Train window: 90 days (optimize parameters)
  - Test window:  30 days (OOS evaluation, no optimization)
  - Step size:    30 days (roll forward)
  - Results: concatenated OOS periods = true performance curve
"""

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

from backtest.costs import (
    capacity_capped_notional,
    cost_params_for,
    round_trip_cost_pct,
)

logger = logging.getLogger(__name__)

# WFO default configuration
WFO_TRAIN_DAYS = 90
WFO_TEST_DAYS = 30
WFO_STEP_DAYS = 30
WFO_EMBARGO_DAYS = 2  # purge gap between train end and test start (avoid label spillover)
ATR_STOP_MULT = 2.0
ATR_TAKE_MULT = 3.0
MAX_HOLD_HOURS = 24
MIN_OOS_TRADES = 100  # below this the Sharpe/DD estimates are statistically unreliable
MIN_TRAIN_TRADES = 10  # a param config needs at least this many train trades to be selectable


def default_param_grid() -> list[dict]:
    """The parameter space WFO searches on each train window.

    These are the knobs of the built-in signal/exit rules (see ``default_signal_fn``
    and ``_check_exit``). WFO fits them in-sample per window and evaluates the
    winner OOS — and the grid size is the honest ``n_trials`` for the Deflated
    Sharpe Ratio (item #3).
    """
    grid = []
    for rsi_long in (30.0, 35.0):
        for rsi_short in (65.0, 70.0):
            for atr_stop in (1.5, 2.0):
                for atr_take in (3.0,):
                    grid.append(
                        {
                            "rsi_long": rsi_long,
                            "rsi_short": rsi_short,
                            "rsi_trend": 55.0,
                            "funding_long": -0.05,
                            "funding_short": 0.10,
                            "atr_stop": atr_stop,
                            "atr_take": atr_take,
                        }
                    )
    return grid


@dataclass
class WFOConfig:
    train_days: int = WFO_TRAIN_DAYS
    test_days: int = WFO_TEST_DAYS
    step_days: int = WFO_STEP_DAYS
    embargo_days: int = WFO_EMBARGO_DAYS
    initial_capital: float = 10_000.0
    risk_pct: float = 0.02  # 2% risk per trade
    mc_runs: int = 1000  # bootstrap iterations
    min_oos_trades: int = MIN_OOS_TRADES
    param_grid: list = field(default_factory=default_param_grid)


@dataclass
class TradeRecord:
    symbol: str
    signal_type: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    entry_time: int
    exit_time: int
    exit_reason: str
    slippage_pct: float = 0.0
    fee_pct: float = 0.0004
    funding_cost_pct: float = 0.0  # signed fraction of notional; +ve = cost to the position

    @property
    def pnl_pct(self) -> float:
        gross = (
            (self.exit_price - self.entry_price) / self.entry_price
            if self.side == "LONG"
            else (self.entry_price - self.exit_price) / self.entry_price
        )
        return gross - self.slippage_pct / 100 - self.fee_pct * 2 - self.funding_cost_pct

    @property
    def pnl_usd(self) -> float:
        return self.pnl_pct * self.entry_price * self.quantity


@dataclass
class PeriodMetrics:
    """Metrics for a single WFO test window."""

    period_start: int  # Unix ms
    period_end: int
    n_trades: int
    win_rate: float
    pl_ratio: float
    sharpe: float
    max_drawdown: float
    total_return: float
    is_oos: bool = True  # True = OOS (test), False = IS (train)


@dataclass
class WFOResult:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    symbol: str = ""
    config: WFOConfig = field(default_factory=WFOConfig)
    periods: list = field(default_factory=list)  # list[PeriodMetrics]
    all_trades: list = field(default_factory=list)  # list[TradeRecord]
    selected_params: list = field(default_factory=list)  # winning params per OOS window
    n_trials: int = 1  # param configs searched (for DSR)

    # Aggregated OOS metrics
    oos_win_rate: float = 0.0
    oos_sharpe: float = 0.0
    oos_max_dd: float = 0.0
    oos_total_return: float = 0.0
    oos_calmar: float = 0.0
    oos_n_trades: int = 0

    # Significance / robustness
    psr: float = 0.0  # Probabilistic Sharpe Ratio (vs SR=0)
    dsr: float = 0.0  # Deflated Sharpe Ratio (vs expected max over n_wfo trials)
    mc_sharpe_p5: float = 0.0  # bootstrap 5th percentile annualized Sharpe
    mc_sharpe_p50: float = 0.0
    mc_sharpe_p95: float = 0.0

    # CPCV / probability of backtest overfitting (None when oskill is unavailable).
    # Single-path WFO cannot estimate the OOS path-Sharpe distribution; CPCV does.
    cpcv: Optional[dict] = None
    pbo: Optional[float] = None

    passed: bool = False
    failure_reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "symbol": self.symbol,
            "oos_win_rate": round(self.oos_win_rate, 4),
            "oos_sharpe": round(self.oos_sharpe, 4),
            "oos_max_dd": round(self.oos_max_dd, 4),
            "oos_total_return": round(self.oos_total_return, 4),
            "oos_calmar": round(self.oos_calmar, 4),
            "oos_n_trades": self.oos_n_trades,
            "psr": round(self.psr, 4),
            "dsr": round(self.dsr, 4),
            "mc_sharpe_p5": round(self.mc_sharpe_p5, 4),
            "mc_sharpe_p50": round(self.mc_sharpe_p50, 4),
            "mc_sharpe_p95": round(self.mc_sharpe_p95, 4),
            "cpcv": self.cpcv,
            "pbo": round(self.pbo, 4) if self.pbo is not None else None,
            "passed": self.passed,
            "failure_reasons": self.failure_reasons,
            "n_trials": self.n_trials,
            "selected_params": self.selected_params,
            "n_wfo_periods": len([p for p in self.periods if p.is_oos]),
            "period_details": [
                {
                    "start": p.period_start,
                    "end": p.period_end,
                    "n_trades": p.n_trades,
                    "win_rate": round(p.win_rate, 4),
                    "sharpe": round(p.sharpe, 4),
                    "is_oos": p.is_oos,
                }
                for p in self.periods
            ],
        }


class WFOEngine:
    """
    Walk-Forward Optimization engine.
    Runs signal detection on historical data in rolling windows.
    Reports only OOS (out-of-sample) performance.
    """

    def __init__(self, config: WFOConfig = None, signal_fn=None):
        self.config = config or WFOConfig()
        # signal_fn(closes, highs, lows, price, funding_rate, regime, params) -> list[dict]
        # Defaults to the built-in parameterised rules; inject an adapter here to
        # backtest a different strategy (e.g. a sel_v2 strategy wrapper).
        self.signal_fn = signal_fn or self.default_signal_fn

    async def run(
        self,
        symbol: str,
        candles: list[dict],  # sorted by open_time asc
        funding_rates: list[dict],
    ) -> WFOResult:
        result = WFOResult(symbol=symbol, config=self.config)

        if len(candles) < (self.config.train_days + self.config.test_days) * 24:
            logger.warning(f"{symbol}: insufficient data for WFO")
            return result

        # Index candles by time for fast lookup
        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        times = [c["open_time"] for c in candles]
        fr_map = {d["funding_time"]: d["funding_rate"] for d in funding_rates}

        # Average daily volume in USD (1h candles → ×24) for the cost/capacity model.
        import statistics

        bar_notional = [c.get("volume", 0.0) * c["close"] for c in candles if c.get("volume")]
        adv_usd = statistics.median(bar_notional) * 24 if bar_notional else 0.0

        train_bars = self.config.train_days * 24  # assuming 1h candles
        test_bars = self.config.test_days * 24
        step_bars = self.config.step_days * 24
        embargo_bars = self.config.embargo_days * 24

        all_oos_trades: list[TradeRecord] = []
        periods: list[PeriodMetrics] = []
        selected_params: list[dict] = []

        grid = self.config.param_grid or [default_param_grid()[0]]
        result.n_trials = len(grid)

        # Slide the window. An embargo gap purges bars between train end and test start so
        # that momentum/label spillover from the (in-sample) train window cannot leak into OOS.
        start = 0
        while start + train_bars + embargo_bars + test_bars <= len(candles):
            train_end = start + train_bars
            test_start = train_end + embargo_bars
            test_end = test_start + test_bars

            # ── IN-SAMPLE: fit parameters on the train window ──────────────────
            train_candles = {
                "closes": closes[start:train_end],
                "highs": highs[start:train_end],
                "lows": lows[start:train_end],
                "times": times[start:train_end],
            }
            train_ctx_lo = max(0, start - 200)
            best_params = self._optimize_params(
                symbol,
                train_candles,
                closes[train_ctx_lo:start],
                highs[train_ctx_lo:start],
                lows[train_ctx_lo:start],
                fr_map,
                grid,
                adv_usd,
            )
            selected_params.append(best_params)

            # ── OUT-OF-SAMPLE: evaluate the winning params (after the embargo) ──
            oos_candles = {
                "closes": closes[test_start:test_end],
                "highs": highs[test_start:test_end],
                "lows": lows[test_start:test_end],
                "times": times[test_start:test_end],
            }
            context_closes = closes[max(0, test_start - 200) : test_start]
            context_highs = highs[max(0, test_start - 200) : test_start]
            context_lows = lows[max(0, test_start - 200) : test_start]

            oos_trades = self._simulate_period(
                symbol=symbol,
                candles=oos_candles,
                context_closes=context_closes,
                context_highs=context_highs,
                context_lows=context_lows,
                fr_map=fr_map,
                initial_capital=self.config.initial_capital,
                params=best_params,
                adv_usd=adv_usd,
            )

            if oos_trades:
                all_oos_trades.extend(oos_trades)
                pm = self._calc_period_metrics(oos_trades, times[test_start], times[test_end - 1], True)
                periods.append(pm)

            start += step_bars

        result.periods = periods
        result.all_trades = all_oos_trades
        result.selected_params = selected_params

        if all_oos_trades:
            self._aggregate_metrics(result)
            self._monte_carlo(result)
            # CPCV path-Sharpe distribution + PBO over the full sample (the
            # combinatorial OOS evidence a single WFO replay cannot produce).
            cpcv_params = selected_params[0] if selected_params else grid[0]
            result.cpcv = self._maybe_run_cpcv(symbol, closes, highs, lows, times, fr_map, cpcv_params, adv_usd)
            if result.cpcv:
                result.pbo = result.cpcv.get("pbo")
            self._check_pass(result)

        return result

    def _maybe_run_cpcv(self, symbol, closes, highs, lows, times, fr_map, params, adv_usd) -> Optional[dict]:
        """Run CPCV over the full sample, returning its summary or None. Never
        propagates: a missing oskill or any pipeline mismatch must not break the
        WFO result — CPCV is supplementary evidence."""
        from backtest.cpcv import run_cpcv

        def backtest_fn(train_idx, test_idx):
            idx = sorted(int(i) for i in test_idx)
            if len(idx) < 2:
                return [0.0] * len(idx)
            lo, hi = idx[0], idx[-1] + 1
            span = {
                "closes": closes[lo:hi],
                "highs": highs[lo:hi],
                "lows": lows[lo:hi],
                "times": times[lo:hi],
            }
            ctx_lo = max(0, lo - 200)
            trades = self._simulate_period(
                symbol=symbol,
                candles=span,
                context_closes=closes[ctx_lo:lo],
                context_highs=highs[ctx_lo:lo],
                context_lows=lows[ctx_lo:lo],
                fr_map=fr_map,
                initial_capital=self.config.initial_capital,
                params=params,
                adv_usd=adv_usd,
            )
            # Per-bar return: a trade's net pnl_pct attributed to its exit bar.
            pos_of_time = {times[i]: k for k, i in enumerate(idx)}
            ret = [0.0] * len(idx)
            for t in trades:
                k = pos_of_time.get(t.exit_time)
                if k is not None:
                    ret[k] += t.pnl_pct
            return ret

        try:
            # Bars are hourly and a trade can hold up to MAX_HOLD_HOURS, so its label spans
            # that many bars — purge train samples overlapping the test window by that horizon
            # (audit P1-8), else CPCV leaks future labels and PBO looks too good.
            return run_cpcv(len(closes), backtest_fn, label_horizon=MAX_HOLD_HOURS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CPCV skipped (%s)", exc)
            return None

    def _simulate_period(
        self,
        symbol: str,
        candles: dict,
        context_closes: list,
        context_highs: list,
        context_lows: list,
        fr_map: dict,
        initial_capital: float,
        params: dict,
        adv_usd: float = 0.0,
    ) -> list[TradeRecord]:
        """Simulate trading over one window with a fixed parameter set."""
        from services.signal.regime.detector import RegimeDetector, _calc_atr

        atr_stop = params["atr_stop"]
        atr_take = params["atr_take"]

        closes = context_closes[:] + candles["closes"]
        highs = context_highs[:] + candles["highs"]
        lows = context_lows[:] + candles["lows"]
        times = candles["times"]
        n_ctx = len(context_closes)

        regime_detector = RegimeDetector(symbol)
        # Warm up regime detector on context
        for i in range(len(context_closes)):
            regime_detector.update(
                context_highs[i] if i < len(context_highs) else context_closes[i],
                context_lows[i] if i < len(context_lows) else context_closes[i],
                context_closes[i],
            )

        trades: list[TradeRecord] = []
        open_pos = None
        equity = initial_capital
        cooldowns: dict = {}

        for i, t in enumerate(times):
            ci = n_ctx + i  # index in full array
            if ci < 20:
                continue

            price = closes[ci]
            regime = regime_detector.update(highs[ci], lows[ci], closes[ci])

            # Check open position exits
            if open_pos:
                trade = self._check_exit(open_pos, highs[ci], lows[ci], price, t, ci - open_pos["entry_ci"])
                if trade:
                    from backtest.metrics import funding_cost_pct

                    trade.funding_cost_pct = funding_cost_pct(fr_map, trade.entry_time, trade.exit_time, trade.side)
                    equity += trade.pnl_usd
                    trades.append(trade)
                    open_pos = None

            if open_pos:
                continue  # one position at a time in this simplified version

            # Generate signals
            signals = self.signal_fn(
                closes[: ci + 1],
                highs[: ci + 1],
                lows[: ci + 1],
                price,
                fr_map.get(t, 0.0),
                regime,
                params,
            )

            for sig in signals:
                key = f"{sig['type']}"
                if cooldowns.get(key, 0) > t:
                    continue

                atr = _calc_atr(highs[: ci + 1], lows[: ci + 1], closes[: ci + 1])
                if not atr or atr <= 0:
                    continue

                side = sig["side"]
                stop = price - atr * atr_stop if side == "LONG" else price + atr * atr_stop
                take = price + atr * atr_take if side == "LONG" else price - atr * atr_take

                qty = (equity * self.config.risk_pct) / (atr * atr_stop)
                # Capacity: clamp notional to a share of ADV (item #14).
                notional = qty * price
                capped = capacity_capped_notional(notional, adv_usd)
                if capped < notional and price > 0:
                    qty = capped / price
                    notional = capped
                cost_pct = round_trip_cost_pct(notional, adv_usd, **cost_params_for(symbol))
                open_pos = {
                    "symbol": symbol,
                    "type": sig["type"],
                    "side": side,
                    "entry_price": price,
                    "stop": stop,
                    "take": take,
                    "qty": qty,
                    "entry_time": t,
                    "entry_ci": ci,
                    "cost_pct": cost_pct,
                }
                cooldowns[key] = t + 4 * 3600 * 1000  # 4h cooldown
                break

        return trades

    @staticmethod
    def default_signal_fn(closes, highs, lows, price, funding_rate, regime, params) -> list[dict]:
        """Built-in parameterised rule set. The thresholds in ``params`` are what
        WFO optimises in-sample (see ``default_param_grid``)."""
        from shared.models.signal import Regime

        if len(closes) < 20:
            return []

        # Fast RSI
        deltas = [closes[i] - closes[i - 1] for i in range(-15, 0)]
        gains = [max(d, 0) for d in deltas]
        losses = [max(-d, 0) for d in deltas]
        ag = sum(gains) / 14
        al = sum(losses) / 14
        rsi = 100 - 100 / (1 + ag / al) if al > 0 else 100.0

        signals = []

        # LONG_SETUP (disabled in TRENDING_DOWN / HIGH_VOLATILITY)
        if regime not in (Regime.TRENDING_DOWN, Regime.HIGH_VOLATILITY):
            if funding_rate < params["funding_long"] and rsi < params["rsi_long"]:
                signals.append({"type": "LONG_SETUP", "side": "LONG"})

        # SHORT_SETUP (disabled in TRENDING_UP / ACCUMULATION)
        if regime not in (Regime.TRENDING_UP, Regime.ACCUMULATION):
            if funding_rate > params["funding_short"] and rsi > params["rsi_short"]:
                signals.append({"type": "SHORT_SETUP", "side": "SHORT"})

        # TREND_CONFIRM (only in TRENDING_UP)
        if regime == Regime.TRENDING_UP and rsi > params["rsi_trend"] and abs(funding_rate) < 0.05:
            signals.append({"type": "TREND_CONFIRM", "side": "LONG"})

        return signals

    def _optimize_params(
        self,
        symbol,
        train_candles,
        ctx_closes,
        ctx_highs,
        ctx_lows,
        fr_map,
        grid,
        adv_usd: float = 0.0,
    ) -> dict:
        """Search the grid on the in-sample train window; return the params with the
        best train Sharpe (requiring a minimum trade count). Falls back to the first
        grid entry if nothing clears the bar."""
        from backtest import metrics

        best = None
        best_sharpe = float("-inf")
        for params in grid:
            trades = self._simulate_period(
                symbol=symbol,
                candles=train_candles,
                context_closes=ctx_closes,
                context_highs=ctx_highs,
                context_lows=ctx_lows,
                fr_map=fr_map,
                initial_capital=self.config.initial_capital,
                params=params,
                adv_usd=adv_usd,
            )
            if len(trades) < MIN_TRAIN_TRADES:
                continue
            daily = metrics.daily_returns_from_trades(trades, self.config.initial_capital)
            sharpe = metrics.sharpe_ratio(daily)
            if sharpe > best_sharpe:
                best_sharpe, best = sharpe, params
        return best if best is not None else grid[0]

    def _check_exit(
        self,
        pos: dict,
        high: float,
        low: float,
        close: float,
        current_time: int,
        bars_held: int,
    ) -> Optional[TradeRecord]:
        exit_price = None
        reason = None

        if pos["side"] == "LONG":
            if low <= pos["stop"]:
                exit_price = pos["stop"]
                reason = "STOP"
            elif high >= pos["take"]:
                exit_price = pos["take"]
                reason = "TAKE"
        else:
            if high >= pos["stop"]:
                exit_price = pos["stop"]
                reason = "STOP"
            elif low <= pos["take"]:
                exit_price = pos["take"]
                reason = "TAKE"

        if bars_held >= MAX_HOLD_HOURS:
            exit_price = close
            reason = "TIMEOUT"

        if exit_price and reason:
            # Round-trip execution cost (spread + fee + sqrt-impact) from the cost
            # model, folded into slippage_pct (×100) so pnl_pct nets it out once.
            cost_pct = pos.get("cost_pct", 0.0)
            return TradeRecord(
                symbol=pos["symbol"],
                signal_type=pos["type"],
                side=pos["side"],
                entry_price=pos["entry_price"],
                exit_price=exit_price,
                quantity=pos["qty"],
                entry_time=pos["entry_time"],
                exit_time=current_time,
                exit_reason=reason,
                slippage_pct=cost_pct * 100.0,
                fee_pct=0.0,  # folded into the modelled cost above
            )
        return None

    def _calc_period_metrics(self, trades: list[TradeRecord], t_start: int, t_end: int, is_oos: bool) -> PeriodMetrics:
        from backtest import metrics

        cap = self.config.initial_capital
        pnls = [t.pnl_usd for t in trades]
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = len(wins) / n if n else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 1
        pl_ratio = avg_win / avg_loss if avg_loss > 0 else 0

        # Sharpe from *daily*-aggregated returns, annualized with 365 (crypto, 24/7).
        daily = metrics.daily_returns_from_trades(trades, cap)
        sharpe = metrics.sharpe_ratio(daily)
        max_dd = metrics.max_drawdown_net(trades, cap)

        return PeriodMetrics(
            period_start=t_start,
            period_end=t_end,
            n_trades=n,
            win_rate=win_rate,
            pl_ratio=pl_ratio,
            sharpe=sharpe,
            max_drawdown=max_dd,
            total_return=sum(pnls),
            is_oos=is_oos,
        )

    def _aggregate_metrics(self, result: WFOResult) -> None:
        from backtest import metrics

        trades = result.all_trades
        if not trades:
            return
        cap = self.config.initial_capital
        pnls = [t.pnl_usd for t in trades]
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        result.oos_n_trades = n
        result.oos_win_rate = len(wins) / n if n else 0

        daily = metrics.daily_returns_from_trades(trades, cap)
        result.oos_sharpe = metrics.sharpe_ratio(daily)
        result.oos_max_dd = metrics.max_drawdown_net(trades, cap)
        result.oos_total_return = sum(pnls) / cap
        result.oos_calmar = result.oos_total_return / result.oos_max_dd if result.oos_max_dd > 0 else 0

        # Significance: PSR vs 0, and DSR deflated by the number of parameter
        # configurations actually searched (result.n_trials = grid size). This is
        # the statistically correct trial count for the Deflated Sharpe Ratio.
        result.psr = metrics.probabilistic_sharpe_ratio(daily)
        result.dsr = metrics.deflated_sharpe_ratio(daily, n_trials=max(1, result.n_trials))

    def _monte_carlo(self, result: WFOResult, runs: int = 1000) -> None:
        """Bootstrap CI for annualized Sharpe by resampling daily returns with replacement.

        (Replaces the previous trade-order shuffle, which preserved the return set and so
        left Sharpe almost unchanged — it tested nothing about edge significance.)
        """
        from backtest import metrics

        trades = result.all_trades
        if len(trades) < 10:
            return
        daily = metrics.daily_returns_from_trades(trades, self.config.initial_capital)
        p5, p50, p95 = metrics.bootstrap_sharpe_ci(daily, n_boot=runs)
        result.mc_sharpe_p5, result.mc_sharpe_p50, result.mc_sharpe_p95 = p5, p50, p95

    def _check_pass(self, result: WFOResult) -> None:
        reasons = []
        if result.oos_win_rate < 0.55:
            reasons.append(f"Win rate {result.oos_win_rate:.1%} < 55%")
        if result.oos_sharpe < 1.0:
            reasons.append(f"Sharpe {result.oos_sharpe:.2f} < 1.0")
        if result.oos_max_dd > 0.15:
            reasons.append(f"Max DD {result.oos_max_dd:.1%} > 15%")
        if result.oos_n_trades < self.config.min_oos_trades:
            reasons.append(f"Only {result.oos_n_trades} OOS trades (< {self.config.min_oos_trades})")
        if result.mc_sharpe_p5 < 0.5:
            reasons.append(f"Bootstrap p5 Sharpe {result.mc_sharpe_p5:.2f} < 0.5")
        # PSR (vs SR=0) and DSR (deflated by the number of parameter configs searched,
        # n_trials = grid size) are both significance gates. Now that WFO performs a real
        # in-sample search, the DSR trial count is meaningful and can be gated on.
        if result.psr < 0.90:
            reasons.append(f"PSR {result.psr:.2f} < 0.90")
        if result.dsr < 0.90:
            reasons.append(f"DSR {result.dsr:.2f} < 0.90 (overfit risk)")
        result.failure_reasons = reasons
        result.passed = len(reasons) == 0
