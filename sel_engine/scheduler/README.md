# sel_engine/scheduler — Bar-Close Scheduler

Deployed as Docker service `sel-bar-runner`. Triggers at every UTC hour boundary
+30 seconds and processes the bar that just closed.

## Scheduler Responsibilities

1. Read raw data from TimescaleDB (candles, sel_features history, sel_oi_history) and Redis (TF accumulator, H samples, depth samples)
2. Compute FeatureVector via `FeatureCalculator`
3. Supplement depth features from orderbook samples (FeatureCalculator uses raw L2 bids/asks; since those are unavailable at bar close, depth is supplemented post-compute from Redis `sel:depth_samples`)
4. Run the state pipeline: `StateEngine.process()` → DwellFilter → CascadeCooling → LegalityChecker
5. Write `sel_features` (features + quantiles) and `sel_state_sequence` (state record) via upsert
6. Update Redis health keys: `sel:scheduler:last_run`, `sel:scheduler:last_bar_processed`, `sel:scheduler:consecutive_failures`

## Startup Sequence

Collectors must be running before the scheduler starts:
1. `sel-orderbook` — provides H samples and depth samples
2. `sel-trade-flow` — provides TF accumulator
3. `sel-oi` — provides OI history in TimescaleDB
4. `data-service` — provides K-line candles in TimescaleDB

The scheduler does **not** back-fill historical bars. It processes only the most
recently closed bar on each trigger.

## Timing

- Trigger: every UTC hour + 30 seconds (e.g., 14:00:30 processes the 13:00 bar)
- 30-second offset allows K-line and collector data to settle
- First trigger after startup: next full UTC hour + 30s (up to ~1H wait on startup)

## Health Check

Docker healthcheck polls `sel:scheduler:last_run` Redis key. Returns healthy if the
scheduler ran within the last 2 hours (tolerates one missed trigger on restart).

```
python -c 'from sel_engine.scheduler.health import check; exit(0 if check() else 1)'
```

## Cold Start Behaviour

The StateRecognizer requires 720 bars of history before leaving cold start. During
the first 720 processed bars, all records have `cold_start=True` and `state=None`
with `none_reason=cold_start`. Cold start ends approximately 30 days after the first
successful bar is written.

Expected `sel_state_sequence` state during cold start:
```
state=NULL, cold_start=TRUE, none_reason='cold_start'
```

## Fault Tolerance

- **Single bar failure**: logged as ERROR, Redis failure counter incremented, no row
  written to `sel_state_sequence`. Next bar processes normally.
- **Data lag** (candle not yet in DB at trigger time): bar skipped, next trigger
  picks up the next bar normally. Missing bars are not back-filled.
- **Idempotency**: if `(symbol, bar_open_time)` already exists in `sel_state_sequence`,
  the bar is skipped silently.

## Known Limitations (EC-10)

The `StateEngine` holds in-memory state for `DwellFilter` (dwell count) and
`CascadeCooling` (cooldown end time). This state is **lost on container restart**.
After a restart, the engine re-warms from scratch:
- During cold start (first 720 bars): no effect — recognizer returns `cold_start=True`
- After cold start: a few bars may have incorrect dwell/cooling behavior until the
  in-memory state catches up

This is a known design limitation; see `audit/engineering_concerns.md` EC-10.

## Failure Troubleshooting

| Symptom | Likely Cause | Check |
|---------|-------------|-------|
| `sel_state_sequence` not growing | Scheduler not running | `docker logs selene-sel-bar-runner-1` |
| All `state=NULL cold_start=TRUE` | Normal — first 720 bars | Count rows in `sel_state_sequence` |
| `data_lag` warnings every hour | K-line delay from data-service | Check data-service logs |
| `consecutive_failures` rising | DB/Redis connectivity | Check DB and Redis health |
| Docker shows `unhealthy` within first 75m | Expected — start_period=75m | Wait for first trigger |
