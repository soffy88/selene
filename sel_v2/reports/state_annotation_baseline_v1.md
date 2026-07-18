# State Annotation Baseline v1 (Wave S2C Part 4)

Offline replay of the live 4H state machine (BarRunner + frozen conditions.py) over the full v2_bars_4h history. **4429 bars**, symbol BTC-USDT. Written to `v2_state_annotation` (NOT `v2_state_history`).

## State distribution

| state | bars | share |
|---|---:|---:|
| Drifting_Calm | 3105 | 70.1% |
| Surging | 1265 | 28.6% |
| Drifting_Charged | 43 | 1.0% |
| Critical | 16 | 0.4% |

## Dwell duration (consecutive bars in state)

| state | runs | p50 | p90 | max |
|---|---:|---:|---:|---:|
| Drifting_Calm | 11 | 195 | 569 | 756 |
| Surging | 13 | 67 | 197 | 273 |
| Drifting_Charged | 1 | 43 | 43 | 43 |
| Critical | 2 | 8 | 8 | 9 |

## Feature completeness

- Degraded bars (cold_start or a None-reason forced a fallback): **3616 / 4429 = 81.6%**
- Dominant state: **Drifting_Calm** at **70.1%** (death-mode STOP threshold = 95.0%).

Degraded bars are historical bars predating the tick/LOB/liquidation feeds, so the OI/funding/entropy/LOB-gated conditions ran on a reduced feature set. This is expected and is exactly what the annotation makes explicit.

---

## Notes for the Wiki (not machine-generated)

- **Not a death-mode signal.** The dominant state (Drifting_Calm 70.1%) is well under the 95% v1.0 death-mode threshold, and all four reachable states appear with sane dwell profiles (calm runs long — p50 195 bars; Surging p50 67; Critical is rare and short, max 9 bars). This is the healthy multi-state distribution the state machine is supposed to produce over a 2-year history.
- **This differs from the *live* `v2_state_history`** (which since 2026-06-15 shows only Surging → Drifting_Charged). That is expected: the live history is what the running engine recorded bar-by-bar with the feeds available at each moment, whereas this annotation is a single full-precompute pass with all currently-available series — so it is the "what the frozen state machine says about all of history" baseline, not a record of live operation.
- **81.6% degraded** simply reflects that most of the 2-year history predates the OI/funding/entropy/LOB/tick feeds (which only流 stably since 2026-07). The `features_missing` array per bar in `v2_state_annotation` names exactly which inputs were absent, so a reader can filter to the feature-complete tail when that matters.

Regenerate: `python -m sel_v2.offline.state_annotator` (writes `v2_state_annotation`, prints this report; the file itself is written when run outside the read-only container mount).
