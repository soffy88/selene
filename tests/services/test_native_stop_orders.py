"""Exchange-native protective stop tests (audit P0-3).

The in-process price-poll monitor cannot protect a position when the service is down
or the feed freezes/gaps — exactly when a leveraged perp gets liquidated. These tests
cover the exchange-native stop capability and that a live fill places one (best-effort,
with a loud alert when it can't).
"""

import asyncio

from services.execution.adapters.base import BaseAdapter, OrderResult
from services.execution.adapters.binance import BinanceAdapter
from services.execution.adapters.okx import OKXAdapter

# ── Binance STOP_MARKET ─────────────────────────────────────────────────────


def test_binance_stop_builds_stop_market(monkeypatch):
    adp = BinanceAdapter("k", "s", testnet=True)
    captured = {}

    async def fake_request(method, path, params=None):
        captured["method"] = method
        captured["path"] = path
        captured["params"] = params
        return {"orderId": 999, "status": "NEW", "clientOrderId": "abc"}

    monkeypatch.setattr(adp, "_request", fake_request)
    res = asyncio.run(adp.place_stop_order("BTCUSDT", "SELL", 0.5, 60000.0, client_order_id="oid"))
    assert res.success and res.exchange_id == "999"
    p = captured["params"]
    assert captured["path"] == "/fapi/v1/order"
    assert p["type"] == "STOP_MARKET"
    assert p["stopPrice"] == "60000.0000"
    assert p["side"] == "SELL"
    assert p["reduceOnly"] == "true"


def test_binance_stop_error_returns_failure(monkeypatch):
    adp = BinanceAdapter("k", "s", testnet=True)

    async def fake_request(method, path, params=None):
        return {"code": -2021, "msg": "Order would immediately trigger."}

    monkeypatch.setattr(adp, "_request", fake_request)
    res = asyncio.run(adp.place_stop_order("BTCUSDT", "SELL", 0.5, 60000.0))
    assert res.success is False
    assert "immediately trigger" in res.error


# ── OKX conditional algo stop ───────────────────────────────────────────────


def test_okx_stop_uses_algo_endpoint(monkeypatch):
    adp = OKXAdapter("k", "s", "pass", testnet=True)
    adp._ct_val = {"BTC-USDT-SWAP": 0.01}  # skip the network ctVal load
    captured = {}

    async def fake_request(method, path, payload=None):
        captured["path"] = path
        captured["payload"] = payload
        return {"code": "0", "data": [{"sCode": "0", "algoId": "A123", "algoClOrdId": "oid"}]}

    monkeypatch.setattr(adp, "_request", fake_request)
    res = asyncio.run(adp.place_stop_order("BTCUSDT", "SELL", 1.0, 60000.0, client_order_id="oid"))
    assert res.success and res.exchange_id == "A123"
    assert captured["path"] == "/api/v5/trade/order-algo"
    pay = captured["payload"]
    assert pay["ordType"] == "conditional"
    assert pay["slTriggerPx"] == "60000.0"
    assert pay["slOrdPx"] == "-1"  # market on trigger
    assert pay["reduceOnly"] == "true"


# ── Base default: unsupported, no throw ─────────────────────────────────────


def test_base_default_reports_unsupported():
    class Dummy(BaseAdapter):
        async def place_order(self, *a, **k): ...
        async def cancel_order(self, *a, **k): ...
        async def get_order(self, *a, **k): ...
        async def get_positions(self): ...
        async def get_orderbook(self, *a, **k): ...
        async def subscribe_fills(self): ...
        async def get_account_balance(self): ...

    res = asyncio.run(Dummy("k", "s").place_stop_order("BTCUSDT", "SELL", 1.0, 60000.0))
    assert res.success is False and "not supported" in res.error


# ── execution wiring: _place_protective_stop ────────────────────────────────


class _StubRec:
    def __init__(self, side, stop_loss, qty=1.0, filled_qty=1.0):
        self.id = "order-1234abcd"
        self.symbol = "BTCUSDT"
        self.side = side
        self.stop_loss = stop_loss
        self.quantity = qty
        self.filled_qty = filled_qty


class _StubAdapter:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def place_stop_order(self, symbol, side, qty, stop_price, reduce_only=True, client_order_id=""):
        self.calls.append((symbol, side, qty, stop_price, reduce_only))
        if self.ok:
            return OrderResult(success=True, exchange_id="STOP-1")
        return OrderResult(success=False, error="rejected")


def test_protective_stop_placed_opposite_side(monkeypatch):
    import services.execution.main as m

    monkeypatch.setattr(m, "_stop_orders", {}, raising=False)
    adp = _StubAdapter(ok=True)
    rec = _StubRec(side="BUY", stop_loss=60000.0)
    placed = asyncio.run(m._place_protective_stop(adp, rec))
    assert placed is True
    # long protected by a SELL stop
    assert adp.calls[0][1] == "SELL"
    assert m._stop_orders[rec.id] == "STOP-1"


def test_protective_stop_failure_alerts_and_keeps_position(monkeypatch):
    import services.execution.main as m

    monkeypatch.setattr(m, "_stop_orders", {}, raising=False)
    alerts = []

    class _R:
        async def xadd(self, stream, payload, **kw):
            alerts.append((stream, payload))

    async def fake_redis():
        return _R()

    monkeypatch.setattr(m, "get_redis", fake_redis)
    adp = _StubAdapter(ok=False)
    rec = _StubRec(side="SELL", stop_loss=70000.0)
    placed = asyncio.run(m._place_protective_stop(adp, rec))
    assert placed is False
    assert rec.id not in m._stop_orders
    assert len(alerts) == 1  # a high-severity risk alert was emitted


def test_no_stop_when_stop_loss_absent(monkeypatch):
    import services.execution.main as m

    monkeypatch.setattr(m, "_stop_orders", {}, raising=False)
    adp = _StubAdapter(ok=True)
    rec = _StubRec(side="BUY", stop_loss=0.0)
    placed = asyncio.run(m._place_protective_stop(adp, rec))
    assert placed is False
    assert adp.calls == []
