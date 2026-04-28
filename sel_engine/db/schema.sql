-- sel_engine DB schema
-- Apply via sel_engine/db/migrations.py

-- ── sel_features: one row per 1H bar per symbol ───────────────────────────────
CREATE TABLE IF NOT EXISTS sel_features (
    time                   TIMESTAMPTZ NOT NULL,
    symbol                 VARCHAR(20)  NOT NULL,
    -- Price layer
    close                  NUMERIC(20, 8),
    delta_p_pct            NUMERIC(10, 6),
    sigma_p_24h            NUMERIC(10, 8),
    -- Liquidity layer
    H                      NUMERIC(10, 8),
    H_sample_count         INTEGER,
    -- Orderbook depth
    top_5_bid_size         NUMERIC(20, 8),
    top_5_ask_size         NUMERIC(20, 8),
    total_depth            NUMERIC(20, 8),
    spread_bps             NUMERIC(10, 4),
    -- Flow layer
    TF                     NUMERIC(20, 8),
    OI                     NUMERIC(20, 8),
    funding_rate           NUMERIC(10, 8),
    -- Derived indicators
    LV                     NUMERIC(6,  4),
    absorption_ratio       NUMERIC(20, 8),
    price_autocorr_12h     NUMERIC(8,  6),
    price_autocorr_24h     NUMERIC(8,  6),
    price_autocorr_48h     NUMERIC(8,  6),
    sigma_p_d2             NUMERIC(10, 8),
    H_change_rate_std_12h  NUMERIC(10, 8),
    OI_hurst               NUMERIC(6,  4),
    -- Metadata
    data_quality           JSONB,
    computed_at            TIMESTAMPTZ DEFAULT NOW()
);

SELECT create_hypertable('sel_features', 'time', if_not_exists => TRUE);

CREATE UNIQUE INDEX IF NOT EXISTS sel_features_time_symbol
    ON sel_features (time, symbol);

-- ── sel_oi_history: persisted OI from Redis every 5 min ──────────────────────
CREATE TABLE IF NOT EXISTS sel_oi_history (
    time     TIMESTAMPTZ NOT NULL,
    symbol   VARCHAR(20)  NOT NULL,
    oi_value NUMERIC(20, 8) NOT NULL
);

SELECT create_hypertable('sel_oi_history', 'time', if_not_exists => TRUE);

CREATE UNIQUE INDEX IF NOT EXISTS sel_oi_history_time_symbol
    ON sel_oi_history (time, symbol);

-- ── sel_funding_history: funding events (~8H cadence) ────────────────────────
CREATE TABLE IF NOT EXISTS sel_funding_history (
    time         TIMESTAMPTZ NOT NULL,
    symbol       VARCHAR(20)  NOT NULL,
    funding_rate NUMERIC(10, 8) NOT NULL
);

SELECT create_hypertable('sel_funding_history', 'time', if_not_exists => TRUE);

CREATE UNIQUE INDEX IF NOT EXISTS sel_funding_history_time_symbol
    ON sel_funding_history (time, symbol);

-- ── sel_orderbook_samples: raw intra-bar orderbook readings (5-min buckets) ──
-- WIKI_REQUIRED: populated by orderbook_collector once deployed
CREATE TABLE IF NOT EXISTS sel_orderbook_samples (
    time_bucket  TIMESTAMPTZ NOT NULL,
    symbol       VARCHAR(20)  NOT NULL,
    H_sample     NUMERIC(10, 8),
    top_5_bid    NUMERIC(20, 8),
    top_5_ask    NUMERIC(20, 8),
    spread_bps   NUMERIC(10, 4),
    sample_count INTEGER DEFAULT 1
);

SELECT create_hypertable('sel_orderbook_samples', 'time_bucket', if_not_exists => TRUE);

-- ── sel_state_sequence: one row per 1H bar — StateEngine pipeline output ──────
-- EC-09 fix (Task 2.2). One row per (symbol, time). feature_vector lives in sel_features.
CREATE TABLE IF NOT EXISTS sel_state_sequence (
    time                TIMESTAMPTZ NOT NULL,
    symbol              TEXT        NOT NULL,
    state               TEXT,                              -- NULL when none_reason != NOT_APPLICABLE
    none_reason         TEXT        NOT NULL DEFAULT 'not_applicable',
    reason              TEXT        NOT NULL DEFAULT '',
    cold_start          BOOLEAN     NOT NULL DEFAULT FALSE,
    is_legal_transition BOOLEAN     NOT NULL DEFAULT TRUE,
    transition_from     TEXT,
    feature_quantiles   JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

SELECT create_hypertable(
    'sel_state_sequence', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE
);

CREATE UNIQUE INDEX IF NOT EXISTS sel_state_sequence_symbol_time
    ON sel_state_sequence (symbol, time);

CREATE INDEX IF NOT EXISTS sel_state_sequence_state_time
    ON sel_state_sequence (state, time DESC);
