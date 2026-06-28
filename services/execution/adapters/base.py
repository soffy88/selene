"""
services/execution/adapters/base.py

Exchange Adapter 抽象基类 + 统一结果模型。
所有交易所适配器必须继承 BaseAdapter 并实现全部抽象方法。

设计原则：
  - 所有方法返回 OrderResult / PositionResult，永不抛异常到上层
  - 内部异常被捕获并转为 success=False + error 字段
  - 支持 WebSocket 成交回报订阅（实盘闭环关键）
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable, AsyncIterator

logger = logging.getLogger("execution.adapter")


# ── 幂等键工具 ────────────────────────────────────────

def sanitize_client_id(client_order_id: str) -> str:
    """Make an idempotency key safe for both OKX (clOrdId) and Binance (newClientOrderId):
    alphanumeric only, <=32 chars. UUIDs become their 32 hex chars (dashes stripped)."""
    if not client_order_id:
        return ""
    cleaned = "".join(ch for ch in client_order_id if ch.isalnum())
    return cleaned[:32]


# Substrings / codes that indicate "this order already exists" (idempotent retry hit).
_DUPLICATE_MARKERS = ("duplicate", "already exist", "repeated", "clordid")
_DUPLICATE_CODES = {"51020", "51603", "-2010", "-4015"}


def is_duplicate_order(code: str, message: str) -> bool:
    if code and str(code) in _DUPLICATE_CODES:
        return True
    msg = (message or "").lower()
    return any(m in msg for m in _DUPLICATE_MARKERS)


# Aliases used internally by adapters.
_sanitize_client_id = sanitize_client_id
_is_duplicate_order = is_duplicate_order


# ── 统一结果模型 ──────────────────────────────────────

@dataclass
class OrderResult:
    success:      bool
    exchange_id:  str   = ""
    filled_price: float = 0.0
    filled_qty:   float = 0.0
    fee_paid:     float = 0.0
    status:       str   = ""   # NEW / PARTIALLY_FILLED / FILLED / CANCELLED
    error:        str   = ""
    client_order_id: str = ""  # echoed idempotency key (clOrdId / newClientOrderId)
    recovered:    bool  = False  # True = result reconstructed from a duplicate-order lookup
    raw:          dict  = field(default_factory=dict)


@dataclass
class CancelResult:
    success: bool
    error:   str = ""


@dataclass
class PositionResult:
    symbol:       str
    side:         str
    size:         float
    entry_price:  float
    unrealized_pnl: float = 0.0
    leverage:     float   = 1.0


@dataclass
class FillEvent:
    """WebSocket 成交回报事件（推送给 execution/main.py 更新 FSM）"""
    exchange_id:  str
    symbol:       str
    side:         str
    filled_qty:   float
    filled_price: float
    fee:          float
    is_final:     bool     # True = 完全成交 / False = 部分成交
    ts:           datetime = field(default_factory=datetime.utcnow)


# ── 抽象基类 ──────────────────────────────────────────

class BaseAdapter(ABC):

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.testnet    = testnet
        self._fill_callbacks: list[Callable[[FillEvent], None]] = []

    def on_fill(self, callback: Callable[[FillEvent], None]):
        """注册成交回报回调（execution/main.py 用于驱动 FSM）"""
        self._fill_callbacks.append(callback)

    async def _emit_fill(self, event: FillEvent):
        for cb in self._fill_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(event)
                else:
                    cb(event)
            except Exception as e:
                logger.error(f"fill callback error: {e}")

    @abstractmethod
    async def place_order(
        self,
        symbol:     str,
        side:       str,       # BUY / SELL
        qty:        float,
        order_type: str,       # MARKET / LIMIT
        price:      float = 0.0,
        reduce_only: bool = False,
        time_in_force: str = "GTC",
        client_order_id: str = "",   # idempotency key; reused across retries so the
                                     # exchange rejects duplicate placements (no double fills)
    ) -> OrderResult:
        """下单。返回 OrderResult，不抛异常。"""

    @abstractmethod
    async def cancel_order(self, symbol: str, exchange_id: str) -> CancelResult:
        """撤单"""

    @abstractmethod
    async def get_order(self, symbol: str, exchange_id: str) -> OrderResult:
        """查询单笔订单状态"""

    @abstractmethod
    async def get_positions(self) -> list[PositionResult]:
        """获取当前所有持仓"""

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict:
        """获取盘口（用于滑点估算）返回 {bids: [[price,qty],...], asks: [...]}"""

    @abstractmethod
    async def subscribe_fills(self):
        """
        WebSocket 订阅成交回报。
        收到成交时调用 self._emit_fill(FillEvent(...))。
        应在独立 asyncio.Task 中运行，断线自动重连。
        """

    @abstractmethod
    async def get_account_balance(self) -> dict:
        """返回 {total_equity, available, margin_used}"""

    async def close(self):
        """清理连接（可选覆盖）"""
        pass


# ── Adapter 注册表 ────────────────────────────────────

_ADAPTERS: dict[str, "BaseAdapter"] = {}


def register_adapter(name: str, adapter: BaseAdapter):
    _ADAPTERS[name.lower()] = adapter
    logger.info(f"Adapter registered: {name}")


def get_adapter(name: str = "binance") -> BaseAdapter:
    key = (name or "binance").lower()
    if key not in _ADAPTERS:
        raise RuntimeError(
            f"Adapter '{key}' not registered. "
            f"Available: {list(_ADAPTERS.keys())}. "
            "Call register_adapter() at startup."
        )
    return _ADAPTERS[key]


def get_all_adapters() -> dict[str, BaseAdapter]:
    return dict(_ADAPTERS)
