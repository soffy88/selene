"""
CryptoWatch v4 — Multi-Factor Signal Scorer
8 factors → weighted composite score → win_probability via calibration.

Factor weights (must sum to 1.0):
  technical_rsi    15%
  technical_ema    15%
  funding_zscore   20%
  oi_momentum      15%
  lsr_divergence   10%
  onchain          10%
  social           10%
  orderbook        5%
"""
import json
import math
import logging
from dataclasses import dataclass, field
from typing import Optional

# Redis 持久化 calibration 参数
REDIS_CALIBRATION_KEY = "cw4:signal:calibration"

logger = logging.getLogger(__name__)

WEIGHTS = {
    "technical_rsi":   0.15,
    "technical_ema":   0.15,
    "funding_zscore":  0.20,
    "oi_momentum":     0.15,
    "lsr_divergence":  0.10,
    "onchain":         0.10,
    "social":          0.10,
    "orderbook":       0.05,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "Weights must sum to 1.0"


@dataclass
class FactorScores:
    """Raw factor scores (-1 to +1 each). Positive = bullish signal."""
    technical_rsi:  float = 0.0   # RSI normalized: oversold → +1, overbought → -1
    technical_ema:  float = 0.0   # EMA alignment: full bull → +1, full bear → -1
    funding_zscore: float = 0.0   # funding Z-score: very negative → +1 (long), very positive → -1
    oi_momentum:    float = 0.0   # OI change: rising OI in uptrend → +1
    lsr_divergence: float = 0.0   # crowd vs smart money divergence
    onchain:        float = 0.0   # net exchange inflow/outflow
    social:         float = 0.0   # social acceleration (not absolute)
    orderbook:      float = 0.0   # bid/ask imbalance

    def to_dict(self) -> dict:
        return {
            "technical_rsi":  round(self.technical_rsi, 4),
            "technical_ema":  round(self.technical_ema, 4),
            "funding_zscore": round(self.funding_zscore, 4),
            "oi_momentum":    round(self.oi_momentum, 4),
            "lsr_divergence": round(self.lsr_divergence, 4),
            "onchain":        round(self.onchain, 4),
            "social":         round(self.social, 4),
            "orderbook":      round(self.orderbook, 4),
        }


@dataclass
class CompositeScore:
    raw:             float              # -1 to +1
    direction_score: float              # 0 to +1 (magnitude in signal direction)
    win_probability: float              # calibrated 0-1 probability
    confidence_lo:   float              # 95% CI lower bound
    confidence_hi:   float              # 95% CI upper bound
    factor_scores:   dict = field(default_factory=dict)
    dominant_factor: str  = ""          # which factor contributed most


# ── Individual factor calculators ────────────────────────────────────────────

def score_rsi(rsi: Optional[float]) -> float:
    """RSI → [-1, +1]. Oversold = bullish (+1), Overbought = bearish (-1)."""
    if rsi is None:
        return 0.0
    # Normalize: RSI 30 → +0.8, RSI 50 → 0, RSI 70 → -0.8
    return round(-(rsi - 50) / 30, 4)


def score_ema_alignment(ema20: Optional[float], ema50: Optional[float],
                         ema200: Optional[float], price: float) -> float:
    """EMA alignment score: full bull stack → +1, full bear → -1."""
    if not ema20 or not ema50:
        return 0.0
    score = 0.0
    # EMA20 vs EMA50 (most recent, weight 0.4)
    score += 0.4 * (1.0 if ema20 > ema50 else -1.0)
    # Price vs EMA20 (weight 0.3)
    score += 0.3 * (1.0 if price > ema20 else -1.0)
    # EMA50 vs EMA200 (long-term, weight 0.3)
    if ema200:
        score += 0.3 * (1.0 if ema50 > ema200 else -1.0)
    return round(score, 4)


def score_funding_zscore(current_rate: float, rate_history: list[float]) -> float:
    """
    Funding rate Z-Score → [-1, +1].
    Very negative funding (shorts paying longs) → bullish → +1.
    Very positive funding (longs paying shorts) → bearish → -1.
    """
    if not rate_history or len(rate_history) < 5:
        return 0.0
    import numpy as np
    arr = np.array(rate_history, dtype=float)
    mean = float(arr.mean())
    std = float(arr.std(ddof=0))
    if std < 1e-15:
        return 0.0
    z = (current_rate - mean) / std
    return round(-max(-1.0, min(1.0, z / 3.0)), 4)


def score_oi_momentum(oi_change_pct: Optional[float], price_change_pct: Optional[float]) -> float:
    """
    OI momentum: rising OI with rising price = confirmed uptrend = bullish.
    Rising OI with falling price = confirmed downtrend = bearish.
    """
    if oi_change_pct is None or price_change_pct is None:
        return 0.0
    # If OI and price move together → strong signal in that direction
    # Normalize: 10% OI change = 0.5 score unit
    oi_norm    = max(-1.0, min(1.0, oi_change_pct / 10.0))
    price_norm = max(-1.0, min(1.0, price_change_pct / 5.0))
    return round(oi_norm * abs(price_norm) * (1 if price_norm >= 0 else -1), 4)


def score_lsr_divergence(long_ratio: Optional[float]) -> float:
    """
    Crowd sentiment: extreme long positioning = bearish (fade the crowd).
    long_ratio: 0-100 (% longs).
    """
    if long_ratio is None:
        return 0.0
    # 75% longs = bearish (-0.8), 25% longs = bullish (+0.8), 50% = neutral
    return round(-(long_ratio - 50) / 30, 4)


# ── Composite scorer ──────────────────────────────────────────────────────────

class MultiFactorScorer:
    """
    Combines 8 factors into a composite direction score,
    then calibrates to a win probability using a sigmoid + historical shift.

    Calibration: the raw score [-1, +1] is mapped to win_probability
    using sigmoid calibration fitted to historical signal outcomes.
    Default params are conservative (center = 0.5, scale = 2.0).
    """

    def __init__(self, calibration_center: float = 0.0, calibration_scale: float = 2.5):
        # Platt calibration parameters (fit these on historical data)
        self._cal_center = calibration_center
        self._cal_scale  = calibration_scale
        # 真实样本数（由 SignalService 通过 set_sample_size 注入，初值 = 冷启动 CI）
        self._n_samples: int = 10

    def set_sample_size(self, n: int) -> None:
        """注入真实历史样本数，用于 Wilson CI 计算。"""
        self._n_samples = max(1, int(n))

    def score(
        self,
        direction: str,             # "LONG" or "SHORT"
        factors:   FactorScores,
        n_samples: Optional[int] = None,  # override sample count; default uses _n_samples
    ) -> CompositeScore:
        """
        Compute composite score for a given direction.
        For LONG: positive raw score = bullish = high win probability.
        For SHORT: invert the score.
        """
        scores_dict = factors.to_dict()

        # Weighted sum
        raw = (
            WEIGHTS["technical_rsi"]   * factors.technical_rsi  +
            WEIGHTS["technical_ema"]   * factors.technical_ema  +
            WEIGHTS["funding_zscore"]  * factors.funding_zscore +
            WEIGHTS["oi_momentum"]     * factors.oi_momentum    +
            WEIGHTS["lsr_divergence"]  * factors.lsr_divergence +
            WEIGHTS["onchain"]         * factors.onchain         +
            WEIGHTS["social"]          * factors.social          +
            WEIGHTS["orderbook"]       * factors.orderbook
        )

        # For SHORT signals, invert the score
        directional = raw if direction == "LONG" else -raw

        # Calibrate to win probability via sigmoid
        win_prob = self._sigmoid(directional)

        # Confidence interval (Wilson interval for proportions, simplified)
        n = n_samples if n_samples is not None else self._n_samples
        ci_half = self._ci_half(win_prob, n)

        # Dominant factor
        contributions = {
            k: abs(WEIGHTS.get(k, 0) * getattr(factors, k, 0))
            for k in scores_dict
        }
        dominant = max(contributions, key=contributions.get)

        return CompositeScore(
            raw=round(raw, 4),
            direction_score=round(directional, 4),
            win_probability=round(win_prob, 4),
            confidence_lo=round(max(0.0, win_prob - ci_half), 4),
            confidence_hi=round(min(1.0, win_prob + ci_half), 4),
            factor_scores=scores_dict,
            dominant_factor=dominant,
        )

    def _sigmoid(self, x: float) -> float:
        """Calibrated sigmoid: maps [-1, +1] → [0.35, 0.75] range."""
        return 1.0 / (1.0 + math.exp(-(x - self._cal_center) * self._cal_scale))

    def _ci_half(self, p: float, n: int) -> float:
        """Wilson confidence interval half-width at 95%."""
        if n < 10:
            return 0.15
        import numpy as np
        z = 1.96
        denominator = 1 + z**2 / n
        center = (p + z**2 / (2*n)) / denominator
        half = z * np.sqrt(p*(1-p)/n + z**2/(4*n**2)) / denominator
        return round(float(half), 4)

    def update_calibration(self, center: float, scale: float) -> None:
        """Update Platt calibration parameters after fitting on historical data."""
        self._cal_center = center
        self._cal_scale  = scale
        logger.info(f"Calibration updated: center={center}, scale={scale}")


async def load_calibration(redis_client) -> Optional[tuple[float, float]]:
    """从 Redis 读取已持久化的 calibration 参数。"""
    try:
        raw = await redis_client.get(REDIS_CALIBRATION_KEY)
        if not raw:
            return None
        d = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        return float(d.get("center", 0.0)), float(d.get("scale", 2.5))
    except Exception as e:
        logger.warning(f"load_calibration: {e}")
        return None


async def save_calibration(
    redis_client, center: float, scale: float, n_samples: int = 0
) -> None:
    """由 backtest 或 WFO 模块写入学习后的 calibration。"""
    payload = {
        "center":    float(center),
        "scale":     float(scale),
        "n_samples": int(n_samples),
    }
    await redis_client.set(REDIS_CALIBRATION_KEY, json.dumps(payload))


DEFAULT_CALIBRATION = (0.0, 2.5)


def _platt_nll(scores: list[float], outcomes: list[int], c: float, s: float) -> float:
    """Negative log-likelihood of sigmoid((x-c)*s) vs binary outcomes."""
    nll = 0.0
    for x, y in zip(scores, outcomes):
        p = 1.0 / (1.0 + math.exp(-(x - c) * s))
        p = min(max(p, 1e-6), 1 - 1e-6)
        nll -= y * math.log(p) + (1 - y) * math.log(1 - p)
    return nll


def _platt_grid_fit(scores: list[float], outcomes: list[int]) -> tuple[float, float]:
    """Coarse grid search (center∈[-0.5,0.5], scale∈[0.5,6.0]) minimising NLL."""
    best = (*DEFAULT_CALIBRATION, 1e18)
    for c_int in range(-20, 21):                      # -0.5 ~ 0.5 step 0.025
        c = c_int * 0.025
        for s_int in range(2, 25):                    # 0.5 ~ 6.0 step 0.25
            s = s_int * 0.25
            nll = _platt_nll(scores, outcomes, c, s)
            if nll < best[2]:
                best = (c, s, nll)
    return round(best[0], 4), round(best[1], 4)


def platt_fit(scores: list[float], outcomes: list[int]) -> tuple[float, float]:
    """Platt scaling: fit sigmoid((x-center)*scale) to map raw scores → P(win).

    Overfitting control (item #16): with enough samples the grid search is fit on
    a chronological TRAIN split and validated on a held-out TEST split — the fitted
    calibration is adopted only if it beats the default on the holdout (per-sample
    NLL), otherwise we keep the neutral default. This stops an in-sample grid search
    from adopting parameters that don't generalise.
    """
    if not scores or len(scores) != len(outcomes) or len(scores) < 20:
        return DEFAULT_CALIBRATION

    n = len(scores)
    if n < 40:
        # Too few to split meaningfully — plain in-sample fit (legacy behaviour).
        return _platt_grid_fit(scores, outcomes)

    # Chronological holdout (data is time-ordered): first 70% train, last 30% test.
    cut = int(n * 0.7)
    tr_s, tr_o = scores[:cut], outcomes[:cut]
    te_s, te_o = scores[cut:], outcomes[cut:]

    c, s = _platt_grid_fit(tr_s, tr_o)
    fitted_test_nll = _platt_nll(te_s, te_o, c, s) / len(te_s)
    default_test_nll = _platt_nll(te_s, te_o, *DEFAULT_CALIBRATION) / len(te_s)
    if fitted_test_nll <= default_test_nll:
        return c, s
    logger.info("platt_fit: fitted calibration failed holdout (%.4f > %.4f); keeping default",
                fitted_test_nll, default_test_nll)
    return DEFAULT_CALIBRATION


# ── Onchain Factor Bridge ─────────────────────────────────────────────────────
# 从 onchain-sentinel 写入的 Redis key 读取链上因子评分。
# key: onchain:state:{symbol}  由 services/onchain/main.py 写入。
# 无数据时返回 0.0（中性），不影响现有评分逻辑。

import json as _json


async def get_onchain_factor(symbol: str) -> float:
    """
    读取链上因子评分 [-1, +1]，已经过 regime 调整。
    在 signal-service 组装 FactorScores 时调用，填入 onchain 字段。
    """
    try:
        from shared.db.connections import get_redis
        r = await get_redis()
        raw = await r.get(f"onchain:state:{symbol}")
        if not raw:
            return 0.0
        state = _json.loads(raw)
        return float(state.get("regime_adj_score", state.get("onchain_score", 0.0)))
    except Exception as e:
        logger.warning(f"get_onchain_factor {symbol}: {e}")
        return 0.0


async def get_onchain_composite(symbol: str) -> dict:
    """
    读取完整的链上状态快照，包含 fused_win_prob、历史胜率等。
    供 gateway /api/v4/onchain/state 使用。
    """
    try:
        from shared.db.connections import get_redis
        r = await get_redis()
        raw = await r.get(f"onchain:state:{symbol}")
        return _json.loads(raw) if raw else {}
    except Exception:
        return {}
