# sel State Inspection Report

**Symbol:** BTCUSDT  
**Period:** last 12 days (since 2026-04-16 01:34 UTC)  
**State source:** in-memory StateEngine (sel_state_sequence was empty)  
**Generated:** 2026-04-28 01:34 UTC  
**Active bars (non-cold-start):** 287

> Pure read-only diagnostic. No state logic modified.
> Thresholds are PLACEHOLDER — calibrate with v1.0.md when available.

---

## Summary

| State | Runs | Bars | Rate | Avg run (bars) | Avg run (hours) |
|---|---|---|---|---|---|
| Cascade | 0 | 0 | 0.0% | 0.0 | 0.0h |
| Critical | 2 | 33 | 11.5% | 16.5 | 16.5h |
| Coiling | 0 | 0 | 0.0% | 0.0 | 0.0h |
| Surging_Up | 0 | 0 | 0.0% | 0.0 | 0.0h |
| Surging_Down | 0 | 0 | 0.0% | 0.0 | 0.0h |
| Drifting_Charged | 0 | 0 | 0.0% | 0.0 | 0.0h |
| Drifting_Calm | 3 | 254 | 88.5% | 84.7 | 84.7h |

---

## State: Cascade

- **Runs:** 0
- **Total bars:** 0
- **Rate:** 0.00% of active bars
- **Avg run duration:** 0.0 bars (0.0h)

_No instances of this state in the inspection window._

---

## State: Critical

- **Runs:** 2
- **Total bars:** 33
- **Rate:** 11.50% of active bars
- **Avg run duration:** 16.5 bars (16.5h)

Showing 2 of 2 runs (evenly spaced for temporal coverage).

### Run 1 of 2: 2026-04-16 21:00 UTC → 2026-04-17 08:00 UTC (12 bars)

**Entered from:** Drifting_Calm  
**Trigger reason (first bar):** `CRITICAL:sigma_p_d2@0.83+autocorr_24h@0.08`  

**Close sparkline** (12 bars):  
`▁ ▃▅▃▂▂▃▆█▅▄`
(`51,928` → `52,305` USDT range)

**OHLC chart:** see [`figures/Critical_run01.txt`](figures/Critical_run01.txt)

**Feature snapshot** (start / mid / end / mean / min / max over run):  

| Feature | Start | Mid | End | Mean | Min | Max |
|---|---|---|---|---|---|---|
| close (USDT) | 51,975 | 52,001 | 52,133 | 52,092 | 51,928 | 52,305 |
| ΔP/P (%) | -0.085474 | -0.056424 | -0.042044 | 0.018468 | -0.286872 | 0.273093 |
| σ(P)_24H | 0.001685 | 0.001703 | 0.001846 | 0.001717 | 0.001523 | 0.001846 |
| funding_rate | — | — | — | — | — | — |
| H (entropy) | — | — | — | — | — | — |
| TF (taker flow) | — | — | — | — | — | — |
| OI | — | — | — | — | — | — |
| LV | — | — | — | — | — | — |
| absorption_ratio | — | — | — | — | — | — |
| autocorr_12h | -0.528200 | 0.040380 | 0.126444 | -0.097605 | -0.528200 | 0.281223 |
| autocorr_24h | -0.316186 | -0.251537 | -0.147054 | -0.227912 | -0.321682 | -0.098633 |
| autocorr_48h | -0.055678 | -0.121700 | -0.126818 | -0.097922 | -0.141503 | -0.055678 |
| σ(P)_d2 | 0.000094 | -0.000089 | 0.000088 | 0.000018 | -0.000089 | 0.000288 |
| OI_hurst | — | — | — | — | — | — |

### Run 2 of 2: 2026-04-26 20:00 UTC → 2026-04-27 16:00 UTC (21 bars)

**Entered from:** Drifting_Calm  
**Trigger reason (first bar):** `CRITICAL:sigma_p_d2@0.84+autocorr_24h@0.17`  

**Close sparkline** (21 bars):  
`▇▇█▆▄▆▅▆▇▅▆▆▅▄▂▃▄▄▃▁ `
(`48,483` → `49,029` USDT range)

**OHLC chart:** see [`figures/Critical_run02.txt`](figures/Critical_run02.txt)

**Feature snapshot** (start / mid / end / mean / min / max over run):  

| Feature | Start | Mid | End | Mean | Min | Max |
|---|---|---|---|---|---|---|
| close (USDT) | 48,995 | 48,861 | 48,483 | 48,809 | 48,483 | 49,029 |
| ΔP/P (%) | 0.115464 | 0.008363 | -0.135238 | -0.044401 | -0.303921 | 0.236755 |
| σ(P)_24H | 0.002318 | 0.002075 | 0.001776 | 0.002020 | 0.001661 | 0.002318 |
| funding_rate | — | — | — | — | — | — |
| H (entropy) | — | — | — | — | — | — |
| TF (taker flow) | — | — | — | — | — | — |
| OI | — | — | — | — | — | — |
| LV | — | — | — | — | — | — |
| absorption_ratio | — | — | — | — | — | — |
| autocorr_12h | -0.167419 | -0.107459 | 0.086265 | -0.048708 | -0.395872 | 0.282243 |
| autocorr_24h | -0.170806 | -0.202622 | 0.048394 | -0.114105 | -0.211618 | 0.048394 |
| autocorr_48h | 0.078963 | 0.051659 | 0.068014 | 0.070053 | 0.044983 | 0.111807 |
| σ(P)_d2 | 0.000096 | -0.000004 | 0.000098 | 0.000008 | -0.000233 | 0.000281 |
| OI_hurst | — | — | — | — | — | — |

---

## State: Coiling

- **Runs:** 0
- **Total bars:** 0
- **Rate:** 0.00% of active bars
- **Avg run duration:** 0.0 bars (0.0h)

_No instances of this state in the inspection window._

---

## State: Surging_Up

- **Runs:** 0
- **Total bars:** 0
- **Rate:** 0.00% of active bars
- **Avg run duration:** 0.0 bars (0.0h)

_No instances of this state in the inspection window._

---

## State: Surging_Down

- **Runs:** 0
- **Total bars:** 0
- **Rate:** 0.00% of active bars
- **Avg run duration:** 0.0 bars (0.0h)

_No instances of this state in the inspection window._

---

## State: Drifting_Charged

- **Runs:** 0
- **Total bars:** 0
- **Rate:** 0.00% of active bars
- **Avg run duration:** 0.0 bars (0.0h)

_No instances of this state in the inspection window._

---

## State: Drifting_Calm

- **Runs:** 3
- **Total bars:** 254
- **Rate:** 88.50% of active bars
- **Avg run duration:** 84.7 bars (84.7h)

Showing 3 of 3 runs (evenly spaced for temporal coverage).

### Run 1 of 3: 2026-04-16 02:00 UTC → 2026-04-16 20:00 UTC (19 bars)

**Entered from:** Drifting_Calm  
**Trigger reason (first bar):** `DWELL_HELD:Drifting_Calm(waiting 3/12 for Drifting_Calm)`  

**Close sparkline** (19 bars):  
`█▇▅▄▆▅▅▅▆▂▄▆▃▄▃▃▃  `
(`52,007` → `52,429` USDT range)

**OHLC chart:** see [`figures/Drifting_Calm_run01.txt`](figures/Drifting_Calm_run01.txt)

**Feature snapshot** (start / mid / end / mean / min / max over run):  

| Feature | Start | Mid | End | Mean | Min | Max |
|---|---|---|---|---|---|---|
| close (USDT) | 52,429 | 52,116 | 52,019 | 52,226 | 52,007 | 52,429 |
| ΔP/P (%) | -0.264472 | -0.381205 | 0.023920 | -0.055126 | -0.381205 | 0.212133 |
| σ(P)_24H | 0.001865 | 0.001910 | 0.001784 | 0.001890 | 0.001784 | 0.001980 |
| funding_rate | — | — | — | — | — | — |
| H (entropy) | — | — | — | — | — | — |
| TF (taker flow) | — | — | — | — | — | — |
| OI | — | — | — | — | — | — |
| LV | — | — | — | — | — | — |
| absorption_ratio | — | — | — | — | — | — |
| autocorr_12h | -0.085226 | -0.109404 | -0.506988 | -0.247004 | -0.537911 | 0.138163 |
| autocorr_24h | -0.070867 | 0.027985 | -0.409240 | -0.136466 | -0.435896 | 0.027985 |
| autocorr_48h | -0.041330 | -0.014991 | -0.054977 | -0.022367 | -0.065212 | 0.013312 |
| σ(P)_d2 | -0.000058 | -0.000012 | -0.000103 | -0.000004 | -0.000141 | 0.000163 |
| OI_hurst | — | — | — | — | — | — |

### Run 2 of 3: 2026-04-17 09:00 UTC → 2026-04-26 19:00 UTC (227 bars)

**Entered from:** Critical  
**Trigger reason (first bar):** `DRIFTING_CALM:sigma_p@0.38`  

**Close sparkline** (227 bars):  
`▇▇▇██▇▇▇▆▆▆▇▇▆▅▅▅▄▄▃▃▃▃▂▂▃▃▃▃▄▃▄▃▃▃▃▂▁▁▁ ▁ ▂▂▁▁ `
(`48,748` → `52,316` USDT range)

**OHLC chart:** see [`figures/Drifting_Calm_run02.txt`](figures/Drifting_Calm_run02.txt)

**Feature snapshot** (start / mid / end / mean / min / max over run):  

| Feature | Start | Mid | End | Mean | Min | Max |
|---|---|---|---|---|---|---|
| close (USDT) | 52,067 | 49,862 | 48,938 | 50,395 | 48,748 | 52,316 |
| ΔP/P (%) | -0.128100 | -0.205125 | 0.285444 | -0.027646 | -0.703260 | 0.585899 |
| σ(P)_24H | 0.001861 | 0.001827 | 0.002383 | 0.002018 | 0.001444 | 0.002492 |
| funding_rate | — | — | — | — | — | — |
| H (entropy) | — | — | — | — | — | — |
| TF (taker flow) | — | — | — | — | — | — |
| OI | — | — | — | — | — | — |
| LV | — | — | — | — | — | — |
| absorption_ratio | — | — | — | — | — | — |
| autocorr_12h | 0.119526 | 0.244736 | -0.264793 | -0.002046 | -0.610984 | 0.722865 |
| autocorr_24h | -0.140511 | 0.061193 | -0.117024 | 0.074561 | -0.291631 | 0.339791 |
| autocorr_48h | -0.110543 | 0.077935 | 0.058678 | 0.087813 | -0.202774 | 0.264782 |
| σ(P)_d2 | -0.000094 | -0.000077 | -0.000060 | -0.000001 | -0.000540 | 0.000599 |
| OI_hurst | — | — | — | — | — | — |

### Run 3 of 3: 2026-04-27 17:00 UTC → 2026-04-28 00:00 UTC (8 bars)

**Entered from:** Critical  
**Trigger reason (first bar):** `DRIFTING_CALM:sigma_p@0.16`  

**Close sparkline** (8 bars):  
` ▁▄▇█▆▆▄`
(`48,568` → `48,926` USDT range)

**OHLC chart:** see [`figures/Drifting_Calm_run03.txt`](figures/Drifting_Calm_run03.txt)

**Feature snapshot** (start / mid / end / mean / min / max over run):  

| Feature | Start | Mid | End | Mean | Min | Max |
|---|---|---|---|---|---|---|
| close (USDT) | 48,568 | 48,926 | 48,741 | 48,767 | 48,568 | 48,926 |
| ΔP/P (%) | 0.176413 | 0.133716 | -0.156833 | 0.066622 | -0.176807 | 0.277117 |
| σ(P)_24H | 0.001766 | 0.001826 | 0.001729 | 0.001785 | 0.001729 | 0.001851 |
| funding_rate | — | — | — | — | — | — |
| H (entropy) | — | — | — | — | — | — |
| TF (taker flow) | — | — | — | — | — | — |
| OI | — | — | — | — | — | — |
| LV | — | — | — | — | — | — |
| absorption_ratio | — | — | — | — | — | — |
| autocorr_12h | 0.056990 | 0.427894 | 0.500385 | 0.348760 | 0.056990 | 0.500385 |
| autocorr_24h | 0.057674 | 0.163592 | 0.200935 | 0.110839 | 0.041753 | 0.200935 |
| autocorr_48h | 0.018595 | -0.033950 | -0.004091 | -0.022779 | -0.043568 | 0.018595 |
| σ(P)_d2 | -0.000087 | 0.000045 | -0.000100 | -0.000022 | -0.000100 | 0.000045 |
| OI_hurst | — | — | — | — | — | — |

---
