"""
sel-bar-runner entry point.

Triggers bar-close processing at every UTC hour boundary + 30 seconds.
The 30-second offset lets K-line and collector data settle in the DB/Redis
before the pipeline reads them.

EC-10 note: StateEngine in-memory state (DwellFilter, CascadeCooling) is lost
on container restart. During the 720-bar cold-start period this has no effect
(recognizer returns cold_start=True). Post-warmup restarts cause a few bars of
incorrect dwell/cooling behavior; this is an accepted limitation.
"""
import asyncio
import faulthandler
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

from shared.db.connections import close_all, get_pg, get_redis
from sel_engine.scheduler.bar_close_runner import BarCloseRunner

logger = logging.getLogger(__name__)

SYMBOL = os.environ.get("SEL_SYMBOL", "BTCUSDT")
_TRIGGER_OFFSET_SECS = 30


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Dump all coroutine stacks to stderr on SIGUSR1 — diagnostic, kept permanently
    faulthandler.enable()
    faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    logger.info("faulthandler registered on SIGUSR1")

    pool = await get_pg()
    redis = await get_redis()
    runner = BarCloseRunner(symbol=SYMBOL, pool=pool, redis=redis)

    logger.info("sel-bar-runner started for symbol=%s", SYMBOL)

    try:
        while True:
            now = datetime.now(timezone.utc)
            # Next trigger = next full UTC hour + 30s
            next_hour = (now + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0
            )
            next_trigger = next_hour + timedelta(seconds=_TRIGGER_OFFSET_SECS)
            wait_secs = (next_trigger - now).total_seconds()

            logger.info(
                "next bar trigger in %.0fs at %s",
                wait_secs,
                next_trigger.isoformat(),
            )
            await asyncio.sleep(wait_secs)

            # bar_open_time = the UTC hour that just closed
            bar_open_time = (
                next_trigger.replace(minute=0, second=0, microsecond=0)
                - timedelta(hours=1)
            )

            try:
                await runner.run_bar(bar_open_time)
            except Exception as exc:
                logger.error(
                    "run_bar(%s) raised: %s",
                    bar_open_time.isoformat(),
                    exc,
                    exc_info=True,
                )
    finally:
        await close_all()


if __name__ == "__main__":
    asyncio.run(main())
