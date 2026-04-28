# P1.5 None-State Breakdown — Before vs After StateNoneReason

Date: 2026-04-28  
Commits: 3728fd1 (schema), c07999b (recognizer), cb540dc (transition), 5e2b2b0 (health)

## Background

Before Task 1.7, `compute_state_distribution()` and `HealthMonitor.generate()` conflated two
distinct None causes into a single `cold_start_bars` bucket:

- `cold_start=True` bars (first WINDOW=720 bars — quantile windows not yet filled)
- `cold_start=False, state=None` bars (post-warmup, no condition matched — could be WIKI data absent OR genuinely no match)

After Task 1.7, three causes are tracked independently via `StateNoneReason`:

| Reason | Meaning |
|--------|---------|
| `COLD_START` | First 720 bars — quantile windows not yet filled |
| `MISSING_DATA` | ≥1 WIKI_REQUIRED feature absent (H, TF, OI); condition short-circuited |
| `NO_MATCH` | All data present; no condition was satisfied (genuine no-match) |
| `NOT_APPLICABLE` | State is not None — reason field not relevant |

---

## Scenario A: price + LV only (no H/TF/OI collectors)

Synthetic run: 1008 total bars (720 warmup + 288 post-warmup), flat price, no WIKI features.

| Metric | Before (P1.0) | After (P1.5) |
|--------|---------------|--------------|
| `cold_start_bars` | 1008 (all bars) | 720 |
| `missing_data_bars` | — | 288 |
| `no_match_bars` | — | 0 |
| `active_bars` | 0 | 0 |

**Before**: All 288 post-warmup bars lumped into `cold_start_bars` because `r.cold_start OR r.state is None` was True (state=None, cold_start=False → still counted).  
**After**: 720 true cold-start bars + 288 MISSING_DATA bars (H_24h_mean, abs_tf_24h_sum, oi_change_rate_24h all None → `_none_reason_for_no_match` returns MISSING_DATA).

---

## Scenario D: full WIKI data (H + TF + OI + OI_hurst, sinusoidal)

Synthetic run: 1008 total bars (720 warmup + 288 post-warmup), sinusoidal patterns, all WIKI features present.

| Metric | Before (P1.0) | After (P1.5) |
|--------|---------------|--------------|
| `cold_start_bars` | 1002 (720 true + 282 no-match) | 720 |
| `missing_data_bars` | — | 0 |
| `no_match_bars` | — | 282 |
| `active_bars` | 6 | 6 |
| `state_rates["Drifting_Calm"]` | 1.0 (6/6) | 1.0 (6/6) |

**Before**: 282 post-warmup no-match bars counted as `cold_start_bars`, inflating that metric by 39%.  
**After**: Correctly split — 720 true cold-start + 282 NO_MATCH + 6 active (all Drifting_Calm). `state_rates` denominator (`active_bars=6`) unchanged.

---

## DEGRADED warning threshold

`HealthMonitor.generate()` now emits `[DEGRADED: collector_data_missing N%]` if:
```
missing_data_bars / total_bars > 0.10
```

In Scenario A (288/1008 = 28.6% missing): warning fires immediately.  
In Scenario D (0/1008 = 0%): no warning.

---

## rule_id encoding in DecisionEngine

`DecisionEngine.decide()` now accepts `none_reason: Optional[str]` and encodes it in `rule_id`:

| Cause | Old rule_id | New rule_id |
|-------|-------------|-------------|
| cold_start (no none_reason passed) | `cold_start` | `cold_start` |
| COLD_START | `cold_start` | `none:cold_start` |
| MISSING_DATA | `cold_start` | `none:missing_data` |
| NO_MATCH | `cold_start` | `none:no_match` |

Backward compatibility: callers that pass no `none_reason` get `rule_id="cold_start"` unchanged.

---

## DecisionTrail.state_none_reason

`DecisionTrail` now has `state_none_reason: Optional[str]` (default `None`).  
Set from `state_output.none_reason` in `DecisionTrailBuilder.build()`.

- Active state bar: `state_none_reason=None`
- COLD_START bar: `state_none_reason="cold_start"`
- MISSING_DATA bar: `state_none_reason="missing_data"`
- NO_MATCH bar: `state_none_reason="no_match"`

This field is the foundation for Task 1.8 signal-lag tracking: only MISSING_DATA and NO_MATCH
bars (not COLD_START) are relevant to `_last_state_time` expiry logic.
