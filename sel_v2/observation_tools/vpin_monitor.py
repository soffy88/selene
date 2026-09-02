"""ICT-1 VPIN live monitor (v2.2 lens batch — observation-only service).

Tick-driven, so it cannot ride the bar-driven ObservationRunner: a standalone
service on the capture_monitor pattern with a short incremental loop. Every
POLL_SECONDS it pulls new v2_ticks rows past a (timestamp, trade_id) cursor,
feeds the streaming VPINCalculator, and — once the 100-bucket warmup is over —
upserts a `vpin` row into v2_inverse_vocab_events whenever a completed bucket's
VPIN exceeds the rolling p95 (frozen signal threshold, analysis/lens_verdict_v1.md).

V_bucket is ADAPTIVE per the candidate-pool definition (30-day avg daily volume
/ 50): bootstrapped at startup from min(30d, available) native tick volume — no
bar-volume unit conversion needed. Restart recovery is stateless: replay the
trailing <=30d of ticks through a fresh calculator (deterministic; no state
table). tool_metadata carries `history_days` so the Month-3 evaluation can
exclude the <30d pilot era (H-ICT1a/1b are DATA-INSUFFICIENT-PENDING until
~2026-08-05).

Run once (bootstrap + report current reading):  python -m sel_v2.observation_tools.vpin_monitor
Run forever (v2-vpin-monitor service):          python -m sel_v2.observation_tools.vpin_monitor --serve

Observation-only: never touches strategies/**, states/**, or the epoch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import timedelta

import asyncpg

from sel_v2.observation_tools.vpin import VPINCalculator

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("vpin_monitor")

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
POLL_SECONDS = 300
REPLAY_DAYS = 30  # restart recovery + V_bucket bootstrap window
BATCH = 500_000
SIGNAL_PCTILE = 95  # frozen — analysis/lens_verdict_v1.md


def _dsn() -> str:
    return os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")


async def _bootstrap_v_bucket(pool) -> float:
    """V_bucket = avg daily tick volume over min(REPLAY_DAYS, available) / 50."""
    row = await pool.fetchrow(
        "SELECT min(timestamp) AS lo, max(timestamp) AS hi FROM v2_ticks WHERE symbol=$1",
        SYMBOL,
    )
    if row is None or row["lo"] is None:
        raise SystemExit("v2_ticks empty — nothing to monitor")
    lo = max(row["lo"], row["hi"] - timedelta(days=REPLAY_DAYS))
    vol = await pool.fetchval(
        "SELECT sum(size) FROM v2_ticks WHERE symbol=$1 AND timestamp >= $2",
        SYMBOL,
        lo,
    )
    days = max((row["hi"] - lo).total_seconds() / 86400.0, 0.25)
    v_bucket = float(vol) / days / 50.0
    logger.info(
        "V_bucket bootstrap: %.1f (tick volume %.0f over %.2f days)",
        v_bucket,
        float(vol),
        days,
    )
    return v_bucket


async def _stream_ticks(pool, calc, since_ts, since_id, on_bucket) -> tuple:
    """Feed ticks past the (timestamp, trade_id) cursor into calc; call
    on_bucket(bucket) for every completed bucket. Returns the new cursor."""
    while True:
        rows = await pool.fetch(
            "SELECT timestamp, price, size, side, trade_id FROM v2_ticks "
            "WHERE symbol=$1 AND (timestamp, trade_id) > ($2, $3) "
            "ORDER BY timestamp, trade_id LIMIT $4",
            SYMBOL,
            since_ts,
            since_id,
            BATCH,
        )
        if not rows:
            return since_ts, since_id
        for r in rows:
            for bucket in calc.on_tick(r["timestamp"], float(r["price"]), float(r["size"]), r["side"]):
                await on_bucket(bucket)
        since_ts, since_id = rows[-1]["timestamp"], rows[-1]["trade_id"]
        if len(rows) < BATCH:
            return since_ts, since_id


async def _persist_signal(pool, calc, bucket, history_start) -> None:
    """Upsert one high-VPIN event, keyed (timestamp, vocab)."""
    vpin = calc.vpin
    p95 = calc.percentile(95)
    p97 = calc.percentile(97)
    history_days = (bucket.close_ts - history_start).total_seconds() / 86400.0
    meta = {
        "vpin": vpin,
        "p95": p95,
        "p97": p97,
        "v_bucket": calc.v_bucket,
        "method": "side",
        "bvc_vpin": calc.bvc_vpin,
        "history_days": round(history_days, 2),
    }
    state = await pool.fetchval(
        "SELECT state FROM v2_state_history WHERE timestamp <= $1 ORDER BY timestamp DESC LIMIT 1",
        bucket.close_ts,
    )
    await pool.execute(
        """
        INSERT INTO v2_inverse_vocab_events
            (timestamp, vocab, intensity, associated_state,
             tool_source, observation_only, tool_metadata)
        VALUES ($1, 'vpin', $2, $3, 'ict', TRUE, $4::jsonb)
        ON CONFLICT (timestamp, vocab) DO UPDATE SET
            intensity = EXCLUDED.intensity,
            associated_state = EXCLUDED.associated_state,
            tool_metadata = EXCLUDED.tool_metadata
        """,
        bucket.close_ts,
        float(vpin),
        state,
        json.dumps(meta, default=str),
    )
    logger.info(
        "vpin signal: %.4f > p95 %.4f at %s (state=%s, history %.1fd)",
        vpin,
        p95,
        bucket.close_ts,
        state,
        history_days,
    )


async def _persist_latest(pool, calc, as_of) -> None:
    """Upsert the current VPIN reading into v2_observation_latest (tool_id ICT1)
    so the SEL observation panel shows the lens alongside the bar-driven tools.
    Same one-row-per-tool idiom as runner.persist_latest_observations."""
    vpin = calc.vpin
    p95 = calc.percentile(SIGNAL_PCTILE)
    warmed = calc.completed_buckets >= calc.warmup_buckets
    if vpin is None:
        signal, label, value, threshold, conf = False, "WARMING", 0.0, 0.0, 0.0
    elif not warmed or p95 is None:
        signal, label, value, threshold, conf = False, "WARMING", vpin, 0.0, 0.0
    else:
        signal = vpin > p95
        label = "VPIN_HIGH" if signal else "NORMAL"
        value, threshold = vpin, p95
        conf = min((vpin - p95) / max(p95, 1e-8), 1.0) if signal else 0.0
    try:
        await pool.execute(
            """
            INSERT INTO v2_observation_latest
                (tool_id, source, signal, value, threshold, label, confidence,
                 timestamp, updated_at)
            VALUES ('ICT1', 'ict', $1, $2, $3, $4, $5, $6, NOW())
            ON CONFLICT (tool_id) DO UPDATE SET
                source = EXCLUDED.source, signal = EXCLUDED.signal,
                value = EXCLUDED.value, threshold = EXCLUDED.threshold,
                label = EXCLUDED.label, confidence = EXCLUDED.confidence,
                timestamp = EXCLUDED.timestamp, updated_at = NOW()
            """,
            signal,
            float(value),
            float(threshold),
            label,
            float(conf),
            as_of,
        )
    except Exception as exc:  # noqa: BLE001 — panel row is cosmetic, never fatal
        logger.warning("persist latest failed: %s", exc)


async def run_loop(serve: bool) -> None:
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2)
    try:
        v_bucket = await _bootstrap_v_bucket(pool)
        calc = VPINCalculator(v_bucket=v_bucket)
        row = await pool.fetchrow(
            "SELECT min(timestamp) AS lo, max(timestamp) AS hi FROM v2_ticks WHERE symbol=$1",
            SYMBOL,
        )
        history_start = max(row["lo"], row["hi"] - timedelta(days=REPLAY_DAYS))
        replay_from = history_start - timedelta(microseconds=1)

        async def on_bucket(bucket):
            if (
                calc.completed_buckets >= calc.warmup_buckets
                and calc.vpin is not None
                and calc.percentile(SIGNAL_PCTILE) is not None
                and calc.vpin > calc.percentile(SIGNAL_PCTILE)
            ):
                try:
                    await _persist_signal(pool, calc, bucket, history_start)
                except Exception as exc:  # noqa: BLE001 — never kill the monitor
                    logger.warning("persist failed: %s", exc)

        # stateless restart recovery: replay the trailing window (idempotent —
        # signal rows upsert on (timestamp, vocab))
        cursor = await _stream_ticks(pool, calc, replay_from, "", on_bucket)
        logger.info(
            "replay done: %d buckets, vpin=%s, p95=%s",
            calc.completed_buckets,
            f"{calc.vpin:.4f}" if calc.vpin is not None else "n/a",
            f"{calc.percentile(95):.4f}" if calc.percentile(95) is not None else "n/a",
        )
        await _persist_latest(pool, calc, cursor[0])
        if not serve:
            return
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                cursor = await _stream_ticks(pool, calc, *cursor, on_bucket)
                await _persist_latest(pool, calc, cursor[0])
            except Exception as exc:  # noqa: BLE001
                logger.error("poll failed: %s", exc)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(run_loop(serve="--serve" in sys.argv))
