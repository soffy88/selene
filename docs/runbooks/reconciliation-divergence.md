# Reconciliation divergence

Trigger: quantity/state mismatch, ghost local, ghost venue.
Risk: double inventory or missing hedge.
Checks: `side_effects` vs `orders` vs venue open orders.
Steps: mark `QUARANTINED`, halt, do not auto-fix.
Verify: no new submits; halt on.
Rollback: none; wait owner.
Owner: Soffy.
