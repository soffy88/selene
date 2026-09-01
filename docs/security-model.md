# Security model

Roles: read, operator, admin. Production requires all three secrets (or a shared `GATEWAY_API_KEY`).

Writes: fail-closed, clock-skew limited, rate-limited, idempotent by `request_id`.

Live execution: artifacts bind git SHA, image digest, config digest, risk policy, venue/account allowlist, capital cap.

Side effects: one row per `venue+account+client_order_id+operation_kind`. Timeout => probe, never blind resubmit. Unsafe divergence => `QUARANTINED` + halt.
