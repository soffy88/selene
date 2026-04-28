-- Paper trading DDL — Wave 2
-- All tables are TimescaleDB hypertables partitioned on `time`.

-- 1. Decision records — one per bar
CREATE TABLE IF NOT EXISTS sel_decisions (
    time                TIMESTAMPTZ     NOT NULL,
    symbol              TEXT            NOT NULL,
    action              TEXT            NOT NULL,   -- open_long | open_short | close | hold | no_action
    rule_id             TEXT            NOT NULL,
    config_hash         TEXT            NOT NULL,
    current_state       TEXT,
    previous_state      TEXT,
    position_multiplier DOUBLE PRECISION,
    target_size_usdt    DOUBLE PRECISION,
    reason              TEXT
);

SELECT create_hypertable('sel_decisions', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS sel_decisions_symbol_time_idx
    ON sel_decisions (symbol, time DESC);


-- 2. Simulated fills — one per trade execution
CREATE TABLE IF NOT EXISTS sel_paper_trades (
    time                TIMESTAMPTZ     NOT NULL,
    symbol              TEXT            NOT NULL,
    side                TEXT            NOT NULL,   -- long | short
    action              TEXT            NOT NULL,   -- open | close
    requested_size_usdt DOUBLE PRECISION NOT NULL,
    fill_price          DOUBLE PRECISION NOT NULL,
    fill_size_usdt      DOUBLE PRECISION NOT NULL,
    slippage_usdt       DOUBLE PRECISION NOT NULL,
    fee_usdt            DOUBLE PRECISION NOT NULL,
    net_size_usdt       DOUBLE PRECISION NOT NULL,
    config_hash         TEXT            NOT NULL
);

SELECT create_hypertable('sel_paper_trades', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS sel_paper_trades_symbol_time_idx
    ON sel_paper_trades (symbol, time DESC);


-- 3. Position snapshots — emitted every bar + on position change
CREATE TABLE IF NOT EXISTS sel_positions (
    time                TIMESTAMPTZ     NOT NULL,
    symbol              TEXT            NOT NULL,
    side                TEXT,                       -- long | short | NULL when no position
    open_time           TIMESTAMPTZ,
    open_price          DOUBLE PRECISION,
    size_usdt           DOUBLE PRECISION,
    current_size_usdt   DOUBLE PRECISION,
    unrealized_pnl_usdt DOUBLE PRECISION,
    entry_fee_usdt      DOUBLE PRECISION
);

SELECT create_hypertable('sel_positions', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS sel_positions_symbol_time_idx
    ON sel_positions (symbol, time DESC);


-- 4. Account state time series — one per bar
CREATE TABLE IF NOT EXISTS sel_account_state (
    time                TIMESTAMPTZ     NOT NULL,
    nav_usdt            DOUBLE PRECISION NOT NULL,
    realized_pnl_usdt   DOUBLE PRECISION NOT NULL,
    unrealized_pnl_usdt DOUBLE PRECISION NOT NULL,
    position_side       TEXT,
    position_size_usdt  DOUBLE PRECISION NOT NULL,
    daily_drawdown_pct  DOUBLE PRECISION NOT NULL,
    consecutive_losses  INTEGER          NOT NULL,
    is_paused           BOOLEAN          NOT NULL,
    pause_until         TIMESTAMPTZ,
    cascade_cooling     BOOLEAN          NOT NULL
);

SELECT create_hypertable('sel_account_state', 'time', if_not_exists => TRUE);
