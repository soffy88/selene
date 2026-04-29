"""
v2_strategy_params table reader.

Single access point for all reads from the v2_strategy_params table.
Avoids scattering raw SQL across strategy modules.

Connection: reads POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER /
            POSTGRES_PASSWORD / POSTGRES_DB from environment, same as
            the rest of the Selene services.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
from typing import Optional

import asyncpg


def _default_db_url() -> str:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "helios")
    password = os.getenv("POSTGRES_PASSWORD", "")
    dbname = os.getenv("POSTGRES_DB", "selene")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def load_strategy_params(
    strategy: str,
    param_names: list[str],
    db_url: Optional[str] = None,
) -> dict[str, float]:
    """
    Load named parameters from v2_strategy_params for the given strategy.

    Only rows with valid_to IS NULL (currently active) are returned.
    Raises RuntimeError if any requested param_name is missing from the table.

    Args:
        strategy:    Value of the `strategy` column (e.g. 'h2', 'tda1').
        param_names: Names to fetch. All must be present or RuntimeError is raised.
        db_url:      Optional DSN override. Falls back to environment variables.

    Returns:
        Dict mapping param_name → float(param_value).
    """
    url = db_url or _default_db_url()

    async def _fetch() -> dict[str, float]:
        conn = await asyncpg.connect(url)
        try:
            rows = await conn.fetch(
                """
                SELECT param_name, param_value
                FROM v2_strategy_params
                WHERE strategy = $1
                  AND param_name = ANY($2)
                  AND valid_to IS NULL
                ORDER BY param_name
                """,
                strategy,
                param_names,
            )
            return {row["param_name"]: float(row["param_value"]) for row in rows}
        finally:
            await conn.close()

    # Run in a dedicated thread so this function is safe to call from both
    # synchronous code and from within a running asyncio event loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(asyncio.run, _fetch()).result()

    missing = [n for n in param_names if n not in result]
    if missing:
        raise RuntimeError(
            f"v2_strategy_params: missing params {missing} for strategy={strategy!r}. "
            "Populate the table by running Wave 1 hawkes_calibration.py first."
        )
    return result
