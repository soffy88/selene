# Selene 10/10 Closure Specification

**Document status:** Execution-ready  
**Baseline repository:** `github.com/soffy88/selene`  
**Audited baseline:** `main@24137634f777e7d993434faafc2d418e31f7b7b1`  
**Date:** 2026-09-01  
**Owner:** Soffy  
**Working branch:** `closure/p0-safety`

This file is the unique Closure Contract. Completion requires Code, Test, Runtime, Artifact, and Operational evidence. Missing any one is `PARTIAL`, `BLOCKED`, or `NO_GO`.

## 0. Formal verdict (current)

```text
RESEARCH_READY = YES
PAPER_READY = YES
SHADOW_READY = PARTIAL
LIMITED_LIVE_READY = NO
AUTO_MAINNET_READY = NO
PUBLIC_PRODUCT_READY = NO
```

Until every P0/P1 gate is `PASS`:

- `EXEC_MODE` stays `PAPER` or `NOTIFY_ONLY`.
- Production `AUTO_EXEC` is forbidden.
- `I_HAVE_OOS_EVIDENCE=yes` cannot fake OOS qualification.
- Risk thresholds, fees, losses, and validation windows must not be weakened to pass a test.
- Backfill must not be presented as live-forward data.
- Shadow/paper/inferred fills must not be claimed as real fills.
- Secrets must not appear in tests, logs, reports, images, or frontend output.
- Unknown/missing/stale inputs fail closed or degrade explicitly.

## 1. Status vocabulary

`PASS` | `PARTIAL` | `BLOCKED` | `FAIL` | `NOT_RUN` | `NO_GO`

## 2. Execution order (do not skip)

1. Protection branch + baseline evidence
2. P0-1 execution-mode fail-closed
3. P0-2 Gateway auth/RBAC/audit
4. P0-3 secret/supply-chain
5. P0-4 durable side-effect authority
6. P1-1/P1-2 OOS artifact + data manifest
7. P1-3 real-strategy qualification
8. P1-4 shadow qualification
9. P1-5 PR #8/#9 controlled merge
10. P2 CI/runtime/release
11. P3 docs/frontend
12. Final audit
13. LIMITED_LIVE owner decision only after final gate is green

## 3. P0-1 implementation notes

Canonical modes: `NOTIFY_ONLY | PAPER | SHADOW | LIMITED_LIVE | AUTO_EXEC`.

- Unset/empty `EXEC_MODE` → `PAPER`.
- Unknown mode → refuse start.
- `CONFIRM_THEN_EXEC` → deprecated alias of `LIMITED_LIVE` (same live hard gate).
- Compose keeps a literal `PAPER` (no `${EXEC_MODE}` interpolation).
- PAPER/NOTIFY_ONLY/SHADOW do not construct venue adapters, subscribe fill WS, or call orderbook REST.
- Non-production live → `funds_scope=testnet` only; cannot claim mainnet.
- Production live requires bound release + OOS + shadow artifacts, matching git SHA, image digest, config digest, risk policy digest, allowlists, capital cap, unexpired `gate_verdict=PASS`.

Authority module: `shared/runtime/release_identity.py`.

## 4. Owner decisions (must stay OWNER_BLOCKED until answered)

1. Open-source license
2. LIMITED_LIVE account, venue, symbol, capital cap
3. OOS/shadow artifact TTL
4. Final risk-policy thresholds and approver
5. PR #8/#9 merge window
6. Historical OI/microstructure data source

The full gate tables, artifact schemas, CI matrix, LIMITED_LIVE limits, and 10/10 checklist from the 2026-09-01 owner spec remain in force. They are not weakened by P0-1 landing.
