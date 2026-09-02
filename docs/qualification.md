# Qualification

OOS and shadow artifacts are built by `selene.evidence.build` and verified by `selene.evidence.verify`. Production live boot calls `shared.runtime.release_identity.verify_boot`.

Minimum OOS gates are in `selene/evidence/schema.py` (`OOS_GATES`). Insufficient trades are `BLOCKED_INSUFFICIENT_DATA`, not PASS. Gap reports are written by `python scripts/report_oos.py` from observed_live records only; backfill is excluded.

Shadow requires ≥30 days and ≥3 regimes counted from records, not the calendar. `python scripts/report_shadow.py` stays BLOCKED until those records exist. Shadow never touches a live trading API.

Independent postgres/redis compose: `docker-compose.qualification.yml` (ports 25432/26379, dummy password, no Helios). PAPER chain and fault injection run in-process. Compose-smoke is PARTIAL until scanner..gateway containers exist on that compose. No Docker → `RUNTIME_BLOCKED_NO_DOCKER`.

PR #8/#9 remain `OWNER_BLOCKED`.
