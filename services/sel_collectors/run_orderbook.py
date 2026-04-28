"""Entry point: apply sel_engine schema migrations, then run orderbook_collector loop."""
import asyncio
import logging
import os

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def _main() -> None:
    from shared.db.connections import get_pg, get_redis
    from sel_engine.db.migrations import apply_schema
    from sel_engine.collectors.orderbook_collector import run_orderbook_collector

    pool = await get_pg()
    redis = await get_redis()

    logger.info("Applying sel_engine schema …")
    await apply_schema(pool)

    symbol  = os.environ.get("SEL_SYMBOL",  "BTCUSDT")
    inst_id = os.environ.get("SEL_INST_ID", "BTC-USDT-SWAP")
    logger.info("Starting orderbook_collector symbol=%s inst=%s", symbol, inst_id)
    await run_orderbook_collector(symbol=symbol, inst_id=inst_id, redis=redis, pool=pool)


if __name__ == "__main__":
    asyncio.run(_main())
