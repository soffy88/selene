-- Durable write/side-effect authority (P0-2 / P0-4). Idempotent.

CREATE TABLE IF NOT EXISTS write_idempotency (
    request_id   TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    actor        TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    body_json    JSONB NOT NULL,
    stored_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS side_effects (
    venue            TEXT NOT NULL,
    account          TEXT NOT NULL,
    client_order_id  TEXT NOT NULL,
    operation_kind   TEXT NOT NULL,
    status           TEXT NOT NULL,
    payload_json     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (venue, account, client_order_id, operation_kind)
);

CREATE INDEX IF NOT EXISTS idx_side_effects_status ON side_effects (status, updated_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id           BIGSERIAL PRIMARY KEY,
    kind         TEXT NOT NULL,
    actor        TEXT NOT NULL,
    request_id   TEXT,
    reason       TEXT,
    path         TEXT,
    git_sha      TEXT,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
