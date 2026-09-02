"""
services/risk/persistence/circuit_breaker.py

移植自 quant-stack/services/risk-engine/src/circuit_breaker.py
适配 cryptowatch v4 的 shared.db.connections 基础设施

改动：
  - 使用 asyncpg（v4 用异步 PG）替代 psycopg2
  - Redis key 与 v4 现有 cw4:risk:status 对齐
  - 保留熔断状态写 PostgreSQL audit_log 的逻辑
  - 支持自动解除（CIRCUIT_AUTO_RESET_HOURS）

Redis Keys：
  cw4:circuit_breaker:state     → OPEN / CLOSED
  cw4:circuit_breaker:open_ts   → ISO时间戳（触发时间）
  cw4:risk:status               → 完整风控状态JSON（原有，继续维护）
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("risk.circuit_breaker")

# Redis Keys
CB_STATE_KEY = "cw4:circuit_breaker:state"
CB_TS_KEY = "cw4:circuit_breaker:open_ts"
CB_REASON_KEY = "cw4:circuit_breaker:reason"

AUTO_RESET_HOURS = int(os.getenv("CIRCUIT_AUTO_RESET_HOURS", "24"))


class PersistentCircuitBreaker:
    """
    持久化熔断器。
    状态双写：Redis（快速读取）+ PostgreSQL audit_log（不可篡改审计）。
    重启后先从 Redis 恢复状态，Redis 丢失则从 PostgreSQL 重建。
    """

    def __init__(self, redis_client, pg_pool=None):
        self._r = redis_client
        self._pg = pg_pool  # asyncpg Pool，可为 None（降级为只用 Redis）

    # ── 状态读取 ──────────────────────────────────────────
    async def is_open(self) -> bool:
        """每次都从 Redis 读，确保多实例感知。触发自动解除检查。"""
        raw = await self._r.get(CB_STATE_KEY)
        if not raw:
            return False
        state = raw.decode() if isinstance(raw, bytes) else raw
        if state.upper() == "OPEN":
            await self._check_auto_reset()
            # 重新读（auto reset 可能已修改）
            raw2 = await self._r.get(CB_STATE_KEY)
            state2 = (raw2.decode() if isinstance(raw2, bytes) else raw2) or "CLOSED"
            return state2.upper() == "OPEN"
        return False

    async def get_state(self) -> dict:
        """返回完整熔断状态，供 risk/main.py 写入 cw4:risk:status"""
        raw_state = await self._r.get(CB_STATE_KEY)
        raw_ts = await self._r.get(CB_TS_KEY)
        raw_reason = await self._r.get(CB_REASON_KEY)

        state = (raw_state.decode() if isinstance(raw_state, bytes) else raw_state) or "CLOSED"
        ts_str = (raw_ts.decode() if isinstance(raw_ts, bytes) else raw_ts) or ""
        reason = (raw_reason.decode() if isinstance(raw_reason, bytes) else raw_reason) or ""

        open_duration_h = None
        if ts_str and state.upper() == "OPEN":
            try:
                open_ts = datetime.fromisoformat(ts_str)
                if open_ts.tzinfo is None:
                    open_ts = open_ts.replace(tzinfo=timezone.utc)
                open_duration_h = round((datetime.now(timezone.utc) - open_ts).total_seconds() / 3600, 2)
            except Exception:
                pass

        return {
            "circuit_broken": state.upper() == "OPEN",
            "circuit_state": state.upper(),
            "circuit_reason": reason,
            "circuit_open_ts": ts_str,
            "open_duration_h": open_duration_h,
            "auto_reset_hours": AUTO_RESET_HOURS,
        }

    # ── 触发熔断 ──────────────────────────────────────────
    async def trip(
        self,
        reason: str,
        drawdown: float = 0.0,
        daily_loss: float = 0.0,
    ) -> None:
        now = datetime.now(timezone.utc)
        pipe = self._r.pipeline()
        pipe.set(CB_STATE_KEY, "OPEN")
        pipe.set(CB_TS_KEY, now.isoformat())
        pipe.set(CB_REASON_KEY, reason)
        await pipe.execute()

        logger.critical(
            f"💣 CIRCUIT BREAKER OPEN | reason={reason} drawdown={drawdown:.2%} daily_loss={daily_loss:.2%}"
        )

        # PostgreSQL 审计记录（不可篡改）
        await self._log_event("OPEN", reason, drawdown, daily_loss)

    # ── 解除熔断 ──────────────────────────────────────────
    async def reset(self, reason: str = "手动解除") -> None:
        pipe = self._r.pipeline()
        pipe.set(CB_STATE_KEY, "CLOSED")
        pipe.delete(CB_TS_KEY)
        pipe.delete(CB_REASON_KEY)
        await pipe.execute()

        logger.warning(f"✅ CIRCUIT BREAKER CLOSED | reason={reason}")
        await self._log_event("CLOSED", reason)

    # ── 自动解除检查 ──────────────────────────────────────
    async def _check_auto_reset(self) -> None:
        if AUTO_RESET_HOURS <= 0:
            return

        raw_ts = await self._r.get(CB_TS_KEY)
        if not raw_ts:
            return

        try:
            ts_str = raw_ts.decode() if isinstance(raw_ts, bytes) else raw_ts
            open_ts = datetime.fromisoformat(ts_str)
            if open_ts.tzinfo is None:
                open_ts = open_ts.replace(tzinfo=timezone.utc)
            elapsed = datetime.now(timezone.utc) - open_ts
            if elapsed >= timedelta(hours=AUTO_RESET_HOURS):
                await self.reset(reason=f"自动解除（已持续 {elapsed.total_seconds() / 3600:.1f}h）")
        except Exception as e:
            logger.error(f"auto_reset error: {e}")

    # ── PostgreSQL 审计 ───────────────────────────────────
    async def _log_event(
        self,
        event: str,
        reason: str,
        drawdown: float = 0.0,
        daily_loss: float = 0.0,
    ) -> None:
        """写入 audit_log 表（已存在于 TimescaleDB schema）"""
        if not self._pg:
            return
        try:
            async with self._pg.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO audit_log
                    (event_type, payload, service)
                    VALUES ($1, $2, $3)
                """,
                    f"CIRCUIT_BREAKER_{event}",
                    json.dumps(
                        {
                            "reason": reason,
                            "drawdown": round(drawdown, 4),
                            "daily_loss": round(daily_loss, 4),
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                    ),
                    "risk-service",
                )
        except Exception as e:
            logger.error(f"audit_log write failed: {e}")

    # ── 从 PostgreSQL 重建状态（Redis 丢失时）────────────
    async def restore_from_pg(self) -> bool:
        """
        Redis 重启丢失数据时，从 audit_log 恢复最新熔断状态。
        返回 True 表示恢复了 OPEN 状态。
        """
        if not self._pg:
            return False
        try:
            async with self._pg.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT event_type, payload, time
                    FROM audit_log
                    WHERE event_type IN ('CIRCUIT_BREAKER_OPEN', 'CIRCUIT_BREAKER_CLOSED')
                    ORDER BY time DESC
                    LIMIT 1
                """)
            if not row:
                return False

            event = row["event_type"]
            payload = row["payload"]
            ts = row["time"]

            if event == "CIRCUIT_BREAKER_OPEN":
                # 检查是否还在有效期内
                if AUTO_RESET_HOURS > 0:
                    elapsed = datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)
                    if elapsed >= timedelta(hours=AUTO_RESET_HOURS):
                        logger.info("restore_from_pg: 熔断已过期，不恢复")
                        return False

                p = payload if isinstance(payload, dict) else json.loads(payload)
                pipe = self._r.pipeline()
                pipe.set(CB_STATE_KEY, "OPEN")
                pipe.set(CB_TS_KEY, ts.isoformat())
                pipe.set(CB_REASON_KEY, p.get("reason", "从PG恢复"))
                await pipe.execute()
                logger.warning(f"restore_from_pg: 恢复 OPEN 状态 reason={p.get('reason')}")
                return True

        except Exception as e:
            logger.error(f"restore_from_pg error: {e}")
        return False
