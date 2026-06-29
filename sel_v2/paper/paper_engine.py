import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
import asyncpg
import redis.asyncio as redis

from sel_v2.strategies.db_writer import DBWriter

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("paper_engine")

# Fixed namespace so a logical paper trade maps to the same v2_trades.id across the
# engine's full-history replays (item #6 — makes persistence idempotent).
_TRADE_NS = uuid.UUID("6f1a7e2c-0b3d-4a5e-9c8b-2d1f0a3b4c5d")


def _trade_id(strategy: str, sub_account: str, entry_time, direction: str, entry_price: float) -> str:
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

    async def _load_last_bar_ts(self) -> datetime | None:
        try:
            raw = await self._redis.get(self.REDIS_KEY_LAST_BAR_TS)
            if raw:
                ts = datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw)
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
            rows = await conn.fetch("SELECT param_key, param_value FROM v2_strategy_params")
            self._strategy_params = {row['param_key']: row['param_value'] for row in rows}
        logger.info(f"Loaded {len(self._strategy_params)} strategy parameters")

    async def _load_bars_df(self):
        """Load the full 4H bar history for the symbol into a DataFrame."""
        import pandas as pd
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT time, open, high, low, close, volume FROM v2_bars_4h "
                "WHERE symbol=$1 ORDER BY time ASC", self._symbol)
        if not rows:
            return None
        return pd.DataFrame([dict(r) for r in rows])

    async def _reprocess(self):
        """Replay the full bar history through the strategy engine. Positions are a pure
        function of history, so a fresh engine each tick gives deterministic, idempotent state.
        TDA is skipped unless `ripser` is installed; OI/funding are fed when available."""
        from sel_v2.paper.strategy_engine import PaperStrategyEngine
        df = await self._load_bars_df()
        if df is None or len(df) < 180:
            logger.info("not enough bars to run strategy engine (have %s)", 0 if df is None else len(df))
            return
        try:
            import ripser  # noqa: F401
            skip_tda = False
        except Exception:
            skip_tda = True
        engine = PaperStrategyEngine(
            total_nav_usdt=float(self._strategy_params.get("paper_total_nav", 100_000) or 100_000),
            instrument=self._symbol, skip_tda=skip_tda,
        )
        summary = engine.process_frame(df)
        self._engine = engine
        await self._persist_summary(summary)
        await self._persist_results(engine)
        logger.info("strategy engine: bars=%s state=%s s1=%s s2=%s equity=%s",
                    summary["bars"], list(summary["state_counts"])[-1:],
                    summary["s1"], summary["s2"], summary["total_equity"])

    async def _persist_summary(self, summary: dict):
        """Publish the latest engine summary to Redis for the UI/monitoring layer."""
        try:
            import json
            await self._redis.set("v2:paper:engine_summary", json.dumps(summary, default=str))
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
                        trade_id=_trade_id(p.strategy, p.sub_account, p.entry_time, d, p.entry_price),
                        strategy=p.strategy, sub_account=p.sub_account,
                        entry_time=p.entry_time, entry_price=p.entry_price, direction=d,
                        size=p.size_usdt, leverage=p.leverage, instrument=p.instrument,
                        entry_state=p.entry_state,
                        exit_time=cp.exit_time, exit_price=cp.exit_price,
                        exit_reason=cp.exit_reason, pnl_usdt=cp.pnl_usdt, pnl_pct=cp.pnl_pct,
                    )
                for p in acct.open_positions:
                    d = _dir_str(p.direction)
                    await self._writer.upsert_trade(
                        trade_id=_trade_id(p.strategy, p.sub_account, p.entry_time, d, p.entry_price),
                        strategy=p.strategy, sub_account=p.sub_account,
                        entry_time=p.entry_time, entry_price=p.entry_price, direction=d,
                        size=p.size_usdt, leverage=p.leverage, instrument=p.instrument,
                        entry_state=p.entry_state,
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
                self._symbol, persistent_ts, max_bar
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
                        self._symbol
                    )
                if latest_bar and latest_bar['time'] > self._last_bar_ts:
                    await self._process_bar(latest_bar)
                    self._last_bar_ts = latest_bar['time']
                    await self._persist_last_bar_ts(self._last_bar_ts)
            except Exception as e:
                logger.error(f"Error in strategy1 loop: {e}")
            await asyncio.sleep(60)

    async def run(self):
        self._pool = await asyncpg.create_pool(self._db_url)
        self._redis = redis.from_url(self._redis_url)
        self._writer = DBWriter(self._db_url)
        await self._writer.connect()
        await self._load_strategy_params()
        # await self._load_open_positions()
        await self._process_backlog()
        
        logger.info("Paper Engine started, entering main loops")
        await asyncio.gather(
            self._strategy1_4h_loop(),
            # self._strategy2_tick_loop(),
            # self._position_management_loop(),
        )

if __name__ == "__main__":
    engine = PaperEngine()
    asyncio.run(engine.run())
