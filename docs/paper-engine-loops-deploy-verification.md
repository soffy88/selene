# Paper-engine sub-bar loops — deployment verification checklist (P1-4)

The `v2-paper-engine` service now runs three loops concurrently (`PaperEngine.run`):

| Loop | Cadence (env) | Role | Authoritative? |
|---|---|---|---|
| `_strategy1_4h_loop` | 60s | reprocess on new sealed 4H bar | **yes** (entries/exits) |
| `_strategy2_tick_loop` | `S2_TICK_INTERVAL_SEC` (default 300s) | reprocess when new ticks arrive, to keep S2 H1 Hawkes / micro current between bars | **yes** (via replay; entries still bar-gated) |
| `_position_management_loop` | `POSITION_MGMT_INTERVAL_SEC` (default 60s) | intra-bar unrealized-PnL monitor + hard-stop alert | **no** (monitoring only) |

Design invariant: **all authoritative open/close state comes from the deterministic
full-history replay** (`_reprocess` → `process_frame`). The two new loops never mutate
positions out-of-band. `_reprocess` is serialised by `self._reprocess_lock`, so the 4H
loop and the tick loop can never replay concurrently.

Everything below was unit-tested with fakes (`tests/sel_v2/test_paper_engine_loops.py`);
this checklist covers what can only be confirmed on a real deploy (DB/Redis/live ticks).

---

## 0. Prerequisites
- [ ] `v2_ticks`, `v2_bars_4h`, `v2_derivatives_snapshots` are being populated
      (collectors healthy; see `services/healthcheck`).
- [ ] `v2_strategy_params` has the H2 rows (`h2_mu_ref/alpha_ref/beta_ref`) so S2 is
      enabled — otherwise S2 is disabled and the tick loop only refreshes S1 context
      (run `python -m sel_v2.offline.hawkes_calibration` first; P0-1).
- [ ] `ENVIRONMENT=development` / `EXEC_MODE=NOTIFY_ONLY` (this is paper only).

## 1. Startup — open-position restore (`_load_open_positions`)
- [ ] Log line `restored N open positions from v2_trades` appears once at boot.
- [ ] N matches `SELECT count(*) FROM v2_trades WHERE exit_time IS NULL AND instrument='BTC-USDT'`.
- [ ] Boot does not crash when there are zero open trades (N=0 path).

## 2. Strategy-2 tick loop (`_strategy2_tick_loop`)
- [ ] Within `S2_TICK_INTERVAL_SEC` of a new tick batch, a replay runs (engine summary
      log line updates; `v2:paper:engine_summary` Redis key `ts` advances).
- [ ] When **no** new ticks have arrived, no replay runs (cursor `_last_tick_ts`
      unchanged; no spurious summary churn). Confirm with a quiet-market window.
- [ ] **Concurrency:** grep logs for overlapping replays — there must be none. The
      4H loop and tick loop both call `_reprocess`; the lock must serialise them.
      Stress: force a new bar and a new tick batch in the same minute, confirm the
      two replays run back-to-back, not interleaved (no torn `v2_trades` writes,
      no duplicate `v2_state_history` conflicts beyond the expected ON CONFLICT).
- [ ] CPU/DB load from the extra replays is acceptable at the chosen interval. If a
      full replay is heavy, raise `S2_TICK_INTERVAL_SEC` (e.g. 600–900s).
- [ ] Idempotency holds: trade count / equity after a tick-triggered replay equals
      what a bar-triggered replay over the same sealed bars produces (positions are a
      pure function of sealed history; mid-bar ticks must not create new entries).

## 3. Position-management monitor (`_position_management_loop`)
- [ ] Redis key `v2:paper:position_risk` is written every `POSITION_MGMT_INTERVAL_SEC`
      and parses as JSON with `mark`, `n_open`, `total_unrealized_usdt`, `positions[]`.
- [ ] `n_open` matches the engine's open positions after the latest replay.
- [ ] `mark` tracks the latest `v2_ticks` price (falls back to latest `v2_bars_4h.close`
      when ticks are momentarily absent).
- [ ] Drive an open position's unrealized loss past `PAPER_HARD_STOP_PCT` (default 5%)
      in a test window → log `intra-bar hard-stop breach on K position(s)` and Redis key
      `v2:paper:risk_alert` is set. Confirm it clears on the next non-breaching cycle
      (key is overwritten each publish; a stale alert key is acceptable only until the
      next cycle).
- [ ] **Boundary check:** the monitor must NOT close positions. Verify a breach does
      not change `v2_trades` (exits still only appear from the bar-gated replay). This
      is the key safety property — the monitor is advisory.

## 4. Failure / resilience
- [ ] Kill Redis briefly: loops log a warning and keep running (no crash; `_publish_*`
      and `_persist_*` swallow Redis errors).
- [ ] Kill the DB pool briefly: loops log errors and recover on the next cycle.
- [ ] Restart the service mid-session: `_load_open_positions` repopulates the snapshot,
      and the first replay reconstructs identical authoritative state (idempotent ids).

## 5. Rollback
If any authoritative-state anomaly appears (duplicate/torn trades, positions changing
without a bar), revert to the 4H-only loop by editing `run()`'s `asyncio.gather` to
`self._strategy1_4h_loop()` only, or set `S2_TICK_INTERVAL_SEC` very high to neutralise
the tick loop. The monitor can be neutralised by setting `POSITION_MGMT_INTERVAL_SEC`
high; it cannot affect positions, so it is safe to leave on.

## Env knobs
| Var | Default | Effect |
|---|---|---|
| `S2_TICK_INTERVAL_SEC` | 300 | tick-loop replay cadence (raise to reduce load) |
| `POSITION_MGMT_INTERVAL_SEC` | 60 | risk-monitor publish cadence |
| `PAPER_HARD_STOP_PCT` | 0.05 | intra-bar unrealized-loss alert threshold |
