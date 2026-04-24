"""
CryptoWatch v4 — Redis Stream Definitions
Single source of truth for all stream names and event formats.
ADR-101: Redis Streams replace asyncio.Queue for cross-service communication.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ── Stream Names ──────────────────────────────────────────────────────────────
STREAM_MARKET_RAW      = "market.raw"       # Data → Signal
STREAM_MARKET_CANDLES  = "market.candles"   # Data → Signal/Backtest
STREAM_SIGNAL_RAW      = "signal.raw"       # Signal → Portfolio
STREAM_SIGNAL_SCORED   = "signal.scored"    # Signal → Portfolio (raw scored signals)
STREAM_SIGNAL_SIZED    = "signal.sized"     # Portfolio → Execution (after Kelly sizing)
STREAM_RISK_CHECK      = "risk.check"       # Execution → Risk
STREAM_RISK_APPROVED   = "risk.approved"    # Risk → Execution
STREAM_ORDER_LIFECYCLE = "order.lifecycle"  # Execution → Portfolio/Notify
STREAM_PORTFOLIO_STATE = "portfolio.state"  # Portfolio → All (broadcast)
STREAM_SYSTEM_ALERTS   = "system.alerts"    # All → Notification
# ── Onchain Sentinel（v4.1 新增）─────────────────────
STREAM_ONCHAIN_EVENTS  = "onchain.events"   # BTC/ETH/SOL worker → OnchainService

# ── Consumer Group Names ──────────────────────────────────────────────────────
CG_SIGNAL    = "signal-service"
CG_PORTFOLIO = "portfolio-service"
CG_RISK      = "risk-service"
CG_EXECUTION = "execution-service"
CG_NOTIFY    = "notification-service"
CG_GATEWAY   = "gateway-service"
CG_ONCHAIN   = "onchain-sentinel"      # v4.1 新增

# ── Stream retention (maxlen) ─────────────────────────────────────────────────
MAXLEN = {
    STREAM_MARKET_RAW:      100_000,
    STREAM_MARKET_CANDLES:  500_000,
    STREAM_SIGNAL_RAW:       50_000,
    STREAM_SIGNAL_SCORED:    10_000,
    STREAM_SIGNAL_SIZED:     10_000,
    STREAM_RISK_CHECK:       10_000,
    STREAM_RISK_APPROVED:    10_000,
    STREAM_ORDER_LIFECYCLE: 100_000,
    STREAM_PORTFOLIO_STATE:  10_000,
    STREAM_SYSTEM_ALERTS:    10_000,
    STREAM_ONCHAIN_EVENTS:   50_000,   # onchain-sentinel
}


# ── Event helpers ─────────────────────────────────────────────────────────────

def encode(data: dict) -> dict:
    """Encode dict values to strings for Redis Stream storage."""
    return {k: json.dumps(v) if not isinstance(v, str) else v for k, v in data.items()}


def decode(fields: dict) -> dict:
    """Decode Redis Stream fields back to Python types."""
    result = {}
    for k, v in fields.items():
        key = k.decode() if isinstance(k, bytes) else k
        val = v.decode() if isinstance(v, bytes) else v
        try:
            result[key] = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            result[key] = val
    return result


@dataclass
class StreamEvent:
    """Wrapper for a Redis Stream message."""
    stream:    str
    msg_id:    str
    data:      dict
    received_at: datetime = field(default_factory=datetime.utcnow)


# Re-export consume from redis_client so both import paths work
def consume(stream, group, consumer, callback, block_ms=100):
    """Lazy import to avoid circular dependency."""
    from shared.db.redis_client import consume as _consume
    return _consume(stream, group, consumer, callback)
