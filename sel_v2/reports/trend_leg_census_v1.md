# Trend Leg Census v1 (Wave V22-C)

Pure OHLC census — no strategy parameters, no entry/exit gates, no P&L. Three coarse zigzag thresholds run independently, all three reported (none picked after seeing results). Fine push-count layer uses the same 1.5x ATR(14) as `sel_v2.offline.substate`.

## Threshold config: 3xATR

### 1. Leg counts

- Total legs: **139** (up: 69, down: 70)
- Unconfirmed tail bars excluded from the census: 63

| year bucket | legs |
|---|---:|
| 2024H2 | 32 |
| 2025 | 74 |
| 2026H1 | 33 |
| 2026H2(partial) | 0 |

### 2. Duration / push / displacement distribution

- Duration (days): p10=1.67 p50=4.17 p90=10.87 n=139
- Push count: p10=0.00 p50=0.00 p90=2.00 n=139
- Net displacement (%): p10=3.46 p50=7.64 p90=15.40 n=139
- Max adverse excursion (%): p10=0.56 p50=1.66 p90=3.88 n=139

### 3. Legs matching the Wiki spec (duration 10-35d, push 3-6)

- **8 / 139 = 5.8%**

### 4. Parent-state-machine overlap (>= 50.0% Surging bars = captured)

- Captured: **43** / Missed: **96** (of 139 total legs)

### 5. Missed-leg profile

- Bars in missed legs, by what the annotation actually called them:

| state | bars | share |
|---|---:|---:|
| Drifting_Calm | 3090 | 96.8% |
| Surging | 101 | 3.2% |

- Missed vs captured legs (median):

| | duration (d) | net displacement (%) | push count |
|---|---:|---:|---:|
| missed | 4.42 | 7.44 | 0.0 |
| captured | 4.00 | 7.87 | 0.0 |

## Threshold config: 5xATR

### 1. Leg counts

- Total legs: **61** (up: 30, down: 31)
- Unconfirmed tail bars excluded from the census: 63

| year bucket | legs |
|---|---:|
| 2024H2 | 14 |
| 2025 | 34 |
| 2026H1 | 13 |
| 2026H2(partial) | 0 |

### 2. Duration / push / displacement distribution

- Duration (days): p10=3.17 p50=9.83 p90=22.17 n=61
- Push count: p10=0.00 p50=2.00 p90=5.00 n=61
- Net displacement (%): p10=6.06 p50=11.85 p90=25.02 n=61
- Max adverse excursion (%): p10=1.14 p50=3.86 p90=7.05 n=61

### 3. Legs matching the Wiki spec (duration 10-35d, push 3-6)

- **18 / 61 = 29.5%**

### 4. Parent-state-machine overlap (>= 50.0% Surging bars = captured)

- Captured: **19** / Missed: **42** (of 61 total legs)

### 5. Missed-leg profile

- Bars in missed legs, by what the annotation actually called them:

| state | bars | share |
|---|---:|---:|
| Drifting_Calm | 2901 | 95.2% |
| Surging | 147 | 4.8% |

- Missed vs captured legs (median):

| | duration (d) | net displacement (%) | push count |
|---|---:|---:|---:|
| missed | 8.50 | 11.48 | 2.0 |
| captured | 10.00 | 12.18 | 2.0 |

## Threshold config: 8pct

### 1. Leg counts

- Total legs: **49** (up: 24, down: 25)
- Unconfirmed tail bars excluded from the census: 63

| year bucket | legs |
|---|---:|
| 2024H2 | 10 |
| 2025 | 28 |
| 2026H1 | 11 |
| 2026H2(partial) | 0 |

### 2. Duration / push / displacement distribution

- Duration (days): p10=2.80 p50=12.67 p90=27.67 n=49
- Push count: p10=0.00 p50=2.00 p90=6.00 n=49
- Net displacement (%): p10=9.38 p50=13.22 p90=27.12 n=49
- Max adverse excursion (%): p10=1.11 p50=4.16 p90=7.21 n=49

### 3. Legs matching the Wiki spec (duration 10-35d, push 3-6)

- **17 / 49 = 34.7%**

### 4. Parent-state-machine overlap (>= 50.0% Surging bars = captured)

- Captured: **16** / Missed: **33** (of 49 total legs)

### 5. Missed-leg profile

- Bars in missed legs, by what the annotation actually called them:

| state | bars | share |
|---|---:|---:|
| Drifting_Calm | 3046 | 86.9% |
| Surging | 458 | 13.1% |

- Missed vs captured legs (median):

| | duration (d) | net displacement (%) | push count |
|---|---:|---:|---:|
| missed | 13.50 | 15.13 | 3.0 |
| captured | 8.33 | 12.07 | 1.0 |

## Verdict table (raw, no conclusion drawn)

| config | Wiki-spec legs | of which captured (>=50% Surging) |
|---|---:|---:|
| 3xATR | 8 | 2 |
| 5xATR | 18 | 7 |
| 8pct | 17 | 5 |

Regenerate: `python -m sel_v2.offline.leg_census`.
