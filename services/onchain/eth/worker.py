"""
onchain_sentinel/services/onchain/eth/worker.py

ETH 链上事件采集器
数据源: Infura WebSocket（需 INFURA_KEY）
输出:   Redis Stream  onchain.events

检测范围：
  - 原生 ETH 大额转账
  - ERC-20 大额（USDT/USDC/WBTC/stETH/DAI）
  - 已知交易所 / 聪明钱包地址匹配
  - Gas 剧变预警（独立事件类型）
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

logger = logging.getLogger("eth.worker")

REDIS_URL     = os.environ.get("REDIS_URL", "redis://:changeme@redis:6379/0")
INFURA_KEY    = os.environ.get("INFURA_KEY", "")
THRESHOLD_ETH = float(os.environ.get("THRESHOLD_ETH", 500))

WSS_URL  = f"wss://mainnet.infura.io/ws/v3/{INFURA_KEY}"
RPC_URL  = f"https://mainnet.infura.io/v3/{INFURA_KEY}"
STREAM   = "onchain.events"

# ERC-20 Transfer topic
ERC20_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

TOKEN_META = {
    "0xdac17f958d2ee523a2206206994597c13d831ec7": (6,  "USDT",  5_000_000),
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": (6,  "USDC",  5_000_000),
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": (8,  "WBTC",  10),
    "0x6b175474e89094c44da98b954eedeac495271d0f": (18, "DAI",   5_000_000),
    "0xae7ab96520de3a18e5e111b5eaab095312d7fe84": (18, "stETH", 1_000),
}

KNOWN_EXCHANGES = {
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance Hot 14",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance Cold",
    "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase 10",
    "0x503828976d22510aad0201ac7ec88293211d23da": "Coinbase 3",
    "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3": "OKX Hot",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX 6",
}

SMART_WALLETS = {
    "0x7a3f9f9e2bfc6d9e9a9b9c9d9e9f0a0b0c0d0e0f": "Alpha Whale #1",
    "0x5cad0000000000000000000000000000000022ee": "MEV Bot #7",
}

_redis: aioredis.Redis = None
_eth_price = [3200.0]


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


# ── RPC 工具 ──────────────────────────────────────────
async def rpc_call(session: aiohttp.ClientSession, method: str, params: list):
    async with session.post(RPC_URL, json={
        "jsonrpc": "2.0", "method": method, "params": params, "id": 1
    }, timeout=aiohttp.ClientTimeout(total=8)) as r:
        d = await r.json()
        return d.get("result")


# ── 区块处理 ──────────────────────────────────────────
async def process_block(session: aiohttp.ClientSession, block_hex: str):
    block_num = int(block_hex, 16)

    block = await rpc_call(session, "eth_getBlockByNumber", [block_hex, True])
    if not block:
        return

    gas_total = 0
    tx_count  = len(block.get("transactions", []))

    tasks = []
    for tx in block.get("transactions", []):
        value_eth = int(tx.get("value", "0x0"), 16) / 1e18
        gas_total += int(tx.get("gasPrice", "0x0"), 16)

        if value_eth >= THRESHOLD_ETH:
            tasks.append(emit_eth_transfer(tx, value_eth, block_num))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    # Gas 均值
    if tx_count:
        gwei_avg = gas_total / tx_count / 1e9
        logger.info(f"Block #{block_num} | {tx_count} txs | Gas {gwei_avg:.1f} Gwei")

    # ERC-20 扫描（并发，不阻塞主循环）
    asyncio.create_task(scan_erc20_logs(session, block_hex, block_num))


async def emit_eth_transfer(tx: dict, value_eth: float, block_num: int):
    frm = (tx.get("from") or "").lower()
    to  = (tx.get("to")   or "contract").lower()

    sev = ("critical" if value_eth >= THRESHOLD_ETH * 10 else
           "high"     if value_eth >= THRESHOLD_ETH * 3  else "medium")

    ev = {
        "id":         uuid.uuid4().hex,
        "chain":      "ETH",
        "symbol":     "ETHUSDT",
        "event_type": "whale_transfer",
        "from_addr":  frm,
        "to_addr":    to,
        "amount":     value_eth,
        "amount_usd": value_eth * _eth_price[0],
        "severity":   sev,
        "meta": {
            "tx_hash":    tx.get("hash", ""),
            "block":      block_num,
            "from_label": KNOWN_EXCHANGES.get(frm) or SMART_WALLETS.get(frm, ""),
            "to_label":   KNOWN_EXCHANGES.get(to)  or SMART_WALLETS.get(to,  ""),
        },
        "ts": time.time(),
    }
    await emit(ev)
    logger.info(f"🐋 ETH {value_eth:.1f} ETH | {sev} | block #{block_num}")


async def scan_erc20_logs(session: aiohttp.ClientSession, block_hex: str, block_num: int):
    try:
        logs = await rpc_call(session, "eth_getLogs", [{
            "fromBlock": block_hex,
            "toBlock":   block_hex,
            "topics":    [ERC20_TRANSFER],
        }])
        if not logs:
            return

        for lg in logs:
            token_addr = lg.get("address", "").lower()
            if token_addr not in TOKEN_META:
                continue

            decimals, symbol, threshold = TOKEN_META[token_addr]
            try:
                amount = int(lg.get("data", "0x0"), 16) / (10 ** decimals)
            except ValueError:
                continue
            if amount < threshold:
                continue

            topics = lg.get("topics", [])
            frm = ("0x" + topics[1][26:]).lower() if len(topics) > 1 else ""
            to  = ("0x" + topics[2][26:]).lower() if len(topics) > 2 else ""

            # USD 估算（简化）
            usd_per_token = {"USDT": 1.0, "USDC": 1.0, "DAI": 1.0,
                             "WBTC": 83000.0, "stETH": _eth_price[0]}.get(symbol, 1.0)
            amount_usd = amount * usd_per_token

            sev = "critical" if amount_usd > 50_000_000 else "high" if amount_usd > 10_000_000 else "medium"

            ev = {
                "id":         uuid.uuid4().hex,
                "chain":      "ETH",
                "symbol":     "ETHUSDT",
                "event_type": "erc20_transfer",
                "from_addr":  frm,
                "to_addr":    to,
                "amount":     amount,
                "amount_usd": amount_usd,
                "severity":   sev,
                "meta": {
                    "token":      symbol,
                    "block":      block_num,
                    "from_label": KNOWN_EXCHANGES.get(frm, ""),
                    "to_label":   KNOWN_EXCHANGES.get(to,  ""),
                },
                "ts": time.time(),
            }
            await emit(ev)
            logger.info(f"💰 ERC20 {amount:,.0f} {symbol} | ${amount_usd/1e6:.1f}M | {sev}")

    except Exception as e:
        logger.warning(f"scan_erc20_logs: {e}")


# ── Gas 轮询补偿 ──────────────────────────────────────
async def poll_gas(session: aiohttp.ClientSession):
    prev = None
    while True:
        try:
            result = await rpc_call(session, "eth_gasPrice", [])
            gwei = int(result, 16) / 1e9
            if prev is not None and abs(gwei - prev) >= 25:
                logger.info(f"⛽ Gas 剧变 {prev:.0f} → {gwei:.0f} Gwei")
                ev = {
                    "id":         uuid.uuid4().hex,
                    "chain":      "ETH",
                    "symbol":     "ETHUSDT",
                    "event_type": "gas_spike",
                    "from_addr":  "",
                    "to_addr":    "",
                    "amount":     gwei,
                    "amount_usd": 0,
                    "severity":   "high" if gwei > 100 else "medium",
                    "meta":       {"prev_gwei": prev, "curr_gwei": gwei},
                    "ts":         time.time(),
                }
                await emit(ev)
            prev = gwei
        except Exception as e:
            logger.warning(f"poll_gas: {e}")
        await asyncio.sleep(30)


async def poll_eth_price():
    while True:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://api.coingecko.com/api/v3/simple/price?ids=ethereum&vs_currencies=usd",
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as r:
                    if r.ok:
                        d = await r.json()
                        _eth_price[0] = d.get("ethereum", {}).get("usd", _eth_price[0])
        except Exception:
            pass
        await asyncio.sleep(30)


# ── WSS 主循环 ────────────────────────────────────────
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [eth %(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if not INFURA_KEY:
        logger.error("INFURA_KEY 未配置，ETH worker 退出")
        return

    asyncio.create_task(poll_eth_price())
    retry = 5

    async with aiohttp.ClientSession() as session:
        asyncio.create_task(poll_gas(session))

        while True:
            try:
                logger.info("Connecting Infura WSS...")
                async with websockets.connect(WSS_URL, ping_interval=20) as ws:
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0",
                        "method": "eth_subscribe",
                        "params": ["newHeads"],
                        "id": 1,
                    }))
                    resp = json.loads(await ws.recv())
                    sub_id = resp.get("result")
                    logger.info(f"✅ Infura 已连接 | sub={sub_id}")
                    retry = 5

                    async for raw in ws:
                        msg    = json.loads(raw)
                        result = msg.get("params", {}).get("result", {})
                        block_hex = result.get("number")
                        if block_hex:
                            asyncio.create_task(process_block(session, block_hex))

            except Exception as e:
                logger.warning(f"WSS error: {e} | retry {retry}s")
                await asyncio.sleep(retry)
                retry = min(retry * 2, 60)


if __name__ == "__main__":
    asyncio.run(main())
