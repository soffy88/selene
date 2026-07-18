# Wave S2C — findings after Step 3-5 completion (2026-07-11)

Recorded so the next diagnosis does NOT re-blame the now-completed S2 Step 3-5 stub.
Deployed on epoch `af6f7d3d`. See commits `8e999ad`…`f1b2747`.

## Finding 1 — S2 entries are now gated UPSTREAM, not by Step 3-5

Step 3 (inverse-vocab Type A/B), Step 4 (cascade filter) and Step 5 (cross-exchange
divergence) are implemented and deployed. S2 still produces **0 entries**, but the binding
constraint has moved upstream of them:

- On the data-rich recent bars (2026-07-05 → now, where tick/LOB/liquidation feeds exist),
  **CUSUM-Short never triggers** — every S2 decision stops at Step 1a
  (`Step 1a: CUSUM-Short not triggered`). Step 3 is never reached, so vocab classification
  never runs on a bar that has data to classify.
- The historical bars that *do* reach Step 3 (527 over full history) predate the tick feed,
  so Absorption/OFI can't form → they correctly abort as "类型未明" (§14.2).

The two prerequisites — a CUSUM-Short trigger AND classifiable microstructure — have not yet
coincided on the same bar. This is a market/data reality (low-vol regime + tick history only
~4-5 days deep), **not a defect in Step 3-5**.

Verified the entry path itself works: a Type-B momentum entry fires end-to-end when
`ofi_persistent_same_direction` + no-Absorption + a CUSUM-Short trigger align (see
`test_s2_step5_divergence.py`). So entries will appear once CUSUM-Short triggers on a
data-rich bar (needs volatility to return and/or tick history to accrue for Absorption to
warm past its 30-sample adaptive floor).

**What to watch** (before concluding S2 is broken again): the `step_reached` distribution in
`v2_strategy_decision` for `strategy_2`. Entries become possible only once bars appear at
`step_reached >= 3` **in the recent window** — i.e. CUSUM-Short starts triggering on
tick-covered bars. Until then, "0 trades" is expected, not a bug.

## Finding 2 — `v2_cusum_events` holds `short` only, no `mid` (by design)

Post-deploy backfill: 678 `short` rows, 0 `mid`. CUSUM-Mid (`cusum_type='mid'`) is the S1
accumulator, which is only fed inside `strategy1_entry.evaluate()` **after** the state and
dwell gates pass (S1 Step 3), and only crosses when its threshold is > 0. It is therefore far
sparser than CUSUM-Short (fed every bar by the engine). The wiring is correct; `mid` rows will
appear when CUSUM-Mid actually crosses on an S1-eligible (Coiling / Drifting-Charged) bar.
Absence of `mid` rows is **not** an indication the mid path is unwired.
