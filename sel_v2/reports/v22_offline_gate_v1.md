# v2.2 Offline Gate Verdict v1 (Wave V22-A)

Offline replay over 4429 aligned BTC-USDT 4H bars (`v2_bars_4h` joined to `v2_state_annotation` by timestamp). Branch B pyramid simulator (D1/D2/D4/D5), parameters frozen per the Wiki ruling — not retuned on these results.

## Gate verdicts

| gate | metric | threshold | value | pass |
|---|---|---|---|---|
| H-V22-1 | first-leg trigger count | >= 20 | 9 | FAIL |
| H-V22-2 | per-leg net expectancy (all batches, w/ costs) | > 0 | -0.00708 | FAIL |
| H-V22-3 | STRUCTURE_BREAK false-positive rate | < 40.0% | 0.0% (0/4) | PASS |

## Exit reason distribution (per leg, final exit)

| reason | legs |
|---|---:|
| stop_break | 4 |
| left_surging | 3 |
| drawdown | 2 |

- Legs that hit a terminal 50% cut before their final exit: **3 / 9**

## Per-leg net P&L distribution (quota-fraction units, all batches merged)

- p10=-0.0288 p50=-0.0112 p90=0.0186 worst=-0.0398 best=0.0349 n=9

## Long vs short

- Long legs:  p10=0.0145 p50=0.0145 p90=0.0145 worst=0.0145 best=0.0145 n=1
- Short legs: p10=-0.0302 p50=-0.0115 p90=0.0099 worst=-0.0398 best=0.0349 n=8

## By year (regime robustness — not a re-tuning basis)

| bucket | legs | net P&L dist |
|---|---:|---|
| 2024H2 | 1 | p10=0.0145 p50=0.0145 p90=0.0145 worst=0.0145 best=0.0145 n=1 |
| 2025 | 6 | p10=-0.0189 p50=-0.0076 p90=0.0171 worst=-0.0261 best=0.0349 n=6 |
| 2026H1 | 2 | p10=-0.0377 p50=-0.0297 p90=-0.0216 worst=-0.0398 best=-0.0196 n=2 |
| 2026H2(partial) | 0 | n/a (no legs) |

## Terminal flag: lead time to leg end + its own false-positive rate

- Lead (bars from terminal_flag latch to the leg's final exit): p10=41.2000 p50=74.0000 p90=130.0000 worst=33.0000 best=144.0000 n=3
- False-positive rate (price makes a new extreme within 10 bars after the cut, i.e. the cut was premature): 33.3% (1/3)

## STOP condition checks

- Accounting (leg weight in [0,1], never negative): enforced at simulation time by `branch_b_sim.AccountingError` — the run completed without raising, so no leg ever exceeded its quota or went negative.

Regenerate: `python -m sel_v2.offline.v22_gate_report`.

---

## Methodology notes (not machine-generated)

- **Surging is data-sparse over 2 years**: only **13 contiguous Surging segments / 1265 bars total** exist in the full history (`v2_state_annotation`, matching the Wave S2C baseline). 9 of those segments actually produced a first-leg trigger (a k=1 RE_PUSH); the other 4 never confirmed a pullback-then-repush cycle within their (often short) dwell. H-V22-1's failure (9 vs the ≥20 gate) is a direct consequence of this scarcity, not a simulator artifact — there simply weren't 20 independent structural attempts to trigger on in this window.
- **Direction-inference bug found and fixed during this Wave**: the first implementation of `substate.py` inferred a Surging leg's long/short direction from a short pre-segment price lookback (3 bars). Run against the real data, this called **all 13 segments "long"**, including several that fell >15–20% over their life (e.g. the 2025-10-12→2025-11-26 segment: 113119→90438, a −20% move). Checking longer lookbacks (6/12/24/48 bars) did not fix this — Surging appears to consistently trigger right after a short-term upward tick even inside a broader downtrend, so no pre-segment window reliably predicts the segment's realized direction. This matches the long-standing deferred item in STATUS.md ("Surging Up/Down direction (sub_state unused)") — the *parent* state machine itself has no directional label, and this offline analysis suggests its entry condition may be structurally long-biased at the trigger moment regardless of the ensuing trend. **Fix applied**: `substate.py` now classifies a leg's direction from the segment's own realized net move (close at segment end vs segment start) — legitimate for this offline, full-history replay (unlike a live system, the whole segment is already in hand), and is what actually makes the D3 short-side mirror reachable in this data (8 of the 9 legs are short under the corrected logic, versus 0 under the original bug). This is a finding for the Wiki, not just a bugfix: if the live Surging condition genuinely can't originate a "Down" entry on its own, D1-D5's mirrored short path may need a state-machine-level fix (out of scope for this offline-only Wave) before it can trade live.
- **H-V22-3's PASS is on a very small sample (n=4 stop_break exits, 0 false positives)** — statistically this is "PASS by absence of evidence," not a confident clearance of the false-positive-rate gate. The terminal-flag false-positive rate (33.3%, n=3) is similarly too small a sample to lean on.
- **H-V22-2 (−0.71% mean per leg) is small in magnitude relative to the sample's spread** (worst −3.98%, best +3.49%, p50 −1.12%) — with n=9 legs this is not a statistically resolved verdict either way; it fails the literal `> 0` gate as specified, nothing more or less claimed here.
- **Verdict, as instructed**: none of the three gates were designed to be actionable individually as a go/no-live decision from this Wave — Part 3's brief was to report the raw numbers and stop regardless of outcome. Two of three gates (H-V22-1, H-V22-2) fail; H-V22-3 passes on a thin sample. This does not proceed to V22-B under this Wave's own stated terms.
