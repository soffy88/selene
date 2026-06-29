import asyncio
import logging
import os
import json
from datetime import datetime, timezone, timedelta
import asyncpg
import aiohttp
import redis.asyncio as redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("healthcheck")

DB_URL = os.environ.get("DB_URL")
REDIS_URL = os.environ.get("REDIS_URL", "redis://helios-redis:6379/3")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# Dead-man's switch: if the execution heartbeat goes stale, alert and halt trading.
DEADMAN_STALE_S = float(os.environ.get("DEADMAN_STALE_S", "180"))

async def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning(f"[ALERT] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # We must not use the internal OKX proxy for Telegram. The NO_PROXY env var handles this.
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            }, timeout=10)
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")

def deadman_age(hb: dict, now_epoch: float) -> float:
    """Seconds since the execution heartbeat. Pure helper for testing (item #10)."""
    return now_epoch - float(hb.get("ts", 0))


async def check_rules(pool, r):
    alerts = []
    now = datetime.now(timezone.utc)
    
    async with pool.acquire() as conn:
        # Rule 1: v2_ticks max(timestamp) age > 5 min -> CRITICAL
        max_tick = await conn.fetchval("SELECT max(timestamp) FROM v2_ticks")
        if max_tick and (now - max_tick).total_seconds() > 300:
            alerts.append(("CRITICAL", "tick_stale", f"v2_ticks stale, last ts: {max_tick}"))

        # Rule 2: v2_lob_snapshots max(timestamp) age > 5 min -> CRITICAL
        max_lob = await conn.fetchval("SELECT max(timestamp) FROM v2_lob_snapshots")
        if max_lob and (now - max_lob).total_seconds() > 300:
            alerts.append(("CRITICAL", "lob_stale", f"v2_lob_snapshots stale, last ts: {max_lob}"))

        # Rule 3: v2_derivatives_snapshots max(timestamp) age > 5 min -> WARNING
        max_deriv = await conn.fetchval("SELECT max(timestamp) FROM v2_derivatives_snapshots")
        if max_deriv and (now - max_deriv).total_seconds() > 300:
            alerts.append(("WARNING", "deriv_stale", f"v2_derivatives stale, last ts: {max_deriv}"))

        # Rule 4: v2_bars_4h missing (age > 4h30m from expected bar boundary) -> CRITICAL
        max_bar = await conn.fetchval("SELECT max(time) FROM v2_bars_4h")
        if max_bar:
            # next bar boundary is max_bar + 4h. It should be written by max_bar + 4h + 30m.
            expected_next = max_bar + timedelta(hours=4)
            if now > expected_next + timedelta(minutes=30):
                alerts.append(("CRITICAL", "bar_missing", f"v2_bars_4h missing, expected bar at {expected_next} not found yet"))

    # Rule 5: paper_engine container logs (not implemented via log scanning here, relying on last_bar_ts instead for simplicity)
    # A stuck paper_engine won't update last_bar_ts.
    last_paper_ts_raw = await r.get("v2:paper:last_bar_ts")
    if last_paper_ts_raw and max_bar:
        last_paper_ts = datetime.fromisoformat(last_paper_ts_raw.decode() if isinstance(last_paper_ts_raw, bytes) else last_paper_ts_raw)
        if last_paper_ts < max_bar and (now - max_bar).total_seconds() > 3600 * 5: # 5 hours behind
            alerts.append(("CRITICAL", "paper_stuck", f"paper_engine stuck, last processed {last_paper_ts}, DB has {max_bar}"))

    # Rule 6: execution dead-man's switch (item #10). The execution reconcile loop
    # writes cw4:execution:heartbeat each cycle; if it exists but is stale the service
    # has wedged/crashed while positions may be open → alert AND trip the halt so no
    # new orders are placed until an operator clears it.
    hb_raw = await r.get("cw4:execution:heartbeat")
    if hb_raw:
        try:
            hb = json.loads(hb_raw.decode() if isinstance(hb_raw, bytes) else hb_raw)
            age = deadman_age(hb, now.timestamp())
            if age > DEADMAN_STALE_S:
                alerts.append(("CRITICAL", "exec_deadman",
                               f"execution heartbeat stale {age:.0f}s (>{DEADMAN_STALE_S:.0f}s) — halting trading"))
                if not await r.get("cw4:execution:halt"):
                    await r.set("cw4:execution:halt", json.dumps(
                        {"reason": "deadman_heartbeat_stale", "age_s": round(age, 1),
                         "tripped_by": "healthcheck", "ts": now.timestamp()}))
                    logger.error(f"dead-man switch tripped: execution heartbeat stale {age:.0f}s")
        except Exception as e:
            logger.warning(f"dead-man check failed to parse heartbeat: {e}")

    # Deduplicate and send
    for severity, rule_id, msg in alerts:
        dedup_key = f"hc_dedup:{rule_id}"
        if not await r.exists(dedup_key):
            await r.set(dedup_key, "1", ex=1800) # 30 min dedup
            await send_telegram(f"[{severity}] {msg}")
            logger.info(f"Alert triggered: [{severity}] {msg}")

async def main():
    pool = await asyncpg.create_pool(DB_URL)
    r = redis.from_url(REDIS_URL)
    logger.info("Healthcheck service started")
    
    while True:
        try:
            await check_rules(pool, r)
        except Exception as e:
            logger.error(f"Error checking rules: {e}")
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
