"""Step 3-5 simulation over the 1s-replay triggers (Wave S2G-0, Part 2).

Takes the triggers `cusum_1s_replay` produced at the accumulator's designed 1s
granularity and asks what the rest of the S2 chain would have done with them:
Step 3 vocab classification (Type A / B / unclear), Step 4 liquidation-pulse +
OI gate, Step 5 perp-spot basis gate.

Reuses `inverse_vocab.detect_absorption` / `detect_sweep` / `classify_entry_type`
verbatim — every module constant (p80/p30/p90 quantiles, MIN_PCTILE_OBS=30,
SWEEP_TOUCH_TOL) is left as shipped.

Two trigger sets are scored, by design:
  * CLUSTER REPS — one trigger per 5-minute cluster (835). Triggers inside a
    5-minute window share essentially the same 60-min Absorption aggregate and
    48h Sweep context, so scoring the whole cluster would just multiply-count.
  * RANDOM 200 — drawn from all 7,177 distinct excursions. Its classification
    distribution is compared against the cluster reps: agreement makes the
    sampling self-validating; disagreement would mean the "cluster is
    homogeneous" assumption is wrong, which is itself the finding.

Read-only offline. Writes `v2_sim_step345` only.

Run:  python -m sel_v2.offline.cusum_1s_step345
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta

import asyncpg
import numpy as np

from sel_v2.strategies.inverse_vocab import (
    classify_entry_type,
    detect_absorption,
    detect_sweep,
)

logger = logging.getLogger(__name__)

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
CLUSTER_GAP_S = 300
RANDOM_N = 200
SEED = 20260719

ABSORPTION_WINDOW_MIN = 60
SWEEP_LOOKBACK_H = 48
LIQ_WINDOW_MIN = 5
LIQ_FALLBACK_BTC = 50.0  # per the Wave, when the 30d p95 sample is empty
OI_WINDOW_MIN = 5
BASIS_ABORT_PCT = 0.5  # Step 5: >0.5% dislocation aborts (strategy2_entry §Step 5)
BASIS_CONF_PCT = 0.05  # 0.05-0.5% band → reduced confidence

CREATE = """
CREATE TABLE IF NOT EXISTS v2_sim_step345 (
    scored_at    timestamptz NOT NULL,
    trigger_set  text        NOT NULL,
    trigger_ts   timestamptz NOT NULL,
    direction    text,
    cluster_len  integer,
    cluster_peak numeric,
    entry_type   text,
    absorption   boolean,
    sweep        boolean,
    step4_pass   boolean,
    step5_pass   boolean,
    would_enter  boolean,
    details      jsonb,
    PRIMARY KEY (scored_at, trigger_set, trigger_ts)
)
"""


def _dsn() -> str:
    return os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")


async def _histories(conn):
    """Adaptive-percentile histories, built on the same definitions the detectors use.

    tf_net / price_response: hourly aggregates over all available ticks.
    volume: 4H bar volumes (what the live Sweep detector percentiles against).
    """
    rows = await conn.fetch(
        """
        SELECT date_trunc('hour', timestamp) h,
               sum(CASE WHEN side='buy' THEN size ELSE -size END) AS net,
               sum(size) AS vol,
               max(price) - min(price) AS rng
        FROM v2_ticks WHERE symbol=$1 GROUP BY 1 ORDER BY 1
        """,
        SYMBOL,
    )
    atr_row = await conn.fetchrow(
        "SELECT avg(high-low) a FROM v2_bars_4h WHERE symbol=$1", SYMBOL
    )
    atr = float(atr_row["a"] or 0) or 1.0
    tf_hist = [
        abs(float(r["net"])) / float(r["vol"]) for r in rows if float(r["vol"] or 0) > 0
    ]
    pr_hist = [float(r["rng"] or 0) / atr for r in rows]
    vol_rows = await conn.fetch(
        "SELECT volume FROM v2_bars_4h WHERE symbol=$1 ORDER BY time", SYMBOL
    )
    return tf_hist, pr_hist, [float(v["volume"]) for v in vol_rows], atr


async def _liq_p95(conn) -> float:
    row = await conn.fetchrow(
        "SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY s) p FROM ("
        "  SELECT sum(size) s FROM v2_liquidations"
        "  WHERE timestamp > now() - interval '30 days' GROUP BY date_trunc('minute',timestamp)) x"
    )
    p = float(row["p"]) if row and row["p"] is not None else 0.0
    return p if p > 0 else LIQ_FALLBACK_BTC


async def score_trigger(conn, ts, direction, hists, liq_thr, atr):
    tf_hist, pr_hist, vol_hist, _ = hists
    d: dict = {}

    # ── Step 3: Absorption over the 60-min window before the trigger ──
    a = await conn.fetchrow(
        "SELECT sum(CASE WHEN side='buy' THEN size ELSE -size END) net, sum(size) vol, "
        "max(price)-min(price) rng FROM v2_ticks "
        "WHERE symbol=$1 AND timestamp > $2 AND timestamp <= $3",
        SYMBOL,
        ts - timedelta(minutes=ABSORPTION_WINDOW_MIN),
        ts,
    )
    absorption = detect_absorption(
        taker_net=float(a["net"] or 0),
        taker_vol=float(a["vol"] or 0),
        price_delta_abs=float(a["rng"] or 0),
        atr=atr,
        tf_net_history=tf_hist,
        price_response_history=pr_hist,
    )

    # ── Step 3: Sweep over the 48h bar window ──
    s = await conn.fetchrow(
        "SELECT max(high) h48, min(low) l48 FROM v2_bars_4h "
        "WHERE symbol=$1 AND time > $2 AND time <= $3",
        SYMBOL,
        ts - timedelta(hours=SWEEP_LOOKBACK_H),
        ts,
    )
    w = await conn.fetchrow(
        "SELECT max(price) th, min(price) tl, sum(size) v FROM v2_ticks "
        "WHERE symbol=$1 AND timestamp > $2 AND timestamp <= $3",
        SYMBOL,
        ts - timedelta(minutes=ABSORPTION_WINDOW_MIN),
        ts,
    )
    post = await conn.fetchrow(
        "SELECT max(price) th, min(price) tl FROM v2_ticks "
        "WHERE symbol=$1 AND timestamp > $2 AND timestamp <= $3",
        SYMBOL,
        ts,
        ts + timedelta(hours=1),
    )
    h48 = float(s["h48"]) if s and s["h48"] is not None else None
    l48 = float(s["l48"]) if s and s["l48"] is not None else None
    sweep_present = False
    sweep = (
        detect_sweep(
            high_48h=h48 or 0.0,
            low_48h=l48 or 0.0,
            touch_high=float(w["th"] or 0),
            touch_low=float(w["tl"] or 0),
            touch_volume=float(w["v"] or 0),
            reverted_from_high=bool(
                post and post["th"] is not None and h48 and float(post["th"]) < h48
            ),
            reverted_from_low=bool(
                post and post["tl"] is not None and l48 and float(post["tl"]) > l48
            ),
            volume_history=vol_hist,
        )
        if h48 and l48
        else None
    )
    sweep_present = bool(sweep and sweep.present)

    # OFI persistence proxy: net taker flow same sign over the window
    ofi_same = (
        (float(a["net"] or 0) > 0)
        if direction == "LONG"
        else (float(a["net"] or 0) < 0)
    )

    etype = (
        classify_entry_type(direction, absorption, sweep, ofi_same) if sweep else None
    )
    d["absorption_details"] = absorption.details
    d["sweep_present"] = sweep_present

    # ── Step 4: liquidation pulse + OI drop ──
    lq = await conn.fetchrow(
        "SELECT coalesce(sum(size),0) s FROM v2_liquidations "
        "WHERE timestamp > $1 AND timestamp <= $2",
        ts - timedelta(minutes=LIQ_WINDOW_MIN),
        ts,
    )
    oi = await conn.fetch(
        "SELECT open_interest FROM v2_derivatives_snapshots WHERE symbol=$1 "
        "AND timestamp > $2 AND timestamp <= $3 ORDER BY timestamp",
        SYMBOL,
        ts - timedelta(minutes=OI_WINDOW_MIN),
        ts,
    )
    liq_pulse = float(lq["s"]) >= liq_thr
    # open_interest can be NULL in the snapshot feed — treat a missing reading as
    # "no OI evidence" rather than crashing or silently scoring it as a drop.
    oi_vals = [
        float(r["open_interest"]) for r in oi if r["open_interest"] is not None
    ]
    oi_drop = len(oi_vals) >= 2 and oi_vals[0] > 0 and oi_vals[0] > oi_vals[-1]
    step4 = bool(liq_pulse or oi_drop)
    d["liq_sum"] = float(lq["s"])
    d["liq_thr"] = liq_thr
    d["oi_samples"] = len(oi_vals)

    # ── Step 5: perp-spot basis (data only exists from 2026-07-11) ──
    px = await conn.fetch(
        "SELECT exchange, price FROM v2_cross_exchange_prices "
        "WHERE timestamp > $1 AND timestamp <= $2 ORDER BY timestamp DESC LIMIT 8",
        ts - timedelta(minutes=5),
        ts,
    )
    step5 = None
    if len(px) >= 2:
        vals = [float(p["price"]) for p in px]
        spread = (max(vals) - min(vals)) / min(vals) * 100 if min(vals) > 0 else 0.0
        step5 = spread < BASIS_ABORT_PCT
        d["basis_pct"] = round(spread, 4)
    else:
        d["basis_pct"] = None  # no cross-exchange data at this timestamp

    would = bool(etype is not None and step4 and (step5 is not False))
    return etype, absorption.present, sweep_present, step4, step5, would, d


async def main() -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(CREATE)
        run = await conn.fetchval(
            "SELECT max(replayed_at) FROM v2_sim_cusum1s_triggers"
        )
        logger.info("scoring triggers from replay run %s", run)

        rows = await conn.fetch(
            "SELECT trigger_ts, direction, peak_value FROM v2_sim_cusum1s_triggers "
            "WHERE replayed_at=$1 ORDER BY trigger_ts",
            run,
        )
        # distinct excursions: gap > 1s
        exc, prev = [], None
        for r in rows:
            if prev is None or (r["trigger_ts"] - prev).total_seconds() > 1:
                exc.append(r)
            prev = r["trigger_ts"]
        # 5-min clusters, tracking length and peak for the stratification
        clusters, cur = [], []
        for r in exc:
            if (
                cur
                and (r["trigger_ts"] - cur[-1]["trigger_ts"]).total_seconds()
                > CLUSTER_GAP_S
            ):
                clusters.append(cur)
                cur = []
            cur.append(r)
        if cur:
            clusters.append(cur)
        logger.info("excursions=%d clusters=%d", len(exc), len(clusters))

        reps = [
            (
                c[0]["trigger_ts"],
                c[0]["direction"],
                len(c),
                max(float(x["peak_value"]) for x in c),
            )
            for c in clusters
        ]
        rng = np.random.default_rng(SEED)
        idx = rng.choice(len(exc), size=min(RANDOM_N, len(exc)), replace=False)
        rand = [
            (exc[i]["trigger_ts"], exc[i]["direction"], 1, float(exc[i]["peak_value"]))
            for i in idx
        ]

        hists = await _histories(conn)
        atr = hists[3]
        liq_thr = await _liq_p95(conn)
        logger.info(
            "atr=%.2f liq_thr=%.2f tf_hist=%d vol_hist=%d",
            atr,
            liq_thr,
            len(hists[0]),
            len(hists[2]),
        )

        scored_at = await conn.fetchval("SELECT now()")
        for name, batch in (("cluster_reps", reps), ("random200", rand)):
            payload = []
            for ts, direction, clen, cpeak in batch:
                et, ab, sw, s4, s5, would, d = await score_trigger(
                    conn, ts, direction, hists, liq_thr, atr
                )
                payload.append(
                    (
                        scored_at,
                        name,
                        ts,
                        direction,
                        clen,
                        cpeak,
                        et,
                        ab,
                        sw,
                        s4,
                        s5,
                        would,
                        __import__("json").dumps(d, default=str),
                    )
                )
            await conn.executemany(
                "INSERT INTO v2_sim_step345 (scored_at,trigger_set,trigger_ts,direction,"
                "cluster_len,cluster_peak,entry_type,absorption,sweep,step4_pass,step5_pass,"
                "would_enter,details) VALUES ($1,$2,$3,$4,$5,$6::numeric,$7,$8,$9,$10,$11,$12,$13::jsonb) "
                "ON CONFLICT DO NOTHING",
                payload,
            )
            logger.info("%s: scored %d", name, len(payload))
    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    asyncio.run(main())
