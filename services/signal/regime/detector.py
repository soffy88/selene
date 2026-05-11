"""
CryptoWatch v4 — Regime Detector
Identifies 5 market regimes using ADX, ATR ratio, EMA alignment.
This is the key v4 upgrade: same signal in different regimes has
very different win rates.
"""
import logging
import math
from collections import deque
from typing import Optional

from shared.models.signal import Regime

logger = logging.getLogger(__name__)

# Regime-specific parameters (from ADR table in v4 spec)
REGIME_PARAMS = {
    Regime.TRENDING_UP:    {"atr_mult": 2.5, "kelly": 0.6, "max_lev": 2.5, "max_hold_h": 48},
    Regime.TRENDING_DOWN:  {"atr_mult": 2.5, "kelly": 0.6, "max_lev": 2.5, "max_hold_h": 48},
    Regime.RANGING:        {"atr_mult": 1.5, "kelly": 0.4, "max_lev": 1.5, "max_hold_h": 24},
    Regime.HIGH_VOLATILITY:{"atr_mult": 3.0, "kelly": 0.3, "max_lev": 1.0, "max_hold_h": 8},
    Regime.ACCUMULATION:   {"atr_mult": 2.0, "kelly": 0.5, "max_lev": 2.0, "max_hold_h": 72},
    Regime.UNKNOWN:        {"atr_mult": 2.0, "kelly": 0.4, "max_lev": 1.5, "max_hold_h": 24},
}

BUFFER = 50   # candles needed for regime calculation


class RegimeDetector:
    """
    Per-symbol regime detector. Maintains a rolling window of OHLCV data
    and recomputes regime on every new closed candle.

    Regime detection logic:
    1. Compute ADX(14) — trend strength
    2. Compute ATR(14) relative to 30d average ATR (volatility state)
    3. Check EMA20/50/200 alignment (trend direction)
    4. Apply state machine with hysteresis (avoid rapid switching)
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self._closes: deque = deque(maxlen=250)
        self._highs:  deque = deque(maxlen=250)
        self._lows:   deque = deque(maxlen=250)

        self._regime: Regime = Regime.UNKNOWN
        self._regime_bars: int = 0    # how many bars in current regime
        self._min_bars_before_switch = 3  # hysteresis: need 3 bars to confirm switch

    def update(self, high: float, low: float, close: float) -> Regime:
        """Feed a new closed candle. Returns current regime."""
        self._closes.append(close)
        self._highs.append(high)
        self._lows.append(low)

        if len(self._closes) < BUFFER:
            return Regime.UNKNOWN

        new_regime = self._classify()
        if new_regime != self._regime:
            self._regime_bars += 1
            if self._regime_bars >= self._min_bars_before_switch:
                old = self._regime
                self._regime = new_regime
                self._regime_bars = 0
                logger.info(f"{self.symbol}: Regime {old.value} → {new_regime.value}")
        else:
            self._regime_bars = 0

        return self._regime

    def _classify(self) -> Regime:
        closes = list(self._closes)
        highs  = list(self._highs)
        lows   = list(self._lows)

        adx    = _calc_adx(highs, lows, closes, period=14)
        atr    = _calc_atr(highs, lows, closes, period=14)
        atr_30 = _calc_atr(highs, lows, closes, period=30)
        ema20  = _ema(closes, 20)
        ema50  = _ema(closes, 50)
        ema200 = _ema(closes, 200) if len(closes) >= 200 else None

        # Anomalous volatility: ATR > 2x its 30d baseline
        if atr and atr_30 and atr_30 > 0 and atr / atr_30 > 2.0:
            return Regime.HIGH_VOLATILITY

        # Trending conditions
        if adx and adx > 25:
            if ema20 and ema50:
                if ema20 > ema50:
                    if ema200 is None or ema50 > ema200:
                        return Regime.TRENDING_UP
                else:
                    if ema200 is None or ema50 < ema200:
                        return Regime.TRENDING_DOWN

        # Accumulation: low ADX, price near recent lows, OI-based (simplified here)
        if adx and adx < 20 and ema20 and ema50:
            if ema20 < ema50 * 1.02:  # flat / slightly bearish structure
                # Check if price is near 30-bar lows (coiling)
                low_30 = min(lows[-30:]) if len(lows) >= 30 else lows[-1]
                if closes[-1] < low_30 * 1.05:
                    return Regime.ACCUMULATION

        # Default: ranging
        return Regime.RANGING

    @property
    def regime(self) -> Regime:
        return self._regime

    @property
    def params(self) -> dict:
        return REGIME_PARAMS[self._regime]

    def get_status(self) -> dict:
        return {
            "symbol": self.symbol,
            "regime": self._regime.value,
            "params": self.params,
            "bars_in_regime": self._regime_bars,
            "data_points": len(self._closes),
        }


# ── Technical helpers (pure functions, no external deps) ─────────────────────

def _ema(closes: list[float], period: int) -> Optional[float]:
    if len(closes) < period:
        return None
    # SMA-seeded EMA (matches original behavior exactly)
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def _calc_atr(highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _calc_adx(highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
    """ADX(14) using Wilder smoothing. Returns 0-100."""
    if len(closes) < period * 2 + 1:
        return None

    plus_dm_list, minus_dm_list, tr_list = [], [], []
    for i in range(1, len(closes)):
        up   = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        plus_dm_list.append(up if up > down and up > 0 else 0)
        minus_dm_list.append(down if down > up and down > 0 else 0)
        tr = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
        tr_list.append(tr)

    def wilder_smooth(values, n):
        s = sum(values[:n])
        result = [s]
        for v in values[n:]:
            s = s - s/n + v
            result.append(s)
        return result

    tr_s  = wilder_smooth(tr_list, period)
    pdm_s = wilder_smooth(plus_dm_list, period)
    mdm_s = wilder_smooth(minus_dm_list, period)

    dx_list = []
    for i in range(len(tr_s)):
        if tr_s[i] == 0:
            continue
        pdi = 100 * pdm_s[i] / tr_s[i]
        mdi = 100 * mdm_s[i] / tr_s[i]
        dx  = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0
        dx_list.append(dx)

    if len(dx_list) < period:
        return None

    adx = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period
    return round(adx, 2)
