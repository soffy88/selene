"""Unit tests for multi-factor scoring and ScoredSignal model."""
import pytest, sys, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))
from services.signal.factors.composite import (
    FactorScores, MultiFactorScorer,
    score_rsi, score_ema_alignment, score_funding_zscore,
    score_oi_momentum, score_lsr_divergence, WEIGHTS
)
from shared.models.signal import ScoredSignal, SignalType, Direction, Regime

class TestFactorScorers:
    def test_rsi_oversold_positive(self):
        assert score_rsi(20) > 0.8   # strongly oversold = bullish
    def test_rsi_overbought_negative(self):
        assert score_rsi(80) < -0.8  # strongly overbought = bearish
    def test_rsi_neutral(self):
        assert abs(score_rsi(50)) < 0.05
    def test_rsi_none_zero(self):
        assert score_rsi(None) == 0.0

    def test_ema_bull_stack_positive(self):
        s = score_ema_alignment(ema20=110, ema50=100, ema200=90, price=115)
        assert s > 0.8   # full bull alignment
    def test_ema_bear_stack_negative(self):
        s = score_ema_alignment(ema20=90, ema50=100, ema200=110, price=85)
        assert s < -0.8  # full bear alignment
    def test_ema_none_returns_zero(self):
        assert score_ema_alignment(None, None, None, 100) == 0.0

    def test_negative_funding_bullish(self):
        history = [0.01]*30
        s = score_funding_zscore(-0.10, history)  # very negative vs history
        assert s > 0.5
    def test_positive_funding_bearish(self):
        history = [0.0]*30
        s = score_funding_zscore(0.15, history)
        assert s < -0.3
    def test_funding_no_history_zero(self):
        assert score_funding_zscore(0.05, []) == 0.0

    def test_oi_rising_price_rising_bullish(self):
        s = score_oi_momentum(oi_change_pct=10.0, price_change_pct=5.0)
        assert s > 0
    def test_oi_rising_price_falling_bearish(self):
        s = score_oi_momentum(oi_change_pct=10.0, price_change_pct=-5.0)
        assert s < 0
    def test_oi_none_zero(self):
        assert score_oi_momentum(None, 5.0) == 0.0

    def test_crowd_long_bearish(self):
        s = score_lsr_divergence(80)  # 80% longs = bearish
        assert s < -0.5
    def test_crowd_short_bullish(self):
        s = score_lsr_divergence(20)  # 20% longs = bullish
        assert s > 0.5
    def test_crowd_neutral(self):
        assert abs(score_lsr_divergence(50)) < 0.05

class TestWeights:
    def test_weights_sum_to_one(self):
        assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
    def test_all_positive(self):
        assert all(v > 0 for v in WEIGHTS.values())
    def test_funding_highest_weight(self):
        assert WEIGHTS["funding_zscore"] == max(WEIGHTS.values())

class TestMultiFactorScorer:
    def setup_method(self):
        self.scorer = MultiFactorScorer()

    def test_strong_long_factors_high_prob(self):
        factors = FactorScores(
            technical_rsi=0.8,    # oversold
            technical_ema=0.7,    # bullish alignment
            funding_zscore=0.9,   # very negative funding
            oi_momentum=0.5,
            lsr_divergence=0.6,   # crowd short
            onchain=0.3, social=0.2, orderbook=0.3,
        )
        result = self.scorer.score("LONG", factors)
        assert result.win_probability > 0.6
        assert result.confidence_lo < result.win_probability < result.confidence_hi

    def test_bearish_factors_low_long_prob(self):
        factors = FactorScores(
            technical_rsi=-0.8, technical_ema=-0.7,
            funding_zscore=-0.8, oi_momentum=-0.5, lsr_divergence=-0.6,
            onchain=-0.3, social=-0.2, orderbook=-0.3,
        )
        result = self.scorer.score("LONG", factors)
        assert result.win_probability < 0.5   # bad time to go long

    def test_short_direction_inverts_score(self):
        factors = FactorScores(
            technical_rsi=-0.8, technical_ema=-0.7,
            funding_zscore=-0.8, oi_momentum=-0.5, lsr_divergence=-0.6,
            onchain=-0.3, social=-0.2, orderbook=-0.3,
        )
        long_r  = self.scorer.score("LONG",  factors)
        short_r = self.scorer.score("SHORT", factors)
        # Same bearish factors → HIGH prob for SHORT, LOW for LONG
        assert short_r.win_probability > long_r.win_probability

    def test_confidence_interval_narrows_with_samples(self):
        factors = FactorScores()
        r_few  = self.scorer.score("LONG", factors, n_samples=10)
        r_many = self.scorer.score("LONG", factors, n_samples=500)
        ci_few  = r_few.confidence_hi - r_few.confidence_lo
        ci_many = r_many.confidence_hi - r_many.confidence_lo
        assert ci_many < ci_few

    def test_dominant_factor_identified(self):
        factors = FactorScores(funding_zscore=0.99)  # one dominant factor
        result = self.scorer.score("LONG", factors)
        assert result.dominant_factor == "funding_zscore"

class TestScoredSignal:
    def test_is_actionable_passes(self):
        s = ScoredSignal(symbol="BTCUSDT", win_probability=0.62,
                         data_quality=0.95, entry_price=50000, stop_loss=49000)
        assert s.is_actionable

    def test_is_not_actionable_low_prob(self):
        s = ScoredSignal(symbol="BTCUSDT", win_probability=0.50,
                         data_quality=0.95, entry_price=50000, stop_loss=49000)
        assert not s.is_actionable

    def test_is_not_actionable_low_quality(self):
        s = ScoredSignal(symbol="BTCUSDT", win_probability=0.65,
                         data_quality=0.80, entry_price=50000, stop_loss=49000)
        assert not s.is_actionable

    def test_risk_reward_calculation(self):
        s = ScoredSignal(entry_price=100, stop_loss=98, take_profit=106,
                         direction=Direction.LONG)
        # stop dist=2, take dist=6 → RR=3.0
        assert abs(s.risk_reward - 3.0) < 0.01

    def test_serialization_roundtrip(self):
        from datetime import datetime
        s = ScoredSignal(
            symbol="ETHUSDT", signal_type=SignalType.LONG_SETUP,
            direction=Direction.LONG, regime=Regime.TRENDING_UP,
            win_probability=0.63, entry_price=3000.0, stop_loss=2940.0,
            take_profit=3180.0,
        )
        d = s.to_dict()
        s2 = ScoredSignal.from_dict(d)
        assert s2.symbol == s.symbol
        assert s2.win_probability == s.win_probability
        assert s2.regime == s.regime
        assert s2.direction == s.direction
