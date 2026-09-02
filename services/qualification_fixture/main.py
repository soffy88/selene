"""Deterministic fixture injector. Writes Redis keys/streams only. No venue HTTP."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

from aiohttp import web

from shared.db.redis_client import get_redis, init_redis
from shared.runtime.service_health import consume_ready, mark_consume, snapshot

logger = logging.getLogger("qual-fixture")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

SYMBOL = os.environ.get("QUAL_SYMBOL", "BTCUSDT")
INTERVAL_MS = int(os.environ.get("CANDLE_INTERVAL_MS", str(3_600_000)))
N_BARS = int(os.environ.get("QUAL_SEED_BARS", "260"))
PORT = int(os.environ.get("SERVICE_PORT", "8090"))


def _klines(n: int) -> list[dict]:
    now_ms = int(time.time() * 1000)
    last_open = now_ms - INTERVAL_MS
    out = []
    price = 80.0
    for i in range(n):
        open_time = last_open - (n - 1 - i) * INTERVAL_MS
        open_px = price
        price = round(price * 1.0008, 4)
        close = price
        out.append(
            {
                "open_time": open_time,
                "open": round(open_px, 4),
                "high": round(close * 1.0015, 4),
                "low": round(open_px * 0.999, 4),
                "close": round(close, 4),
                "volume": 1500.0 + i,
            }
        )
    return out


async def seed(r) -> None:
    klines = _klines(N_BARS)
    last = klines[-1]
    tickers = [
        {
            "symbol": SYMBOL,
            "priceChangePercent": "6.5",
            "quoteVolume": "50000000",
            "lastPrice": str(last["close"]),
        }
    ]
    funding = [0.0001] * 29 + [-0.001]
    pipe = r.pipeline()
    pipe.set("qual:tickers", json.dumps(tickers))
    pipe.set(f"qual:klines:{SYMBOL}", json.dumps(klines))
    pipe.set(f"qual:funding:{SYMBOL}", json.dumps(funding))
    pipe.set(f"qual:funding_now:{SYMBOL}", "-0.001")
    pipe.set(f"qual:oi_change:{SYMBOL}", "5.0")
    pipe.set(f"qual:long_ratio:{SYMBOL}", "32.0")
    pipe.set("cw4:real_exchange_calls", "0")
    pipe.hset("cw4:prices", SYMBOL, json.dumps({"price": last["close"]}))
    pipe.set("qual:fixture:ready", datetime.now(timezone.utc).isoformat())
    await pipe.execute()
    mark_consume("fixture")
    logger.info("seeded %s bars=%d last_close=%s", SYMBOL, len(klines), last["close"])


async def refresh_loop() -> None:
    while True:
        r = get_redis()
        await seed(r)
        await asyncio.sleep(float(os.environ.get("QUAL_REFRESH_S", "15")))


async def livez(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "qualification-fixture"})


async def readyz(_request: web.Request) -> web.Response:
    r = get_redis()
    ready = bool(await r.get("qual:fixture:ready")) and consume_ready("fixture", max_age_s=60)
    body = {"ready": ready, "service": "qualification-fixture", **snapshot("fixture")}
    return web.json_response(body, status=200 if ready else 503)


async def main() -> None:
    init_redis(os.environ.get("REDIS_URL", "redis://127.0.0.1:26379/0"))
    await seed(get_redis())
    app = web.Application()
    app.router.add_get("/livez", livez)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/health", livez)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("qualification-fixture listening on %s", PORT)
    await refresh_loop()


if __name__ == "__main__":
    asyncio.run(main())
