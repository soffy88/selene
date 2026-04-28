# sel Collectors — RUNBOOK

> Collectors feed the three stub features (H, TF, OI) into the sel state engine.
> All three must be running for `feature_completeness` to exceed ~0.45.
> TF uses Redis-only accumulation; H and OI persist to TimescaleDB.

---

## Services

| Service | Container | Write target | Write frequency |
|---|---|---|---|
| sel-orderbook | selene-sel-orderbook-1 | `sel_orderbook_samples` (TimescaleDB) + Redis `sel:h_samples:*` | Every 60s (1 row per 5-min bucket) |
| sel-trade-flow | selene-sel-trade-flow-1 | Redis `sel:tf_accum:BTCUSDT:{bar_ts}` | Every 5s poll; value accumulates per 1H bar |
| sel-oi | selene-sel-oi-1 | `sel_oi_history` (TimescaleDB) | Every 300s (5 min) |

---

## Start / Stop

```bash
# Start all three
docker compose up -d sel-orderbook sel-trade-flow sel-oi

# Stop all three
docker compose stop sel-orderbook sel-trade-flow sel-oi

# Restart a single collector
docker compose restart sel-orderbook

# View logs (live)
docker compose logs -f sel-orderbook
docker compose logs -f sel-trade-flow
docker compose logs -f sel-oi
```

---

## Health Check SQLs

### sel_orderbook_samples (written by sel-orderbook, every 60s)

```sql
-- Row count and recency
SELECT
    COUNT(*)                          AS total_rows,
    MAX(time_bucket)                  AS latest_sample,
    NOW() - MAX(time_bucket)          AS lag,
    COUNT(*) FILTER (WHERE time_bucket >= NOW() - INTERVAL '1 hour') AS rows_last_1h
FROM sel_orderbook_samples
WHERE symbol = 'BTCUSDT';
```

Expected: `rows_last_1h` ≥ 12 (one row per 5-min bucket × 12 = 1 hour).
Alert if `lag > 3 minutes`.

```sql
-- H sample quality
SELECT
    AVG(H_sample) AS mean_H,
    MIN(H_sample) AS min_H,
    MAX(H_sample) AS max_H,
    COUNT(*) FILTER (WHERE H_sample IS NULL) AS null_H_count
FROM sel_orderbook_samples
WHERE symbol = 'BTCUSDT'
  AND time_bucket >= NOW() - INTERVAL '1 hour';
```

### sel_oi_history (written by sel-oi, every 5 min)

```sql
SELECT
    COUNT(*)                    AS total_rows,
    MAX(time)                   AS latest_oi,
    NOW() - MAX(time)           AS lag,
    AVG(oi_value)               AS mean_oi_usd,
    MAX(oi_value)               AS max_oi_usd
FROM sel_oi_history
WHERE symbol = 'BTCUSDT'
  AND time >= NOW() - INTERVAL '1 hour';
```

Expected: ≥ 12 rows per hour.  Alert if `lag > 8 minutes`.

### trade_flow — Redis (sel-trade-flow accumulates per bar)

```bash
# Check current bar TF value via docker exec
docker exec selene-sel-trade-flow-1 python3 -c "
import asyncio, redis.asyncio as aioredis, os
from datetime import datetime, timezone

async def check():
    r = aioredis.from_url(os.environ['REDIS_URL'], decode_responses=True)
    bar_ts = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
    key = f'sel:tf_accum:BTCUSDT:{bar_ts}'
    val = await r.get(key)
    ids_key = f'sel:tf_trade_ids:BTCUSDT:{bar_ts}'
    n_ids = await r.scard(ids_key)
    print(f'TF accum this bar : {val} USDT')
    print(f'Unique trade IDs  : {n_ids}')
    await r.aclose()

asyncio.run(check())
"
```

Expected after 5 minutes: `n_ids` ≥ 1000 (OKX BTC-USDT-SWAP is very liquid).

---

## OKX Endpoints Used

| Collector | Endpoint | Notes |
|---|---|---|
| orderbook | `GET /api/v5/market/books?instId=BTC-USDT-SWAP&sz=20` | Top-20 levels |
| trade-flow | `GET /api/v5/market/trades?instId=BTC-USDT-SWAP&limit=100` | Deduplicated by tradeId |
| oi-persister | `GET /api/v5/public/open-interest?instId=BTC-USDT-SWAP&instType=SWAP` | Prefers `oiUsd` field |

---

## How TF flows into sel_features

Trade flow does **not** persist to a dedicated DB table.
At each 1H bar close, `FeatureCalculator.compute()` calls `get_tf_from_redis()` which reads:

```
sel:tf_accum:BTCUSDT:{bar_ts_iso}   →  float (signed USDT notional)
```

The value is then written as `sel_features.TF` at bar close. The Redis key expires 2H after bar close.

> Implication: if `sel-trade-flow` is down for an entire 1H bar, that bar's `TF` will be `None`.
> There is no way to back-fill TF after the fact.

---

## How H flows into sel_features

1. `sel-orderbook` samples every 60s → pushes `H` value to Redis list `sel:h_samples:BTCUSDT:{bar_ts}`.
2. `FeatureCalculator.compute()` reads the full list, averages all samples, uses the mean as bar `H`.
3. Simultaneously, each sample is persisted to `sel_orderbook_samples` (5-min bucket upsert).

```sql
-- Verify H samples in Redis were persisted
SELECT time_bucket, H_sample, spread_bps
FROM sel_orderbook_samples
WHERE symbol = 'BTCUSDT'
ORDER BY time_bucket DESC
LIMIT 10;
```

---

## Common Failures

### Container keeps restarting

```bash
docker compose logs --tail=50 sel-orderbook
```

Most likely cause: `TIMESCALE_URL` or `REDIS_URL` not set in `.env`.
Check: `docker compose config | grep -A3 sel-orderbook` to inspect resolved env.

### H / OI values are None after collector is running

- Check `sel_orderbook_samples` row count above — if 0, the collector is writing to wrong DB.
- The FeatureCalculator reads H from Redis (not from DB) at bar close. Check Redis key:

```bash
docker exec selene-sel-orderbook-1 python3 -c "
import asyncio, redis.asyncio as aioredis, os
from datetime import datetime, timezone

async def check():
    r = aioredis.from_url(os.environ['REDIS_URL'], decode_responses=True)
    bar_ts = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
    key = f'sel:h_samples:BTCUSDT:{bar_ts}'
    samples = await r.lrange(key, 0, -1)
    print(f'H samples this bar: {len(samples)} values')
    print(f'Latest 5: {samples[-5:]}')
    await r.aclose()

asyncio.run(check())
"
```

### OI not updating

- OKX OI endpoint can return empty `data` array during exchange maintenance.
- Check if `oi_value` column type accepts the USD notional (can be ~10^10 for BTC).
- `sel_oi_history.oi_value` is `NUMERIC(20,8)` → max ~10^12, sufficient.

### TF always 0 or wrong sign

- Verify OKX trade `side` field: `"buy"` = taker lifts ask (positive), `"sell"` = taker hits bid (negative).
- Check dedup: if Redis key `sel:tf_trade_ids:BTCUSDT:{bar_ts}` is empty after 5 minutes, the set is not persisting (check REDIS_URL DB number).

---

## Expected write rates (steady state)

| Table / Key | Rows/hour | Rows/day |
|---|---|---|
| `sel_orderbook_samples` | ~12 (5-min buckets) | ~288 |
| `sel_oi_history` | ~12 | ~288 |
| `sel:tf_accum` (Redis, per bar) | 1 key/hour (rolled) | n/a |
| `sel:h_samples` (Redis, per bar) | ~60 appends/hour | n/a |
