# Qualification

OOS and shadow artifacts are built by `selene.evidence.build` and verified by `selene.evidence.verify`. Production live boot calls `shared.runtime.release_identity.verify_boot`.

Minimum OOS gates are in `selene/evidence/schema.py` (`OOS_GATES`). Insufficient trades are `BLOCKED_INSUFFICIENT_DATA`, not PASS.

Shadow requires ≥30 days and ≥3 regimes. That clock has not been satisfied.

PR #8/#9 remain `OWNER_BLOCKED`.
