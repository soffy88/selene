-- Isolated qualification schema. Vanilla PostgreSQL (no TimescaleDB, no Helios).
-- Dummy credentials only. Idempotent.

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
    gap_before    BOOLEAN       DEFAULT FALSE,
    PRIMARY KEY (symbol, interval, time)
);

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

CREATE TABLE IF NOT EXISTS risk_state (
    id           TEXT PRIMARY KEY,
    halted       BOOLEAN NOT NULL DEFAULT FALSE,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    event_type TEXT,
    entity_id  TEXT,
    payload    JSONB,
    service    TEXT,
    time       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    time              TIMESTAMPTZ   NOT NULL PRIMARY KEY,
    total_equity      DECIMAL(20,2) NOT NULL,
    available_capital DECIMAL(20,2),
    total_exposure    DECIMAL(20,2),
    leverage          DECIMAL(6,3),
    var_95            DECIMAL(20,2),
    cvar_95           DECIMAL(20,2),
    current_drawdown  DECIMAL(6,4),
    max_drawdown      DECIMAL(6,4),
    drawdown_level    VARCHAR(10),
    daily_pnl         DECIMAL(20,8),
    strategy_alloc    JSONB
);
