# Selene Weekly Report — 2026-W17

> Decision rules version: `a1b2c3d4e5f6a7b8` (Claude default: True)
> Generated: 2026-04-28T01:01:49.401982+00:00
> ⚠️ All figures are paper trading (simulated). Not real capital.

## 1. State Distribution

| State | Count | Rate | Expected Range | Status |
|---|---|---|---|---|
| Coiling | 42 | 25.0% | [10%, 25%] | ✅ OK |
| Surging_Up | 28 | 16.7% | [5%, 15%] | ⚠️ ABOVE |
| Surging_Down | 14 | 8.3% | [5%, 15%] | ✅ OK |
| Drifting_Calm | 42 | 25.0% | [20%, 45%] | ✅ OK |
| Drifting_Charged | 14 | 8.3% | [5%, 20%] | ✅ OK |
| Critical | 14 | 8.3% | [2%, 10%] | ✅ OK |
| Cascade | 14 | 8.3% | [0%, 3%] | ⚠️ ABOVE |

Health warnings: Surging_Up rate 0.1667 above expected [0.05, 0.15]; Cascade rate 0.0833 above expected [0.001, 0.03]

## 2. State Transition Quality

Legal transition rate: 87.3%

Illegal transition types (top 5):
| Transition | Count |
|---|---|
| Cascade->Surging_Up | 2 |

## 3. Decision Distribution

| Action | Count |
|---|---|
| open_long | 7 |
| open_short | 0 |
| close | 44 |
| hold | 48 |
| no_action | 69 |

## 4. Paper PnL

Week PnL: $-434.20 (-4.34%)  
Cumulative PnL: $-434.20  
Max drawdown this week: 0.0%  
Starting NAV: $10,000.00  
Ending NAV: $10,141.95  

## 5. Risk Events

| Rule | Count |
|---|---|
| max_loss_per_trade | 1 |

Misfire rate (opened, force-closed by risk within 24H): 14.3% (1/7 opens)

## 6. State-Decision Alignment

Verifies decisions matched the configured YAML rules:
| State | Expected Action | Actual Action | Match Rate |
|---|---|---|---|
| Cascade | close | close | 100% |
| Coiling | no_action | no_action | 100% |
| Critical | hold | hold | 100% |
| Drifting_Calm | close | close | 100% |
| Drifting_Calm | hold | hold | 100% |
| Drifting_Charged | hold | hold | 100% |
| Surging_Down | no_action | no_action | 100% |
| Surging_Up | close | close | 100% |
| Surging_Up | hold | hold | 100% |
| Surging_Up | no_action | no_action | 100% |
| Surging_Up | open_long | open_long | 100% |

## 7. Price Outcomes After State (sel hypothesis test)

| State | N bars | Next 24H return (mean) | Next 24H return (median) | Positive rate |
|---|---|---|---|---|
| Coiling | 36 | +0.4% | +0.4% | 100% |
| Surging_Up | 24 | +0.4% | +0.4% | 100% |
| Surging_Down | 12 | +0.4% | +0.4% | 100% |
| Drifting_Calm | 36 | +0.4% | +0.4% | 100% |
| Drifting_Charged | 12 | +0.4% | +0.4% | 100% |
| Critical | 12 | +0.4% | +0.4% | 100% |
| Cascade | 12 | +0.4% | +0.4% | 100% |

> Note: some states may have N < 30 — treat those rows with caution.
> These numbers should NOT be used for parameter calibration.
> Reporting only for self-falsification tracking.

## 8. Failure Cases — Top 5 Losses

| # | Open Time | State | Action | Close Time | PnL | Reason Closed |
|---|---|---|---|---|---|---|
| 1 | 2026-04-21 05:00 | Drifting_Calm | close | 2026-04-21 05:00 | -$300.00 | max_loss_per_trade |
| 2 | 2026-04-21 15:00 | Surging_Up | close | 2026-04-21 15:00 | -$200.00 | normal |
| 3 | 2026-04-21 07:00 | Cascade | close | 2026-04-21 07:00 | -$42.10 | normal |
| 4 | 2026-04-21 19:00 | Cascade | close | 2026-04-21 19:00 | -$42.10 | normal |
| 5 | 2026-04-22 07:00 | Cascade | close | 2026-04-22 07:00 | -$42.10 | normal |

## 9. sel Hypothesis Health

⚠️ WATCH: Surging_Up rate (16.7%) above expected ceiling (15.0%). May indicate Surging_Up conditions are too lenient.
⚠️ WATCH: Cascade rate (8.3%) above expected ceiling (3.0%). May indicate Cascade conditions are too lenient.
✅ PASS: Legal transition rate (87.3%) within acceptable range (>80%).
✅ PASS: Misfire rate (14%) within acceptable range.

