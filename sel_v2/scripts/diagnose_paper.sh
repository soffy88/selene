#!/usr/bin/env bash
# Diagnose whether sel_v2 paper trading is producing — and persisting — S1/S2 trades.
#
# Answers "S1 有交易吗 / 为什么前端看不到":
#   - Is the data preflight satisfied (≥180 bars, strategy params, LOB)?
#   - Does v2_trades actually contain S1/S2 rows (open + closed)?
#   - Do the API endpoints the frontend *could* call return them?
#   - Are there persist failures / "no ticks" / cold-start markers in the engine log?
#
# Run on the host where docker-compose is deployed:
#     bash sel_v2/scripts/diagnose_paper.sh
# Overrides:
#     GATEWAY_URL=http://localhost:5000  DC="docker compose"  bash sel_v2/scripts/diagnose_paper.sh
set -uo pipefail

cd "$(dirname "$0")/../.." || exit 1            # repo root
[ -f .env ] && { set -a; . ./.env; set +a; }   # load SELENE_APP_PASSWORD etc.

DC="${DC:-docker compose}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:5000}"
PGPW="${SELENE_APP_PASSWORD:-}"

psql_run() {  # pipe SQL on stdin into the platform-postgres container as selene_app
  $DC exec -T -e PGPASSWORD="$PGPW" platform-postgres \
    psql -U selene_app -d selene -P pager=off -v ON_ERROR_STOP=0 "$@"
}

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  sel_v2 paper 诊断                                            ║"
echo "╚══════════════════════════════════════════════════════════════╝"

echo
echo "════ 1+2. 前置数据 & v2_trades 按策略 ════"
psql_run <<'SQL'
\echo '── 前置数据 (paper_startup 的 _CHECKS) ──'
SELECT count(*) AS v2_bars_4h_rows  FROM v2_bars_4h;            -- 需 >= 180
SELECT count(*) AS v2_lob_snapshots FROM v2_lob_snapshots;     -- S2 需 > 0
SELECT coalesce(string_agg(param_key, ', '), '(空)') AS strategy_params FROM v2_strategy_params;
\echo '── v2_trades 按策略 (这就是答案: S1 有没有交易) ──'
SELECT strategy,
       count(*)                                   AS total,
       count(*) FILTER (WHERE exit_time IS NULL)  AS open_now,
       count(*) FILTER (WHERE exit_time IS NOT NULL) AS closed,
       round(coalesce(sum(pnl_usdt),0)::numeric, 2)  AS pnl_usdt
FROM v2_trades GROUP BY strategy ORDER BY strategy;
\echo '── 最近 5 笔 ──'
SELECT strategy, direction, entry_time, exit_time, round(pnl_usdt::numeric,2) AS pnl
FROM v2_trades ORDER BY entry_time DESC LIMIT 5;
\echo '── 近 100 bar 状态分布 (上半状态机是否被现货/STUB 卡住) ──'
SELECT state, count(*) FROM (
  SELECT state FROM v2_state_history ORDER BY timestamp DESC LIMIT 100
) s GROUP BY state ORDER BY 2 DESC;
SQL

echo
echo "════ 3. API 端点 (前端 *能* 调但目前没调的) ════"
echo "→ GET /api/v2/sel/trades/recent"
curl -s --max-time 8 "$GATEWAY_URL/api/v2/sel/trades/recent?limit=5" || echo "  (gateway 不可达: $GATEWAY_URL)"
echo
echo "→ GET /api/v2/sel/trades/open"
curl -s --max-time 8 "$GATEWAY_URL/api/v2/sel/trades/open" || echo "  (gateway 不可达)"
echo

echo
echo "════ 4. paper 引擎日志关键字 ════"
$DC logs --tail=3000 v2-paper-engine 2>/dev/null \
  | grep -iE "failed to persist|ENTER_LONG|ENTER_SHORT|exit|no ticks|GAP|v2_bars_4h is empty|cold|disabled|s2_enabled" \
  | tail -25 \
  || echo "  (取不到 v2-paper-engine 日志)"

echo
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  怎么读结果                                                   ║"
echo "╚══════════════════════════════════════════════════════════════╝"
cat <<'TXT'
  • 步骤2 v2_trades 有 strategy_1 行  → S1 在交易，问题纯在前端没界面 → 我去补交易面板(第2步)。
  • v2_trades 空，但 v2_bars_4h < 180 → 数据不足/冷启动未完成 → 先回填:
        python -m sel_v2.data.okx_backfill --symbol BTC-USDT --years 2
  • v2_trades 空，bars 够，但 strategy_params 为(空) → 没跑校准 → 先跑:
        python -m sel_v2.offline.calibrate_all --from-db
  • 日志出现 "failed to persist" → 交易算出来了但写库失败(schema/权限) → 把那行贴我。
  • 状态分布只有 Drifting_Calm/Coiling/Critical，没有 Surging_* → 上半状态机因 LOB/OFI
    数据未流入而不可达(S1 仍可在 Coiling/Drifting-Charged 入场，不影响 S1 有无交易)。
TXT
