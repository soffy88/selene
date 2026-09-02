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
import json
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

    # The live v2_strategy_params is a flat key/value table: param_key (text) /
    # param_value (jsonb), with the strategy encoded as a key prefix
    # (e.g. 'h2_branching_threshold'). Map (strategy, name) -> '{strategy}_{name}'.
    key_for = {f"{strategy}_{n}": n for n in param_names}

    def _as_float(v) -> float:
        if isinstance(v, str):
            v = json.loads(v)  # jsonb arrives as a JSON string
        return float(v)

    async def _fetch() -> dict[str, float]:
        conn = await asyncpg.connect(url)
        try:
            rows = await conn.fetch(
                "SELECT param_key, param_value FROM v2_strategy_params WHERE param_key = ANY($1)",
                list(key_for.keys()),
            )
            return {key_for[r["param_key"]]: _as_float(r["param_value"]) for r in rows}
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


def save_strategy_params(
    strategy: str,
    params: dict[str, float],
    db_url: Optional[str] = None,
) -> None:
    """
    Upsert named parameters into v2_strategy_params for the given strategy.

    The inverse of load_strategy_params: each (strategy, name) is written to the
    flat key '{strategy}_{name}' with the value JSON-encoded as a JSONB scalar.
    Existing keys are overwritten (ON CONFLICT). This is how offline calibration
    (e.g. hawkes_calibration) makes its results available to the live strategies —
    without it, Strategy 2 silently disables itself for lack of H2 reference params.

    Args:
        strategy: Value encoded as the key prefix (e.g. 'h2', 'tda1').
        params:   {name: value}. Values are coerced to float before storage.
        db_url:   Optional DSN override. Falls back to environment variables.
    """
    url = db_url or _default_db_url()
    rows = [(f"{strategy}_{name}", json.dumps(float(value))) for name, value in params.items()]

    async def _store() -> None:
        conn = await asyncpg.connect(url)
        try:
            await conn.executemany(
                "INSERT INTO v2_strategy_params (param_key, param_value, updated_at) "
                "VALUES ($1, $2::jsonb, NOW()) "
                "ON CONFLICT (param_key) DO UPDATE "
                "SET param_value = EXCLUDED.param_value, updated_at = NOW()",
                rows,
            )
        finally:
            await conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _store()).result()
