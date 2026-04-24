"""
onchain_sentinel/services/onchain/btc/worker.py

BTC 链上事件采集器（升级版）
数据源: Mempool.space WebSocket（免费无需 Key）
输出: Redis Stream  onchain.events（供 main.py 的 OnchainSentinel 消费）

相比旧版改进：
  - 不再 print 到 stdout，而是 xadd 到 Redis Stream
  - 携带结构化字段（amount_usd / severity / from_addr / to_addr）
  - 接入已知地址标签库
"""

import asyncio
import json
import logging
import os
import time
import uuid

import aiohttp
import redis.asyncio as aioredis
import websockets

logger = logging.getLogger("btc.worker")

REDIS_URL       = os.environ.get("REDIS_URL", "redis://:changeme@redis:6379/0")
THRESHOLD_BTC   = float(os.environ.get("THRESHOLD_BTC", 100))
THRESHOLD_MINER = float(os.environ.get("THRESHOLD_MINER", 500))

WSS_URL   = "wss://mempool.space/api/v1/ws"
API_BASE  = "https://mempool.space/api"
STREAM    = "onchain.events"

KNOWN_EXCHANGES = {
    "1ck6khy6mhgyvmrq4paafkydrg1ejbh1ce": "Binance",
    "34xp4vrocgjym3xr7ycvpfhocnxv4twseo": "Binance Cold",
    "3m219kr5venenb47ewrpfwyb5jq2djxrp6": "OKX",
}
KNOWN_MINERS = {
    "bc1qjasf9z3h7w3jspkhtgatgpyvvzgpa2wwd2lr38": "F2Pool",
    "bc1qcd6cf3ml4x8d2xshlv9fcuqh7p72cfnf4j3gny": "AntPool",
    "bc1qv8mmd4pxe3y6dgs3hp9yr47rnkqpas9afwcn4x": "ViaBTC",
}

_redis: aioredis.Redis = None


async def get_redis():
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=False)
    return _redis


async def emit_event(event: dict):
    """写入 Redis Stream"""
    r = await get_redis()
    encoded = {k: json.dumps(v) if not isinstance(v, (str, bytes)) else v
               for k, v in event.items()}
    await r.xadd(STREAM, encoded, maxlen=50000, approximate=True)


async def analyze_tx(session: aiohttp.ClientSession, txid: str, btc_price: float):
    try:
        async with session.get(f"{API_BASE}/tx/{txid}",
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200:
                return
            tx = await r.json()

        out_total_btc = sum(o.get("value", 0) for o in tx.get("vout", [])) / 1e8
        if out_total_btc < THRESHOLD_BTC:
            return

        # 地址提取
        in_addrs  = [i.get("prevout", {}).get("scriptpubkey_address", "") for i in tx.get("vin", [])]
        out_addrs = [o.get("scriptpubkey_address", "") for o in tx.get("vout", [])]

        primary_from = in_addrs[0]  if in_addrs  else ""
        primary_to   = out_addrs[0] if out_addrs else ""

        fa = primary_from.lower()
        ta = primary_to.lower()

        # 严重度判断
        if out_total_btc >= THRESHOLD_BTC * 10:
            severity = "critical"
        elif out_total_btc >= THRESHOLD_BTC * 3:
            severity = "high"
        elif out_total_btc >= THRESHOLD_BTC:
            severity = "medium"
        else:
            severity = "low"

        # 检测休眠地址
        first_seen = tx.get("status", {}).get("block_time", 0)
        dormant_years = 0
        if first_seen:
            age_days = (time.time() - first_seen) / 86400
            dormant_years = int(age_days / 365)

        event = {
            "id":          uuid.uuid4().hex,
            "chain":       "BTC",
            "symbol":      "BTCUSDT",
            "event_type":  "miner_transfer" if fa in KNOWN_MINERS else "whale_transfer",
            "from_addr":   primary_from,
            "to_addr":     primary_to,
            "amount":      out_total_btc,
            "amount_usd":  out_total_btc * btc_price,
            "severity":    severity,
            "meta":        {
                "txid":          txid,
                "dormant_years": dormant_years,
                "in_count":      len(in_addrs),
                "out_count":     len(out_addrs),
                "from_label":    KNOWN_MINERS.get(fa) or KNOWN_EXCHANGES.get(fa, ""),
                "to_label":      KNOWN_EXCHANGES.get(ta, ""),
            },
            "ts": time.time(),
        }

        await emit_event(event)
        logger.info(f"🐋 emit BTC {out_total_btc:.1f} BTC | {severity} | {txid[:16]}...")

    except Exception as e:
        logger.warning(f"analyze_tx {txid}: {e}")


async def poll_mempool_txs(session: aiohttp.ClientSession, btc_price_ref: list):
    seen = set()
    while True:
        try:
            async with session.get(f"{API_BASE}/mempool/recent",
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.ok:
                    txs = await r.json()
                    tasks = []
                    for tx in txs:
                        txid = tx.get("txid", "")
                        val  = tx.get("value", 0) / 1e8
                        if txid and txid not in seen and val >= THRESHOLD_BTC:
                            seen.add(txid)
                            tasks.append(analyze_tx(session, txid, btc_price_ref[0]))
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    if len(seen) > 3000:
                        seen.clear()
        except Exception as e:
            logger.warning(f"poll_mempool: {e}")
        await asyncio.sleep(60)


async def poll_btc_price(price_ref: list):
    """简单价格轮询"""
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    if r.ok:
                        d = await r.json()
                        price_ref[0] = d.get("bitcoin", {}).get("usd", price_ref[0])
        except Exception:
            pass
        await asyncio.sleep(30)


async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [btc %(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    btc_price = [83000.0]  # 可变引用

    async with aiohttp.ClientSession() as session:
        asyncio.create_task(poll_mempool_txs(session, btc_price))
        asyncio.create_task(poll_btc_price(btc_price))

        retry = 5
        while True:
            try:
                logger.info("Connecting to Mempool.space WSS...")
                async with websockets.connect(WSS_URL, ping_interval=30) as ws:
                    await ws.send(json.dumps({"action": "want", "data": ["blocks"]}))
                    logger.info("✅ Mempool.space connected")
                    retry = 5
                    async for raw in ws:
                        data = json.loads(raw)
                        if "block" in data:
                            b = data["block"]
                            logger.info(
                                f"⛏  Block #{b.get('height')} | "
                                f"{b.get('tx_count')} txs | "
                                f"fee {b.get('extras', {}).get('medianFee', '?')} sat/vB"
                            )
            except Exception as e:
                logger.warning(f"WSS error: {e} | retry in {retry}s")
                await asyncio.sleep(retry)
                retry = min(retry * 2, 60)


if __name__ == "__main__":
    asyncio.run(main())
