import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
import asyncpg
import redis.asyncio as redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("paper_engine")

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

    async def _process_bar(self, bar):
        # Stub for state evaluation and strategy logic
        logger.info(f"bar_processed: ts={bar['time']} state=STUB")
        # In actual implementation, call state machine and strategy filters here

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
        for bar in backlog:
            await self._process_bar(bar)
            await self._persist_last_bar_ts(bar['time'])
        self._last_bar_ts = max_bar
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
