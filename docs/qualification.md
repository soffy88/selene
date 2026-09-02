# Qualification

OOS and shadow artifacts are built by `selene.evidence.build` and verified by `selene.evidence.verify`. Production live boot calls `shared.runtime.release_identity.verify_boot`.

Minimum OOS gates are in `selene/evidence/schema.py` (`OOS_GATES`). Insufficient trades are `BLOCKED_INSUFFICIENT_DATA`, not PASS. Gap reports are written by `python scripts/report_oos.py` from observed_live records only; backfill is excluded.

Shadow requires ≥30 days and ≥3 regimes counted from records, not the calendar. `python scripts/report_shadow.py` stays BLOCKED until those records exist. Shadow never touches a live trading API.

Independent compose: `docker-compose.qualification.yml` (no Helios). Services: postgres, redis, deterministic-fixture, scanner, signal, portfolio, risk, execution, gateway. EXEC_MODE is literal PAPER. Scanner uses `SCANNER_SOURCE=fixture`. Run `PYTHONPATH=. python scripts/run_qualification_stack.py`. No Docker → `RUNTIME_BLOCKED_NO_DOCKER`.

PR #8/#9 remain `OWNER_BLOCKED`.
