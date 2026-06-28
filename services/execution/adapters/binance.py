"""
services/execution/adapters/binance.py

Binance USDM Futures 适配器（实盘 + Testnet 双模式）。

关键实现：
  1. REST 下单 / 撤单 / 查询
  2. WebSocket userData stream → 实时成交回报（FillEvent）
  3. HMAC-SHA256 签名
  4. 指数退避重连
  5. 部分成交累积（累积均价 + 累积数量）
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from datetime import datetime
from typing import Optional

import aiohttp
import websockets

from services.execution.adapters.base import (
    BaseAdapter, OrderResult, CancelResult, PositionResult, FillEvent,
    _sanitize_client_id, _is_duplicate_order,
)

logger = logging.getLogger("adapter.binance")

# REST
LIVE_BASE   = "https://fapi.binance.com"
TEST_BASE   = "https://testnet.binancefuture.com"

# WebSocket
LIVE_WS_BASE = "wss://fstream.binance.com"
TEST_WS_BASE = "wss://stream.binancefuture.com"


class BinanceAdapter(BaseAdapter):

    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        super().__init__(api_key, api_secret, testnet)
        self._base    = TEST_BASE   if testnet else LIVE_BASE
        self._ws_base = TEST_WS_BASE if testnet else LIVE_WS_BASE
        self._session: Optional[aiohttp.ClientSession] = None
        self._listen_key: Optional[str] = None
        # 部分成交累积：exchange_id → {qty, notional}
        self._partial_fills: dict[str, dict] = {}

    # ── HTTP 工具 ─────────────────────────────────────

    async def _sess(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-MBX-APIKEY": self.api_key}
            )
        return self._session

    def _sign(self, params: dict) -> str:
        qs = urllib.parse.urlencode(params)
        return hmac.new(
            self.api_secret.encode(), qs.encode(), hashlib.sha256
        ).hexdigest()

    async def _request(
        self, method: str, path: str,
        params: dict = None, signed: bool = True,
        retries: int = 3,
    ) -> Optional[dict]:
        params = params or {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)

        url = self._base + path
        s   = await self._sess()

        for attempt in range(retries):
            try:
                async with getattr(s, method.lower())(
                    url, params=params if method == "GET" else None,
                    data=params  if method != "GET" else None,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    data = await r.json()
                    if r.status == 200:
                        return data
                    # Rate limit
                    if r.status == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    logger.error(f"Binance {method} {path} → {r.status}: {data}")
                    return None
            except Exception as e:
                logger.warning(f"Request attempt {attempt+1}/{retries}: {e}")
                await asyncio.sleep(1)
        return None

    # ── 下单 ──────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "LIMIT",
        price: float = 0.0,
        reduce_only: bool = False,
        time_in_force: str = "GTC",
        client_order_id: str = "",
    ) -> OrderResult:
        cl_ord_id = _sanitize_client_id(client_order_id)
        params = {
            "symbol":   symbol,
            "side":     side.upper(),
            "type":     order_type.upper(),
            "quantity": f"{qty:.6f}".rstrip("0").rstrip("."),
        }
        if cl_ord_id:
            params["newClientOrderId"] = cl_ord_id
        if order_type.upper() == "LIMIT":
            params["price"]        = f"{price:.4f}"
            params["timeInForce"]  = time_in_force
        if reduce_only:
            params["reduceOnly"] = "true"

        data = await self._request("POST", "/fapi/v1/order", params)
        if not data:
            return OrderResult(success=False, error="no response from exchange")

        if "code" in data:
            err = data.get("msg", str(data["code"]))
            # Idempotency: duplicate newClientOrderId means a prior attempt already landed.
            # Recover its real state rather than failing hard or risking a second placement.
            if cl_ord_id and _is_duplicate_order(str(data.get("code", "")), err):
                logger.warning(f"Binance duplicate clientOrderId={cl_ord_id}; recovering existing order")
                recovered = await self._get_order_by_client_id(symbol, cl_ord_id)
                if recovered:
                    return recovered
            logger.error(f"place_order error: {err}")
            return OrderResult(success=False, error=err, client_order_id=cl_ord_id)

        # 立即成交（MARKET or IOC）
        filled_price = float(data.get("avgPrice", 0) or 0)
        filled_qty   = float(data.get("executedQty", 0) or 0)
        fee          = 0.0  # fee comes in via userData stream

        status = data.get("status", "NEW")
        logger.info(
            f"place_order OK: {symbol} {side} {qty} @ {price} "
            f"→ {status} exchange_id={data['orderId']}"
        )
        return OrderResult(
            success=True,
            exchange_id=str(data["orderId"]),
            filled_price=filled_price,
            filled_qty=filled_qty,
            fee_paid=fee,
            status=status,
            client_order_id=str(data.get("clientOrderId", cl_ord_id)),
            raw=data,
        )

    async def _get_order_by_client_id(self, symbol: str, cl_ord_id: str) -> Optional[OrderResult]:
        data = await self._request("GET", "/fapi/v1/order", {
            "symbol": symbol, "origClientOrderId": cl_ord_id,
        })
        if not data or "code" in data:
            return None
        return OrderResult(
            success=True,
            exchange_id=str(data.get("orderId", "")),
            filled_price=float(data.get("avgPrice", 0) or 0),
            filled_qty=float(data.get("executedQty", 0) or 0),
            status=data.get("status", ""),
            client_order_id=str(data.get("clientOrderId", cl_ord_id)),
            recovered=True,
            raw=data,
        )

    # ── 撤单 ──────────────────────────────────────────

    async def cancel_order(self, symbol: str, exchange_id: str) -> CancelResult:
        data = await self._request("DELETE", "/fapi/v1/order", {
            "symbol": symbol, "orderId": exchange_id,
        })
        if not data or "code" in data:
            err = (data or {}).get("msg", "cancel failed")
            return CancelResult(success=False, error=err)
        return CancelResult(success=True)

    # ── 查单 ──────────────────────────────────────────

    async def get_order(self, symbol: str, exchange_id: str) -> OrderResult:
        data = await self._request("GET", "/fapi/v1/order", {
            "symbol": symbol, "orderId": exchange_id,
        })
        if not data or "code" in data:
            return OrderResult(success=False, error="query failed")
        return OrderResult(
            success=True,
            exchange_id=str(data["orderId"]),
            filled_price=float(data.get("avgPrice", 0) or 0),
            filled_qty=float(data.get("executedQty", 0) or 0),
            status=data.get("status", ""),
            raw=data,
        )

    # ── 持仓 ──────────────────────────────────────────

    async def get_positions(self) -> list[PositionResult]:
        data = await self._request("GET", "/fapi/v2/positionRisk", {})
        if not data:
            return []
        results = []
        for p in data:
            qty = float(p.get("positionAmt", 0))
            if abs(qty) < 1e-8:
                continue
            results.append(PositionResult(
                symbol=p["symbol"],
                side="LONG" if qty > 0 else "SHORT",
                size=abs(qty),
                entry_price=float(p.get("entryPrice", 0)),
                unrealized_pnl=float(p.get("unRealizedProfit", 0)),
                leverage=float(p.get("leverage", 1)),
            ))
        return results

    # ── 盘口（滑点估算用）────────────────────────────

    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict:
        data = await self._request("GET", "/fapi/v1/depth", {
            "symbol": symbol, "limit": depth,
        }, signed=False)
        if not data:
            return {"bids": [], "asks": []}
        return {
            "bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
            "asks": [[float(p), float(q)] for p, q in data.get("asks", [])],
        }

    # ── 账户余额 ──────────────────────────────────────

    async def get_account_balance(self) -> dict:
        data = await self._request("GET", "/fapi/v2/account", {})
        if not data:
            return {"total_equity": 0, "available": 0, "margin_used": 0}
        return {
            "total_equity": float(data.get("totalWalletBalance", 0)),
            "available":    float(data.get("availableBalance", 0)),
            "margin_used":  float(data.get("totalInitialMargin", 0)),
        }

    # ── Listen Key 管理 ──────────────────────────────

    async def _get_listen_key(self) -> Optional[str]:
        data = await self._request("POST", "/fapi/v1/listenKey", {}, signed=False)
        return data.get("listenKey") if data else None

    async def _keepalive_listen_key(self):
        """每30分钟延续 listen key（Binance 60分钟过期）"""
        while True:
            await asyncio.sleep(1800)
            if self._listen_key:
                await self._request("PUT", "/fapi/v1/listenKey",
                                    {"listenKey": self._listen_key}, signed=False)

    # ── WebSocket userData stream（成交回报核心）────────

    async def subscribe_fills(self):
        """
        订阅 userData stream，实时接收成交回报。
        解析 ORDER_TRADE_UPDATE 事件 → FillEvent → 通知 execution/main.py。
        断线自动指数退避重连。
        """
        asyncio.create_task(self._keepalive_listen_key())
        retry = 5

        while True:
            try:
                self._listen_key = await self._get_listen_key()
                if not self._listen_key:
                    logger.error("Failed to get listen key, retry in 30s")
                    await asyncio.sleep(30)
                    continue

                ws_url = f"{self._ws_base}/ws/{self._listen_key}"
                logger.info(f"userData WS connecting: {ws_url[:60]}...")

                async with websockets.connect(ws_url, ping_interval=20) as ws:
                    logger.info("✅ Binance userData stream connected")
                    retry = 5  # 重置退避

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            await self._handle_ws_message(msg)
                        except Exception as e:
                            logger.error(f"WS message error: {e}")

            except Exception as e:
                logger.warning(f"userData WS error: {e} | retry in {retry}s")
                await asyncio.sleep(retry)
                retry = min(retry * 2, 60)

    async def _handle_ws_message(self, msg: dict):
        event = msg.get("e")

        if event == "ORDER_TRADE_UPDATE":
            o = msg.get("o", {})
            status      = o.get("X")   # order status
            exec_type   = o.get("x")   # execution type: TRADE / NEW / CANCELED
            exchange_id = str(o.get("i", ""))
            symbol      = o.get("s", "")
            side        = o.get("S", "")   # BUY / SELL
            last_qty    = float(o.get("l", 0))   # last filled qty
            last_price  = float(o.get("L", 0))   # last filled price
            fee         = float(o.get("n", 0))   # commission

            if exec_type != "TRADE" or last_qty <= 0:
                return

            # 累积部分成交
            acc = self._partial_fills.setdefault(exchange_id, {"qty": 0.0, "notional": 0.0})
            acc["qty"]     += last_qty
            acc["notional"] += last_qty * last_price

            is_final = status in ("FILLED",)

            fill = FillEvent(
                exchange_id  = exchange_id,
                symbol       = symbol,
                side         = side,
                filled_qty   = last_qty,
                filled_price = last_price,
                fee          = fee,
                is_final     = is_final,
                ts           = datetime.utcnow(),
            )
            await self._emit_fill(fill)

            if is_final:
                self._partial_fills.pop(exchange_id, None)

            logger.info(
                f"FILL {symbol} {side} qty={last_qty} price={last_price} "
                f"status={status} {'FINAL' if is_final else 'PARTIAL'}"
            )

        elif event == "ACCOUNT_UPDATE":
            # 可在此更新本地账户余额缓存
            pass

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
