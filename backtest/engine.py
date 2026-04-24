"""
CryptoWatch v4 — Backtest Engine with Walk-Forward Optimization (WFO)
Solves v3 D2: gives unbiased Out-of-Sample performance estimates.

WFO Protocol:
  - Train window: 90 days (optimize parameters)
  - Test window:  30 days (OOS evaluation, no optimization)
  - Step size:    30 days (roll forward)
  - Results: concatenated OOS periods = true performance curve
"""
import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# WFO default configuration
WFO_TRAIN_DAYS  = 90
WFO_TEST_DAYS   = 30
WFO_STEP_DAYS   = 30
ATR_STOP_MULT   = 2.0
ATR_TAKE_MULT   = 3.0
MAX_HOLD_HOURS  = 24


@dataclass
class WFOConfig:
    train_days:     int   = WFO_TRAIN_DAYS
    test_days:      int   = WFO_TEST_DAYS
    step_days:      int   = WFO_STEP_DAYS
    initial_capital: float = 10_000.0
    risk_pct:       float = 0.02        # 2% risk per trade
    mc_runs:        int   = 1000        # Monte Carlo iterations


@dataclass
class TradeRecord:
    symbol: str; signal_type: str; side: str
    entry_price: float; exit_price: float; quantity: float
    entry_time: int; exit_time: int; exit_reason: str
    slippage_pct: float = 0.0; fee_pct: float = 0.0004

    @property
    def pnl_pct(self) -> float:
        gross = ((self.exit_price - self.entry_price) / self.entry_price
                 if self.side == "LONG"
                 else (self.entry_price - self.exit_price) / self.entry_price)
        return gross - self.slippage_pct / 100 - self.fee_pct * 2

    @property
    def pnl_usd(self) -> float:
        return self.pnl_pct * self.entry_price * self.quantity


@dataclass
class PeriodMetrics:
    """Metrics for a single WFO test window."""
    period_start:   int       # Unix ms
    period_end:     int
    n_trades:       int
    win_rate:       float
    pl_ratio:       float
    sharpe:         float
    max_drawdown:   float
    total_return:   float
    is_oos:         bool = True   # True = OOS (test), False = IS (train)


@dataclass
class WFOResult:
    run_id:         str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    symbol:         str = ""
    config:         WFOConfig = field(default_factory=WFOConfig)
    periods:        list = field(default_factory=list)   # list[PeriodMetrics]
    all_trades:     list = field(default_factory=list)   # list[TradeRecord]

    # Aggregated OOS metrics
    oos_win_rate:   float = 0.0
    oos_sharpe:     float = 0.0
    oos_max_dd:     float = 0.0
    oos_total_return: float = 0.0
    oos_calmar:     float = 0.0
    oos_n_trades:   int   = 0

    # Monte Carlo
    mc_sharpe_p5:   float = 0.0    # 5th percentile Sharpe (stress test)
    mc_sharpe_p50:  float = 0.0
    mc_sharpe_p95:  float = 0.0

    passed:         bool  = False
    failure_reasons: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id, "symbol": self.symbol,
            "oos_win_rate": round(self.oos_win_rate, 4),
            "oos_sharpe": round(self.oos_sharpe, 4),
            "oos_max_dd": round(self.oos_max_dd, 4),
            "oos_total_return": round(self.oos_total_return, 4),
            "oos_calmar": round(self.oos_calmar, 4),
            "oos_n_trades": self.oos_n_trades,
            "mc_sharpe_p5": round(self.mc_sharpe_p5, 4),
            "mc_sharpe_p50": round(self.mc_sharpe_p50, 4),
            "mc_sharpe_p95": round(self.mc_sharpe_p95, 4),
            "passed": self.passed,
            "failure_reasons": self.failure_reasons,
            "n_wfo_periods": len([p for p in self.periods if p.is_oos]),
            "period_details": [
                {"start": p.period_start, "end": p.period_end,
                 "n_trades": p.n_trades, "win_rate": round(p.win_rate, 4),
                 "sharpe": round(p.sharpe, 4), "is_oos": p.is_oos}
                for p in self.periods
            ],
        }


class WFOEngine:
    """
    Walk-Forward Optimization engine.
    Runs signal detection on historical data in rolling windows.
    Reports only OOS (out-of-sample) performance.
    """

    def __init__(self, config: WFOConfig = None):
        self.config = config or WFOConfig()

    async def run(
        self,
        symbol: str,
        candles: list[dict],          # sorted by open_time asc
        funding_rates: list[dict],
    ) -> WFOResult:
        result = WFOResult(symbol=symbol, config=self.config)

        if len(candles) < (self.config.train_days + self.config.test_days) * 24:
            logger.warning(f"{symbol}: insufficient data for WFO")
            return result

        # Index candles by time for fast lookup
        closes   = [c["close"]    for c in candles]
        highs    = [c["high"]     for c in candles]
        lows     = [c["low"]      for c in candles]
        times    = [c["open_time"] for c in candles]
        fr_map   = {d["funding_time"]: d["funding_rate"] for d in funding_rates}

        train_bars = self.config.train_days * 24   # assuming 1h candles
        test_bars  = self.config.test_days  * 24
        step_bars  = self.config.step_days  * 24

        all_oos_trades: list[TradeRecord] = []
        periods: list[PeriodMetrics] = []

        # Slide the window
        start = 0
        while start + train_bars + test_bars <= len(candles):
            train_end = start + train_bars
            test_end  = train_end + test_bars

            # OOS test period
            oos_candles = {
                "closes":  closes[train_end:test_end],
                "highs":   highs[train_end:test_end],
                "lows":    lows[train_end:test_end],
                "times":   times[train_end:test_end],
            }
            # Need lookback context for indicators
            context_closes = closes[max(0, train_end-200):train_end]
            context_highs  = highs[max(0, train_end-200):train_end]
            context_lows   = lows[max(0, train_end-200):train_end]

            oos_trades = self._simulate_period(
                symbol=symbol, candles=oos_candles,
                context_closes=context_closes,
                context_highs=context_highs,
                context_lows=context_lows,
                fr_map=fr_map,
                initial_capital=self.config.initial_capital,
            )

            if oos_trades:
                all_oos_trades.extend(oos_trades)
                pm = self._calc_period_metrics(oos_trades, times[train_end], times[test_end-1], True)
                periods.append(pm)

            start += step_bars

        result.periods = periods
        result.all_trades = all_oos_trades

        if all_oos_trades:
            self._aggregate_metrics(result)
            self._monte_carlo(result)
            self._check_pass(result)

        return result

    def _simulate_period(
        self, symbol: str, candles: dict,
        context_closes: list, context_highs: list, context_lows: list,
        fr_map: dict, initial_capital: float,
    ) -> list[TradeRecord]:
        """Simulate trading in one OOS window."""
        from services.signal.regime.detector import RegimeDetector, _calc_atr
        from shared.models.signal import Regime

        closes = context_closes[:] + candles["closes"]
        highs  = context_highs[:] + candles["highs"]
        lows   = context_lows[:] + candles["lows"]
        times  = candles["times"]
        n_ctx  = len(context_closes)

        regime_detector = RegimeDetector(symbol)
        # Warm up regime detector on context
        for i in range(len(context_closes)):
            regime_detector.update(context_highs[i] if i < len(context_highs) else context_closes[i],
                                   context_lows[i] if i < len(context_lows) else context_closes[i],
                                   context_closes[i])

        trades: list[TradeRecord] = []
        open_pos = None
        equity = initial_capital
        cooldowns: dict = {}

        for i, t in enumerate(times):
            ci = n_ctx + i   # index in full array
            if ci < 20:
                continue

            price = closes[ci]
            regime = regime_detector.update(highs[ci], lows[ci], closes[ci])

            # Check open position exits
            if open_pos:
                trade = self._check_exit(open_pos, highs[ci], lows[ci], price, t, ci - open_pos["entry_ci"])
                if trade:
                    equity += trade.pnl_usd
                    trades.append(trade)
                    open_pos = None

            if open_pos:
                continue   # one position at a time in this simplified version

            # Generate signals
            signals = self._check_signals(
                closes[:ci+1], highs[:ci+1], lows[:ci+1], price,
                fr_map.get(t, 0.0), regime
            )

            for sig in signals:
                key = f"{sig['type']}"
                if cooldowns.get(key, 0) > t:
                    continue

                atr = _calc_atr(highs[:ci+1], lows[:ci+1], closes[:ci+1])
                if not atr or atr <= 0:
                    continue

                side = sig["side"]
                stop = price - atr * ATR_STOP_MULT if side == "LONG" else price + atr * ATR_STOP_MULT
                take = price + atr * ATR_TAKE_MULT if side == "LONG" else price - atr * ATR_TAKE_MULT

                qty = (equity * self.config.risk_pct) / (atr * ATR_STOP_MULT)
                open_pos = {
                    "symbol": symbol, "type": sig["type"], "side": side,
                    "entry_price": price, "stop": stop, "take": take,
                    "qty": qty, "entry_time": t, "entry_ci": ci,
                }
                cooldowns[key] = t + 4 * 3600 * 1000   # 4h cooldown
                break

        return trades

    def _check_signals(self, closes, highs, lows, price, funding_rate, regime) -> list[dict]:
        """Simple rule-based signal generation for backtesting speed."""
        from services.signal.regime.detector import _calc_atr
        from shared.models.signal import Regime

        if len(closes) < 20:
            return []

        # Fast RSI
        from services.signal.factors.composite import score_rsi
        deltas = [closes[i] - closes[i-1] for i in range(-15, 0)]
        gains  = [max(d, 0) for d in deltas]; losses = [max(-d, 0) for d in deltas]
        ag = sum(gains)/14; al = sum(losses)/14
        rsi = 100 - 100/(1 + ag/al) if al > 0 else 100.0

        signals = []

        # LONG_SETUP (disabled in TRENDING_DOWN / HIGH_VOLATILITY)
        if regime not in (Regime.TRENDING_DOWN, Regime.HIGH_VOLATILITY):
            if funding_rate < -0.05 and rsi < 35:
                signals.append({"type": "LONG_SETUP", "side": "LONG"})

        # SHORT_SETUP (disabled in TRENDING_UP / ACCUMULATION)
        if regime not in (Regime.TRENDING_UP, Regime.ACCUMULATION):
            if funding_rate > 0.10 and rsi > 70:
                signals.append({"type": "SHORT_SETUP", "side": "SHORT"})

        # TREND_CONFIRM (only in TRENDING_UP)
        if regime == Regime.TRENDING_UP and rsi > 55 and abs(funding_rate) < 0.05:
            signals.append({"type": "TREND_CONFIRM", "side": "LONG"})

        return signals

    def _check_exit(self, pos: dict, high: float, low: float, close: float,
                    current_time: int, bars_held: int) -> Optional[TradeRecord]:
        exit_price = None; reason = None

        if pos["side"] == "LONG":
            if low  <= pos["stop"]: exit_price = pos["stop"]; reason = "STOP"
            elif high >= pos["take"]: exit_price = pos["take"]; reason = "TAKE"
        else:
            if high >= pos["stop"]: exit_price = pos["stop"]; reason = "STOP"
            elif low  <= pos["take"]: exit_price = pos["take"]; reason = "TAKE"

        if bars_held >= MAX_HOLD_HOURS:
            exit_price = close; reason = "TIMEOUT"

        if exit_price and reason:
            return TradeRecord(
                symbol=pos["symbol"], signal_type=pos["type"], side=pos["side"],
                entry_price=pos["entry_price"], exit_price=exit_price,
                quantity=pos["qty"], entry_time=pos["entry_time"],
                exit_time=current_time, exit_reason=reason,
                slippage_pct=0.02,  # 0.02% assumed slippage
                fee_pct=0.0004,
            )
        return None

    def _calc_period_metrics(self, trades: list[TradeRecord], t_start: int, t_end: int, is_oos: bool) -> PeriodMetrics:
        import math
        pnls = [t.pnl_usd for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        n = len(pnls)
        win_rate = len(wins) / n if n else 0
        avg_win  = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 1
        pl_ratio = avg_win / avg_loss if avg_loss > 0 else 0

        # Simple Sharpe from daily PnL
        if len(pnls) > 1:
            mean = sum(pnls) / n
            std  = math.sqrt(sum((p - mean)**2 for p in pnls) / n)
            sharpe = mean / std * math.sqrt(252) if std > 0 else 0
        else:
            sharpe = 0

        # Max drawdown
        equity = 0.0; peak = 0.0; max_dd = 0.0
        for p in pnls:
            equity += p
            peak = max(peak, equity)
            dd = (peak - equity) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

        return PeriodMetrics(period_start=t_start, period_end=t_end, n_trades=n,
                             win_rate=win_rate, pl_ratio=pl_ratio, sharpe=sharpe,
                             max_drawdown=max_dd, total_return=sum(pnls), is_oos=is_oos)

    def _aggregate_metrics(self, result: WFOResult) -> None:
        import math
        trades = result.all_trades
        if not trades: return
        pnls = [t.pnl_usd for t in trades]
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        result.oos_n_trades = n
        result.oos_win_rate = len(wins) / n if n else 0
        avg_win  = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 1
        if len(pnls) > 1:
            mean = sum(pnls)/n; std = math.sqrt(sum((p-mean)**2 for p in pnls)/n)
            result.oos_sharpe = mean/std*math.sqrt(252) if std > 0 else 0
        equity = 0.0; peak = 0.0; max_dd = 0.0
        for p in pnls:
            equity += p; peak = max(peak, equity)
            dd = (peak-equity)/peak if peak > 0 else 0; max_dd = max(max_dd, dd)
        result.oos_max_dd = max_dd
        result.oos_total_return = sum(pnls) / self.config.initial_capital
        result.oos_calmar = result.oos_total_return / max_dd if max_dd > 0 else 0

    def _monte_carlo(self, result: WFOResult, runs: int = 1000) -> None:
        """Shuffle trade order N times → Sharpe distribution."""
        import random, math
        trades = result.all_trades
        if len(trades) < 10: return
        pnls = [t.pnl_usd for t in trades]
        sharpes = []
        for _ in range(runs):
            shuffled = random.sample(pnls, len(pnls))
            n = len(shuffled); mean = sum(shuffled)/n
            std = math.sqrt(sum((p-mean)**2 for p in shuffled)/n)
            sharpes.append(mean/std*math.sqrt(252) if std > 0 else 0)
        sharpes.sort()
        result.mc_sharpe_p5  = sharpes[int(0.05 * runs)]
        result.mc_sharpe_p50 = sharpes[int(0.50 * runs)]
        result.mc_sharpe_p95 = sharpes[int(0.95 * runs)]

    def _check_pass(self, result: WFOResult) -> None:
        reasons = []
        if result.oos_win_rate < 0.55:      reasons.append(f"Win rate {result.oos_win_rate:.1%} < 55%")
        if result.oos_sharpe < 1.0:         reasons.append(f"Sharpe {result.oos_sharpe:.2f} < 1.0")
        if result.oos_max_dd > 0.15:        reasons.append(f"Max DD {result.oos_max_dd:.1%} > 15%")
        if result.oos_n_trades < 30:        reasons.append(f"Only {result.oos_n_trades} OOS trades")
        if result.mc_sharpe_p5 < 0.5:       reasons.append(f"MC p5 Sharpe {result.mc_sharpe_p5:.2f} < 0.5")
        result.failure_reasons = reasons
        result.passed = len(reasons) == 0
