# Redis / PostgreSQL outage

Trigger: health/readyz red, lag alerts, writer errors.
Risk: lost consume, stalled persist, false-green health.
Checks: `docker ps`, `redis-cli ping`, `SELECT 1`.
Steps: do not flip EXEC_MODE. Restore volume from backup. `redis-check-aof --fix` only on copies.
Verify: readyz green, no duplicate `side_effects` rows.
Rollback: stay PAPER.
Owner: Soffy.
