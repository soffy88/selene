"""
onchain_sentinel/services/onchain/sol/worker.py

SOL 链上事件采集器
数据源: Helius WebSocket（需 HELIUS_KEY）
输出:   Redis Stream  onchain.events

生产者-消费者解耦架构（防止 WSS recv() 被慢速 HTTP 阻塞）：
  Producer: WSS logsSubscribe → 签名入队
  Consumer: 批量调用 Helius Enhanced API 解析 → emit 到 Redis
"""

import asyncio
import json
import logging
import os
import time
import uuid
from collections import OrderedDict

import aiohttp
import redis.asyncio as aioredis
import websockets

logger = logging.getLogger("sol.worker")

REDIS_URL     = os.environ.get("REDIS_URL", "redis://:changeme@redis:6379/0")
HELIUS_KEY    = os.environ.get("HELIUS_KEY", "")
THRESHOLD_SOL = float(os.environ.get("THRESHOLD_SOL", 50000))

WSS_URL  = f"wss://atlas-mainnet.helius-rpc.com/?api-key={HELIUS_KEY}"
API_URL  = f"https://api.helius.xyz/v0/transactions?api-key={HELIUS_KEY}"
STREAM   = "onchain.events"

KNOWN_EXCHANGES_SOL = {
    "CRX7bcgRDsGZMHqkNjEEJkjdqFbB7XrY2MXKXuV8B2N": "Binance SOL",
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1": "Jump Trading",
}

SMART_WALLETS_SOL = {
    "8xmG3QxXLKkWKb3KqMg6zW2XZBJhLpA3Kqp9xM12abc": "SOL Degen King",
}

_redis: aioredis.Redis = None
_sol_price = [145.0]


# ── LimitedSet 防内存泄漏 ─────────────────────────────
class LimitedSet:
    def __init__(self, maxsize=2000):
        self._d   = OrderedDict()
        self._max = maxsize

    def add(self, key) -> bool:
        if key in self._d:
            return False
        self._d[key] = True
        if len(self._d) >= self._max:
            for _ in range(self._max // 2):
                self._d.popitem(last=False)
        return True

seen_sigs = LimitedSet(2000)
sig_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)


async def get_redis():
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=False)
    return _redis


async def emit(event: dict):
    r = await get_redis()
    encoded = {k: json.dumps(v) if not isinstance(v, (str, bytes)) else v
               for k, v in event.items()}
    await r.xadd(STREAM, encoded, maxlen=50000, approximate=True)


# ── Producer: WSS → Queue ─────────────────────────────
async def producer(ws):
    async for raw in ws:
        data = json.loads(raw)
        result = data.get("params", {}).get("result", {})
        value  = result.get("value", {})
        sig    = value.get("signature", "")
        if sig and seen_sigs.add(sig):
            try:
                sig_queue.put_nowait(sig)
            except asyncio.QueueFull:
                logger.warning("sig_queue full, dropping")


# ── Consumer: Queue → Helius API → Redis ─────────────
async def consumer(session: aiohttp.ClientSession):
    while True:
        # 攒批次（最多 10 个）
        batch = []
        try:
            sig = await asyncio.wait_for(sig_queue.get(), timeout=2.0)
            batch.append(sig)
        except asyncio.TimeoutError:
            continue

        while len(batch) < 10:
            try:
                batch.append(sig_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        asyncio.create_task(parse_batch(session, batch))


async def parse_batch(session: aiohttp.ClientSession, sigs: list):
    try:
        async with session.post(
            API_URL,
            json={"transactions": sigs},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                return
            txs = await r.json()

        for tx in txs:
            await analyze_tx(session, tx)

    except Exception as e:
        logger.warning(f"parse_batch: {e}")


async def analyze_tx(session: aiohttp.ClientSession, tx: dict):
    sig = tx.get("signature", "")
    ts  = tx.get("timestamp", time.time())

    # ── 原生 SOL 大额 ──────────────────────────────────
    for nt in tx.get("nativeTransfers", []):
        amount_sol = nt.get("amount", 0) / 1e9
        if amount_sol < THRESHOLD_SOL:
            continue

        frm = (nt.get("fromUserAccount") or "").lower()
        to  = (nt.get("toUserAccount")   or "").lower()

        sev = ("critical" if amount_sol >= THRESHOLD_SOL * 5 else
               "high"     if amount_sol >= THRESHOLD_SOL * 2 else "medium")

        ev = {
            "id":         uuid.uuid4().hex,
            "chain":      "SOL",
            "symbol":     "SOLUSDT",
            "event_type": "whale_transfer",
            "from_addr":  frm,
            "to_addr":    to,
            "amount":     amount_sol,
            "amount_usd": amount_sol * _sol_price[0],
            "severity":   sev,
            "meta": {
                "signature":  sig,
                "from_label": KNOWN_EXCHANGES_SOL.get(frm, "") or SMART_WALLETS_SOL.get(frm, ""),
                "to_label":   KNOWN_EXCHANGES_SOL.get(to,  "") or SMART_WALLETS_SOL.get(to,  ""),
            },
            "ts": float(ts),
        }
        await emit(ev)
        logger.info(f"🐋 SOL {amount_sol:,.0f} SOL | ${amount_sol*_sol_price[0]/1e6:.1f}M | {sev}")

    # ── SPL Token 大额 ────────────────────────────────
    for tt in tx.get("tokenTransfers", []):
        amount = float(tt.get("tokenAmount", 0))
        symbol = tt.get("symbol", "TOKEN")
        frm    = (tt.get("fromUserAccount") or "").lower()
        to     = (tt.get("toUserAccount")   or "").lower()

        if amount < 1_000_000:
            continue

        ev = {
            "id":         uuid.uuid4().hex,
            "chain":      "SOL",
            "symbol":     "SOLUSDT",
            "event_type": "spl_transfer",
            "from_addr":  frm,
            "to_addr":    to,
            "amount":     amount,
            "amount_usd": amount,   # SPL token USD 需接 price feed
            "severity":   "medium",
            "meta":       {"token": symbol, "signature": sig},
            "ts":         float(ts),
        }
        await emit(ev)
        logger.info(f"💎 SPL {amount:,.0f} {symbol}")

    # ── NFT 大额成交 ──────────────────────────────────
    nft = tx.get("events", {}).get("nft", {})
    if nft:
        price_sol = nft.get("amount", 0) / 1e9
        if price_sol >= 100:
            nft_name = (nft.get("nfts") or [{}])[0].get("name", "Unknown NFT")
            buyer    = (nft.get("buyer")  or "").lower()
            seller   = (nft.get("seller") or "").lower()
            ev = {
                "id":         uuid.uuid4().hex,
                "chain":      "SOL",
                "symbol":     "SOLUSDT",
                "event_type": "nft_sale",
                "from_addr":  seller,
                "to_addr":    buyer,
                "amount":     price_sol,
                "amount_usd": price_sol * _sol_price[0],
                "severity":   "high" if price_sol >= 500 else "medium",
                "meta":       {"nft_name": nft_name, "signature": sig},
                "ts":         float(ts),
            }
            await emit(ev)
            logger.info(f"🖼️  NFT {nft_name} {price_sol:.1f} SOL")


async def poll_sol_price():
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    if r.ok:
                        d = await r.json()
                        _sol_price[0] = d.get("solana", {}).get("usd", _sol_price[0])
        except Exception:
            pass
        await asyncio.sleep(30)


# ── WSS 主循环 ────────────────────────────────────────
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [sol %(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not HELIUS_KEY:
        logger.error("HELIUS_KEY 未配置，SOL worker 退出")
        return

    asyncio.create_task(poll_sol_price())
    retry = 5

    async with aiohttp.ClientSession() as session:
        asyncio.create_task(consumer(session))

        while True:
            try:
                logger.info("Connecting Helius WSS...")
                async with websockets.connect(WSS_URL, ping_interval=30) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "logsSubscribe",
                        "params": [{"mentions": []}, {"commitment": "confirmed"}],
                    }))
                    confirm = json.loads(await ws.recv())
                    logger.info(f"✅ Helius 已连接 | sub={confirm.get('result')}")
                    retry = 5
                    await producer(ws)

            except Exception as e:
                logger.warning(f"WSS error: {e} | retry {retry}s")
                await asyncio.sleep(retry)
                retry = min(retry * 2, 60)


if __name__ == "__main__":
    asyncio.run(main())
