"""v2 on-chain exchange-flow collector — OUT OF SCOPE (optimization item #24).

Status: NOT DEPLOYED. On-chain exchange-flow ingestion requires a paid data
source (Glassnode / Nansen / CryptoQuant); none is funded, so this remained a
no-op stub and was removed from docker-compose. Per DATA_FOUNDATION_ROADMAP §6,
`v2_onchain_exchange_flows` has zero state-machine impact today.

To ACTIVATE later:
  1. Fund a provider and set ONCHAIN_API_KEY (+ provider base URL) in the env.
  2. Implement collect() below to fetch large exchange in/out flows and INSERT
     into v2_onchain_exchange_flows (schema in sel_v2/db/schema.sql).
  3. Re-add the v2-onchain-collector service to docker-compose.yml.

Running this module today exits immediately with a clear message rather than
looping forever as a wasted container.
"""

import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("v2_onchain_collector")


def main() -> int:
    if not os.getenv("ONCHAIN_API_KEY"):
        logger.warning(
            "on-chain exchange flows are OUT OF SCOPE: no ONCHAIN_API_KEY / paid "
            "source configured. See module docstring to activate. Exiting."
        )
        return 0
    logger.error(
        "ONCHAIN_API_KEY is set but the collector is not implemented yet "
        "(item #24). Implement collect() before deploying."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
