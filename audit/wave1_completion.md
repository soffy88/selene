# Wave 1 Completion Audit

**Date**: 2026-04-29  
**Branch**: main  
**Spec refs**: sel-language-v2.0.md §32, sel-language-v2.1-patches.md §13.1  

## Deliverables Checklist

| Task | File | Status | Notes |
|------|------|--------|-------|
| 1.1 Directory skeleton | `sel_v2/` hierarchy | ✅ Done | `sel_v2/{db,data,offline,engine,strategies}` + `analysis/` |
| 1.2 DB schema | `sel_v2/db/schema.sql` | ✅ Done | 14 v2_ tables, 10 TimescaleDB hypertables |
| 1.3 OKX backfill | `sel_v2/data/okx_backfill.py` | ✅ Done | 5096 bars 2024-01-01 → 2026-04-29 |
| 1.4 T1 Transfer Entropy | `sel_v2/offline/transfer_entropy.py` | ✅ Done | STE causal map, 30 pairs × 200 surrogates |
| 1.5 W1 Wavelet | `sel_v2/offline/wavelet.py` | ✅ Done | 6-level DWT, energy distribution |
| 1.6 TDA1 Calibration | `sel_v2/offline/tda_calibration.py` | ✅ Done | Persistence Landscape L^1, fixed 2 bugs |
| 1.7 Hawkes H2 | `sel_v2/offline/hawkes_calibration.py` | ✅ Done | Exponential Hawkes MLE, η=0.55 full-fit |
| 1.8 This audit | `audit/wave1_completion.md` | ✅ Done | — |

## Analysis Outputs

| File | Description |
|------|-------------|
| `analysis/data/btc_4h.parquet` | 5096 BTC-USDT 4H bars |
| `analysis/causal_map_v1.md` | T1 STE causal map |
| `analysis/wavelet_multiscale_v1.md` | W1 wavelet energy analysis |
| `analysis/tda_calibration_v1.md` | TDA1 persistence landscape calibration |
| `analysis/hawkes_calibration_v1.md` | H2 Hawkes branching ratio calibration |

---

## 1.2 DB Schema Summary

**Tables created**: 14 (all v2_ prefix)  
**Hypertables**: 10 (TimescaleDB)  

| Table | Type | Notes |
|-------|------|-------|
| v2_ticks | hypertable (1-day chunks) | |
| v2_lob_snapshots | hypertable (6h chunks) | |
| v2_derivatives_snapshots | hypertable | |
| v2_liquidations | hypertable | |
| v2_onchain_exchange_flows | hypertable | |
| v2_bars_4h | hypertable | UNIQUE on (time, symbol) |
| v2_state_history | hypertable | Composite PK (id, timestamp) |
| v2_cusum_events | hypertable | Composite PK (id, entry_time) |
| v2_inverse_vocab_events | hypertable | Composite PK (id, timestamp) |
| v2_trades | hypertable | Composite PK (id, entry_time) |
| v2_decision_trail | regular | |
| v2_strategy_params | regular | |
| v2_tool_evaluation_results | regular | v2.1 §15 addition |
| v2_strategy_phase_history | regular | v2.1 §15 addition |

**Bug fixed**: TimescaleDB requires composite PK including partition column. Fixed 4 hypertables from UUID-only PK to `(id, timestamp)` composite.

---

## 1.3 OKX Backfill

```
Symbol: BTC-USDT  Bar: 4H  
Bars: 5096  
Coverage: 2024-01-01 00:00:00+00:00 → 2026-04-29 04:00:00+00:00  
Output: analysis/data/btc_4h.parquet  
Mode: OKX public REST /api/v5/market/history-candles (no auth)
```

---

## 1.4 T1 Transfer Entropy (STE) Results

**Method**: Symbolic Transfer Entropy, ordinal patterns d=3, lag=1, 200 phase-randomized surrogates  
**Signals**: price_return, realised_vol, hl_range, volume_change, signed_vol_flow, cum_vol_imbalance  
**Pairs tested**: 30 directed pairs  

**Result**: 0 significant causal edges (p < 0.05)

**Design validation**:
- ⚠️ No significant edges detected with OHLCV-derived proxies
- Root cause: signed_vol_flow / OFI proxy (OHLCV volume) is too coarse vs. true LOB OFI
- Re-validate when real LOB OFI, OI, funding rate data available

---

## 1.5 W1 Wavelet (DWT) Results

**Method**: db4 wavelet, 6-level DWT, 4H returns  

| Level | Scale | Energy % | Finding |
|-------|-------|----------|---------|
| L1 | ~8H | 49.3% | Dominant — sub-daily noise |
| L2 | ~16H | 25.0% | Moderate |
| L3 | ~32H | 13.3% | Low |
| L4 | ~64H | 6.3% | Low |
| L5 | ~128H | 3.0% | Low |
| L6 | ~256H | 1.6% | Low |

**Design validation**:
- ✅ 4H anchor justified: 74.3% of energy sits in L1+L2 (sub-16H noise zone). 4H aggregation filters this.
- ✅ CUSUM-Short alignment: strategy 2 (30s–10min) addresses scales below L1, consistent with v2.0 dual-strategy rationale.

---

## 1.6 TDA1 Persistence Landscape Results

**Method**: Takens embedding (d=4, τ=1) → Vietoris-Rips → H1 persistence landscape L^1 norm  
**Rolling window**: 50 bars (200h), step 5 bars, 1010 windows  

| Statistic | Value |
|-----------|-------|
| Mean L^1 | 0.000030 |
| Std L^1 | 0.000076 |
| 95th pct (threshold) | 0.000097 |

**Cascade sensitivity**: 0/4 (0%)  
**Control false positive rate**: 0/4 (0%)

**Initial production parameter**: `TDA1_THRESHOLD_STATIC = 0.000097`

**Bugs fixed during development**:
1. Input was log-returns (stationary → no H1 loops) — changed to log-prices per Gidea & Katz 2018
2. `np.trapz` removed in NumPy 2.0 — replaced with `np.trapezoid`

**Design validation**:
- ❌ Low cascade sensitivity with current proxy parameters
- ✅ 0% false positive rate
- Note: TDA L^1 is one of two conditions in combined logic (v2.1 §2.1); single-tool failure is filtered

---

## 1.7 H2 Hawkes Branching Ratio Results

**Method**: Exponential Hawkes MLE (Nelder-Mead, 5+ restarts)  
**Events**: |4H log-return| > 1σ (1060 events, 20.8% of bars)  
**Rolling window**: 540 bars (90 days), step 6 bars (1 day), 760 windows  

**Full-dataset fit**:

| Parameter | Value |
|-----------|-------|
| μ (baseline) | 0.093136 |
| α (excitation) | 0.023899 |
| β (decay) | 0.043163 |
| **η = α/β** | **0.5537** |

**Rolling summary**:

| Statistic | Value |
|-----------|-------|
| Median η | 0.34 |
| 90th pct η | 0.62 |
| 95th pct η | 0.73 |
| Fraction > 0.85 | 4.2% |

**Cascade sensitivity**: 0/4 (0%)  
**Control false positive rate**: 0/3 (0%)

**Initial production parameter**: `HAWKES_BRANCHING_RATIO_THRESHOLD = 0.85`

**Design validation**:
- ❌ Low cascade sensitivity — OHLCV proxy (|return| > 1σ) is insufficient
- ✅ 0% false positive rate
- Full-dataset η = 0.55 indicates BTC 4H process is historically sub-critical
- Wave 3 tick-level Hawkes required for reliable discrimination

---

## Bugs & Fixes

| # | File | Bug | Fix |
|---|------|-----|-----|
| 1 | schema.sql | UUID-only PK incompatible with TimescaleDB hypertable partitioning | Changed to composite PK (id, timestamp) for 4 tables |
| 2 | tda_calibration.py | Used log-returns for Takens embedding (stationary → 0 H1 persistence) | Changed to log-prices (Gidea & Katz 2018) |
| 3 | tda_calibration.py | `np.trapz` removed in NumPy 2.0; exception silently caught → all L^1 = 0 | Replaced with `np.trapezoid` |
| 4 | hawkes_calibration.py | MLE optimizer overflow on some windows (α/β → ∞) | Clipped to max 10.0 for statistics; flagged in report |

---

## Production Parameter Summary

```python
# TDA1 (v2.1 §12.1)
TDA1_THRESHOLD_STATIC = 0.000097  # 95th pct of 1010-window L^1 distribution
TDA1_QUANTILE = 0.95
TDA1_ROLLING_WINDOW_BARS = 540    # 90 days rolling
TDA1_D = 4
TDA1_TAU = 1
TDA1_WINDOW = 50                  # bars per TDA computation

# H2 Hawkes (v2.1 §5.2)
HAWKES_BRANCHING_RATIO_THRESHOLD = 0.85  # α/β > this = Critical
HAWKES_WINDOW_BARS = 540                 # 90 days rolling
HAWKES_EVENT_SIGMA = 1.0                 # event = |return| > 1σ
HAWKES_MU_REF = 0.093136
HAWKES_ALPHA_REF = 0.023899
HAWKES_BETA_REF = 0.043163
HAWKES_ETA_REF = 0.5537                  # full-dataset reference η
```

---

## Limitations (Wave 1 Scope)

1. **Proxy-based signals**: T1, TDA1, H2 all rely on OHLCV-derived proxies. Real LOB OFI, trade tick data, funding rates required for production-quality calibration (Wave 3).
2. **Cascade sample size**: 4 events — far too small for statistical validation (noted in v2.0 §18.3).
3. **No live paper data yet**: All calibration is retrospective on historical bars. Recalibrate after 90 days of paper trading data accumulates.
4. **H2 discrete approximation**: Hawkes is a continuous-time model; 4H bars underestimate inter-event timing precision.
5. **TDA parameter sensitivity**: d=4 and τ=1 not confirmed via Cao's method (deferred). AMI estimate gives τ_opt=4; production should test τ ∈ {1,2,3,4} and take max L^1.

---

## Next Steps (Wave 2+)

| Wave | Task | Prerequisite |
|------|------|-------------|
| Wave 2 | H1 Hawkes online detector | Live data stream |
| Wave 3 | Tick-level Hawkes + LOB OFI T1 re-run | LOB collector running |
| Wave 3 | TDA1 realtime + τ sweep | Production deployment |
| Post-W3 | Recalibrate all thresholds | 90 days paper data |

---

*Wave 1 complete. All offline calibration deliverables implemented and documented.*
