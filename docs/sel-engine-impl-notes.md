# sel-engine Wave 1 — Implementation Notes

## Wiki Gap

**WIKI_GAP: `spec-lang-v1.0.md` does not exist in `_helios-platform`.**
All feature definitions and the 6 market-state taxonomy (Coiling / Surging / Drifting-Calm /
Drifting-Charged / Critical / Cascade) were taken directly from the Wave 1 task description.
The formal spec document must be created before Wave 2 (state identification) begins.

---

## Feature Availability Status

| Feature | Status | Dependency |
|---|---|---|
| `close` | Available | candles hypertable |
| `delta_p_pct` | Available (2+ bars) | candles |
| `sigma_p_24h` | Available (25+ bars) | candles |
| `price_autocorr_12h/24h/48h` | Available (13/25/49+ bars) | candles |
| `sigma_p_d2` | Available (3+ sigma_p rows) | sel_features |
| `funding_rate` | Available | Redis `cw4:funding_rates` hash |
| `H` | **STUB** | orderbook_collector not deployed |
| `H_change_rate_std_12h` | **STUB** | H history not available |
| `top_5_bid_size` | **STUB** | orderbook_collector not deployed |
| `top_5_ask_size` | **STUB** | orderbook_collector not deployed |
| `total_depth` | **STUB** | orderbook_collector not deployed |
| `spread_bps` | **STUB** | orderbook_collector not deployed |
| `TF` | **STUB** | trade_flow_collector not deployed |
| `OI` | **STUB** | oi_persister not deployed (Redis-only currently) |
| `LV` | **STUB** | requires total_depth + spread_bps |
| `absorption_ratio` | **STUB** | requires TF |
| `OI_hurst` | **STUB** | requires 48+ OI history rows |

---

## Data Gaps & Wiki Decisions Needed

### 1. OI source ambiguity
OKX `/api/v5/public/open-interest` returns `oiUsd`, `oiCcy`, and `oi` (contract count).
**Decision needed**: which OI unit does the sel spec use? Current implementation prefers
`oiUsd` → `oiCcy` → `oi` as fallback. For Hurst exponent stability, consistent units matter.

### 2. TF (taker flow) granularity
OKX `/api/v5/market/trades` returns up to 100 most recent trades. With high volume, trades
may be missed between polls. Alternatives: WebSocket public trade channel (lower latency,
no gaps). **Decision needed**: REST polling (simpler) vs WebSocket (more complete)?

### 3. H entropy side
Spec says `H = -Σ p_i * log(p_i) where p_i = bid_i_size / total_bid_size`.
Only bid side is specified. Implementation computes bid-side entropy.
**Decision needed**: should ask-side entropy also be tracked separately or averaged?

### 4. Candle history depth
Current candles hypertable has ~12 days of 1H bars. The backfill script can extend this to
~2 years via OKX history-candles API. **Action required**: run backfill before sel features
can be computed for any meaningful time range.

### 5. LV formula normalization
The LV composite uses `depth_score * 0.5 + min(spread_score, 2.0) * 0.25`. This caps LV
at `0.5 + 0.5 = 1.0`. The weighting (50/25 split) is an assumption. Spec does not define
exact weights. **Decision needed before Wave 2**: calibrate weights on historical data.

### 6. sigma_p_24h annualization
Spec says "24H rolling std of log returns (annualized equivalent: 24H window)". Current
implementation returns raw 24H std (not annualized). If annualized: multiply by `sqrt(8760)`
for 1H bars. **Decision needed**: annualized or raw?

---

## Deviations from spec-lang-v1.0.md

None — spec document does not yet exist. All decisions above are tentative.

---

## Module Layout

```
sel_engine/
  features/
    schema.py      FeatureVector + FeatureAvailability dataclasses
    price.py       compute_price_features, compute_autocorr, compute_sigma_p_d2
    liquidity.py   compute_orderbook_entropy, compute_H_from_samples, compute_H_change_rate_std
    orderbook.py   compute_depth_features (real + stub)
    flow.py        get_funding_rate_from_redis, get_tf_from_redis, compute_absorption_ratio
    derived.py     compute_LV, compute_hurst_rs, compute_all_derived
    calculator.py  FeatureCalculator async orchestrator
  db/
    schema.sql     DDL: sel_features, sel_oi_history, sel_funding_history, sel_orderbook_samples
    migrations.py  apply_schema() via asyncpg
    writer.py      write_feature_vector(), write_oi_snapshot(), etc.
    reader.py      read_features(), read_closes(), read_sigma_p_history(), etc.
  collectors/
    orderbook_collector.py  Sample OKX books every 60s → Redis + DB
    trade_flow_collector.py Accumulate taker flow → Redis
    oi_persister.py         Persist OI every 5min → sel_oi_history
  validator.py  FeatureValidator: missing rate, outlier rate, distribution stats
  backfill.py   Back-fill 1H candles from OKX history-candles API
```
