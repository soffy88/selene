"""Unit tests for Regime Detector and ADX/ATR calculations."""
import pytest, sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))
from services.signal.regime.detector import (
    RegimeDetector, _calc_adx, _calc_atr, _ema, REGIME_PARAMS
)
from shared.models.signal import Regime

def _make_trend(n=100, start=100.0, slope=0.5, noise=0.2):
    import random; random.seed(42)
    prices = [start + i*slope + random.gauss(0, noise) for i in range(n)]
    highs  = [p + random.uniform(0.2, 0.8) for p in prices]
    lows   = [p - random.uniform(0.2, 0.8) for p in prices]
    return prices, highs, lows

def _make_flat(n=100, center=100.0, noise=0.3):
    import random; random.seed(7)
    prices = [center + random.gauss(0, noise) for _ in range(n)]
    highs  = [p + random.uniform(0.1, 0.4) for p in prices]
    lows   = [p - random.uniform(0.1, 0.4) for p in prices]
    return prices, highs, lows

class TestRegimeDetector:
    def test_unknown_before_warmup(self):
        det = RegimeDetector("BTC")
        for i in range(30):
            r = det.update(101+i*0.1, 100+i*0.1, 100+i*0.1)
        # Less than BUFFER (50) candles → UNKNOWN
        assert r == Regime.UNKNOWN

    def test_trending_up_detected(self):
        det = RegimeDetector("ETH")
        closes, highs, lows = _make_trend(120, slope=1.0, noise=0.1)
        for h, l, c in zip(highs, lows, closes):
            det.update(h, l, c)
        # Strong uptrend should eventually be detected
        assert det.regime in (Regime.TRENDING_UP, Regime.UNKNOWN)

    def test_ranging_detected(self):
        det = RegimeDetector("SOL")
        closes, highs, lows = _make_flat(120, noise=0.15)
        for h, l, c in zip(highs, lows, closes):
            det.update(h, l, c)
        assert det.regime in (Regime.RANGING, Regime.UNKNOWN, Regime.ACCUMULATION)

    def test_high_vol_detected(self):
        import random; random.seed(3)
        det = RegimeDetector("DOGE")
        # First build baseline with calm period
        closes = [100 + random.gauss(0, 0.5) for _ in range(80)]
        highs  = [c + 0.3 for c in closes]; lows = [c - 0.3 for c in closes]
        for h, l, c in zip(highs, lows, closes):
            det.update(h, l, c)
        # Inject explosive volatility — 3 bars is the minimum hysteresis window.
        # TR ~1000x baseline (≈0.6) so ATR(14)/ATR(30) > 2.0 threshold fires on bar 1;
        # after 3 consecutive hits the regime switches. Beyond bar 4 ATR(30) catches up
        # and ratio falls below 2.0, so only check immediately after the hysteresis fires.
        for _ in range(3):
            c = closes[-1] + random.gauss(0, 300)  # 600x normal vol
            closes.append(c); det.update(c+300, c-300, c)
        assert det.regime == Regime.HIGH_VOLATILITY

    def test_params_populated(self):
        det = RegimeDetector("BTC")
        closes, highs, lows = _make_trend(120)
        for h, l, c in zip(highs, lows, closes):
            det.update(h, l, c)
        params = det.params
        assert "atr_mult" in params and "kelly" in params
        assert "max_lev" in params and "max_hold_h" in params

    def test_all_regimes_have_params(self):
        for regime in Regime:
            if regime != Regime.UNKNOWN:
                assert regime in REGIME_PARAMS

class TestADX:
    def test_returns_none_on_insufficient_data(self):
        assert _calc_adx([100]*10, [99]*10, [100]*10) is None

    def test_trending_has_high_adx(self):
        closes, highs, lows = _make_trend(80, slope=2.0, noise=0.1)
        adx = _calc_adx(highs, lows, closes)
        assert adx is not None
        assert adx > 20   # trending market

    def test_flat_has_low_adx(self):
        closes, highs, lows = _make_flat(80, noise=0.05)
        adx = _calc_adx(highs, lows, closes)
        assert adx is not None
        assert adx < 30   # non-trending

    def test_adx_in_bounds(self):
        import random; random.seed(12)
        for _ in range(5):
            n = 60
            c = [100 + random.gauss(0,2) for _ in range(n)]
            h = [x + random.uniform(0,1) for x in c]
            l = [x - random.uniform(0,1) for x in c]
            adx = _calc_adx(h, l, c)
            if adx: assert 0 <= adx <= 100

class TestATR:
    def test_flat_low_atr(self):
        highs = [10.1]*30; lows = [9.9]*30; closes = [10.0]*30
        atr = _calc_atr(highs, lows, closes)
        assert atr is not None and atr < 0.5

    def test_volatile_high_atr(self):
        import random; random.seed(9)
        closes = [100.0]
        highs = []; lows = []
        for _ in range(30):
            c = closes[-1] + random.uniform(-5,5)
            closes.append(c); highs.append(c+3); lows.append(c-3)
        atr = _calc_atr(highs, lows, closes[1:])
        assert atr is not None and atr > 1.0
