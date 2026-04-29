# TDA1 Persistence Landscape Calibration

**Date**: 2026-04-29  
**Method**: Takens Embedding → Vietoris-Rips → Persistent Homology (H1) → Landscape L^1 norm  
**Library**: ripser  
**Embedding**: d=4, τ=1 (estimated τ=4)  
**Window**: 50 4H bars (200h)  
**Step**: 5 bars  
**Data**: BTC-USDT 4H log returns  
**Coverage**: 2024-01-01 00:00:00+00:00 → 2026-04-29 04:00:00+00:00  
**N bars**: 5096  

## L^1 Norm Summary Statistics

| Statistic | Value |
|---|---|
| Mean | 0.000030 |
| Std | 0.000076 |
| 90th percentile | 0.000063 |
| 95th percentile (v2.1 threshold) | 0.000097 |
| 97th percentile | 0.000125 |

**v2.1 spec**: Critical entry condition = L^1 norm > 95th percentile + 12h monotone rise.
**Initial threshold** (static): 0.000097  
**Production threshold**: rolling 90-day 95th percentile (recalculated per bar).  

## Cascade Event Validation

| Date | Label | L^1 Norm | 90th %tile | 95th %tile | Above 90% | Above 95% |
|---|---|---|---|---|---|---|
| 2024-04-13 | Cascade | 0.000005 | 0.000054 | 0.000119 | ❌ | ❌ |
| 2024-08-05 | Cascade | 0.000006 | 0.000068 | 0.000072 | ❌ | ❌ |
| 2025-02-03 | Cascade | 0.000016 | 0.000065 | 0.000082 | ❌ | ❌ |
| 2025-04-07 | Cascade | 0.000006 | 0.000086 | 0.000122 | ❌ | ❌ |
| 2024-03-14 | Normal | 0.000011 | 0.000031 | 0.000059 | ❌ | ❌ |
| 2024-10-16 | Normal | 0.000063 | 0.000063 | 0.000069 | ❌ | ❌ |
| 2025-01-15 | Normal | 0.000020 | 0.000065 | 0.000076 | ❌ | ❌ |
| 2025-06-01 | Normal | 0.000006 | 0.000102 | 0.000133 | ❌ | ❌ |

**Cascade events above 95th %tile**: 0/4 (0% sensitivity)
**Control events above 95th %tile (false positive rate)**: 0/4 (0%)

## Initial Threshold Parameters (for paper trading launch)

```python
# TDA1 Critical state condition (v2.1 spec, H2 eq)
# Use rolling 90-day 95th percentile in production
TDA1_THRESHOLD_STATIC = 0.000097  # initial, replace with rolling
TDA1_QUANTILE = 0.95
TDA1_ROLLING_WINDOW_BARS = 540  # 90 days × 6 bars/day

# Embedding parameters
TDA1_D = 4
TDA1_TAU = 1
TDA1_WINDOW = 50  # bars per computation
```

## Computational Performance

| Metric | Value |
|---|---|
| Total windows computed | 1010 |
| Window size | 50 bars (200h) |
| Step size | 5 bars (20h) |

**Production latency estimate**: ~0.5-2s per 4H bar on CPU (100-point cloud, ripser). 
Well within 4H decision window. Monitor in production.

## Rolling Threshold Time Series (sample)

| Date | L^1 Norm | 90th %tile | 95th %tile | 97th %tile |
|---|---|---|---|---|
| 2024-01-09 | 0.000020 | 0.000020 | 0.000020 | 0.000020 |
| 2024-02-05 | 0.000002 | 0.000125 | 0.000165 | 0.000166 |
| 2024-03-04 | 0.000002 | 0.000031 | 0.000127 | 0.000164 |
| 2024-03-31 | 0.000003 | 0.000053 | 0.000127 | 0.000164 |
| 2024-04-28 | 0.000015 | 0.000053 | 0.000072 | 0.000093 |
| 2024-05-25 | 0.000010 | 0.000084 | 0.000139 | 0.000156 |
| 2024-06-22 | 0.000009 | 0.000059 | 0.000122 | 0.000150 |
| 2024-07-19 | 0.000006 | 0.000048 | 0.000118 | 0.000150 |
| 2024-08-16 | 0.000028 | 0.000058 | 0.000070 | 0.000072 |
| 2024-09-12 | 0.000028 | 0.000060 | 0.000069 | 0.000071 |
| 2024-10-10 | 0.000005 | 0.000060 | 0.000069 | 0.000071 |
| 2024-11-06 | 0.000003 | 0.000055 | 0.000059 | 0.000063 |
| 2024-12-04 | 0.000004 | 0.000045 | 0.000061 | 0.000064 |
| 2024-12-31 | 0.000009 | 0.000060 | 0.000063 | 0.000078 |
| 2025-01-28 | 0.000087 | 0.000065 | 0.000082 | 0.000086 |
| 2025-02-24 | 0.000003 | 0.000065 | 0.000085 | 0.000089 |
| 2025-03-24 | 0.000011 | 0.000086 | 0.000122 | 0.000220 |
| 2025-04-20 | 0.000005 | 0.000124 | 0.000138 | 0.000220 |
| 2025-05-18 | 0.000003 | 0.000124 | 0.000138 | 0.000220 |
| 2025-06-14 | 0.000007 | 0.000052 | 0.000124 | 0.000126 |
| 2025-07-12 | 0.000003 | 0.000027 | 0.000045 | 0.000083 |
| 2025-08-08 | 0.000029 | 0.000023 | 0.000033 | 0.000045 |
| 2025-09-05 | 0.000011 | 0.000031 | 0.000045 | 0.000046 |
| 2025-10-02 | 0.000045 | 0.000028 | 0.000035 | 0.000050 |
| 2025-10-30 | 0.000027 | 0.000043 | 0.000049 | 0.000073 |
| 2025-11-26 | 0.000039 | 0.000046 | 0.000049 | 0.000052 |
| 2025-12-24 | 0.000004 | 0.000057 | 0.000084 | 0.000097 |
| 2026-01-20 | 0.000103 | 0.000059 | 0.000095 | 0.000102 |
| 2026-02-17 | 0.000009 | 0.000098 | 0.000280 | 0.000861 |
| 2026-03-16 | 0.000003 | 0.000074 | 0.000227 | 0.000861 |
| 2026-04-13 | 0.000005 | 0.000101 | 0.000227 | 0.000861 |

## sel v2.0 Design Validation

❌ **Low Cascade sensitivity**: 0/4 events detected. TDA1 may not be reliable with current parameters. Recommended: reduce threshold to 90th percentile and re-evaluate. Or wait for more live paper data before relying on this condition.

✅ **False positive rate**: 0/4 control events (0%) exceeded threshold. Acceptable specificity.

**Production recommendation per v2.1 §2.1**: TDA L^1 > 95th %tile is ONE of two new main conditions. Combined logic requires either (σ-based full + one of TDA/Hawkes) OR (σ-based partial + both TDA and Hawkes). Single-tool false alarms are filtered.

## Limitations

- **Cascade sample**: ~4 events. Sample size far too small for robust statistical validation.
  (This is an expected limitation per v2.0 §18.3.)
- **Temporal alignment**: TDA window ends at bar close; events are detected at bar open.
  True lead time may differ from what appears here.
- **Post-ETF validity**: Literature mostly uses 2018 data. BTC structure changed in 2024+.
- **τ estimation**: AMI tau estimation on 4H bars is approximate.
  Production should use persistent homology across τ ∈ {1,2,3} and take max.
- **Dimension d=4**: Cao's method should be applied to confirm d. Not computed here.

---
*Generated by sel_v2/offline/tda_calibration.py — Wave 1 deliverable*