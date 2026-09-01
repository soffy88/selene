# Selene

Selene is a deterministic crypto research and paper-trading stack (CryptoWatch v4 + SEL v2). It is **not** a live broker, not a social copy-trading network, and not qualified for unattended mainnet execution.

**PAPER is the default and recommended mode.** Do not enable live trading. `LIMITED_LIVE` and `AUTO_EXEC` are fail-closed and require bound OOS + shadow + release artifacts. Those artifacts do not exist yet.

## Status

```text
RESEARCH_READY = YES
PAPER_READY = YES
SHADOW_READY = PARTIAL
LIMITED_LIVE_READY = NO
AUTO_MAINNET_READY = NO
PUBLIC_PRODUCT_READY = PARTIAL
```

See `docs/SEL-10-10-CLOSURE.md`.

## Architecture

Scanner → signal → portfolio → risk → execution (PAPER) plus the SEL v2 4H state machine (Hawkes / CUSUM / TDA / LOB entropy). Details: `docs/architecture.md`.

## Quick start (PAPER)

```bash
cp .env.example .env
# EXEC_MODE stays PAPER. Do not set AUTO_EXEC.
docker compose config --quiet
PYTHONPATH=. python -m pytest tests/unit/test_release_identity.py tests/unit/test_gateway_auth.py tests/unit/test_side_effects.py tests/evidence -q
python -m shared.runtime.release_identity --health
```

Full compose depends on the Helios network (`platform-postgres`, `helios-redis`). Without it, `scripts/compose_smoke.py` reports `BLOCKED`, not PASS.

## Tests

```bash
ruff check shared/runtime shared/security shared/ledger selene
pytest -q tests/unit/test_release_identity.py tests/evidence
python scripts/fault_injection.py
python -m selene.security.verify_redaction --root .
```

## Data and trading risk

- Backfill is not live. Live `v2_state_history` queries must bound `timestamp >= 2026-06-15`.
- Paper fills are not real fills. Shadow fills are not real fills.
- No deployed strategy has out-of-sample alpha evidence.

## License

`OWNER_BLOCKED` — the owner has not selected a license. See `LICENSE`.
