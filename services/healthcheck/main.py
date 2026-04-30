"""
selene 健康度监控 daemon
- 按 configs/healthcheck-rules.yaml 周期检查
- 异常写 system.alerts stream → helios-alert-notifier 推 Telegram
- 30min 去重防告警轰炸
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import docker
import redis.asyncio as aioredis
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("healthcheck")

REDIS_URL = os.environ.get("REDIS_URL", "redis://helios-redis:6379/3")
PG_URL = os.environ.get("TIMESCALE_URL", "").replace("postgresql+asyncpg://", "postgresql://")
RULES_PATH = os.environ.get("RULES_PATH", "/app/configs/healthcheck-rules.yaml")
ALERT_STREAM = "system.alerts"
DEDUP_PREFIX = "healthcheck:dedup:"


class HealthChecker:
    def __init__(self):
        self.rules = []
        self.redis = None
        self.pg_pool = None
        self.docker = None
        self.interval = 60
        self.dedup_min = 30

    async def setup(self):
        cfg = yaml.safe_load(Path(RULES_PATH).read_text())
        self.rules = cfg["rules"]
        self.interval = cfg["meta"]["check_interval_seconds"]
        self.dedup_min = cfg["meta"]["alert_dedup_minutes"]
        self.redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        if PG_URL:
            try:
                self.pg_pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=2)
            except Exception as e:
                logger.warning(f"pg pool unavailable: {e}")
        try:
            self.docker = docker.from_env()
        except Exception as e:
            logger.warning(f"docker client unavailable: {e}")
        logger.info(f"loaded {len(self.rules)} rules, interval={self.interval}s")

    async def run(self):
        while True:
            for rule in self.rules:
                try:
                    breached, msg = await self.check_rule(rule)
                    if breached:
                        await self.fire_alert(rule, msg)
                except Exception as e:
                    logger.error(f"rule {rule['id']} check failed: {e}")
            await asyncio.sleep(self.interval)

    async def check_rule(self, rule):
        check = rule["check"]
        if check == "data_freshness":
            return await self._chk_freshness(rule)
        if check == "redis_stream_freshness":
            return await self._chk_stream_freshness(rule)
        if check == "redis_value":
            return await self._chk_redis_value(rule)
        if check == "docker_health":
            return await self._chk_docker(rule)
        if check == "redis_memory":
            return await self._chk_redis_mem(rule)
        return False, ""

    async def _chk_freshness(self, rule):
        if not self.pg_pool:
            return False, ""
        async with self.pg_pool.acquire() as c:
            row = await c.fetchrow(
                f"SELECT max({rule['time_column']}) latest, NOW()-max({rule['time_column']}) age FROM {rule['table']}"
            )
        if row["latest"] is None:
            return True, f"{rule['table']} 表为空"
        age_sec = row["age"].total_seconds()
        if age_sec > rule["max_age_minutes"] * 60:
            return True, f"{rule['table']} 最新 {row['latest']}, 滞后 {int(age_sec/60)}min"
        return False, ""

    async def _chk_stream_freshness(self, rule):
        """Check Redis stream last-entry timestamp via auto-generated stream ID (ms since epoch)."""
        try:
            db = rule.get("db", 3)
            r_url = REDIS_URL.rsplit("/", 1)[0] + f"/{db}"
            r = aioredis.from_url(r_url, decode_responses=True)
            entries = await r.xrevrange(rule["stream"], count=1)
            await r.aclose()
            if not entries:
                return True, f"stream {rule['stream']} 为空"
            entry_id = entries[0][0]
            ms = int(entry_id.split("-")[0])
            age_sec = (time.time() * 1000 - ms) / 1000
            if age_sec > rule["max_age_minutes"] * 60:
                return True, f"stream {rule['stream']} 最新条目 {int(age_sec/60)}min 前"
        except Exception as e:
            logger.debug(f"stream freshness {rule['stream']}: {e}")
        return False, ""

    async def _chk_redis_value(self, rule):
        key = rule["key"]
        try:
            if "json_field" in rule:
                raw = await self.redis.get(key)
                val = json.loads(raw or "{}").get(rule["json_field"])
            else:
                val = await self.redis.get(key)
            if val is None:
                return False, ""
            val = float(val)
            op, thr = rule["operator"], float(rule["threshold"])
            cond = (op == ">" and val > thr) or (op == "<" and val < thr) or (op == "=" and val == thr)
            if cond:
                return True, f"{key} = {val} {op} {thr}"
        except Exception as e:
            logger.debug(f"redis_value {key} parse err: {e}")
        return False, ""

    async def _chk_docker(self, rule):
        if not self.docker:
            return False, ""
        pat = re.compile(rule["pattern"])
        target = rule["state"]
        bad = []
        for c in self.docker.containers.list(all=True):
            if not pat.match(c.name):
                continue
            status = c.status
            health = c.attrs.get("State", {}).get("Health", {}).get("Status", "")
            if target == "unhealthy" and health == "unhealthy":
                bad.append(c.name)
            elif target == "restarting" and status == "restarting":
                bad.append(c.name)
        if bad:
            return True, f"{target}: {', '.join(bad)}"
        return False, ""

    async def _chk_redis_mem(self, rule):
        cursor = 0
        oversize = []
        while True:
            cursor, keys = await self.redis.scan(cursor=cursor, match=rule["pattern"], count=100)
            for k in keys:
                try:
                    sz = await self.redis.execute_command("MEMORY", "USAGE", k)
                    if sz and int(sz) > rule["max_bytes"]:
                        oversize.append(f"{k}={int(sz)/1024/1024:.1f}MB")
                except Exception:
                    pass
            if cursor == 0:
                break
        if oversize:
            return True, "; ".join(oversize[:5])
        return False, ""

    async def fire_alert(self, rule, message):
        dedup_key = f"{DEDUP_PREFIX}{rule['id']}"
        if await self.redis.get(dedup_key):
            return
        await self.redis.setex(dedup_key, self.dedup_min * 60, "1")
        payload = {
            "type": "healthcheck",
            "severity": rule["severity"],
            "rule_id": rule["id"],
            "description": rule["description"],
            "message": message,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        await self.redis.xadd(ALERT_STREAM, {"data": json.dumps(payload, ensure_ascii=False)})
        logger.warning(f"ALERT {rule['severity']} {rule['id']}: {message}")


async def main():
    h = HealthChecker()
    await h.setup()
    await h.run()


if __name__ == "__main__":
    asyncio.run(main())
