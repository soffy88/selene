import asyncio
import logging
import os
import json
import time
from datetime import datetime, timezone, timedelta
import asyncpg
import aiohttp
import redis.asyncio as redis

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("healthcheck")

DB_URL = os.environ.get("DB_URL")
REDIS_URL = os.environ.get("REDIS_URL", "redis://helios-redis:6379/3")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# Dead-man's switch: if the execution heartbeat goes stale, alert and halt trading.
DEADMAN_STALE_S = float(os.environ.get("DEADMAN_STALE_S", "180"))
# Cold-start grace before we flag a table as *empty*: a freshly-deployed collector
# legitimately has 0 rows for a few minutes. Past this, an empty table means the feed
# is down. (2026-07-03 audit P0-d — the blind spot that hid a 13h full outage.)
_SERVICE_START = time.time()
EMPTY_GRACE_S = float(os.environ.get("EMPTY_GRACE_S", "900"))


def _freshness_alert(rule_id, label, max_ts, now, stale_s, severity, uptime_s):
    """One alert for a table that is either STALE (has rows but old) or EMPTY (no rows
    at all, past the cold-start grace). The old rules guarded on `if max_ts and ...`, so
    an empty table — where max(ts) is NULL — never fired; a total feed outage looked
    identical to 'healthy'. Empty is the more severe, more common failure, so we catch
    it explicitly here."""
    if max_ts is None:
        if uptime_s > EMPTY_GRACE_S:
            return (
                "CRITICAL",
                f"{rule_id}_empty",
                f"{label} is EMPTY — 0 rows (data pipeline down / collector not writing)",
            )
        return None
    if (now - max_ts).total_seconds() > stale_s:
        return (severity, rule_id, f"{label} stale, last ts: {max_ts}")
    return None


async def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning(f"[ALERT] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # We must not use the internal OKX proxy for Telegram. The NO_PROXY env var handles this.
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": message,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")


def deadman_age(hb: dict, now_epoch: float) -> float:
    """Seconds since the execution heartbeat. Pure helper for testing (item #10)."""
    return now_epoch - float(hb.get("ts", 0))


async def check_rules(pool, r):
    alerts = []
    now = datetime.now(timezone.utc)
    uptime_s = time.time() - _SERVICE_START

    async with pool.acquire() as conn:
        # Rules 1-3: microstructure feeds — STALE (>5min) OR EMPTY (0 rows past grace).
        for rule_id, label, table, severity in (
            ("tick_stale", "v2_ticks", "v2_ticks", "CRITICAL"),
            ("lob_stale", "v2_lob_snapshots", "v2_lob_snapshots", "CRITICAL"),
            ("deriv_stale", "v2_derivatives", "v2_derivatives_snapshots", "WARNING"),
        ):
            max_ts = await conn.fetchval(f"SELECT max(timestamp) FROM {table}")
            a = _freshness_alert(rule_id, label, max_ts, now, 300, severity, uptime_s)
            if a:
                alerts.append(a)

        # Rule 4: v2_bars_4h — EMPTY (0 rows past grace) OR missing the latest bar.
        max_bar = await conn.fetchval("SELECT max(time) FROM v2_bars_4h")
        if max_bar is None:
            if uptime_s > EMPTY_GRACE_S:
                alerts.append(
                    (
                        "CRITICAL",
                        "bar_empty",
                        "v2_bars_4h is EMPTY — 0 rows (bar foundation gone; nothing downstream can run)",
                    )
                )
        else:
            # next bar boundary is max_bar + 4h. It should be written by max_bar + 4h + 30m.
            expected_next = max_bar + timedelta(hours=4)
            if now > expected_next + timedelta(minutes=30):
                alerts.append(
                    (
                        "CRITICAL",
                        "bar_missing",
                        f"v2_bars_4h missing, expected bar at {expected_next} not found yet",
                    )
                )

        # Rule 4b: state-dwell anomaly (Wave S2C). A trend/coiling state stuck too long is
        # the "Surging pinned 54 bars, nobody noticed" failure from the diagnosis. Only the
        # ACTIVE states (Surging / Coiling) alert — long Drifting_* dwell is normal and quiet.
        dwell_rows = await conn.fetch(
            "SELECT state FROM v2_state_history ORDER BY timestamp DESC LIMIT 200"
        )
        if dwell_rows:
            cur_state = dwell_rows[0]["state"]
            dwell = 0
            for row in dwell_rows:
                if row["state"] == cur_state:
                    dwell += 1
                else:
                    break
            if cur_state in ("Surging", "Coiling") and dwell > 42:  # 7 days x 6 bars
                alerts.append(
                    (
                        "WARNING",
                        "state_dwell_anomaly",
                        f"state '{cur_state}' has held {dwell} 4H bars (>42 / 7d) — "
                        "check the state machine isn't stuck (Drifting_* long dwell is normal)",
                    )
                )

    # Rule 5: paper_engine container logs (not implemented via log scanning here, relying on last_bar_ts instead for simplicity)
    # A stuck paper_engine won't update last_bar_ts.
    last_paper_ts_raw = await r.get("v2:paper:last_bar_ts")
    if last_paper_ts_raw and max_bar:
        last_paper_ts = datetime.fromisoformat(
            last_paper_ts_raw.decode()
            if isinstance(last_paper_ts_raw, bytes)
            else last_paper_ts_raw
        )
        if (
            last_paper_ts < max_bar and (now - max_bar).total_seconds() > 3600 * 5
        ):  # 5 hours behind
            alerts.append(
                (
                    "CRITICAL",
                    "paper_stuck",
                    f"paper_engine stuck, last processed {last_paper_ts}, DB has {max_bar}",
                )
            )

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
                alerts.append(
                    (
                        "CRITICAL",
                        "exec_deadman",
                        f"execution heartbeat stale {age:.0f}s (>{DEADMAN_STALE_S:.0f}s) — halting trading",
                    )
                )
                if not await r.get("cw4:execution:halt"):
                    await r.set(
                        "cw4:execution:halt",
                        json.dumps(
                            {
                                "reason": "deadman_heartbeat_stale",
                                "age_s": round(age, 1),
                                "tripped_by": "healthcheck",
                                "ts": now.timestamp(),
                            }
                        ),
                    )
                    logger.error(
                        f"dead-man switch tripped: execution heartbeat stale {age:.0f}s"
                    )
        except Exception as e:
            logger.warning(f"dead-man check failed to parse heartbeat: {e}")

    # Deduplicate and send
    for severity, rule_id, msg in alerts:
        dedup_key = f"hc_dedup:{rule_id}"
        if not await r.exists(dedup_key):
            await r.set(dedup_key, "1", ex=1800)  # 30 min dedup
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
