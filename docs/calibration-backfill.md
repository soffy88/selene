# Calibration parameter backfill — runbook (P1-7)

The strategies ship with textbook-placeholder thresholds (self-labelled "calibrate
after paper Month 1"). This backfill computes the data-derived values from real
history and writes them to `v2_strategy_params`, which the live system reads via
`load_strategy_params`. Without them the engine falls back to the hardcoded defaults
in `sel_v2/states/schema.py` (`hawkes_br_threshold=0.85`, `tda_l1_threshold=0.000097`)
and **Strategy 2 stays disabled** (no `h2_*_ref` rows → `from_h2_reference` raises).

## What gets written

| Calibration | Keys (`v2_strategy_params.param_key`) | Read by |
|---|---|---|
| H2 Hawkes (`hawkes_calibration`) | `h2_mu_ref`, `h2_alpha_ref`, `h2_beta_ref`, `h2_branching_ratio_threshold` | `hawkes_intensity.from_h2_reference`, `replay.py` |
| TDA1 L¹ (`tda_calibration`) | `tda1_l1_threshold_p90/p95/p97` | `replay.py` |

Degenerate fits are rejected, not persisted: TDA skips non-finite quantiles; Hawkes
skips `beta<=0` or branching ratio outside `(0, 10]` (a diverged MLE on too-few/noisy
events would otherwise poison the live H1 intensity). A DB failure is logged, not
fatal — the markdown reports are still produced.

## Run it

One command, bars pulled from the live `v2_bars_4h`:

```bash
python -m sel_v2.offline.calibrate_all --from-db
# or from a parquet:
python -m sel_v2.offline.calibrate_all --data analysis/data/btc_4h.parquet
# compute without writing (dry run):
python -m sel_v2.offline.calibrate_all --data ... --no-persist
```

Production windows: Hawkes uses a 540-bar (90-day) rolling window — needs ≳2 years of
4H bars to be meaningful (it raises on fewer bars than the window). TDA defaults are
fine on a few hundred bars.

## Verify (deploy checklist)

- [ ] Bars available: `SELECT count(*) FROM v2_bars_4h WHERE symbol='BTC-USDT'`
      is ≳ 540 (ideally ~2y ≈ 4380).
- [ ] Run completes and logs `Persisted H2 reference params ...` and
      `Persisted TDA1 L^1 thresholds ...` (NOT "Skipping DB persist: degenerate ...").
- [ ] All keys present — programmatically:
      ```python
      from sel_v2.offline.calibrate_all import verify_params_present
      res = await verify_params_present(pool)   # {"present", "missing", "ok"}
      assert res["ok"], res["missing"]
      ```
      or SQL: `SELECT param_key FROM v2_strategy_params` contains all 7 keys.
- [ ] Restart `v2-paper-engine`; its log shows S2 enabled (no
      "Strategy 2 disabled — H2 Hawkes params unavailable").
- [ ] Sanity: `h2_branching_ratio` in the run summary is sub-critical (≈0.5–0.9, not
      ~1e308); `tda1_l1_threshold_p95` is on the order of 1e-4.
- [ ] Re-run is idempotent (upsert by `param_key`); values update in place.

## Schedule

Re-run monthly (or after a regime shift) to keep thresholds current — e.g. a cron
calling `python -m sel_v2.offline.calibrate_all --from-db`. The placeholders are a
cold-start; this backfill is what moves the system onto data-derived parameters.
