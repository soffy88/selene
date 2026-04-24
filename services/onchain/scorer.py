"""
onchain_sentinel/services/onchain/scorer.py

P1: 信号融合层
职责：把链上原始事件（鲸鱼转账、交易所流量、矿工归集）
转化为 [-1, +1] 的 onchain factor score，
写入 Redis，供 cryptowatch-v4 的 MultiFactorScorer 消费。

Redis 写入目标：
  signal.raw  stream         ← 触发 signal-service 重新评分
  onchain:state:{symbol}     ← 当前链上状态快照
  onchain:alert:{id}         ← 完整预警详情（TTL 24h）

还订阅：
  signal.scored stream       ← 读 regime + win_probability，叠加到推送消息
"""

import asyncio
import json
import logging
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger("onchain.scorer")

# ── Regime 调整倍数（与 cw4 RegimeDetector 对齐）
REGIME_MULTIPLIERS = {
    "TRENDING_UP":      1.3,
    "TRENDING_DOWN":    0.9,
    "RANGING":          1.0,
    "HIGH_VOLATILITY":  0.7,
    "ACCUMULATION":     1.2,
    "UNKNOWN":          1.0,
}

# ── 链上信号语义权重 [-1, +1]
SIGNAL_WEIGHTS = {
    "whale_inflow_exchange":  -0.4,   # 流入交易所 → 抛压 → bearish
    "whale_outflow_exchange": +0.4,   # 提出交易所 → 囤币 → bullish
    "whale_unknown":           0.0,
    "miner_sell":             -0.3,
    "miner_accumulate":       +0.2,
    "smart_wallet_long":      +0.5,
    "smart_wallet_exit":      -0.5,
    "dormant_wake":           -0.2,   # 休眠地址激活 → 解套出逃
    "net_exchange_outflow":   +0.35,
    "net_exchange_inflow":    -0.35,
    "rune_volume_spike":      +0.15,
}

# ── 已知地址标签（生产环境对接 Arkham / Etherscan Labels）
KNOWN_EXCHANGES = {
    "1ck6khy6mhgyvm rq4paafkydrg1ejbh1ce": "Binance",
    "34xp4vrocgjym3xr7ycvpfhocnxv4twseo": "Binance Cold",
    "3m219kr5venenb47ewrpfwyb5jq2djxrp6": "OKX",
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3": "OKX",
}

KNOWN_MINERS = {
    "bc1qjasf9z3h7w3jspkhtgatgpyvvzgpa2wwd2lr38": "F2Pool",
    "bc1qcd6cf3ml4x8d2xshlv9fcuqh7p72cfnf4j3gny": "AntPool",
}

SMART_WALLETS: dict = {
    # 用你量化系统已追踪的地址替换
    "0x7a3f9f9e2bfc6d9e9a9b9c9d9e9f0a0b0c0d0e0f": {"label": "Alpha Whale", "win_rate": 0.91},
}


@dataclass
class RawOnchainEvent:
    """从 BTC/ETH/SOL worker 传来的原始链上事件"""
    id:          str   = field(default_factory=lambda: uuid.uuid4().hex)
    chain:       str   = ""
    symbol:      str   = "BTCUSDT"
    event_type:  str   = ""     # whale_transfer / exchange_flow / miner / smart_wallet / rune
    from_addr:   str   = ""
    to_addr:     str   = ""
    amount:      float = 0.0
    amount_usd:  float = 0.0
    severity:    str   = "medium"
    meta:        dict  = field(default_factory=dict)
    ts:          float = field(default_factory=time.time)

    def classify(self) -> str:
        fa = self.from_addr.lower()
        ta = self.to_addr.lower()
        is_from_ex    = fa in KNOWN_EXCHANGES
        is_to_ex      = ta in KNOWN_EXCHANGES
        is_from_miner = fa in KNOWN_MINERS
        is_from_smart = fa in SMART_WALLETS
        is_smart_exit = is_from_smart and is_to_ex

        if self.event_type == "rune":
            return "rune_volume_spike"
        if is_smart_exit:
            return "smart_wallet_exit"
        if is_from_smart:
            return "smart_wallet_long"
        if is_from_miner:
            return "miner_sell" if is_to_ex else "miner_accumulate"
        if is_to_ex and not is_from_ex:
            return "whale_inflow_exchange"
        if is_from_ex and not is_to_ex:
            return "whale_outflow_exchange"
        if self.meta.get("dormant_years", 0) >= 2:
            return "dormant_wake"
        return "whale_unknown"


class OnchainScorer:
    """
    维护每个交易对的链上因子状态。
    接收原始事件 → EWMA 更新评分 → 写 Redis。
    """

    def __init__(self, symbol: str, ewma_alpha: float = 0.15):
        self.symbol  = symbol
        self._alpha  = ewma_alpha
        self._score  = 0.0
        self._event_log: deque = deque(maxlen=500)
        self._ex_inflow:  float = 0.0
        self._ex_outflow: float = 0.0
        self._last_regime = "UNKNOWN"
        self._last_win_prob: Optional[float] = None
        self._last_ci_lo: Optional[float] = None
        self._last_ci_hi: Optional[float] = None

    # ── 事件摄入 ──────────────────────────────────────
    def ingest(self, event: RawOnchainEvent) -> float:
        signal_class = event.classify()
        raw_w = SIGNAL_WEIGHTS.get(signal_class, 0.0)

        # 幅度归一化：$1M=0.33, $10M=0.67, $100M=1.0（对数刻度）
        magnitude = min(1.0, math.log10(max(event.amount_usd / 1e6, 1.0)) / 3.0)

        # 严重度乘数
        sev_mult = {"critical": 1.5, "high": 1.2, "medium": 1.0, "low": 0.7}.get(event.severity, 1.0)

        weighted = raw_w * magnitude * sev_mult

        # EWMA 更新
        self._score = self._alpha * weighted + (1 - self._alpha) * self._score
        self._score = max(-1.0, min(1.0, self._score))

        # 交易所流量累计
        if signal_class == "whale_inflow_exchange":
            self._ex_inflow  += event.amount
        elif signal_class == "whale_outflow_exchange":
            self._ex_outflow += event.amount

        self._event_log.append({
            "id":          event.id,
            "class":       signal_class,
            "weighted":    round(weighted, 4),
            "score_after": round(self._score, 4),
            "amount_usd":  event.amount_usd,
            "ts":          event.ts,
        })

        logger.info(
            f"[{self.symbol}] {signal_class} | "
            f"${event.amount_usd/1e6:.1f}M | sev={event.severity} | "
            f"w={weighted:+.3f} → score={self._score:+.4f}"
        )
        return self._score

    # ── Regime 调整 ───────────────────────────────────
    def apply_regime(self, regime: str) -> float:
        self._last_regime = regime
        mult = REGIME_MULTIPLIERS.get(regime, 1.0)
        return max(-1.0, min(1.0, self._score * mult))

    # ── 从 signal.scored 更新 cw4 评分反馈 ──────────
    def update_from_scored(self, win_prob: float, ci_lo: float, ci_hi: float):
        """cw4 signal-service 返回的最新评分，用于推送时展示"""
        self._last_win_prob = win_prob
        self._last_ci_lo    = ci_lo
        self._last_ci_hi    = ci_hi

    # ── 综合评分（用于推送决策）─────────────────────
    def composite_score(self) -> dict:
        """
        整合链上因子 + cw4 评分，产出最终可交易评估。
        这是 P3 决策层的核心输出。
        """
        onchain_adj = self.apply_regime(self._last_regime)
        net_flow    = (self._ex_outflow - self._ex_inflow)
        net_flow_norm = max(-1.0, min(1.0, net_flow / 1000.0))  # 1000 BTC = 满分

        result = {
            "symbol":            self.symbol,
            "onchain_score":     round(self._score, 4),
            "regime_adj_score":  round(onchain_adj, 4),
            "net_exchange_flow": round(net_flow, 2),
            "regime":            self._last_regime,
        }

        if self._last_win_prob is not None:
            # 融合：cw4 win_prob 70% 权重 + onchain 30% 权重调整
            onchain_adj_prob = (onchain_adj + 1) / 2   # [-1,+1] → [0,1]
            fused_prob = 0.70 * self._last_win_prob + 0.30 * onchain_adj_prob
            result["cw4_win_prob"]   = round(self._last_win_prob, 4)
            result["fused_win_prob"] = round(fused_prob, 4)
            result["ci_lo"]          = round(self._last_ci_lo or 0, 4)
            result["ci_hi"]          = round(self._last_ci_hi or 0, 4)
            # 信号是否具有可交易性（融合后胜率超阈值）
            result["actionable"]     = fused_prob >= 0.56

        return result

    def get_state(self) -> dict:
        state = self.composite_score()
        state["recent_events"] = list(self._event_log)[-10:]
        state["updated_at"]    = datetime.utcnow().isoformat()
        return state

    def decay(self, hours: float = 1.0):
        """每小时自然衰减 5%"""
        self._score *= (0.95 ** hours)
