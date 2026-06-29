# System Consolidation — canonical choices (optimization item #18)

This repo accumulated several parallel implementations of the same concern. This
document records the **canonical** choice for each so new work targets one system,
and marks the others **deprecated**. Physical deletion is intentionally deferred —
it needs a human sign-off and a migration of any residual references — so the
deprecated modules remain in place with a header pointing here.

## Paper trading
- **Canonical:** `sel_v2/paper/` (the `v2-paper-engine` service, deployed in
  `docker-compose.yml`; persists trades + state history as of item #6).
- **Deprecated:** `paper_trading/` (v1, Wave 2). Not in `docker-compose.yml`; its
  only importer (`reports/scheduler.py`) is itself unscheduled. Keep for reference
  until its tests' coverage is ported; do not extend it.

## Weekly reports
- **Canonical:** `sel_v2/reports/` (v2 sections + scheduler, aligned with the v2
  tables). Note: `sel_v2/reports/scheduler.py:_collect_report_data` is still a stub
  and must query the live tables before scheduling (tracked separately).
- **Deprecated:** `reports/` (v4 `WeeklyReportGenerator`). Neither report scheduler
  is wired into compose; consolidate onto the v2 generator.

## OI / funding pipelines
Three partial implementations exist; the live decision path must use exactly one.
- **Canonical (live):** the `sel_engine` collectors (`oi_persister` → `sel_oi_history`)
  feeding `services/sel_bar_runner`, **plus** the helixa loaders
  (`sel_v2/scheduler/derivatives_loader.py`) for offline replay.
- **Deprecated / redundant:** `sel_v2/data/v2_derivatives_collector.py`
  (`v2_derivatives_snapshots`) is not consumed by the live runner. Wire it into the
  live path or retire it; do not maintain three sources in parallel.

## Database schemas
These are not strict duplicates — they own different services — but they overlap on
candles/OI/funding. Ownership:
- `sel_v2/db/schema.sql` — the v2 research/paper stack (`v2_*` tables). **Canonical
  for v2.**
- `sel_engine/db/schema.sql` — the sel_engine collector stack (`sel_*` tables).
  Canonical for system A.
- `infra/timescaledb/schema.sql` — the cw4 live-trading services
  (`orders`/`positions`/`signals`/`candles`). Canonical for execution/risk/portfolio.
- `helixa` (external) — read-only derivatives/taker-flow source; not owned here.

Action: keep the three internal schemas (different owners) but treat the candle/OI/
funding overlap as **read from the owning system only**; do not add a fourth.
