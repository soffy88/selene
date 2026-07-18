"""
CryptoWatch v4 — Redis Client
Connection factory + Stream publish/consume helpers.
All services use this module for Redis interaction.
"""

import asyncio
import json
import logging
from typing import AsyncIterator, Callable, Optional

import redis.asyncio as aioredis
from redis.exceptions import TimeoutError as RedisTimeoutError

from shared.events.streams import MAXLEN, encode, decode, StreamEvent

logger = logging.getLogger(__name__)

_redis: Optional[aioredis.Redis] = None


def init_redis(url: str) -> aioredis.Redis:
    global _redis
    # socket_timeout + keepalive:redis 服务端重启后留下的半开 TCP 连接会让
    # 阻塞式 XREADGROUP 永远挂在 await 上(consume() 的外层重试根本轮不到执行,
    # 2026-07-12 事故 gateway/notification 消费者就是这么死的)。有超时后异常
    # 走 consume() 的 benign-timeout 分支,连接被拆除,下一条命令自动重连。
    # 30s 需大于 consume() 的 block_ms(默认 1s),避免正常空轮询被误伤。
    _redis = aioredis.from_url(
        url,
        decode_responses=False,
        socket_timeout=30,
        socket_connect_timeout=5,
        socket_keepalive=True,
        health_check_interval=60,
    )
    return _redis


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    return _redis


async def health_check() -> bool:
    try:
        await get_redis().ping()
        return True
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        return False


# ── Stream Publisher ──────────────────────────────────────────────────────────


async def publish(stream: str, data: dict) -> str:
    """
    Publish an event to a Redis Stream.
    Returns the message ID assigned by Redis.
    Uses MAXLEN to cap stream size (approximate trimming for performance).
    """
    r = get_redis()
    maxlen = MAXLEN.get(stream, 10_000)
    msg_id = await r.xadd(stream, encode(data), maxlen=maxlen, approximate=True)
    return msg_id.decode() if isinstance(msg_id, bytes) else msg_id


# ── Consumer Group Manager ────────────────────────────────────────────────────


async def ensure_consumer_group(stream: str, group: str) -> None:
    """Create consumer group if it doesn't exist. Idempotent."""
    r = get_redis()
    try:
        await r.xgroup_create(stream, group, id="0", mkstream=True)
        logger.info(f"Created consumer group '{group}' on stream '{stream}'")
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


async def consume(
    stream: str,
    group: str,
    consumer: str,
    handler: Callable[[StreamEvent], None],
    batch_size: int = 10,
    block_ms: int = 1000,
) -> None:
    """
    Consume messages from a Redis Stream using Consumer Group semantics.
    Runs forever — call as asyncio.create_task().
    Acknowledges messages after successful handler execution.
    """
    r = get_redis()
    await ensure_consumer_group(stream, group)

    # First: recover any pending messages from previous runs
    pending = await r.xreadgroup(group, consumer, {stream: "0"}, count=100)
    if pending:
        for s, messages in pending:
            for msg_id, fields in messages:
                event = StreamEvent(
                    stream=stream,
                    msg_id=msg_id.decode() if isinstance(msg_id, bytes) else msg_id,
                    data=decode(fields),
                )
                try:
                    await handler(event)
                    await r.xack(stream, group, msg_id)
                except Exception as e:
                    logger.error(f"Handler error on pending msg {msg_id}: {e}")

    # Main loop: consume new messages
    logger.info(f"Consumer '{consumer}' listening on '{stream}' (group='{group}')")
    while True:
        try:
            results = await r.xreadgroup(
                group, consumer, {stream: ">"}, count=batch_size, block=block_ms
            )
            if not results:
                continue
            for s, messages in results:
                for msg_id, fields in messages:
                    event = StreamEvent(
                        stream=stream,
                        msg_id=msg_id.decode() if isinstance(msg_id, bytes) else msg_id,
                        data=decode(fields),
                    )
                    try:
                        await handler(event)
                        await r.xack(stream, group, msg_id)
                    except Exception as e:
                        logger.error(f"Handler error for {msg_id}: {e}", exc_info=True)
                        # Don't ACK on error — message stays pending for retry
        except asyncio.CancelledError:
            logger.info(f"Consumer '{consumer}' stopped")
            break
        except (RedisTimeoutError, asyncio.TimeoutError):
            # Benign: with a blocking XREADGROUP (block=block_ms) and no client socket_timeout,
            # redis-py's read timeout races the server's BLOCK window, so a quiet stream raises
            # a spurious TimeoutError every empty cycle. It means "no new messages" — just loop
            # (don't log a traceback or sleep 1s, which would drop the next block window).
            continue
        except Exception as e:
            logger.error(f"Stream consumer error: {e}", exc_info=True)
            await asyncio.sleep(1)


async def get_stream_info(stream: str) -> dict:
    """Get stream length and consumer group stats."""
    r = get_redis()
    try:
        info = await r.xinfo_stream(stream)
        groups = await r.xinfo_groups(stream)
        return {
            "length": info.get(b"length", info.get("length", 0)),
            "groups": len(groups),
            "first_entry": str(info.get(b"first-entry", "")),
            "last_entry": str(info.get(b"last-entry", "")),
        }
    except Exception:
        return {"length": 0, "groups": 0}


# ── Cache helpers (Redis Hash/String for real-time state) ─────────────────────


async def set_json(key: str, value: dict, ex: int = None) -> None:
    r = get_redis()
    await r.set(key, json.dumps(value), ex=ex)


async def get_json(key: str) -> Optional[dict]:
    r = get_redis()
    raw = await r.get(key)
    return json.loads(raw) if raw else None


async def hset_json(name: str, key: str, value: dict) -> None:
    r = get_redis()
    await r.hset(name, key, json.dumps(value))


async def hget_json(name: str, key: str) -> Optional[dict]:
    r = get_redis()
    raw = await r.hget(name, key)
    return json.loads(raw) if raw else None


async def hgetall_json(name: str) -> dict:
    r = get_redis()
    raw = await r.hgetall(name)
    result = {}
    for k, v in raw.items():
        key = k.decode() if isinstance(k, bytes) else k
        try:
            result[key] = json.loads(v)
        except Exception:
            result[key] = v
    return result
