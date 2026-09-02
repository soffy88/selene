"""
onchain_sentinel/services/onchain/historian.py

P2: 历史回测验证层
职责：
  - 记录每一条链上预警 + 触发时的价格背景
  - 48/72h 后回填实际价格结果
  - 计算同类事件的历史胜率（"过去90天同类信号后，BTC 1h 涨幅均值 +2.3%，胜率 61%"）
  - 写入 SQLite（本地，不依赖 TimescaleDB）

数据库文件：~/.chain_sentinel/onchain_history.db
"""

import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("onchain.historian")

DB_PATH = Path.home() / ".chain_sentinel" / "onchain_history.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 价格结果回填窗口
OUTCOME_WINDOWS_H = [1, 4, 24, 72]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建表，幂等"""
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS onchain_alerts (
        id            TEXT PRIMARY KEY,
        symbol        TEXT NOT NULL,
        chain         TEXT NOT NULL,
        signal_class  TEXT NOT NULL,    -- whale_inflow_exchange / smart_wallet_long / ...
        severity      TEXT NOT NULL,
        amount_usd    REAL NOT NULL,
        onchain_score REAL NOT NULL,    -- score at trigger time
        regime        TEXT NOT NULL,    -- cw4 regime at trigger time
        win_prob_cw4  REAL,             -- cw4 win_probability at trigger time
        fused_prob    REAL,             -- final fused probability
        price_at_alert REAL NOT NULL,  -- BTC/ETH price at trigger
        ts            REAL NOT NULL,    -- unix timestamp
        -- Outcome fields (filled in by backfill job)
        price_1h      REAL,
        price_4h      REAL,
        price_24h     REAL,
        price_72h     REAL,
        ret_1h        REAL,             -- (price_1h - price_at_alert) / price_at_alert
        ret_4h        REAL,
        ret_24h       REAL,
        ret_72h       REAL,
        direction     TEXT,             -- LONG / SHORT（基于 signal_class 判断）
        outcome_1h    INTEGER,          -- 1=win, 0=loss (NULL=pending)
        outcome_4h    INTEGER,
        outcome_24h   INTEGER,
        outcome_72h   INTEGER,
        backfill_done INTEGER DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_symbol_class ON onchain_alerts(symbol, signal_class);
    CREATE INDEX IF NOT EXISTS idx_ts           ON onchain_alerts(ts);
    CREATE INDEX IF NOT EXISTS idx_backfill     ON onchain_alerts(backfill_done, ts);

    CREATE TABLE IF NOT EXISTS wallet_history (
        id           TEXT PRIMARY KEY,
        address      TEXT NOT NULL,
        chain        TEXT NOT NULL,
        action_class TEXT NOT NULL,
        amount_usd   REAL,
        symbol       TEXT,
        price_at_action REAL,
        price_24h    REAL,
        pnl_pct_24h  REAL,
        win_24h      INTEGER,
        ts           REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_wallet_addr ON wallet_history(address, chain);
    """)
    conn.commit()
    conn.close()
    logger.info(f"SQLite DB initialized: {DB_PATH}")


# ── 记录预警 ──────────────────────────────────────────
def record_alert(
    alert_id: str,
    symbol: str,
    chain: str,
    signal_class: str,
    severity: str,
    amount_usd: float,
    onchain_score: float,
    regime: str,
    price_at_alert: float,
    win_prob_cw4: Optional[float] = None,
    fused_prob: Optional[float] = None,
):
    direction = _infer_direction(signal_class)
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO onchain_alerts
            (id, symbol, chain, signal_class, severity, amount_usd,
             onchain_score, regime, win_prob_cw4, fused_prob,
             price_at_alert, ts, direction)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                alert_id,
                symbol,
                chain,
                signal_class,
                severity,
                amount_usd,
                onchain_score,
                regime,
                win_prob_cw4,
                fused_prob,
                price_at_alert,
                time.time(),
                direction,
            ),
        )
        conn.commit()
    except Exception as e:
        logger.error(f"record_alert error: {e}")
    finally:
        conn.close()


def _infer_direction(signal_class: str) -> str:
    bullish = {"whale_outflow_exchange", "smart_wallet_long", "miner_accumulate", "net_exchange_outflow"}
    bearish = {"whale_inflow_exchange", "smart_wallet_exit", "miner_sell", "dormant_wake", "net_exchange_inflow"}
    if signal_class in bullish:
        return "LONG"
    if signal_class in bearish:
        return "SHORT"
    return "NEUTRAL"


# ── 价格结果回填 ──────────────────────────────────────
async def backfill_outcomes(get_price_fn) -> int:
    """
    扫描未回填的预警，尝试填入实际价格结果。
    get_price_fn: async fn(symbol, ts_unix) -> float | None
    返回本次回填的记录数。
    """
    now = time.time()
    conn = get_db()
    filled = 0

    try:
        rows = conn.execute(
            """
            SELECT * FROM onchain_alerts
            WHERE backfill_done = 0 AND ts < ?
            ORDER BY ts ASC LIMIT 200
        """,
            (now - 3600,),
        ).fetchall()  # 至少等1小时再尝试回填

        for row in rows:
            alert_id = row["id"]
            ts = row["ts"]
            symbol = row["symbol"]
            price0 = row["price_at_alert"]
            direction = row["direction"]

            updates = {}
            all_done = True

            for window_h in OUTCOME_WINDOWS_H:
                target_ts = ts + window_h * 3600
                if target_ts > now:
                    all_done = False
                    continue  # 还没到回填时间

                col = f"price_{window_h}h"
                if row[col] is not None:
                    continue  # 已经填过

                price = await get_price_fn(symbol, target_ts)
                if price is None:
                    all_done = False
                    continue

                ret = (price - price0) / price0 if price0 > 0 else 0.0
                # 判断胜负（根据方向）
                if direction == "LONG":
                    outcome = 1 if ret > 0.005 else 0  # 涨超0.5% = win
                elif direction == "SHORT":
                    outcome = 1 if ret < -0.005 else 0  # 跌超0.5% = win
                else:
                    outcome = None

                updates[col] = price
                updates[f"ret_{window_h}h"] = round(ret, 6)
                updates[f"outcome_{window_h}h"] = outcome

            if updates:
                updates["backfill_done"] = 1 if all_done else 0
                set_clause = ", ".join(f"{k}=?" for k in updates)
                conn.execute(f"UPDATE onchain_alerts SET {set_clause} WHERE id=?", list(updates.values()) + [alert_id])
                conn.commit()
                filled += 1

    except Exception as e:
        logger.error(f"backfill_outcomes error: {e}")
    finally:
        conn.close()

    if filled:
        logger.info(f"backfill: {filled} records updated")
    return filled


# ── 历史胜率查询（P3 推送用）─────────────────────────
def get_signal_stats(
    symbol: str,
    signal_class: str,
    window_h: int = 24,
    lookback_days: int = 90,
) -> dict:
    """
    查询过去 N 天同类信号的统计结果。
    返回格式供 Dashboard 和推送消息直接使用。
    """
    cutoff = time.time() - lookback_days * 86400
    outcome_col = f"outcome_{window_h}h"
    ret_col = f"ret_{window_h}h"

    conn = get_db()
    try:
        rows = conn.execute(
            f"""
            SELECT {outcome_col}, {ret_col}
            FROM onchain_alerts
            WHERE symbol=? AND signal_class=? AND ts>?
              AND {outcome_col} IS NOT NULL
            ORDER BY ts DESC LIMIT 200
        """,
            (symbol, signal_class, cutoff),
        ).fetchall()

        if not rows:
            return {"n": 0, "win_rate": None, "avg_ret": None, "signal_class": signal_class, "window_h": window_h}

        n = len(rows)
        wins = sum(1 for r in rows if r[outcome_col] == 1)
        rets = [r[ret_col] for r in rows if r[ret_col] is not None]
        avg_r = sum(rets) / len(rets) if rets else 0.0

        return {
            "n": n,
            "win_rate": round(wins / n, 3),
            "avg_ret_pct": round(avg_r * 100, 2),
            "signal_class": signal_class,
            "window_h": window_h,
            "lookback_days": lookback_days,
            "summary": f"过去{lookback_days}天 {n}次 | 胜率{wins / n:.0%} | 均值{avg_r * 100:+.2f}%",
        }
    finally:
        conn.close()


def get_wallet_stats(address: str, chain: str) -> dict:
    """聪明钱包历史胜率"""
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT pnl_pct_24h, win_24h FROM wallet_history
            WHERE address=? AND chain=? AND win_24h IS NOT NULL
            ORDER BY ts DESC LIMIT 100
        """,
            (address, chain),
        ).fetchall()

        if not rows:
            return {"address": address, "n": 0, "win_rate": None}

        n = len(rows)
        wins = sum(1 for r in rows if r["win_24h"] == 1)
        pnls = [r["pnl_pct_24h"] for r in rows if r["pnl_pct_24h"] is not None]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0.0

        return {
            "address": address,
            "chain": chain,
            "n": n,
            "win_rate": round(wins / n, 3),
            "avg_pnl_pct": round(avg_pnl * 100, 2),
            "summary": f"本地记录 {n}笔 | 胜率{wins / n:.0%} | 均值PnL{avg_pnl * 100:+.2f}%",
        }
    finally:
        conn.close()


def get_daily_summary(symbol: str, lookback_days: int = 7) -> list:
    """最近 N 天每天的链上活跃度"""
    cutoff = time.time() - lookback_days * 86400
    conn = get_db()
    try:
        rows = conn.execute(
            """
            SELECT
                date(ts, 'unixepoch') as day,
                COUNT(*) as n_alerts,
                SUM(amount_usd) as total_usd,
                AVG(onchain_score) as avg_score,
                SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) as n_critical
            FROM onchain_alerts
            WHERE symbol=? AND ts>?
            GROUP BY day ORDER BY day DESC
        """,
            (symbol, cutoff),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── 初始化
init_db()
