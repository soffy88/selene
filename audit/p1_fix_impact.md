# P1 Fix Impact — State Distribution Comparison

Date: 2026-04-28  
Commits: 982d2ad (Coiling), b53aefe (Surging), 6873a83 (Drifting fixes), c92cb37 (recognizer fallback)

## What changed (P1 vs P0 conditions)

| State | P0 (proxy) | P1 (v1.0-aligned) |
|-------|-----------|-------------------|
| Coiling Cond3/4 | `price_autocorr_24h > 60th` / `OI ≥ 50th` | `oi_change_rate_24h > 70th` / `tf_dp_ratio_24h > 80th` |
| Surging Cond1 | `abs_delta_p_pct > 70th` | `price_slope_6h > 80th` |
| Surging Cond2 | `sigma_p_24h > 60th` | `\|tf_directional_ratio_6h\| > 70%` (hard short-circuit if TF absent) |
| Surging Cond3 | `price_autocorr_12h > 60th` | `sigma_rising_12h AND sigma_change_rate_std_6h < 50th` |
| Surging direction | `sign(delta_p_pct)` | `sign(tf_directional_ratio_6h)` |
| Drifting-Calm | single gate `sigma ≤ 50th` + **catch-all** | 4-AND: σ∈[30,60th], H_24h_mean∈[40,80th], abs_tf<50th, \|oi_cr\|<50th |
| Drifting-Charged | `sigma ≤ 50th AND (OI>70th OR abs_fr>60th)` | 4-AND: σ∈[40,70th], H_24h_mean<50th, abs_tf∈[30,70th], OI_hurst>0.6 |
| Fallback | `state=DRIFTING_CALM` (catch-all) | `state=None, reason=NO_STATE_MATCHED` |

## Synthetic 12-day distribution (288 post-warmup bars)

All runs use 720-bar warmup to fill quantile windows, then 288 bars (12 days × 24h).

### Scenario A: price + LV only (no H/TF/OI collectors)

```
post-warmup bars   : 288
true cold_start    : 0
NO_STATE_MATCHED   : 288  (100.0%)
state matched      : 0    (0.0%)
```

**P0 expected**: ~50% Drifting-Calm (sigma catch-all fires for all low-vol bars).  
**P1 result**: 100% NO_STATE — correct per §10.1 principle 3. Without WIKI collectors
no 4-AND condition has sufficient data to verify all gates.

### Scenario B: price + LV + OI (no H/TF collectors)

```
post-warmup bars   : 288
true cold_start    : 0
NO_STATE_MATCHED   : 288  (100.0%)
state matched      : 0    (0.0%)
```

OI alone does not unlock any state in P1. All states requiring H_24h_mean or
abs_tf_24h_sum (Drifting-Calm/Charged/Coiling) short-circuit immediately. Only
Critical Cond4 (OI_hurst) and Coiling Cond3 (oi_change_rate_24h) benefit from
OI, but they still need other conditions to pass first.

### Scenario C: price + LV, Cascade injected every 48h

```
post-warmup bars   : 288
true cold_start    : 0
NO_STATE_MATCHED   : 282  (97.9%)
state matched      : 6    (2.1%)
  Cascade          :    6  (2.1%)
```

Cascade is the only state that fires without WIKI collectors (primary gate:
abs_delta_p_pct > 97th; secondary: LV > 95th). The 6 injected bars (delta_p=40,
LV=1.0) successfully triggered Cascade. Remaining 282 bars correctly return
NO_STATE (no WIKI data to satisfy other states).

### Scenario D: full WIKI data (H + TF + OI + OI_hurst, sinusoidal)

```
post-warmup bars   : 288
true cold_start    : 0
NO_STATE_MATCHED   : 282  (97.9%)
state matched      : 6    (2.1%)
  Drifting_Calm    :    6  (2.1%)
```

**P0 expected with catch-all**: ~50%+ DRIFTING_CALM (sigma single-gate fires whenever σ ≤ 50th).  
**P1 result**: 2.1% Drifting-Calm — only fires when all 4 conditions simultaneously
fall in their respective bands. Sinusoidal data rarely satisfies all 4-AND gates
at once. This is correct: Drifting-Calm should be rare (genuine equilibrium is rare).

## Key takeaways

1. **Before WIKI collectors are live**: All post-warmup bars return `state=None`
   (reason="NO_STATE_MATCHED"). Paper trading engine maps this to NO_ACTION per
   existing `if current_state is None: return NO_ACTION` logic in engine.py.

2. **Catch-all removed**: P0's Drifting-Calm catch-all produced false-positive states
   whenever σ was low, regardless of whether entropy, flow, or OI data supported it.
   P1 eliminates this — silence is the correct response when data is insufficient.

3. **Cascade works without WIKI data**: Cascade fires on price velocity + LV alone,
   which are always available from OHLCV + orderbook data. This is the intended
   behavior: extreme market events are detectable without full collector warmup.

4. **State frequency will be low initially**: Once all collectors are live, expect
   most states to have hit rates in the low single-digit percentages. High-frequency
   state firing would indicate threshold miscalibration. Thresholds are PLACEHOLDER
   per v1.0.md — empirical calibration is pending.

5. **No regression on test suite**: 78/78 tests pass after P1 fixes.
