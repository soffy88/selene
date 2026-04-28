"""Entry point: run trade_flow_collector loop (Redis-only, no DB writes needed)."""
import asyncio
import logging
import os

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def _main() -> None:
    from shared.db.connections import get_redis
    from sel_engine.collectors.trade_flow_collector import run_trade_flow_collector

    redis = await get_redis()

    symbol  = os.environ.get("SEL_SYMBOL",  "BTCUSDT")
    inst_id = os.environ.get("SEL_INST_ID", "BTC-USDT-SWAP")
    logger.info("Starting trade_flow_collector symbol=%s inst=%s", symbol, inst_id)
    await run_trade_flow_collector(symbol=symbol, inst_id=inst_id, redis=redis)


if __name__ == "__main__":
    asyncio.run(_main())
