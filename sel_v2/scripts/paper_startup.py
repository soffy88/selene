import asyncio
import argparse
import logging
import os
from datetime import datetime
import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("paper_startup")

async def check_conditions(db_url):
    async with asyncpg.create_pool(db_url) as pool:
        async with pool.acquire() as conn:
            # 1. v2_bars_4h count >= 180
            bar_count = await conn.fetchval("SELECT count(*) FROM v2_bars_4h")
            logger.info(f"Condition 1: v2_bars_4h count = {bar_count}")
            if bar_count < 180:
                logger.error("v2_bars_4h count < 180")
                return False
                
            # 2. v2_strategy_params check
            params = await conn.fetch("SELECT param_key FROM v2_strategy_params")
            param_keys = {p['param_key'] for p in params}
            logger.info(f"Condition 2: Loaded {len(param_keys)} strategy params")
            required = {'h2_branching_threshold', 'cusum_mid_quantile'}
            if not required.issubset(param_keys):
                logger.error(f"Missing required params: {required - param_keys}")
                return False
                
            # 3. v2_lob_snapshots count > 0
            lob_count = await conn.fetchval("SELECT count(*) FROM v2_lob_snapshots")
            logger.info(f"Condition 3: v2_lob_snapshots count = {lob_count}")
            if lob_count == 0:
                logger.error("v2_lob_snapshots is empty")
                return False
                
            # 4. helixa check (WARNING level)
            try:
                # Assuming helixa is accessible via public schema or similar
                helixa_exists = await conn.fetchval("SELECT 1 FROM information_schema.tables WHERE table_name = 'metadata' AND table_catalog = 'helixa'")
                if not helixa_exists:
                    logger.warning("helixa database/metadata table not found (non-blocking)")
            except:
                logger.warning("Failed to check helixa (non-blocking)")
                
    return True

async def provision(db_url):
    PHASE_HISTORY_SQL = """
    INSERT INTO v2_strategy_phase_history (
        timestamp, strategy, from_phase, to_phase,
        rolling_w, rolling_r, sample_size,
        kelly_fraction_estimated, kelly_cap_lower, kelly_cap_upper,
        decision_id, created_at
    ) VALUES
        (NOW(), 'strategy_1', NULL, 'phase_0', NULL, NULL, 0, NULL, 0.20, 0.20, NULL, NOW()),
        (NOW(), 'strategy_2', NULL, 'phase_0', NULL, NULL, 0, NULL, 0.10, 0.10, NULL, NOW())
    ON CONFLICT DO NOTHING;
    """
    
    DECISION_TRAIL_SQL = """
    INSERT INTO v2_decision_trail (
        timestamp, decision_type, trigger_source, target_component,
        wiki_decision, decision_basis, created_at, created_by
    ) VALUES (
        NOW(), 'paper_startup', 'manual', 'paper_engine',
        'paper trading 正式启动',
        'Wave 1-5 重建完成 + WSL 崩溃后全新部署',
        NOW(), 'wiki'
    );
    """
    
    async with asyncpg.create_pool(db_url) as pool:
        async with pool.acquire() as conn:
            await conn.execute(PHASE_HISTORY_SQL)
            await conn.execute(DECISION_TRAIL_SQL)
            logger.info("Provisioning SQL executed successfully")

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--auto-exec", action="store_true")
    args = parser.parse_args()

    if args.dry_run:
        logger.info("Dry run mode, skipping DB checks")
        return

    if await check_conditions(args.db_url):
        logger.info("Pre-checks PASSED")
        if args.auto_exec:
            await provision(args.db_url)
    else:
        logger.error("Pre-checks FAILED")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
