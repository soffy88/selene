import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
import asyncpg
import redis.asyncio as redis

from sel_v2.runtime.staleness import enforcement_for, is_bar_stale, is_stale
from sel_v2.strategies.db_writer import DBWriter

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("paper_engine")

# Fixed namespace so a logical paper trade maps to the same v2_trades.id across the
# engine's full-history replays (item #6 — makes persistence idempotent).
_TRADE_NS = uuid.UUID("6f1a7e2c-0b3d-4a5e-9c8b-2d1f0a3b4c5d")

# Cadence of the Strategy-2 tick loop (reprocess when new ticks arrive, so S2's
# H1 Hawkes / micro state stays current ahead of the 4H bar boundary).
S2_TICK_INTERVAL_SEC = int(os.environ.get("S2_TICK_INTERVAL_SEC", "300"))
# Cadence of the intra-bar position-risk monitor.
POSITION_MGMT_INTERVAL_SEC = int(os.environ.get("POSITION_MGMT_INTERVAL_SEC", "60"))
# Intra-bar unrealized-loss fraction that raises a risk alert between bars.
PAPER_HARD_STOP_PCT = float(os.environ.get("PAPER_HARD_STOP_PCT", "0.05"))


def _trade_id(
    strategy: str, sub_account: str, entry_time, direction: str, entry_price: float
) -> str:
    key = f"{strategy}|{sub_account}|{entry_time}|{direction}|{entry_price}"
    return str(uuid.uuid5(_TRADE_NS, key))


def _dir_str(direction) -> str:
    return getattr(direction, "value", direction)


class PaperEngine:
    REDIS_KEY_LAST_BAR_TS = "v2:paper:last_bar_ts"

    def __init__(self):
        self._db_url = os.environ.get("DB_URL")
        self._redis_url = os.environ.get("REDIS_URL", "redis://helios-redis:6379/3")
        self._symbol = "BTC-USDT"
        self._pool = None
        self._redis = None
        self._last_bar_ts = None
        self._strategy_params = {}
        self._open_positions = []
        self._degraded = False
        self._engine = None
        self._writer = None
        # Serialise reprocessing: the 4H loop and the S2 tick loop both trigger a
        # full-history replay, and they must never run concurrently (they share
        # self._engine and write the same v2_* rows).
        self._reprocess_lock = asyncio.Lock()
        self._last_tick_ts = None  # newest v2_ticks timestamp the tick loop has seen
        # GL1 T0.4: last known fresh/stale reading per source, so v2_staleness_events
        # only logs transitions (not a per-cycle snapshot).
        self._staleness_state: dict[str, bool] = {}

    async def _load_last_bar_ts(self) -> datetime | None:
        try:
            raw = await self._redis.get(self.REDIS_KEY_LAST_BAR_TS)
            if raw:
                ts = datetime.fromisoformat(
                    raw.decode() if isinstance(raw, bytes) else raw
                )
                logger.info(f"last_bar_ts loaded from Redis: {ts}")
                return ts
        except Exception as e:
            logger.warning(f"failed to load last_bar_ts from Redis: {e}")
        return None

    async def _persist_last_bar_ts(self, ts: datetime):
        try:
            await self._redis.set(self.REDIS_KEY_LAST_BAR_TS, ts.isoformat())
        except Exception as e:
            logger.error(f"failed to persist last_bar_ts: {e}")

    async def _load_strategy_params(self):
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT param_key, param_value FROM v2_strategy_params"
            )
            self._strategy_params = {
                row["param_key"]: row["param_value"] for row in rows
            }
        logger.info(f"Loaded {len(self._strategy_params)} strategy parameters")

    def _param_float(self, key: str, default: float) -> float:
        """v2_strategy_params value → float. Values arrive as raw JSONB text
        (e.g. '0.85'); a missing/unparseable key falls back to `default`."""
        raw = self._strategy_params.get(key)
        if raw is None:
            return default
        try:
            return float(json.loads(raw) if isinstance(raw, str) else raw)
        except (ValueError, TypeError):
            logger.warning(
                "_param_float: unparseable %s=%r, using default %s", key, raw, default
            )
            return default

    async def _load_bars_df(self):
        """Load the full 4H bar history for the symbol into a DataFrame."""
        import pandas as pd

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT time, open, high, low, close, volume FROM v2_bars_4h "
                "WHERE symbol=$1 ORDER BY time ASC",
                self._symbol,
            )
        if not rows:
            return None
        return pd.DataFrame([dict(r) for r in rows])

    async def _load_derivatives_series(self, df):
        """As-of-join OI + funding from v2_derivatives_snapshots onto the 4H bar grid.

        Without these the state machine collapses to Drifting_Calm and never opens a
        position; feeding them unlocks the Coiling/Surging/Drifting_Charged entry
        states (the reason the live engine never traded). NaN where no snapshot
        exists at/before a bar — the engine treats NaN as 'unknown' (conservative)."""
        import bisect
        import numpy as np

        n = len(df)
        oi = np.full(n, np.nan)
        funding = np.full(n, np.nan)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT timestamp, open_interest, funding_rate FROM v2_derivatives_snapshots "
                "WHERE symbol=$1 ORDER BY timestamp ASC",
                self._symbol,
            )
        if not rows:
            return oi, funding
        ts = [r["timestamp"] for r in rows]
        ois = [
            float(r["open_interest"]) if r["open_interest"] is not None else np.nan
            for r in rows
        ]
        frs = [
            float(r["funding_rate"]) if r["funding_rate"] is not None else np.nan
            for r in rows
        ]
        for i, bt in enumerate(df["time"]):
            j = bisect.bisect_right(ts, bt) - 1  # most recent snapshot <= bar time
            if j >= 0:
                oi[i] = ois[j]
                funding[i] = frs[j]
        return oi, funding

    async def _load_tick_times(self, df, lookback_days: int = 14):
        """Load recent trade arrival times (Unix seconds) from v2_ticks to drive the
        Strategy-2 H1 Hawkes intensity. Bounded to a recent window — intensity decays,
        so only recent ticks affect recent bars (item: tick→H1 wiring)."""
        from datetime import timedelta
        import numpy as np

        last_bar = df["time"].iloc[-1]
        cutoff = last_bar - timedelta(days=lookback_days)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT timestamp FROM v2_ticks WHERE symbol=$1 AND timestamp >= $2 "
                "ORDER BY timestamp ASC",
                self._symbol,
                cutoff,
            )
        if not rows:
            return None
        return np.array([r["timestamp"].timestamp() for r in rows], dtype=float)

    async def _load_microstructure_series(self, df, lookback_days: int = 120):
        """Per-4H-bar microstructure features from real v2_ticks + v2_lob_snapshots,
        used to derive the Strategy-2 inverse-vocab tags (Sweep/Absorption) and OFI
        persistence. Aggregated in SQL via time_bucket for efficiency; aligned to the
        bar grid (NaN where no data). Bounded to a recent window.

        Returns dict of np arrays aligned to df rows:
          taker_net  — Σ buy size − Σ sell size in the bar (signed taker flow)
          taker_vol  — Σ |size| (total taken volume)
          lob_imb    — mean(bid_depth − ask_depth) over the bar's LOB snapshots
          lob_depth  — mean(bid_depth + ask_depth) over the bar's LOB snapshots (total
                       top-of-book depth; feeds the Cascade thin-book condition, P1-3)
          entropy    — mean(entropy) over the bar's LOB snapshots (feeds Coiling's
                       entropy_low condition; collected all along but never aggregated)
        """
        from datetime import timedelta
        import numpy as np

        n = len(df)
        out = {
            k: np.full(n, np.nan)
            for k in (
                "taker_net",
                "taker_vol",
                "lob_imb",
                "lob_depth",
                "entropy",
                "liq_vol",
            )
        }
        cutoff = df["time"].iloc[-1] - timedelta(days=lookback_days)
        # bar open time -> row index
        idx = {t: i for i, t in enumerate(df["time"])}
        async with self._pool.acquire() as conn:
            flow = await conn.fetch(
                "SELECT time_bucket('4 hours', timestamp) AS b, "
                "  COALESCE(SUM(size) FILTER (WHERE side='buy'),0) "
                "  - COALESCE(SUM(size) FILTER (WHERE side='sell'),0) AS net, "
                "  COALESCE(SUM(size),0) AS vol "
                "FROM v2_ticks WHERE symbol=$1 AND timestamp >= $2 GROUP BY b",
                self._symbol,
                cutoff,
            )
            lob = await conn.fetch(
                "SELECT time_bucket('4 hours', timestamp) AS b, "
                "  AVG(bid_depth - ask_depth) AS imb, "
                "  AVG(bid_depth + ask_depth) AS depth, "
                "  AVG(entropy) AS entropy "
                "FROM v2_lob_snapshots WHERE symbol=$1 AND timestamp >= $2 GROUP BY b",
                self._symbol,
                cutoff,
            )
            # Per-bar liquidation volume (Wave S2C Step-4 cascade guard). Sums both sides;
            # NaN where no liquidations covered the bar (feed is sparse), so the pctile stays
            # cold and the 50 BTC absolute floor governs.
            liq = await conn.fetch(
                "SELECT time_bucket('4 hours', timestamp) AS b, "
                "  COALESCE(SUM(size),0) AS liqvol "
                "FROM v2_liquidations WHERE symbol=$1 AND timestamp >= $2 GROUP BY b",
                self._symbol,
                cutoff,
            )
        for r in flow:
            i = idx.get(r["b"])
            if i is not None:
                out["taker_net"][i] = float(r["net"])
                out["taker_vol"][i] = float(r["vol"])
        for r in lob:
            i = idx.get(r["b"])
            if i is not None:
                if r["imb"] is not None:
                    out["lob_imb"][i] = float(r["imb"])
                if r["depth"] is not None:
                    out["lob_depth"][i] = float(r["depth"])
                if r["entropy"] is not None:
                    out["entropy"][i] = float(r["entropy"])
        for r in liq:
            i = idx.get(r["b"])
            if i is not None and r["liqvol"] is not None:
                out["liq_vol"][i] = float(r["liqvol"])
        return out

    async def _load_cross_exchange_price(self):
        """Latest Binance-spot price + its age in seconds, for the S2 Step-5 divergence check
        (Wave S2C Part 3). Returns (price, age_seconds) or None when the poller feed is empty.
        The engine only applies it to the current bar and treats age>120s as unavailable."""
        from datetime import datetime, timezone

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT price, timestamp FROM v2_cross_exchange_prices "
                "WHERE exchange = 'binance_spot' ORDER BY timestamp DESC LIMIT 1"
            )
        if row is None or row["price"] is None:
            return None
        age = (datetime.now(timezone.utc) - row["timestamp"]).total_seconds()
        return float(row["price"]), age

    @staticmethod
    def _ofi_proxy(df):
        """Bar-level signed-volume OFI proxy (sign(return) x volume). Coarse but
        always available from OHLCV, so Surging's flow signal can fire even before
        the LOB/taker-flow collectors are live. Replace with true OFI when available."""
        import numpy as np

        close = df["close"].values.astype(float)
        vol = df["volume"].values.astype(float) if "volume" in df else np.ones(len(df))
        sign = np.sign(np.diff(close, prepend=close[0]))
        return sign * vol

    async def _reprocess(self):
        """Lock-serialised full-history replay. The 4H loop and the S2 tick loop both
        call this; the lock guarantees only one replay mutates self._engine / the
        v2_* rows at a time."""
        async with self._reprocess_lock:
            await self._reprocess_inner()

    # ── staleness (GL1 T0.4) ─────────────────────────────────────────────────────
    async def _latest_funding_oi_ts(self):
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT MAX(timestamp) FROM v2_derivatives_snapshots WHERE symbol=$1",
                self._symbol,
            )

    async def _latest_lob_ts(self):
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT MAX(timestamp) FROM v2_lob_snapshots WHERE symbol=$1",
                self._symbol,
            )

    async def _compute_staleness(self, df) -> dict:
        """Single point (GL1 T0.4) where "is source X too old" gets decided for this
        cycle. Returns {source: StalenessEnforcement} for currently-stale sources only
        (process_frame's contract — fresh sources are simply absent). Logs a
        v2_staleness_events row only on a fresh<->stale transition."""
        now = datetime.now(timezone.utc)
        readings = {
            "ticks": await self._latest_tick_ts(),
            "funding_oi": await self._latest_funding_oi_ts(),
            "lob": await self._latest_lob_ts(),
        }
        stale_map = {src: is_stale(src, ts, now) for src, ts in readings.items()}
        stale_map["bar_4h"] = is_bar_stale(
            df["time"].iloc[-1] if len(df) else None, now
        )
        readings["bar_4h"] = df["time"].iloc[-1] if len(df) else None

        result = {}
        for source, stale in stale_map.items():
            if stale:
                result[source] = enforcement_for(source, stale=True)
            if self._staleness_state.get(source) != stale:
                last_update = readings[source]
                age_s = (now - last_update).total_seconds() if last_update else None
                reason = (
                    enforcement_for(source, stale=True).reason_code if stale else None
                )
                await self._writer.write_staleness_event(
                    source, stale, reason, last_update, age_s
                )
                logger.info(
                    "staleness transition: %s stale=%s reason=%s", source, stale, reason
                )
            self._staleness_state[source] = stale
        return result

    async def _reprocess_inner(self):
        """Replay the full bar history through the strategy engine. Positions are a pure
        function of history, so a fresh engine each tick gives deterministic, idempotent state.
        TDA is skipped unless `ripser` is installed; OI/funding are fed when available."""
        from sel_v2.paper.strategy_engine import PaperStrategyEngine

        df = await self._load_bars_df()
        if df is None or len(df) < 180:
            logger.info(
                "not enough bars to run strategy engine (have %s)",
                0 if df is None else len(df),
            )
            return
        try:
            import ripser  # noqa: F401

            skip_tda = False
        except Exception:
            skip_tda = True
        engine = PaperStrategyEngine(
            total_nav_usdt=float(
                self._strategy_params.get("paper_total_nav", 100_000) or 100_000
            ),
            instrument=self._symbol,
            skip_tda=skip_tda,
            # Calibrated state-machine thresholds from Wave 1 calibrate_all (same keys
            # replay.py reads). Without this the paper path silently ran on the
            # hardcoded dataclass defaults even after calibration was persisted.
            hawkes_threshold=self._param_float("h2_branching_ratio_threshold", 0.85),
            tda_threshold=self._param_float("tda1_l1_threshold_p95", 0.000097),
        )
        # Feed OI/funding (real, from v2_derivatives_snapshots) + a bar-derived OFI
        # proxy so the trading states can fire and the engine actually opens positions.
        oi_series, funding_series = await self._load_derivatives_series(df)
        ofi_series = self._ofi_proxy(df)
        # Real trade arrivals drive the Strategy-2 H1 Hawkes intensity.
        tick_times = await self._load_tick_times(df)
        # Per-bar microstructure → Strategy-2 inverse-vocab + OFI persistence.
        micro = await self._load_microstructure_series(df)
        # Wave S2C Part 3: latest Binance-spot price for the S2 Step-5 divergence check.
        cross_price = await self._load_cross_exchange_price()
        # GL1 T0.4: gate new entries / suppress the CUSUM-reversal exit on the current
        # bar only when a source is too old to trust (process_frame applies it only to
        # the newest bar — see its docstring).
        staleness = await self._compute_staleness(df)
        summary = engine.process_frame(
            df,
            oi_series=oi_series,
            funding_series=funding_series,
            ofi_series=ofi_series,
            tick_times=tick_times,
            micro=micro,
            staleness=staleness,
            cross_price=cross_price,
        )
        self._engine = engine
        await self._persist_summary(summary)
        await self._persist_results(engine)
        await self._refresh_observations(df, oi_series, funding_series)
        logger.info(
            "strategy engine: bars=%s state=%s s1=%s s2=%s equity=%s",
            summary["bars"],
            list(summary["state_counts"])[-1:],
            summary["s1"],
            summary["s2"],
            summary["total_equity"],
        )

    async def _refresh_observations(self, df, oi_series, funding_series):
        """Run the 7 observation-only tools over the recent window and persist their latest
        readings for the SEL observation panel (#3). Throttled to a NEW BAR only — the tools
        are windowed and expensive (HMM refit etc.), and don't change intra-bar, so running
        them on every tick would be wasteful. Non-blocking: failures never stop the engine."""
        try:
            latest_ts = df["time"].iloc[-1]
            if getattr(self, "_last_obs_bar_ts", None) == latest_ts:
                return  # already ran for this bar
            from sel_v2.observation_tools.runner import (
                run_recent_observations,
                persist_latest_observations,
            )

            results = run_recent_observations(
                df, oi_series=oi_series, funding_series=funding_series
            )
            if results:
                async with self._pool.acquire() as conn:
                    await persist_latest_observations(conn, results)
                self._last_obs_bar_ts = latest_ts
        except Exception as e:  # noqa: BLE001
            logger.warning("observation refresh failed: %s", e)

    async def _persist_summary(self, summary: dict):
        """Publish the latest engine summary to Redis for the UI/monitoring layer."""
        try:
            import json

            await self._redis.set(
                "v2:paper:engine_summary", json.dumps(summary, default=str)
            )
        except Exception as e:
            logger.warning("failed to persist engine summary: %s", e)

    async def _persist_results(self, engine) -> None:
        """Persist the engine's state history and trades to the v2_* tables that the
        paper API (sel_v2/paper_interface/api.py) reads (item #6). Idempotent: state
        rows ON CONFLICT DO NOTHING by timestamp, trades upserted by deterministic id.
        Non-blocking — a write failure logs and does not stop the engine."""
        if self._writer is None:
            return
        try:
            records = getattr(engine, "records", None)
            if records:
                await self._writer.write_states_bulk(records)

            for acct in (engine.accounts.subaccount_1, engine.accounts.subaccount_2):
                for cp in acct.closed_positions:
                    p = cp.position
                    d = _dir_str(p.direction)
                    await self._writer.upsert_trade(
                        trade_id=_trade_id(
                            p.strategy, p.sub_account, p.entry_time, d, p.entry_price
                        ),
                        strategy=p.strategy,
                        sub_account=p.sub_account,
                        entry_time=p.entry_time,
                        entry_price=p.entry_price,
                        direction=d,
                        size=p.size_usdt,
                        leverage=p.leverage,
                        instrument=p.instrument,
                        entry_state=p.entry_state,
                        exit_time=cp.exit_time,
                        exit_price=cp.exit_price,
                        exit_reason=cp.exit_reason,
                        pnl_usdt=cp.pnl_usdt,
                        pnl_pct=cp.pnl_pct,
                    )
                for p in acct.open_positions:
                    d = _dir_str(p.direction)
                    await self._writer.upsert_trade(
                        trade_id=_trade_id(
                            p.strategy, p.sub_account, p.entry_time, d, p.entry_price
                        ),
                        strategy=p.strategy,
                        sub_account=p.sub_account,
                        entry_time=p.entry_time,
                        entry_price=p.entry_price,
                        direction=d,
                        size=p.size_usdt,
                        leverage=p.leverage,
                        instrument=p.instrument,
                        entry_state=p.entry_state,
                    )

            # Per-bar decision trail (the audit "why" for recent bars, #2).
            if hasattr(engine, "decision_trail"):
                await self._writer.write_decision_trail_bulk(engine.decision_trail())

            # CUSUM trigger events (orphan-table fix): CUSUM-Short/Mid fired all along but
            # write_cusum_event had no caller, so v2_cusum_events stayed empty and misled two
            # rounds of diagnosis. Upsert is idempotent on (timestamp, cusum_type).
            if hasattr(engine, "cusum_events"):
                await self._writer.write_cusum_events_bulk(engine.cusum_events())

            # Inverse-vocab signatures (Wave S2C Step 3 telemetry).
            if hasattr(engine, "vocab_events"):
                await self._writer.write_inverse_vocab_events_bulk(
                    engine.vocab_events()
                )

            # Latest per-strategy decision (the "why no entry this bar" the UI shows).
            from datetime import datetime as _dt

            for strat, dec in getattr(engine, "latest_decisions", lambda: {})().items():
                if not dec:
                    continue
                ts = dec.get("timestamp")
                ts = _dt.fromisoformat(ts) if isinstance(ts, str) else ts
                await self._writer.upsert_latest_decision(
                    strategy=strat,
                    timestamp=ts,
                    action=dec.get("action") or "OBSERVE",
                    reason=dec.get("reason"),
                    step_reached=dec.get("step_reached"),
                    state_4h=dec.get("state_4h"),
                    direction=dec.get("direction"),
                )
        except Exception as e:
            logger.warning("failed to persist engine results: %s", e)

    async def _process_bar(self, bar):
        # A new 4H bar closed — reprocess the full history through the strategy engine.
        await self._reprocess()

    async def _process_backlog(self):
        persistent_ts = await self._load_last_bar_ts()
        async with self._pool.acquire() as conn:
            max_bar = await conn.fetchval(
                "SELECT MAX(time) FROM v2_bars_4h WHERE symbol = $1", self._symbol
            )
        if max_bar is None:
            logger.warning("v2_bars_4h is empty")
            return
        if persistent_ts is None:
            self._last_bar_ts = max_bar
            await self._persist_last_bar_ts(max_bar)
            logger.info(f"first start, initialized last_bar_ts to {max_bar}")
            return
        if persistent_ts >= max_bar:
            self._last_bar_ts = persistent_ts
            logger.info(f"no backlog, last_bar_ts = {persistent_ts}")
            return

        async with self._pool.acquire() as conn:
            backlog = await conn.fetch(
                "SELECT * FROM v2_bars_4h WHERE symbol=$1 AND time>$2 AND time<=$3 ORDER BY time ASC",
                self._symbol,
                persistent_ts,
                max_bar,
            )
        logger.info(f"processing {len(backlog)} backlog bars")
        # The engine replays full history internally, so reprocess once after advancing the
        # cursor rather than per bar (which would be O(n²)).
        await self._reprocess()
        self._last_bar_ts = max_bar
        await self._persist_last_bar_ts(max_bar)
        logger.info(f"backlog complete, last_bar_ts = {max_bar}")

    async def _strategy1_4h_loop(self):
        while True:
            try:
                async with self._pool.acquire() as conn:
                    latest_bar = await conn.fetchrow(
                        "SELECT * FROM v2_bars_4h WHERE symbol=$1 ORDER BY time DESC LIMIT 1",
                        self._symbol,
                    )
                if latest_bar and latest_bar["time"] > self._last_bar_ts:
                    await self._process_bar(latest_bar)
                    self._last_bar_ts = latest_bar["time"]
                    await self._persist_last_bar_ts(self._last_bar_ts)
            except Exception as e:
                logger.error(f"Error in strategy1 loop: {e}")
            await asyncio.sleep(60)

    # ── Strategy 2 tick loop ────────────────────────────────────────────────────
    async def _latest_tick_ts(self):
        async with self._pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT MAX(timestamp) FROM v2_ticks WHERE symbol=$1", self._symbol
            )

    async def _maybe_reprocess_on_new_ticks(self) -> bool:
        """Trigger a replay when new ticks have arrived since the last one seen, so
        Strategy 2's tick-fed H1 Hawkes intensity and microstructure stay current
        between 4H bars. Authoritative entries/exits remain bar-gated (they occur at
        sealed 4H bars inside process_frame); this only keeps S2 state fresh and is
        rate-limited by the loop's sleep. Returns True if a replay was run."""
        latest = await self._latest_tick_ts()
        if latest is not None and (
            self._last_tick_ts is None or latest > self._last_tick_ts
        ):
            self._last_tick_ts = latest
            await self._reprocess()
            return True
        return False

    async def _strategy2_tick_loop(self):
        while True:
            try:
                await self._maybe_reprocess_on_new_ticks()
            except Exception as e:
                logger.error(f"Error in strategy2 tick loop: {e}")
            await asyncio.sleep(S2_TICK_INTERVAL_SEC)

    # ── Position management (intra-bar risk monitor) ────────────────────────────
    async def _latest_mark(self):
        """Most recent traded price (falls back to the latest sealed bar close)."""
        try:
            async with self._pool.acquire() as conn:
                v = await conn.fetchval(
                    "SELECT price FROM v2_ticks WHERE symbol=$1 ORDER BY timestamp DESC LIMIT 1",
                    self._symbol,
                )
                if v is None:
                    v = await conn.fetchval(
                        "SELECT close FROM v2_bars_4h WHERE symbol=$1 ORDER BY time DESC LIMIT 1",
                        self._symbol,
                    )
            return None if v is None else float(v)
        except Exception as e:
            logger.warning("failed to read latest mark: %s", e)
            return None

    def _current_open_positions(self) -> list[dict]:
        """Open positions to monitor. Prefer the live engine (authoritative, costed);
        fall back to the v2_trades snapshot restored at startup before the first replay."""
        if self._engine is not None:
            out = []
            for acct in (
                self._engine.accounts.subaccount_1,
                self._engine.accounts.subaccount_2,
            ):
                for p in acct.open_positions:
                    out.append(
                        {
                            "strategy": p.strategy,
                            "sub_account": p.sub_account,
                            "direction": _dir_str(p.direction).upper(),
                            "entry_price": float(p.entry_price),
                            "notional_usdt": float(p.notional_usdt),
                        }
                    )
            return out
        return [
            {
                "strategy": r["strategy"],
                "sub_account": r["sub_account"],
                "direction": str(r["direction"]).upper(),
                "entry_price": float(r["entry_price"]),
                "notional_usdt": float(r["size"]) * float(r["leverage"]),
            }
            for r in self._open_positions
        ]

    def _position_risk_snapshot(self, positions: list[dict], mark: float) -> dict:
        """Pure: unrealized PnL per open position vs `mark`, plus hard-stop breaches.
        Monitoring only — authoritative exits stay with the deterministic replay."""
        rows, total_unreal, breaches = [], 0.0, []
        for p in positions:
            entry = p["entry_price"]
            if entry <= 0:
                continue
            upnl_pct = (
                (mark - entry) / entry
                if p["direction"] == "LONG"
                else (entry - mark) / entry
            )
            upnl_usdt = upnl_pct * p["notional_usdt"]
            total_unreal += upnl_usdt
            row = {
                **p,
                "mark": mark,
                "unrealized_pnl_pct": upnl_pct,
                "unrealized_pnl_usdt": upnl_usdt,
            }
            rows.append(row)
            if upnl_pct <= -PAPER_HARD_STOP_PCT:
                breaches.append(row)
        return {
            "mark": mark,
            "n_open": len(rows),
            "total_unrealized_usdt": total_unreal,
            "positions": rows,
            "breaches": breaches,
        }

    async def _publish_position_risk(self) -> dict | None:
        mark = await self._latest_mark()
        if mark is None:
            return None
        snapshot = self._position_risk_snapshot(self._current_open_positions(), mark)
        try:
            await self._redis.set(
                "v2:paper:position_risk", json.dumps(snapshot, default=str)
            )
            if snapshot["breaches"]:
                logger.warning(
                    "intra-bar hard-stop breach on %d position(s) @ mark=%.2f",
                    len(snapshot["breaches"]),
                    mark,
                )
                await self._redis.set(
                    "v2:paper:risk_alert", json.dumps(snapshot["breaches"], default=str)
                )
        except Exception as e:
            logger.warning("failed to publish position risk: %s", e)
        return snapshot

    async def _position_management_loop(self):
        while True:
            try:
                await self._publish_position_risk()
            except Exception as e:
                logger.error(f"Error in position management loop: {e}")
            await asyncio.sleep(POSITION_MGMT_INTERVAL_SEC)

    # ── Startup position restore ────────────────────────────────────────────────
    async def _load_open_positions(self):
        """Restore the open-position snapshot from v2_trades (exit_time IS NULL) so
        the position monitor and the paper API have immediate visibility after a
        restart, before the first full-history replay rebuilds authoritative state."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, strategy, sub_account, direction, entry_price, size, "
                    "leverage, instrument, entry_time, entry_state "
                    "FROM v2_trades WHERE exit_time IS NULL AND instrument=$1 "
                    "ORDER BY entry_time ASC",
                    self._symbol,
                )
            self._open_positions = [dict(r) for r in rows]
            logger.info(
                "restored %d open positions from v2_trades", len(self._open_positions)
            )
        except Exception as e:
            logger.warning("failed to restore open positions: %s", e)
            self._open_positions = []

    async def run(self):
        self._pool = await asyncpg.create_pool(self._db_url)
        self._redis = redis.from_url(self._redis_url)
        self._writer = DBWriter(self._db_url)
        await self._writer.connect()
        await self._load_strategy_params()
        await self._load_open_positions()
        await self._process_backlog()

        logger.info("Paper Engine started, entering main loops")
        # Authoritative entries/exits are computed for every sealed 4H bar inside
        # _process_bar -> process_frame (deterministic full-history replay). The
        # two sub-bar loops are coherent with that model:
        #   - _strategy2_tick_loop():      replay when new ticks arrive so S2's H1
        #                                  Hawkes/micro stay current between bars
        #                                  (lock-serialised with the 4H loop).
        #   - _position_management_loop(): intra-bar risk monitor + alert (non-
        #                                  authoritative; exits remain bar-gated).
        await asyncio.gather(
            self._strategy1_4h_loop(),
            self._strategy2_tick_loop(),
            self._position_management_loop(),
        )


if __name__ == "__main__":
    engine = PaperEngine()
    asyncio.run(engine.run())
