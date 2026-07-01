"""Stream consumer benign-timeout handling.

With a blocking XREADGROUP (block=block_ms) and no client socket_timeout, redis-py's read
timeout races the server's BLOCK window, so a quiet stream raises a spurious
redis TimeoutError every empty cycle. That must be treated as "no new messages" (loop),
NOT as an error (which logged a traceback and slept 1s, dropping the next window).
"""
import asyncio

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

import shared.db.redis_client as rc


class _FakeRedis:
    def __init__(self, main_behaviors):
        self._b = list(main_behaviors)
        self.acked = []

    async def xreadgroup(self, group, consumer, streams, count=None, block=None):
        if list(streams.values())[0] == "0":     # pending-recovery call
            return []
        b = self._b.pop(0)
        if isinstance(b, BaseException):
            raise b
        return b

    async def xack(self, stream, group, msg_id):
        self.acked.append(msg_id)


def _run(monkeypatch, main_behaviors, handler):
    fake = _FakeRedis(main_behaviors)
    monkeypatch.setattr(rc, "get_redis", lambda: fake)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(rc, "ensure_consumer_group", _noop)
    asyncio.run(rc.consume("s", "g", "c", handler, block_ms=1))
    return fake


def test_timeout_is_benign_not_error(monkeypatch, caplog):
    seen = []

    async def handler(ev):
        seen.append(ev)

    # main loop: spurious TimeoutError, then cancel to break
    with caplog.at_level("ERROR"):
        _run(monkeypatch, [RedisTimeoutError("Timeout reading from redis"),
                           asyncio.CancelledError()], handler)
    assert seen == []                                  # no messages, none handled
    assert "Stream consumer error" not in caplog.text  # timeout did NOT hit the error path


def test_real_message_still_processed(monkeypatch):
    seen = []

    async def handler(ev):
        seen.append(ev.data)

    # one real message (id, {field:val}) then a benign timeout then cancel
    msg = [("s", [(b"1-0", {b"k": b"v"})])]
    fake = _run(monkeypatch, [msg, RedisTimeoutError("x"), asyncio.CancelledError()], handler)
    assert len(seen) == 1                 # message delivered to the handler
    assert fake.acked == [b"1-0"]         # and acked
