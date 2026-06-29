-- ============================================================
-- sel v2.0 database schema  (v2_ prefix)
-- Ref: sel-language-v2.0.md §32 + sel-language-v2.1-patches.md §15
-- Apply: psql -U helios -d selene -f schema.sql
-- ============================================================

-- extension already present on platform-postgres; no-op if exists
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- RAW TIME-SERIES (hypertable)
-- ============================================================

-- NOTE: columns below MUST match the collector INSERTs in sel_v2/data/*.
-- (Reconciled in optimization item #1 — the prior schema diverged from the
--  collectors, which silently failed every INSERT and left these tables empty.)
CREATE TABLE IF NOT EXISTS v2_ticks (
    timestamp   TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL DEFAULT 'BTC-USDT',
    price       NUMERIC     NOT NULL,
    size        NUMERIC     NOT NULL,
    side        TEXT        NOT NULL,  -- 'buy' / 'sell'
    trade_id    TEXT                   -- exchange tradeId (string)
);
SELECT create_hypertable('v2_ticks', 'timestamp',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE);
-- Dedup key for WS-reconnect replays (item #6): same trade can't land twice.
CREATE UNIQUE INDEX IF NOT EXISTS uix_v2_ticks
    ON v2_ticks (timestamp, symbol, trade_id);
ALTER TABLE v2_ticks SET (timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol');
SELECT add_compression_policy('v2_ticks', INTERVAL '7 days',
    if_not_exists => TRUE);

-- LOB snapshots (compressed at 6h chunks)
CREATE TABLE IF NOT EXISTS v2_lob_snapshots (
    timestamp   TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL DEFAULT 'BTC-USDT',
    bids        JSONB       NOT NULL,  -- [[price, size], ...]
    asks        JSONB       NOT NULL,
    bid_depth   NUMERIC,
    ask_depth   NUMERIC,
    entropy     NUMERIC
);
SELECT create_hypertable('v2_lob_snapshots', 'timestamp',
    chunk_time_interval => INTERVAL '6 hours',
    if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS uix_v2_lob_snapshots
    ON v2_lob_snapshots (timestamp, symbol);

-- Derivatives snapshots (OI / funding / mark / index price)
CREATE TABLE IF NOT EXISTS v2_derivatives_snapshots (
    timestamp       TIMESTAMPTZ NOT NULL,
    symbol          TEXT        NOT NULL,  -- 'BTC-USDT'
    funding_rate    NUMERIC,
    open_interest   NUMERIC,
    mark_price      NUMERIC,
    index_price     NUMERIC
);
SELECT create_hypertable('v2_derivatives_snapshots', 'timestamp',
    if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS uix_v2_derivatives_snapshots
    ON v2_derivatives_snapshots (timestamp, symbol);

-- Liquidation events
CREATE TABLE IF NOT EXISTS v2_liquidations (
    timestamp   TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    side        TEXT        NOT NULL,
    size        NUMERIC     NOT NULL,
    price       NUMERIC     NOT NULL,
    loss        NUMERIC
);
SELECT create_hypertable('v2_liquidations', 'timestamp',
    if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS uix_v2_liquidations
    ON v2_liquidations (timestamp, symbol, side, size, price);

-- On-chain exchange flows (minimum set: large BTC in/out)
CREATE TABLE IF NOT EXISTS v2_onchain_exchange_flows (
    timestamp       TIMESTAMPTZ NOT NULL,
    exchange        TEXT        NOT NULL,
    direction       TEXT        NOT NULL,  -- 'in' / 'out'
    amount_btc      NUMERIC     NOT NULL,
    block_height    BIGINT      NOT NULL
);
SELECT create_hypertable('v2_onchain_exchange_flows', 'timestamp',
    if_not_exists => TRUE);

-- ============================================================
-- DERIVED TIME-SERIES (hypertable)
-- ============================================================

-- 4H OHLCV aggregated bars (primary time anchor)
CREATE TABLE IF NOT EXISTS v2_bars_4h (
    time        TIMESTAMPTZ NOT NULL,   -- bar open time
    symbol      TEXT        NOT NULL DEFAULT 'BTC-USDT',
    open        NUMERIC     NOT NULL,
    high        NUMERIC     NOT NULL,
    low         NUMERIC     NOT NULL,
    close       NUMERIC     NOT NULL,
    volume      NUMERIC     NOT NULL,
    vwap        NUMERIC,                 -- written by v2_bar_aggregator / okx_backfill
    tick_count  INTEGER,                 -- ticks aggregated into this bar
    source      TEXT        NOT NULL DEFAULT 'okx'
);
SELECT create_hypertable('v2_bars_4h', 'time',
    if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS uix_v2_bars_4h ON v2_bars_4h (time, symbol);

-- State machine history
CREATE TABLE IF NOT EXISTS v2_state_history (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp       TIMESTAMPTZ NOT NULL,  -- 4H bar close time
    state           TEXT        NOT NULL,
    sub_state       TEXT,
    state_features  JSONB       NOT NULL,  -- actual values at entry
    transition_from TEXT,
    transition_via  TEXT,                  -- Release / Stress / etc
    duration_4h     INTEGER
);
SELECT create_hypertable('v2_state_history', 'timestamp',
    if_not_exists => TRUE);

-- CUSUM events
CREATE TABLE IF NOT EXISTS v2_cusum_events (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp           TIMESTAMPTZ NOT NULL,
    cusum_type          TEXT        NOT NULL,  -- 'mid' / 'short'
    direction           TEXT        NOT NULL,  -- 'up' / 'down'
    peak_value          NUMERIC     NOT NULL,
    threshold_h         NUMERIC     NOT NULL,
    z_returns_window    JSONB
);
SELECT create_hypertable('v2_cusum_events', 'timestamp',
    if_not_exists => TRUE);

-- Inverse-vocab (reverse-inference) events
-- Includes v2.1 tool observation columns
CREATE TABLE IF NOT EXISTS v2_inverse_vocab_events (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp           TIMESTAMPTZ NOT NULL,
    vocab               TEXT        NOT NULL,  -- 'Sweep' / 'Absorption' / etc
    intensity           NUMERIC,
    associated_state    TEXT,
    triggered_decision  BOOLEAN,
    -- v2.1 additions
    tool_source         TEXT,          -- 'hawkes' / 'transfer_entropy' / 'tda' / etc
    observation_only    BOOLEAN        DEFAULT TRUE,
    tool_metadata       JSONB
);
SELECT create_hypertable('v2_inverse_vocab_events', 'timestamp',
    if_not_exists => TRUE);

-- ============================================================
-- TRADING RECORDS (hypertable on entry_time)
-- ============================================================

CREATE TABLE IF NOT EXISTS v2_trades (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy            TEXT        NOT NULL,  -- 'strategy_1' / 'strategy_2'
    sub_account         TEXT        NOT NULL,
    -- entry
    entry_time          TIMESTAMPTZ NOT NULL,
    entry_price         NUMERIC     NOT NULL,
    direction           TEXT        NOT NULL,  -- 'long' / 'short'
    size                NUMERIC     NOT NULL,
    leverage            NUMERIC     NOT NULL,
    instrument          TEXT        NOT NULL,
    -- decision context
    entry_state         TEXT        NOT NULL,
    entry_cusum_id      UUID,
    entry_vocab         JSONB,
    entry_confidence    NUMERIC,
    -- exit (nullable while open)
    exit_time           TIMESTAMPTZ,
    exit_price          NUMERIC,
    exit_reason         TEXT,
    -- PnL
    pnl_usdt            NUMERIC,
    pnl_pct             NUMERIC,
    fees_paid           NUMERIC,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
SELECT create_hypertable('v2_trades', 'entry_time',
    if_not_exists => TRUE);

-- ============================================================
-- NON-HYPERTABLE (ID-based lookup)
-- ============================================================

-- Decision trail (governance + audit)
CREATE TABLE IF NOT EXISTS v2_decision_trail (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp           TIMESTAMPTZ NOT NULL,
    decision_type       TEXT        NOT NULL,
    trigger_source      TEXT        NOT NULL,
    week_number         TEXT,
    target_component    TEXT        NOT NULL,
    parameter_name      TEXT,
    old_value           JSONB,
    new_value           JSONB,
    claude_suggestion   TEXT,
    wiki_decision       TEXT        NOT NULL,
    decision_basis      TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    created_by          TEXT        NOT NULL,
    revisited_at        TIMESTAMPTZ,
    revisit_outcome     TEXT,
    -- v2.1 additions
    tool_evaluation     JSONB,
    evaluation_phase    TEXT
);
CREATE INDEX IF NOT EXISTS idx_v2_decision_trail_ts
    ON v2_decision_trail (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_v2_decision_trail_component
    ON v2_decision_trail (target_component, timestamp DESC);

-- Strategy parameters version table
CREATE TABLE IF NOT EXISTS v2_strategy_params (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy        TEXT        NOT NULL,
    param_name      TEXT        NOT NULL,
    param_value     JSONB       NOT NULL,
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,
    decision_id     UUID        REFERENCES v2_decision_trail(id),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_v2_strategy_params_active
    ON v2_strategy_params (strategy, param_name)
    WHERE valid_to IS NULL;

-- Tool evaluation results (monthly reviews)
CREATE TABLE IF NOT EXISTS v2_tool_evaluation_results (
    id                      UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp               TIMESTAMPTZ NOT NULL,
    tool_id                 TEXT    NOT NULL,
    tool_name               TEXT    NOT NULL,
    evaluation_phase        TEXT    NOT NULL,  -- 'month_3' / 'month_6' / etc
    lead_time_seconds       NUMERIC,
    false_positive_rate     NUMERIC,
    correlation_with_others JSONB,
    sample_size             INTEGER,
    decision                TEXT    NOT NULL,  -- 'upgrade' / 'maintain' / 'deprecate'
    decision_reason         TEXT,
    created_by              TEXT    NOT NULL,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_v2_tool_eval_phase
    ON v2_tool_evaluation_results (evaluation_phase, tool_id);

-- Kelly Phase switch history
CREATE TABLE IF NOT EXISTS v2_strategy_phase_history (
    id                          UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp                   TIMESTAMPTZ NOT NULL,
    strategy                    TEXT    NOT NULL,
    from_phase                  TEXT,
    to_phase                    TEXT    NOT NULL,
    rolling_w                   NUMERIC,
    rolling_r                   NUMERIC,
    sample_size                 INTEGER,
    kelly_fraction_estimated    NUMERIC,
    kelly_cap_lower             NUMERIC,
    kelly_cap_upper             NUMERIC,
    decision_id                 UUID    REFERENCES v2_decision_trail(id),
    created_at                  TIMESTAMPTZ DEFAULT NOW()
);
