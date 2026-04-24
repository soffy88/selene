"""
Unit tests for the multi-factor signal scorer.
Validates factor direction, weight normalization, and probability calibration.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))
import pytest
import math

from services.signal.factors.composite import (
    MultiFactorScorer, FactorScores,
    score_rsi, score_ema_alignment, score_funding_zscore,
    score_oi_momentum, score_lsr_divergence, WEIGHTS,
)


class TestIndividualFactors:
    def test_rsi_oversold_bullish(self):
        assert score_rsi(20) > 0.5     # deeply oversold → strong bullish
        assert score_rsi(80) < -0.5    # overbought → bearish
        assert abs(score_rsi(50)) < 0.1  # neutral

    def test_rsi_boundary(self):
        assert score_rsi(None) == 0.0
        # RSI 0 → most bullish, RSI 100 → most bearish
        assert score_rsi(0) > score_rsi(50) > score_rsi(100)

    def test_ema_bullish_alignment(self):
        # Full bull: EMA20 > EMA50 > EMA200, price > EMA20
        score = score_ema_alignment(110, 100, 90, 115)
        assert score > 0.5

    def test_ema_bearish_alignment(self):
        score = score_ema_alignment(90, 100, 110, 85)
        assert score < -0.5

    def test_ema_none_handling(self):
        assert score_ema_alignment(None, None, None, 100) == 0.0

    def test_funding_very_negative_bullish(self):
        """Extremely negative funding rate → shorts paying → long bias."""
        history = [0.0] * 10 + [-0.01] * 10  # baseline near 0
        score = score_funding_zscore(-0.15, history)  # 3σ below mean
        assert score > 0.3

    def test_funding_very_positive_bearish(self):
        history = [0.0] * 20
        score = score_funding_zscore(0.15, history)
        assert score < -0.3

    def test_oi_momentum_rising_with_price(self):
        """Rising OI + rising price = confirmed uptrend = bullish."""
        score = score_oi_momentum(oi_change_pct=15.0, price_change_pct=5.0)
        assert score > 0

    def test_oi_momentum_rising_with_falling_price(self):
        """Rising OI + falling price = shorts piling in = bearish."""
        score = score_oi_momentum(oi_change_pct=15.0, price_change_pct=-5.0)
        assert score < 0

    def test_lsr_crowd_long_bearish(self):
        """80% of crowd long → fade the crowd → bearish signal."""
        score = score_lsr_divergence(80.0)
        assert score < -0.5

    def test_lsr_crowd_short_bullish(self):
        """20% long (80% short) → crowd panic → contrarian bullish."""
        score = score_lsr_divergence(20.0)
        assert score > 0.5

    def test_lsr_neutral_at_50(self):
        assert abs(score_lsr_divergence(50.0)) < 0.1

    def test_all_factors_bounded(self):
        """All factor scores must be in [-1, +1]."""
        scores = [
            score_rsi(30), score_rsi(70),
            score_ema_alignment(110, 100, 90, 115),
            score_funding_zscore(-0.1, [0.0]*20),
            score_oi_momentum(10, 5), score_oi_momentum(-10, -5),
            score_lsr_divergence(75), score_lsr_divergence(25),
        ]
        for s in scores:
            assert -1.0 <= s <= 1.0, f"Score out of bounds: {s}"


class TestWeights:
    def test_weights_sum_to_one(self):
        total = sum(WEIGHTS.values())
        assert abs(total - 1.0) < 1e-9

    def test_all_factors_have_weights(self):
        factors = FactorScores()
        factor_names = [f for f in vars(factors)]
        for name in factor_names:
            assert name in WEIGHTS, f"Factor '{name}' has no weight"

    def test_funding_highest_weight(self):
        """Funding rate is the most predictive factor (per spec)."""
        assert WEIGHTS["funding_zscore"] >= max(
            v for k, v in WEIGHTS.items() if k != "funding_zscore"
        )


class TestCompositeScorer:
    def setup_method(self):
        self.scorer = MultiFactorScorer()

    def test_all_bullish_factors_high_probability(self):
        factors = FactorScores(
            technical_rsi=0.8, technical_ema=0.9,
            funding_zscore=0.9, oi_momentum=0.7,
            lsr_divergence=0.8, onchain=0.5,
            social=0.5, orderbook=0.5,
        )
        result = self.scorer.score("LONG", factors, n_samples=200)
        assert result.win_probability > 0.65

    def test_all_bearish_factors_low_long_probability(self):
        factors = FactorScores(
            technical_rsi=-0.8, technical_ema=-0.9,
            funding_zscore=-0.8, oi_momentum=-0.7,
            lsr_divergence=-0.7,
        )
        result = self.scorer.score("LONG", factors, n_samples=200)
        assert result.win_probability < 0.45

    def test_short_inverts_score(self):
        """For SHORT signals, bullish factors should decrease win probability."""
        factors = FactorScores(technical_rsi=0.8, funding_zscore=0.8)
        long_result  = self.scorer.score("LONG",  factors)
        short_result = self.scorer.score("SHORT", factors)
        assert long_result.win_probability > short_result.win_probability

    def test_confidence_interval_wider_with_less_data(self):
        """Fewer samples → wider confidence interval."""
        factors = FactorScores(technical_rsi=0.5, funding_zscore=0.3)
        few  = self.scorer.score("LONG", factors, n_samples=20)
        many = self.scorer.score("LONG", factors, n_samples=500)
        ci_few  = few.confidence_hi  - few.confidence_lo
        ci_many = many.confidence_hi - many.confidence_lo
        assert ci_few > ci_many

    def test_probability_in_valid_range(self):
        """Win probability must always be in [0, 1]."""
        import random
        random.seed(0)
        for _ in range(100):
            factors = FactorScores(
                **{k: random.uniform(-1, 1) for k in vars(FactorScores())}
            )
            result = self.scorer.score("LONG", factors)
            assert 0.0 <= result.win_probability <= 1.0
            assert 0.0 <= result.confidence_lo <= result.win_probability
            assert result.win_probability <= result.confidence_hi <= 1.0

    def test_dominant_factor_identified(self):
        """Should identify which factor contributes most."""
        factors = FactorScores(funding_zscore=1.0)   # only funding active
        result = self.scorer.score("LONG", factors)
        assert result.dominant_factor == "funding_zscore"

    def test_neutral_factors_near_50pct(self):
        """All zeros → near-neutral probability."""
        factors = FactorScores()   # all 0.0
        result = self.scorer.score("LONG", factors)
        assert 0.40 < result.win_probability < 0.60
