# cw4 现状探索报告（W1-PRE-B）

**日期**：2026-04-20  
**目的**：为 Helios v1.3 W1 改造准备 cw4 接入依据  
**路径**：`/mnt/d/project/cryptowatch`

---

## 1. 运行中的 cw4 服务（13 个容器）

| 容器名 | 状态 | 对外端口 |
|--------|------|---------|
| cryptowatch-gateway-1 | healthy 23h | **0.0.0.0:5000** |
| cryptowatch-data-service-1 | healthy 23h | **0.0.0.0:8001** |
| cryptowatch-signal-service-1 | healthy 23h | **0.0.0.0:8002** |
| cryptowatch-portfolio-service-1 | healthy 23h | **0.0.0.0:8003** |
| cryptowatch-risk-service-1 | healthy 23h | **0.0.0.0:8004** |
| cryptowatch-execution-service-1 | healthy 23h | **0.0.0.0:8005** |
| cryptowatch-monitoring-service-1 | healthy 23h | **0.0.0.0:8021** |
| cryptowatch-onchain-sentinel-1 | healthy 23h | **0.0.0.0:8020** |
| cryptowatch-onchain-eth-worker-1 | Up 23h | 内部 |
| cryptowatch-onchain-btc-worker-1 | Up 23h | 内部 |
| cryptowatch-onchain-sol-worker-1 | Up 23h | 内部 |
| cryptowatch-timescaledb-1 | healthy 23h | **0.0.0.0:5433**→5432 |
| cryptowatch-redis-1 | healthy 23h | **0.0.0.0:6380**→6379 |
| cryptowatch-frontend-1 | Up 23h | 0.0.0.0:8088→80 |

---

## 2. HTTP API 可达性（全 9 个 port 均返回 200）

```
port 5000  -> /health: HTTP 200  ← Gateway（主入口）
port 8001  -> /health: HTTP 200  ← data-service
port 8002  -> /health: HTTP 200  ← signal-service
port 8003  -> /health: HTTP 200  ← portfolio-service
port 8004  -> /health: HTTP 200  ← risk-service
port 8005  -> /health: HTTP 200  ← execution-service
port 8006  -> /health: HTTP 200
port 8020  -> /health: HTTP 200  ← onchain-sentinel
port 8021  -> /health: HTTP 200  ← monitoring-service
```

### Gateway 路由完整清单（port 5000）

```
GET  /api/v4/market                    市场快照（20 symbol 价格+资金费率）
GET  /api/v4/signals                   信号列表
GET  /api/v4/signals/pending           待确认信号
POST /api/v4/signals/{id}/confirm      确认信号
POST /api/v4/signals/{id}/reject       拒绝信号
GET  /api/v4/signals/weights           因子权重
GET  /api/v4/portfolio/state           组合持仓状态
GET  /api/v4/portfolio/pnl             组合盈亏
GET  /api/v4/regime                    全局 regime 分布
GET  /api/v4/regime/current            当前主导 regime
GET  /api/v4/regime/hmm/{symbol}       HMM regime 状态
GET  /api/v4/regime/fused              全部 symbol fused regime
GET  /api/v4/risk/status               风险状态
GET  /api/v4/risk/circuit-breaker      熔断状态
POST /api/v4/risk/circuit-breaker/reset 熔断重置
GET  /api/v4/orders                    订单列表
GET  /api/v4/execution/slippage/{sym}  滑点估算
GET  /api/v4/funding/opportunities     资金费率套利机会
GET  /api/v4/funding/positions         套利持仓
POST /api/v4/funding/execute/{sym}     执行套利
GET  /api/v4/backtest/wfo              WFO 回测
GET  /api/v4/moonshots                 Moonshot 标的
GET  /api/v4/onchain/state             链上全局状态
GET  /api/v4/onchain/state/{sym}       单 symbol 链上状态
GET  /api/v4/onchain/stats/{sym}       历史统计
GET  /api/v4/onchain/wallet/{address}  钱包行为分析
GET  /api/v4/onchain/summary/{sym}     链上摘要
GET  /api/v4/monitor/health            监控健康
GET  /api/v4/monitor/report            最新监控报告
POST /api/v4/monitor/trigger           手动触发报告
GET  /api/v4/monitor/recommendation    IC 趋势建议
GET  /api/v4/monitor/trend             IC 趋势数据
GET  /api/v4/system/overview           系统概览
GET  /api/v4/config/{module}           模块配置查询
```

---

## 3. Helios 可调用的关键 endpoint

当前 cw4 **实时可用**的 endpoint（全部 HTTP 200）：

| Helios 模块 | cw4 endpoint | 数据内容 | 状态 |
|------------|--------------|---------|------|
| B2 告警流·链上列 | `GET /api/v4/onchain/state` | BTC/ETH/SOL 链上评分、净交易所流量、近期鲸鱼事件 | ✅ 活跃（但 regime=UNKNOWN） |
| B2 告警流·链上列 | `GET /api/v4/onchain/stats/{sym}` | 历史信号统计（class/window 参数） | ✅ 可用 |
| B3 宏观·机构资金 | `GET /api/v4/funding/opportunities` | 资金费率套利机会 | ✅（当前无机会=[] 符合熊市） |
| /b 衍生品 | `GET /api/v4/market` | 20 symbol 资金费率+价格 | ✅ 实时 |
| /b 衍生品 | `GET /api/v4/signals/weights` | 因子权重（IC 加权） | ✅ 可用 |
| /structure 清算 | ❌ 无 /api/v4/liquidations | cw4 **没有**清算数据 endpoint | 缺失 |
| B3 宏观·ETF | ❌ 无 ETF flow endpoint | cw4 **没有** ETF 资金流数据 | 缺失 |

---

## 4. onchain-sentinel 深度盘点（port 8020）

### 架构

- 3 条链 3 个 worker（btc/eth/sol）→ `onchain.events` Redis stream
- `onchain-sentinel` 消费、评分、写 `signal.raw` + `system.alerts`
- HTTP 暴露 `/health` + `/api/v4/onchain/*`（被 gateway 聚合）

### 当前实时状态（2026-04-20）

```json
BTCUSDT: onchain_score=0.0, regime=UNKNOWN, cw4_win_prob=0.5553, fused_win_prob=0.5387
ETHUSDT: onchain_score=0.0, regime=UNKNOWN
SOLUSDT: onchain_score=0.0, regime=UNKNOWN
prices: BTC=$74,283  ETH=$2,271  SOL=$83.86
```

**问题**：regime 全是 UNKNOWN，说明 signal-service 的 HMM 还未产出 regime（缺数据或未运行充分）。`onchain_score=0` 说明有鲸鱼事件但被归类为 `whale_unknown`（地址未在已知库中），语义权重为 0。

### 支持的信号分类（scorer.py）

```
whale_inflow_exchange   -0.4  # 流入交易所 bearish
whale_outflow_exchange  +0.4  # 提出交易所 bullish
whale_unknown            0.0
miner_sell              -0.3
miner_accumulate        +0.2
smart_wallet_long       +0.5
smart_wallet_exit       -0.5
dormant_wake            -0.2
net_exchange_outflow    +0.35
net_exchange_inflow     -0.35
rune_volume_spike       +0.15
```

### 推送阈值

- STRONG：fused_win_prob ≥ 0.62 → Telegram + 企微 + 飞书
- WATCH：fused_win_prob ≥ 0.56 → Telegram

---

## 5. TimescaleDB Schema

**连接**：`localhost:5433`  用户: `cw4`  库: `cw4`

### 表清单

| 表 | 类型 | 有数据 | 说明 |
|----|------|--------|------|
| candles | hypertable (5 chunks) | ✅ 168,000 行 | 20 symbol × 1h，覆盖 2026-04-08 ~ 04-19 |
| funding_rates | hypertable (0 chunks) | ❌ 空 | 资金费率历史（只有实时 Redis） |
| open_interest | hypertable (0 chunks) | ❌ 空 | OI 历史（只有实时 Redis） |
| portfolio_snapshots | hypertable (0 chunks) | ❌ 空 | 组合快照 |
| signals | 普通表 | ❌ 0 行 | 交易信号记录 |
| orders | 普通表 | ❌ 0 行 | 订单记录 |
| positions | 普通表 | ❌ 0 行 | 持仓记录 |
| audit_log | 普通表 | 未查 | 审计日志 |

### candles schema（关键）

```sql
time         TIMESTAMPTZ NOT NULL
symbol       VARCHAR(20) NOT NULL
interval     VARCHAR(8)  NOT NULL    -- 仅 "1h"
open/high/low/close  NUMERIC(20,8)
volume       NUMERIC(20,8)
quote_volume NUMERIC(20,8)
is_anomaly   BOOLEAN
gap_before   BOOLEAN
```

**Helios 可直接读 candles hypertable** 获取 12 天历史 K 线。

---

## 6. Docker 网络

- cw4 网络名：`cryptowatch_cw4`（bridge 模式）
- cw4 内部 17 个容器互联，含 prometheus + grafana

### kanpan-api 当前网络

```
kanpan-net          ← kanpan 内部网络
quant-stack_quant-net ← 已接入 quant-stack
```

**kanpan-api 未在 `cryptowatch_cw4` 网络中**，因此无法通过容器名访问 cw4 服务。

### 接入方案

| 方案 | 实现 | 风险 |
|------|------|------|
| **A. HTTP localhost（推荐）** | kanpan 直接调用 `localhost:5000`（cw4 gateway） | 零风险，已验证 200 |
| B. 加入 cw4 网络 | docker-compose 加 `networks: - cryptowatch_cw4` | 需重启 kanpan-api |
| C. Cloudflare Tunnel 互联 | 适合跨机器 | 延迟+复杂度 |

**推荐方案 A**：cw4 gateway 已绑定 `0.0.0.0:5000`，WSL2 内 localhost 直通，无需修改任何网络配置。

---

## 7. cw4 Redis

- 地址：`localhost:6380`（容器内 `redis:6379`）
- kanpan 用 `localhost:6381`（kanpan-redis），两个 Redis 完全独立
- cw4 Redis key 前缀：`onchain:state:*`, `cw4:monitor:*`
- 直接读 cw4 Redis 可绕过 HTTP 获取实时状态（可选，非必须）

---

## 8. 集成可行性评估

### 方案 A：HTTP Only（最小接入）

**工作量**：1-2 天  
**步骤**：kanpan-api 新增 `cw4_client.py`，封装 `GET localhost:5000/api/v4/*`  
**风险**：低。cw4 gateway 已稳定运行 23h+，所有 endpoint 200。  
**适合**：Helios B2 链上告警、B3 资金费率、市场快照。

### 方案 B：Postgres 直读 hypertable

**工作量**：0.5 天（仅需连接字符串）  
**步骤**：`postgresql://cw4:xxx@localhost:5433/cw4`，直接 SELECT candles  
**风险**：低。candles 有 168k 行实际数据，schema 清晰。  
**适合**：Helios 需要历史 K 线做回测或趋势计算时。

### 我的推荐：A（HTTP）为主，B（Postgres）按需补充

原因：HTTP 方案零侵入、零风险，cw4 gateway 已聚合所有数据；Postgres 只在需要批量历史数据时才接入。

---

## 9. 缺失的 cw4 API（需新增）

Helios v1.3 PRD 需要但 cw4 当前无法提供：

| Helios 需求 | 缺失 endpoint | 建议新增位置 |
|------------|--------------|------------|
| /structure 清算热力图 | `GET /api/v4/liquidations?symbol=BTC&window=24h` | cw4 data-service 或直接 Binance API |
| B3 ETF 资金流 | `GET /api/v4/etf/flows?asset=BTC` | cw4 新增 ETF collector（复杂度高） |
| 鲸鱼告警流（实时） | `GET /api/v4/onchain/alerts/stream` | onchain-sentinel 添加 SSE/websocket |
| B2 鲸鱼告警历史列表 | `GET /api/v4/onchain/alerts?since=&limit=` | onchain-sentinel 添加（historian 已有 SQLite 存储） |

**最紧急**：`/api/v4/onchain/alerts` — historian.py 里 SQLite 已记录历史，只需加一个 GET 路由。

---

## 10. 立即可用的 Helios 接入清单

```python
# kanpan 可直接调用（无需改动 cw4）
CW4_BASE = "http://localhost:5000"

# B2 链上告警（当前有事件，但 score=0 因 regime=UNKNOWN）
GET  {CW4_BASE}/api/v4/onchain/state          # 实时链上评分
GET  {CW4_BASE}/api/v4/onchain/state/{symbol} # 单 symbol

# B3 资金费率（有 20 symbol 实时数据）
GET  {CW4_BASE}/api/v4/market                 # 资金费率 + 价格

# 资金费率套利机会
GET  {CW4_BASE}/api/v4/funding/opportunities

# 因子信号与权重
GET  {CW4_BASE}/api/v4/signals/weights
GET  {CW4_BASE}/api/v4/signals?status=pending

# 系统状态
GET  {CW4_BASE}/api/v4/system/overview
GET  {CW4_BASE}/api/v4/regime/current
```
