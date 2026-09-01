# Execution halt

Trigger: `cw4:execution:halt` set, Telegram halt alert.
Risk: new orders dropped; stale halt can silence the chain for days.
Checks: `GET /execution/reconcile`, ledger vs venue.
Steps: operator admin `POST /api/v4/risk/circuit-breaker/reset` with reason. Do not clear halt to make tests pass.
Verify: halt key absent, no ghost orders.
Rollback: re-set halt.
Owner: Soffy.
