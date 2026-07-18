-- CryptoWatch v4 — TimescaleDB Schema
-- Run once on fresh DB. Idempotent (IF NOT EXISTS everywhere).

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ── Raw OHLCV candles (time-series, partitioned by time) ─────────────────────
CREATE TABLE IF NOT EXISTS candles (
    time          TIMESTAMPTZ   NOT NULL,
    symbol        VARCHAR(20)   NOT NULL,
    interval      VARCHAR(8)    NOT NULL,
    open          DECIMAL(20,8) NOT NULL,
    high          DECIMAL(20,8) NOT NULL,
    low           DECIMAL(20,8) NOT NULL,
    close         DECIMAL(20,8) NOT NULL,
    volume        DECIMAL(20,8) NOT NULL,
    quote_volume  DECIMAL(20,8),
    is_anomaly    BOOLEAN       DEFAULT FALSE,
    gap_before    BOOLEAN       DEFAULT FALSE
);
SELECT create_hypertable('candles', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_candles_sym_int ON candles (symbol, interval, time DESC);
-- signal-service 落库 ON CONFLICT 去重依赖(scanner 重启重播种、消费组重投递都会重复)
CREATE UNIQUE INDEX IF NOT EXISTS uq_candles_sym_int_time ON candles (symbol, interval, time);

-- Continuous aggregate: 1h from 1m
CREATE MATERIALIZED VIEW IF NOT EXISTS candles_1h
WITH (timescaledb.continuous) AS
    SELECT time_bucket('1 hour', time) AS time,
           symbol,
           first(open, time)  AS open,
           max(high)          AS high,
           min(low)           AS low,
           last(close, time)  AS close,
           sum(volume)        AS volume
    FROM candles
    WHERE interval = '1m'
    GROUP BY time_bucket('1 hour', time), symbol
WITH NO DATA;

-- Continuous aggregate: 4h from 1h
CREATE MATERIALIZED VIEW IF NOT EXISTS candles_4h
WITH (timescaledb.continuous) AS
    SELECT time_bucket('4 hours', time) AS time,
           symbol,
           first(open, time)  AS open,
           max(high)          AS high,
           min(low)           AS low,
           last(close, time)  AS close,
           sum(volume)        AS volume
    FROM candles_1h
    GROUP BY time_bucket('4 hours', time), symbol
WITH NO DATA;

-- ── Funding rates ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS funding_rates (
    time          TIMESTAMPTZ   NOT NULL,
    symbol        VARCHAR(20)   NOT NULL,
    rate_pct      DECIMAL(10,6) NOT NULL,
    is_anomaly    BOOLEAN       DEFAULT FALSE
);
SELECT create_hypertable('funding_rates', 'time', if_not_exists => TRUE);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fr_sym_time ON funding_rates (symbol, time DESC);

-- ── Open Interest history ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS open_interest (
    time          TIMESTAMPTZ   NOT NULL,
    symbol        VARCHAR(20)   NOT NULL,
    oi_usd        DECIMAL(20,2) NOT NULL,
    oi_change_pct DECIMAL(8,4)
);
SELECT create_hypertable('open_interest', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_oi_sym ON open_interest (symbol, time DESC);

-- ── Signals ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS signals (
    id               UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol           VARCHAR(20)   NOT NULL,
    regime           VARCHAR(20)   NOT NULL,
    signal_type      VARCHAR(32)   NOT NULL,
    direction        VARCHAR(8)    NOT NULL,
    win_probability  DECIMAL(5,4)  NOT NULL,
    confidence_lo    DECIMAL(5,4),
    confidence_hi    DECIMAL(5,4),
    expected_return  DECIMAL(8,6),
    factor_scores    JSONB,
    regime_adjusted  BOOLEAN       DEFAULT FALSE,
    entry_price      DECIMAL(20,8),
    stop_loss        DECIMAL(20,8),
    take_profit      DECIMAL(20,8),
    max_hold_hours   INT,
    data_quality     DECIMAL(4,3),
    status           VARCHAR(16)   DEFAULT 'pending',
    created_at       TIMESTAMPTZ   DEFAULT NOW(),
    confirmed_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_signals_sym     ON signals (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_type    ON signals (signal_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_status  ON signals (status);

-- ── Orders ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS orders (
    id             UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    signal_id      UUID          REFERENCES signals(id),
    symbol         VARCHAR(20)   NOT NULL,
    exchange       VARCHAR(16)   NOT NULL,
    side           VARCHAR(8)    NOT NULL,
    order_type     VARCHAR(16)   NOT NULL,
    quantity       DECIMAL(20,8) NOT NULL,
    limit_price    DECIMAL(20,8),
    stop_price     DECIMAL(20,8),
    take_profit    DECIMAL(20,8),
    filled_price   DECIMAL(20,8),
    filled_qty     DECIMAL(20,8),
    slippage_pct   DECIMAL(8,6),
    fee_paid       DECIMAL(20,8),
    state          VARCHAR(32)   NOT NULL,
    exchange_id    VARCHAR(64),
    kelly_fraction DECIMAL(8,6),
    risk_usd       DECIMAL(20,2),
    reject_reason  TEXT,
    close_reason   TEXT,
    realized_pnl   DECIMAL(20,8),
    created_at     TIMESTAMPTZ   DEFAULT NOW(),
    closed_at      TIMESTAMPTZ
);
-- Backfill column for existing deployments (CREATE TABLE IF NOT EXISTS won't add it). (item #7)
ALTER TABLE orders ADD COLUMN IF NOT EXISTS take_profit DECIMAL(20,8);
CREATE INDEX IF NOT EXISTS idx_orders_sym   ON orders (symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders (state);
CREATE INDEX IF NOT EXISTS idx_orders_sig   ON orders (signal_id);

-- ── Positions ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS positions (
    id              UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol          VARCHAR(20)   NOT NULL,
    side            VARCHAR(8)    NOT NULL,
    entry_price     DECIMAL(20,8) NOT NULL,
    quantity        DECIMAL(20,8) NOT NULL,
    stop_loss       DECIMAL(20,8),
    take_profit     DECIMAL(20,8),
    strategy        VARCHAR(32),
    signal_id       UUID,
    status          VARCHAR(16)   DEFAULT 'open',
    unrealized_pnl  DECIMAL(20,8) DEFAULT 0,
    realized_pnl    DECIMAL(20,8),
    exit_price      DECIMAL(20,8),
    close_reason    TEXT,
    opened_at       TIMESTAMPTZ   DEFAULT NOW(),
    closed_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pos_sym    ON positions (symbol, status);
CREATE INDEX IF NOT EXISTS idx_pos_status ON positions (status, opened_at DESC);

-- ── Portfolio snapshots (time-series) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    time              TIMESTAMPTZ   NOT NULL,
    total_equity      DECIMAL(20,2) NOT NULL,
    available_capital DECIMAL(20,2),
    total_exposure    DECIMAL(20,2),
    leverage          DECIMAL(6,3),
    var_95            DECIMAL(20,2),
    cvar_95           DECIMAL(20,2),
    current_drawdown  DECIMAL(6,4),
    max_drawdown      DECIMAL(6,4),
    drawdown_level    VARCHAR(10),
    daily_pnl         DECIMAL(20,2),
    strategy_alloc    JSONB,
    PRIMARY KEY (time)
);
SELECT create_hypertable('portfolio_snapshots', 'time', if_not_exists => TRUE);

-- ── Audit log (immutable, append-only) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL     PRIMARY KEY,
    time       TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    event_type VARCHAR(32)   NOT NULL,
    entity_id  UUID,
    payload    JSONB         NOT NULL,
    service    VARCHAR(32)
);
CREATE INDEX IF NOT EXISTS idx_audit_time  ON audit_log (time DESC);
CREATE INDEX IF NOT EXISTS idx_audit_type  ON audit_log (event_type, time DESC);

-- Revoke delete on audit_log to enforce immutability
REVOKE DELETE ON audit_log FROM PUBLIC;
