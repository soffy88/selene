import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v2_onchain_collector")

async def main():
    logger.info("v2_onchain_collector: no API key configured, stub mode")
    while True:
        logger.warning("onchain stub: waiting for API key")
        await asyncio.sleep(600)

if __name__ == "__main__":
    asyncio.run(main())
