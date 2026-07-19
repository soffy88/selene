"""Dual-arm shadow recorder for S2 entries (Wave EXEC-S, Part 2).

For every S2 would-enter signal, book two hypothetical executions side by side —
a market arm filled immediately at the signal price, and a limit arm resting at a
structural level — so the D4 gate (N>=30 and limit-arm net advantage > 0) can be
decided on evidence instead of intuition.

SHADOW ONLY. This service polls `v2_strategy_decision` read-only and writes
`v2_shadow_orders`. It places no order of any kind, touches no live table, and
feeds nothing back into strategies/**, states/** or the epoch.

Frozen parameters (Wave EXEC-S; copied, not tuned)
  Type A (reversal)  level = sweep extreme, pulled 0.1×ATR_1h to the inside
                     δ clipped to [0.15, 1.2]×ATR ; T = 120 min
                     on timeout: CANCEL, do not chase — a missed entry books 0
  Type B (momentum)  level = the 48h breakout reference
                     δ clipped to [0.15, 0.8]×ATR ; T = 60 min
                     on timeout: cross to market, booking the slippage honestly
  fill rule          a trade must print STRICTLY through L (buy: trade < L,
                     sell: trade > L). Touching L is not a fill; there is no
                     queue model, so this is deliberately conservative.
  outcome_i          d × (mark_24h − fill price) / fill price − fee
                     taker 0.08%, maker 0.03%; an unfilled/cancelled arm books 0.

Known gaps, deliberately recorded rather than papered over
  * Absorption has no price anywhere in the data (`v2_inverse_vocab_events`
    carries only booleans `result_low`/`effort_high` and a `price_response`
    ratio), so the Wave's "use the Absorption price if it is better" branch has
    nothing to read. Type A prices off the sweep extreme alone and stamps
    `notes.absorption = 'unavailable_no_price_in_source'` on every such row.
  * The design doc (`sel_exec_limit_design_draft1.md`) is not in the repo, so the
    `v2_shadow_orders` column set below is THIS MODULE'S design, not a copy of
    its §5. Reconcile before treating the schema as authoritative.
  * `entry_type` is not a column on `v2_strategy_decision`; it survives only
    inside the free-text `reason` ("… Entry approved — Type A (reversal) …").
    It is parsed with a strict regex and the raw reason is stored alongside; an
    unparseable reason is recorded as NULL rather than guessed.

Run:  python -m sel_v2.shadow.shadow_exec
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

SYMBOL = os.environ.get("SYMBOLS", "BTC-USDT")
POLL_SECONDS = float(os.environ.get("SHADOW_POLL_S", "30"))
STRATEGY = "strategy_2"

# ── frozen parameters ────────────────────────────────────────────────────────
INSIDE_ATR = 0.1  # Type A: pull the level this far inside the sweep extreme
CLIP_A = (0.15, 1.2)  # δ bounds, ×ATR
CLIP_B = (0.15, 0.8)
TIMEOUT_A_MIN = 120
TIMEOUT_B_MIN = 60
TAKER_FEE = 0.0008
MAKER_FEE = 0.0003
MARK_HORIZONS = {"30m": 30, "4h": 240, "24h": 1440}

WOULD_ENTER_ACTIONS = ("ENTER_LONG", "ENTER_SHORT")
_TYPE_RE = re.compile(r"Type\s+([AB])\b")

# Schema authored here, NOT copied from the (missing) design §5 — see module docstring.
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS v2_shadow_orders (
    signal_ts        timestamptz NOT NULL,
    strategy         text        NOT NULL,
    symbol           text        NOT NULL,
    direction        text        NOT NULL,
    entry_type       text,
    atr_1h           numeric,
    signal_price     numeric     NOT NULL,
    -- market arm: crosses immediately at the signal price
    market_price     numeric     NOT NULL,
    -- limit arm
    limit_price      numeric,
    delta_atr        numeric,
    delta_clipped    boolean     NOT NULL DEFAULT false,
    ref_kind         text,
    ref_price        numeric,
    deadline_ts      timestamptz,
    limit_status     text        NOT NULL,
    limit_fill_ts    timestamptz,
    limit_fill_price numeric,
    -- marks
    mark_30m         numeric,
    mark_4h          numeric,
    mark_24h         numeric,
    -- outcomes (filled once mark_24h lands)
    outcome_market   numeric,
    outcome_limit    numeric,
    reason_raw       text,
    notes            jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    -- v2_strategy_decision has no signal_id; its own PK is the signal identity,
    -- and it is what makes replay idempotent.
    PRIMARY KEY (signal_ts, strategy)
)
"""

RESTING = "RESTING"
FILLED = "FILLED"
CANCELLED = "TIMEOUT_CANCELLED"  # Type A: do not chase
CROSSED = "TIMEOUT_MARKET"  # Type B: cross to market


def _dsn() -> str:
    return os.environ["DB_URL"].replace("postgresql+asyncpg://", "postgresql://")


def parse_entry_type(reason: Optional[str]) -> Optional[str]:
    """Pull 'A'/'B' out of the decision reason. None when absent — never guessed."""
    if not reason:
        return None
    m = _TYPE_RE.search(reason)
    return m.group(1) if m else None


def clip_delta(delta_atr: float, entry_type: str) -> tuple[float, bool]:
    """Clip δ into the Type's frozen band. Returns (δ, was_clipped)."""
    lo, hi = CLIP_A if entry_type == "A" else CLIP_B
    clipped = min(max(delta_atr, lo), hi)
    return clipped, clipped != delta_atr


def timeout_minutes(entry_type: str) -> int:
    return TIMEOUT_A_MIN if entry_type == "A" else TIMEOUT_B_MIN


def limit_level(
    signal_price: float, ref_price: float, atr: float, direction: str, entry_type: str
) -> tuple[float, float, bool]:
    """Price the resting order.

    Type A rests just INSIDE the swept extreme (0.1×ATR back toward the market);
    Type B rests at the 48h breakout reference itself. Both are then expressed as
    a δ from the signal price and clipped into the Type's band, which is what
    actually determines the level — so a structural level further away than the
    cap is pulled in rather than silently accepted.
    """
    if entry_type == "A":
        # inside = toward the market from the extreme
        raw = (
            ref_price + INSIDE_ATR * atr
            if direction == "LONG"
            else ref_price - INSIDE_ATR * atr
        )
    else:
        raw = ref_price

    delta = abs(signal_price - raw) / atr if atr > 0 else 0.0
    delta, was_clipped = clip_delta(delta, entry_type)
    # A long buys below the market, a short sells above it.
    level = (
        signal_price - delta * atr
        if direction == "LONG"
        else signal_price + delta * atr
    )
    return level, delta, was_clipped


def is_filled(direction: str, level: float, extreme: float) -> bool:
    """Strict crossing only. Touching L is NOT a fill (no queue model)."""
    return extreme < level if direction == "LONG" else extreme > level


def outcome(
    direction: str, fill_price: Optional[float], mark: Optional[float], fee: float
) -> Optional[float]:
    """d × (mark − fill)/fill − fee. An arm that never filled books exactly 0."""
    if fill_price is None:
        return 0.0
    if mark is None or fill_price == 0:
        return None
    d = 1.0 if direction == "LONG" else -1.0
    return d * (mark - fill_price) / fill_price - fee


@dataclass
class Signal:
    ts: datetime
    direction: str
    entry_type: Optional[str]
    reason: Optional[str]


async def _price_at(conn: asyncpg.Connection, ts: datetime) -> Optional[float]:
    """Last trade at or before ts."""
    row = await conn.fetchrow(
        "SELECT price FROM v2_ticks WHERE symbol=$1 AND timestamp <= $2 "
        "ORDER BY timestamp DESC LIMIT 1",
        SYMBOL,
        ts,
    )
    return float(row["price"]) if row else None


async def _atr_1h(conn: asyncpg.Connection, ts: datetime) -> Optional[float]:
    """ATR_1h from the 14 hourly bars that CLOSED before ts, aggregated from ticks
    so it shares a source with the fill test (same choice as offline/fill_prob)."""
    rows = await conn.fetch(
        """
        SELECT date_trunc('hour', timestamp) AS h,
               max(price) AS high, min(price) AS low,
               (array_agg(price ORDER BY timestamp DESC))[1] AS close
        FROM v2_ticks
        WHERE symbol=$1 AND timestamp < date_trunc('hour', $2::timestamptz)
        GROUP BY 1 ORDER BY 1 DESC LIMIT 14
        """,
        SYMBOL,
        ts,
    )
    if len(rows) < 2:
        return None
    bars = list(reversed(rows))
    trs = []
    for i in range(1, len(bars)):
        high, low = float(bars[i]["high"]), float(bars[i]["low"])
        prev_close = float(bars[i - 1]["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return sum(trs) / len(trs) if trs else None


async def _structural_ref(
    conn: asyncpg.Connection, ts: datetime, direction: str, entry_type: str
) -> tuple[Optional[float], Optional[str]]:
    """The level the limit rests against.

    Type A: the swept extreme (`touch_low`/`touch_high` on the most recent Sweep).
    Type B: the 48h breakout reference (`low_48h`/`high_48h`), taken from the same
    vocab event, falling back to the actual 48h tick extreme when no Sweep is near.

    Absorption is NOT consulted: it carries no price in this data (see docstring).
    """
    key_extreme = "touch_low" if direction == "LONG" else "touch_high"
    key_ref = "low_48h" if direction == "LONG" else "high_48h"
    row = await conn.fetchrow(
        """
        SELECT tool_metadata FROM v2_inverse_vocab_events
        WHERE vocab='Sweep' AND timestamp <= $1 AND timestamp > $1 - interval '48 hours'
        ORDER BY timestamp DESC LIMIT 1
        """,
        ts,
    )
    meta = row["tool_metadata"] if row else None
    if isinstance(meta, str):
        meta = json.loads(meta)
    if meta:
        wanted = key_extreme if entry_type == "A" else key_ref
        if meta.get(wanted) is not None:
            return float(meta[wanted]), f"sweep.{wanted}"

    if entry_type == "B":
        agg = "min(price)" if direction == "LONG" else "max(price)"
        row = await conn.fetchrow(
            f"SELECT {agg} AS v FROM v2_ticks WHERE symbol=$1 "
            "AND timestamp <= $2 AND timestamp > $2 - interval '48 hours'",
            SYMBOL,
            ts,
        )
        if row and row["v"] is not None:
            return float(row["v"]), "ticks.48h_extreme"
    return None, None


async def _window_extreme(
    conn: asyncpg.Connection, direction: str, start: datetime, end: datetime
) -> Optional[float]:
    agg = "min(price)" if direction == "LONG" else "max(price)"
    row = await conn.fetchrow(
        f"SELECT {agg} AS v FROM v2_ticks WHERE symbol=$1 AND timestamp > $2 AND timestamp <= $3",
        SYMBOL,
        start,
        end,
    )
    return float(row["v"]) if row and row["v"] is not None else None


async def record_signal(conn: asyncpg.Connection, sig: Signal) -> bool:
    """Book both arms for one signal. Idempotent on (signal_ts, strategy)."""
    price = await _price_at(conn, sig.ts)
    atr = await _atr_1h(conn, sig.ts)
    if price is None or not atr:
        logger.warning("skip %s: price=%s atr=%s not available", sig.ts, price, atr)
        return False

    etype = sig.entry_type
    notes: dict = {"absorption": "unavailable_no_price_in_source"}
    level = delta = ref = ref_kind = None
    clipped = False
    status = RESTING
    deadline = None

    if etype is None:
        notes["entry_type"] = "unparseable_from_reason"
        status = "NO_TYPE"
    else:
        ref, ref_kind = await _structural_ref(conn, sig.ts, sig.direction, etype)
        if ref is None:
            notes["structural_ref"] = "unavailable"
            status = "NO_REF"
        else:
            level, delta, clipped = limit_level(price, ref, atr, sig.direction, etype)
            deadline = sig.ts + timedelta(minutes=timeout_minutes(etype))

    await conn.execute(
        """
        INSERT INTO v2_shadow_orders
          (signal_ts, strategy, symbol, direction, entry_type, atr_1h, signal_price,
           market_price, limit_price, delta_atr, delta_clipped, ref_kind, ref_price,
           deadline_ts, limit_status, reason_raw, notes)
        VALUES ($1,$2,$3,$4,$5,$6::numeric,$7::numeric,$7::numeric,$8::numeric,
                $9::numeric,$10,$11,$12::numeric,$13,$14,$15,$16::jsonb)
        ON CONFLICT (signal_ts, strategy) DO NOTHING
        """,
        sig.ts,
        STRATEGY,
        SYMBOL,
        sig.direction,
        etype,
        atr,
        price,
        level,
        delta,
        clipped,
        ref_kind,
        ref,
        deadline,
        status,
        sig.reason,
        json.dumps(notes),
    )
    return True


async def poll_new_signals(conn: asyncpg.Connection) -> int:
    """Book any would-enter S2 decision not yet shadowed."""
    rows = await conn.fetch(
        """
        SELECT d.timestamp, d.direction, d.reason
        FROM v2_strategy_decision d
        LEFT JOIN v2_shadow_orders s
          ON s.signal_ts = d.timestamp AND s.strategy = d.strategy
        WHERE d.strategy = $1 AND d.action = ANY($2::text[]) AND s.signal_ts IS NULL
        ORDER BY d.timestamp
        """,
        STRATEGY,
        list(WOULD_ENTER_ACTIONS),
    )
    n = 0
    for r in rows:
        sig = Signal(
            ts=r["timestamp"],
            direction=(r["direction"] or "").upper(),
            entry_type=parse_entry_type(r["reason"]),
            reason=r["reason"],
        )
        if await record_signal(conn, sig):
            n += 1
    if n:
        logger.info("shadowed %d new S2 signal(s)", n)
    return n


async def advance_resting(conn: asyncpg.Connection) -> int:
    """Drive resting limit arms to a terminal state.

    State lives in the table, never in memory, so a restart resumes exactly where
    it left off — the crash-recovery requirement.
    """
    rows = await conn.fetch(
        "SELECT signal_ts, strategy, direction, entry_type, limit_price, deadline_ts, "
        "signal_price FROM v2_shadow_orders WHERE limit_status = $1",
        RESTING,
    )
    now = datetime.now(timezone.utc)
    n = 0
    for r in rows:
        ts, direction, level = r["signal_ts"], r["direction"], float(r["limit_price"])
        # use the ROW's strategy, never the module constant: a row from another
        # strategy would otherwise match no UPDATE and be reprocessed forever.
        strat = r["strategy"]
        deadline = r["deadline_ts"]
        end = min(now, deadline)
        extreme = await _window_extreme(conn, direction, ts, end)
        if extreme is not None and is_filled(direction, level, extreme):
            fill_row = await conn.fetchrow(
                "SELECT timestamp FROM v2_ticks WHERE symbol=$1 AND timestamp > $2 "
                "AND timestamp <= $3 AND "
                + ("price < $4" if direction == "LONG" else "price > $4")
                + " ORDER BY timestamp LIMIT 1",
                SYMBOL,
                ts,
                end,
                Decimal(str(level)),
            )
            await conn.execute(
                "UPDATE v2_shadow_orders SET limit_status=$1, limit_fill_ts=$2, "
                "limit_fill_price=$3::numeric, updated_at=now() WHERE signal_ts=$4 AND strategy=$5",
                FILLED,
                fill_row["timestamp"] if fill_row else end,
                level,
                ts,
                strat,
            )
            n += 1
        elif now >= deadline:
            if r["entry_type"] == "A":
                await conn.execute(
                    "UPDATE v2_shadow_orders SET limit_status=$1, updated_at=now() "
                    "WHERE signal_ts=$2 AND strategy=$3",
                    CANCELLED,
                    ts,
                    strat,
                )
            else:
                # Type B crosses to market at the deadline; the slippage vs the
                # signal price is exactly what we are here to measure.
                px = await _price_at(conn, deadline)
                await conn.execute(
                    "UPDATE v2_shadow_orders SET limit_status=$1, limit_fill_ts=$2, "
                    "limit_fill_price=$3::numeric, updated_at=now() "
                    "WHERE signal_ts=$4 AND strategy=$5",
                    CROSSED,
                    deadline,
                    px,
                    ts,
                    strat,
                )
            n += 1
    return n


async def backfill_marks(conn: asyncpg.Connection) -> int:
    """Fill mark_30m/4h/24h once each horizon has elapsed, then compute outcomes."""
    rows = await conn.fetch(
        "SELECT signal_ts, strategy, direction, market_price, limit_price, limit_fill_price, "
        "limit_status, mark_30m, mark_4h, mark_24h FROM v2_shadow_orders "
        "WHERE mark_24h IS NULL"
    )
    now = datetime.now(timezone.utc)
    n = 0
    for r in rows:
        ts = r["signal_ts"]
        updates: dict[str, float] = {}
        for name, minutes in MARK_HORIZONS.items():
            col = f"mark_{name}"
            if r[col] is not None:
                continue
            at = ts + timedelta(minutes=minutes)
            if now < at:
                continue
            px = await _price_at(conn, at)
            if px is not None:
                updates[col] = px
        if not updates:
            continue

        mark24 = updates.get("mark_24h", r["mark_24h"])
        sets = ", ".join(f"{c}=${i + 1}::numeric" for i, c in enumerate(updates))
        params: list = list(updates.values())
        if mark24 is not None:
            fill = r["limit_fill_price"]
            om = outcome(
                r["direction"], float(r["market_price"]), float(mark24), TAKER_FEE
            )
            # An arm that never filled books 0; a Type-B cross pays taker, a real
            # limit fill pays maker.
            if r["limit_status"] == CANCELLED or fill is None:
                ol = 0.0
            else:
                fee = TAKER_FEE if r["limit_status"] == CROSSED else MAKER_FEE
                ol = outcome(r["direction"], float(fill), float(mark24), fee)
            sets += f", outcome_market=${len(params) + 1}::numeric, outcome_limit=${len(params) + 2}::numeric"
            params += [om, ol]
        params += [ts, r["strategy"]]
        await conn.execute(
            f"UPDATE v2_shadow_orders SET {sets}, updated_at=now() "
            f"WHERE signal_ts=${len(params) - 1} AND strategy=${len(params)}",
            *params,
        )
        n += 1
    return n


async def tick(conn: asyncpg.Connection) -> None:
    await poll_new_signals(conn)
    await advance_resting(conn)
    await backfill_marks(conn)


async def serve_forever() -> None:
    conn = await asyncpg.connect(_dsn())
    try:
        await conn.execute(CREATE_SQL)
        logger.info(
            "shadow_exec started (poll %.0fs, strategy=%s)", POLL_SECONDS, STRATEGY
        )
        while True:
            try:
                await tick(conn)
            except Exception as exc:  # noqa: BLE001 — a bad cycle must not kill the loop
                logger.error("shadow cycle failed: %s", exc)
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if "--once" in sys.argv:

        async def _once() -> None:
            conn = await asyncpg.connect(_dsn())
            try:
                await conn.execute(CREATE_SQL)
                await tick(conn)
            finally:
                await conn.close()

        asyncio.run(_once())
    else:
        asyncio.run(serve_forever())
