"""Shared canonical market data models used across all v4 services."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Interval(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


@dataclass(frozen=True)
class BarData:
    symbol: str
    interval: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int = 0
    is_anomaly: bool = False
    gap_before: bool = False

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def dt(self) -> datetime:
        return datetime.utcfromtimestamp(self.open_time / 1000)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.open_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class FundingRateData:
    symbol: str
    funding_time: int
    rate_pct: float
    is_anomaly: bool = False

    @property
    def dt(self) -> datetime:
        return datetime.utcfromtimestamp(self.funding_time / 1000)


@dataclass
class MarketSnapshot:
    symbol: str
    price: float
    funding_rate: Optional[float] = None
    next_funding_in: Optional[int] = None
    oi_usd: Optional[float] = None
    oi_change_24h: Optional[float] = None
    long_ratio: Optional[float] = None
    volume_24h: Optional[float] = None
    price_change_24h: Optional[float] = None
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class OrderBook:
    symbol: str
    bids: list  # [(price, qty)]
    asks: list
    timestamp: int
    daily_volume_usd: float = 0.0

    @property
    def bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid * 100 if self.mid else 0.0

    def depth_usd(self, side: str, levels: int = 5) -> float:
        book = self.bids if side == "BUY" else self.asks
        return sum(p * q for p, q in book[:levels])
