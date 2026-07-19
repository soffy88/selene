"""Chunked export of the frozen-window 1s series (Wave S2G Part 5, hardened rerun).

Exists because the comparison it feeds used to run as one long query against the
live database. On 2026-07-19 an abandoned 14M-row analysis query queued the
TimescaleDB compression policy behind it and every writer behind that, stalling
v2_ticks persistence for 3h20m — and the healthcheck's own probe sat in the same
queue, so monitoring went silent exactly when it was needed.

The fix is structural rather than careful: export ONCE in day-sized chunks, each
with a statement_timeout and each reported as it lands, then do the replay and the
diff entirely from the file. After this script finishes, nothing in the
reconciliation touches the database at all.

Run:  REPLAY_END='2026-07-19 08:00:00+00:00' python -m sel_v2.offline.s2g_export_1s
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import asyncpg

logger = logging.getLogger(__name__)

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
OUT = os.environ.get("S2G_EXPORT", "/tmp/s2g_1s.npz")
CHUNK_HOURS = 24
STATEMENT_TIMEOUT_MS = 120_000  # a chunk that cannot finish in 2min is a red flag

REPLAY_END = (
    datetime.fromisoformat(os.environ["REPLAY_END"])
    if os.environ.get("REPLAY_END")
    else datetime.now(timezone.utc)
)

# Same rule as sel_v2.data.tick_1s: the second's price is its highest trade_id.
# (length, text) rather than ::bigint — equally correct for digit strings and 3.6x
# cheaper, which matters when this runs against a live database.
CHUNK_SQL = """
SELECT date_trunc('second', timestamp) AS s,
       (array_agg(price ORDER BY timestamp DESC, length(trade_id) DESC,
                  trade_id DESC))[1] AS px
FROM v2_ticks
WHERE symbol = $1 AND timestamp >= $2::timestamptz AND timestamp < $3::timestamptz
GROUP BY 1 ORDER BY 1
"""


def _dsn() -> str:
    return os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")


async def main() -> int:
    import pandas as pd

    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        first = await conn.fetchval(
            "SELECT min(timestamp) FROM v2_ticks WHERE symbol=$1", SYMBOL
        )
        if first is None:
            logger.error("no ticks")
            return 2

        frames, cursor, n_chunks = [], first, 0
        while cursor < REPLAY_END:
            upper = min(cursor + timedelta(hours=CHUNK_HOURS), REPLAY_END)
            rows = await conn.fetch(CHUNK_SQL, SYMBOL, cursor, upper)
            n_chunks += 1
            logger.info(
                "chunk %d %s..%s -> %d seconds",
                n_chunks,
                cursor.date(),
                upper.date(),
                len(rows),
            )
            if rows:
                frames.append(
                    pd.DataFrame(
                        {
                            "s": [r["s"] for r in rows],
                            "px": [float(r["px"]) for r in rows],
                        }
                    )
                )
            cursor = upper
    finally:
        await conn.close()  # DB is out of the picture from here on

    if not frames:
        logger.error("no rows exported")
        return 2
    df = pd.concat(frames, ignore_index=True).drop_duplicates("s").sort_values("s")
    # .npz, not parquet: the sel_v2 image carries no pyarrow, and this is a
    # throwaway intermediate — two arrays, no schema, no extra dependency.
    import numpy as np

    np.savez_compressed(
        OUT,
        secs=np.array([t.timestamp() for t in df["s"]], dtype="float64"),
        px=df["px"].to_numpy(dtype="float64"),
    )
    logger.info(
        "exported %d seconds (%s .. %s) -> %s",
        len(df),
        df["s"].iloc[0],
        df["s"].iloc[-1],
        OUT,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(main()))
