"""Entry point: apply sel_engine schema migrations, then run oi_persister loop."""
import asyncio
import logging
import os

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


async def _main() -> None:
    from shared.db.connections import get_pg
    from sel_engine.db.migrations import apply_schema
    from sel_engine.collectors.oi_persister import run_oi_persister

    pool = await get_pg()

    logger.info("Applying sel_engine schema …")
    await apply_schema(pool)

    symbol  = os.environ.get("SEL_SYMBOL",  "BTCUSDT")
    inst_id = os.environ.get("SEL_INST_ID", "BTC-USDT-SWAP")
    logger.info("Starting oi_persister symbol=%s inst=%s", symbol, inst_id)
    await run_oi_persister(symbol=symbol, inst_id=inst_id, pool=pool)


if __name__ == "__main__":
    asyncio.run(_main())
