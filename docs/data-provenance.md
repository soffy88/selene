# Data provenance

Classes: `observed_live`, `backfilled`, `derived_replay`.

Live performance queries must use `observed_live` only. `v2_state_history` rows before `2026-06-15` are a one-time backfill; live SQL must include `timestamp >= TIMESTAMPTZ '2026-06-15'`. Mixing is an error, not a STATUS.md reminder.

See `shared/data/provenance.py`.
