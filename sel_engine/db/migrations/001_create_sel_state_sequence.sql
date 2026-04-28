-- Migration 001: Create sel_state_sequence hypertable
-- Task 2.2 / EC-09 fix
-- Applied: 2026-04-28
--
-- Persists one row per 1H bar: the final StateRecord output from the
-- DwellFilter → CascadeCooling → LegalityChecker pipeline.
-- feature_vector is NOT stored here; it lives in sel_features.

CREATE TABLE IF NOT EXISTS sel_state_sequence (
    time                TIMESTAMPTZ NOT NULL,
    symbol              TEXT        NOT NULL,
    state               TEXT,                              -- NULL when none_reason != NOT_APPLICABLE
    none_reason         TEXT        NOT NULL DEFAULT 'not_applicable',
    reason              TEXT        NOT NULL DEFAULT '',   -- detail string from StateRecord.reason
    cold_start          BOOLEAN     NOT NULL DEFAULT FALSE,
    is_legal_transition BOOLEAN     NOT NULL DEFAULT TRUE,
    transition_from     TEXT,                              -- NULL on cold-start or from-None transitions
    feature_quantiles   JSONB,                             -- quantile ranks passed to recognizer
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

SELECT create_hypertable(
    'sel_state_sequence', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

-- Primary uniqueness: one row per (symbol, bar_time)
CREATE UNIQUE INDEX IF NOT EXISTS sel_state_sequence_symbol_time
    ON sel_state_sequence (symbol, time);

-- Accelerate state distribution queries (state_rates computation)
CREATE INDEX IF NOT EXISTS sel_state_sequence_state_time
    ON sel_state_sequence (state, time DESC);

-- ── ROLLBACK ──────────────────────────────────────────────────────────────────
-- DROP TABLE IF EXISTS sel_state_sequence CASCADE;
