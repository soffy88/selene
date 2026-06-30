"""
services/execution/adapters/okx.py

OKX Futures 适配器（模拟盘 + 实盘双模式）。
结构与 BinanceAdapter 对称，方便 SmartRouter 切换。
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
# websockets is lazy-imported inside subscribe_fills (keeps the module import-safe
# where the optional WS lib isn't installed, e.g. unit-test/CI environments).

from services.execution.adapters.base import (
    BaseAdapter, OrderResult, CancelResult, PositionResult, FillEvent,
    _sanitize_client_id, _is_duplicate_order,
)

logger = logging.getLogger("adapter.okx")

LIVE_BASE = "https://www.okx.com"
DEMO_BASE = "https://www.okx.com"   # demo 用同域名，区别在 header
LIVE_WS   = "wss://ws.okx.com:8443/ws/v5/private"
DEMO_WS   = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"


class OKXAdapter(BaseAdapter):

    def __init__(
        self, api_key: str, api_secret: str, passphrase: str,
        testnet: bool = False,
    ):
        super().__init__(api_key, api_secret, testnet)
        self._passphrase = passphrase
        self._base       = LIVE_BASE
        self._ws_url     = DEMO_WS if testnet else LIVE_WS
        self._session: Optional[aiohttp.ClientSession] = None
        self._ct_val: dict[str, float] = {}  # instId → ctVal (base currency per contract)

    async def _load_ct_val(self) -> None:
        """Fetch and cache contract sizes for SWAP instruments."""
        data = await self._request("GET", "/api/v5/public/instruments?instType=SWAP")
        if data and data.get("code") == "0":
            for inst in data.get("data", []):
                self._ct_val[inst["instId"]] = float(inst.get("ctVal", 1))
            logger.info(f"Loaded ctVal for {len(self._ct_val)} SWAP instruments")

    def _to_contracts(self, inst_id: str, qty: float) -> int:
        """Convert base-currency quantity to whole contracts (lot size = 1)."""
        ct = self._ct_val.get(inst_id, 0)
        if ct <= 0:
            return max(1, round(qty))
        return max(1, round(qty / ct))

    # ── 签名 ──────────────────────────────────────────

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = ts + method.upper() + path + body
        return base64.b64encode(
            hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return {
            "OK-ACCESS-KEY":        self.api_key,
            "OK-ACCESS-SIGN":       self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP":  ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
            "Content-Type":         "application/json",
            **({"x-simulated-trading": "1"} if self.testnet else {}),
        }

    async def _sess(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _request(
        self, method: str, path: str, body: dict = None, retries: int = 3,
    ) -> Optional[dict]:
        body_str = json.dumps(body) if body else ""
        headers  = self._headers(method, path, body_str)
        url      = self._base + path
        s        = await self._sess()

        for attempt in range(retries):
            try:
                kw = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=10)}
                if method == "GET":
                    async with s.get(url, **kw) as r:
                        return await r.json()
                else:
                    async with s.post(url, data=body_str, **kw) as r:
                        return await r.json()
            except Exception as e:
                logger.warning(f"OKX request attempt {attempt+1}: {e}")
                await asyncio.sleep(1)
        return None

    # ── 下单 ──────────────────────────────────────────

    async def place_order(
        self, symbol: str, side: str, qty: float,
        order_type: str = "limit", price: float = 0.0,
        reduce_only: bool = False, time_in_force: str = "GTC",
        client_order_id: str = "",
    ) -> OrderResult:
        # OKX instId format: BTC-USDT-SWAP
        inst_id = symbol.replace("USDT", "-USDT-SWAP") if "USDT" in symbol else symbol
        if not self._ct_val:
            await self._load_ct_val()
        contracts = self._to_contracts(inst_id, qty)
        cl_ord_id = _sanitize_client_id(client_order_id)
        payload = {
            "instId":  inst_id,
            "tdMode":  "cross",
            "side":    side.lower(),
            "ordType": order_type.lower(),
            "sz":      str(contracts),
        }
        if cl_ord_id:
            payload["clOrdId"] = cl_ord_id
        if order_type.lower() == "limit":
            payload["px"] = str(price)
        if reduce_only:
            payload["reduceOnly"] = "true"

        data = await self._request("POST", "/api/v5/trade/order", payload)
        if not data:
            return OrderResult(success=False, error="no response")

        code = data.get("code", "")
        if code != "0":
            inner = (data.get("data") or [{}])[0]
            s_code = inner.get("sCode", "")
            s_msg = inner.get("sMsg", "")
            err = s_msg or data.get("msg", f"code={code}")
            # Idempotency: a duplicate clOrdId means a prior attempt already reached the
            # exchange. Recover its real state instead of double-sending or failing hard.
            if cl_ord_id and _is_duplicate_order(s_code, err):
                logger.warning(f"OKX duplicate clOrdId={cl_ord_id}; recovering existing order")
                recovered = await self._get_order_by_client_id(inst_id, cl_ord_id)
                if recovered:
                    return recovered
            logger.error(f"OKX order error code={code} msg={data.get('msg')} sCode={s_code} sMsg={s_msg} payload={payload}")
            return OrderResult(success=False, error=err, client_order_id=cl_ord_id)

        order = data["data"][0]
        if order.get("sCode", "0") != "0":
            if cl_ord_id and _is_duplicate_order(order.get("sCode", ""), order.get("sMsg", "")):
                logger.warning(f"OKX duplicate clOrdId={cl_ord_id}; recovering existing order")
                recovered = await self._get_order_by_client_id(inst_id, cl_ord_id)
                if recovered:
                    return recovered
            logger.error(f"OKX order item error sCode={order['sCode']} sMsg={order.get('sMsg')} payload={payload}")
            return OrderResult(success=False, error=order.get("sMsg", f"sCode={order['sCode']}"), client_order_id=cl_ord_id)
        return OrderResult(
            success=True,
            exchange_id=order.get("ordId", ""),
            status="NEW",
            client_order_id=order.get("clOrdId", cl_ord_id),
            raw=order,
        )

    async def place_stop_order(
        self, symbol: str, side: str, qty: float, stop_price: float,
        reduce_only: bool = True, client_order_id: str = "",
    ) -> OrderResult:
        """Exchange-native conditional stop (market on trigger) via the algo endpoint —
        survives a service/feed outage, unlike the in-process price-poll monitor."""
        inst_id = symbol.replace("USDT", "-USDT-SWAP") if "USDT" in symbol else symbol
        if not self._ct_val:
            await self._load_ct_val()
        contracts = self._to_contracts(inst_id, qty)
        cl_ord_id = _sanitize_client_id(client_order_id)
        payload = {
            "instId":      inst_id,
            "tdMode":      "cross",
            "side":        side.lower(),
            "ordType":     "conditional",
            "sz":          str(contracts),
            "slTriggerPx": str(stop_price),
            "slOrdPx":     "-1",   # -1 ⇒ market order on trigger
        }
        if reduce_only:
            payload["reduceOnly"] = "true"
        if cl_ord_id:
            payload["algoClOrdId"] = cl_ord_id

        data = await self._request("POST", "/api/v5/trade/order-algo", payload)
        if not data:
            return OrderResult(success=False, error="no response")
        if data.get("code", "") != "0":
            inner = (data.get("data") or [{}])[0]
            err = inner.get("sMsg") or data.get("msg", f"code={data.get('code')}")
            logger.error(f"OKX stop order error msg={err} payload={payload}")
            return OrderResult(success=False, error=err, client_order_id=cl_ord_id)
        order = data["data"][0]
        if order.get("sCode", "0") != "0":
            return OrderResult(success=False, error=order.get("sMsg", f"sCode={order.get('sCode')}"),
                               client_order_id=cl_ord_id)
        return OrderResult(
            success=True,
            exchange_id=order.get("algoId", ""),
            status="NEW",
            client_order_id=order.get("algoClOrdId", cl_ord_id),
            raw=order,
        )

    async def _get_order_by_client_id(self, inst_id: str, cl_ord_id: str) -> Optional[OrderResult]:
        data = await self._request("GET", f"/api/v5/trade/order?instId={inst_id}&clOrdId={cl_ord_id}")
        if not data or data.get("code") != "0" or not data.get("data"):
            return None
        o = data["data"][0]
        return OrderResult(
            success=True,
            exchange_id=o.get("ordId", ""),
            filled_price=float(o.get("avgPx") or 0),
            filled_qty=float(o.get("fillSz") or 0),
            status=o.get("state", ""),
            client_order_id=o.get("clOrdId", cl_ord_id),
            recovered=True,
            raw=o,
        )

    async def cancel_order(self, symbol: str, exchange_id: str) -> CancelResult:
        inst_id = symbol.replace("USDT", "-USDT-SWAP")
        data = await self._request("POST", "/api/v5/trade/cancel-order", {
            "instId": inst_id, "ordId": exchange_id,
        })
        ok = data and data.get("code") == "0"
        return CancelResult(success=ok, error="" if ok else str(data))

    async def get_order(self, symbol: str, exchange_id: str) -> OrderResult:
        inst_id = symbol.replace("USDT", "-USDT-SWAP")
        data = await self._request("GET", f"/api/v5/trade/order?instId={inst_id}&ordId={exchange_id}")
        if not data or data.get("code") != "0":
            return OrderResult(success=False, error="query failed")
        o = data["data"][0]
        return OrderResult(
            success=True,
            exchange_id=o["ordId"],
            filled_price=float(o.get("avgPx") or 0),
            filled_qty=float(o.get("fillSz") or 0),
            status=o.get("state", ""),
            raw=o,
        )

    async def get_positions(self) -> list[PositionResult]:
        data = await self._request("GET", "/api/v5/account/positions")
        if not data or data.get("code") != "0":
            return []
        results = []
        for p in data.get("data", []):
            qty = float(p.get("pos", 0))
            if abs(qty) < 1e-8:
                continue
            results.append(PositionResult(
                symbol=p.get("instId", "").replace("-USDT-SWAP", "USDT"),
                side="LONG" if qty > 0 else "SHORT",
                size=abs(qty),
                entry_price=float(p.get("avgPx") or 0),
                unrealized_pnl=float(p.get("upl") or 0),
                leverage=float(p.get("lever") or 1),
            ))
        return results

    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict:
        inst_id = symbol.replace("USDT", "-USDT-SWAP")
        data = await self._request("GET", f"/api/v5/market/books?instId={inst_id}&sz={depth}")
        if not data or data.get("code") != "0":
            return {"bids": [], "asks": []}
        book = data["data"][0]
        return {
            "bids": [[float(p), float(q)] for p, q, *_ in book.get("bids", [])],
            "asks": [[float(p), float(q)] for p, q, *_ in book.get("asks", [])],
        }

    async def get_account_balance(self) -> dict:
        data = await self._request("GET", "/api/v5/account/balance")
        if not data or data.get("code") != "0":
            return {"total_equity": 0, "available": 0, "margin_used": 0}
        detail = data["data"][0].get("details", [{}])[0]
        eq = float(detail.get("eq") or 0)
        av = float(detail.get("availEq") or 0)
        return {"total_equity": eq, "available": av, "margin_used": eq - av}

    # ── WebSocket 成交回报 ─────────────────────────────

    async def subscribe_fills(self):
        import websockets   # lazy: optional WS lib, only needed for the live fill stream
        retry = 5
        while True:
            try:
                async with websockets.connect(self._ws_url, ping_interval=20) as ws:
                    # 登录
                    ts  = str(int(time.time()))
                    sig = base64.b64encode(
                        hmac.new(self.api_secret.encode(),
                                 (ts + "GET" + "/users/self/verify").encode(),
                                 hashlib.sha256).digest()
                    ).decode()
                    await ws.send(json.dumps({
                        "op": "login",
                        "args": [{"apiKey": self.api_key, "passphrase": self._passphrase,
                                  "timestamp": ts, "sign": sig}]
                    }))
                    await ws.recv()  # login response

                    # 订阅 orders channel
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": [{"channel": "orders", "instType": "SWAP"}]
                    }))
                    logger.info("✅ OKX orders WS connected")
                    retry = 5

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            await self._handle_ws_message(msg)
                        except Exception as e:
                            logger.error(f"OKX WS parse error: {e}")

            except Exception as e:
                logger.warning(f"OKX WS error: {e} | retry {retry}s")
                await asyncio.sleep(retry)
                retry = min(retry * 2, 60)

    async def _handle_ws_message(self, msg: dict):
        if msg.get("arg", {}).get("channel") != "orders":
            return
        for o in msg.get("data", []):
            state    = o.get("state", "")
            fill_qty = float(o.get("fillSz") or 0)
            if fill_qty <= 0:
                continue
            fill = FillEvent(
                exchange_id  = o.get("ordId", ""),
                symbol       = o.get("instId", "").replace("-USDT-SWAP", "USDT"),
                side         = o.get("side", "").upper(),
                filled_qty   = fill_qty,
                filled_price = float(o.get("fillPx") or 0),
                fee          = float(o.get("fee") or 0),
                is_final     = state == "filled",
            )
            await self._emit_fill(fill)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
