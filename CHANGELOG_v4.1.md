# CryptoWatch v4.1 — Onchain Sentinel 集成变更记录

## 新增文件

| 文件 | 说明 |
|------|------|
| `services/onchain/__init__.py` | 包初始化 |
| `services/onchain/main.py` | **主服务**：Redis 桥接 + 评分融合 + 推送决策 |
| `services/onchain/scorer.py` | **OnchainScorer**：EWMA 评分引擎，regime 调整 |
| `services/onchain/historian.py` | **SQLite 历史层**：记录预警 + 回填结果 + 胜率查询 |
| `services/onchain/btc/worker.py` | BTC 采集：Mempool.space WSS，无需 Key |
| `services/onchain/eth/worker.py` | ETH 采集：Infura WSS + ERC-20 大额扫描 |
| `services/onchain/sol/worker.py` | SOL 采集：Helius WSS，生产者-消费者解耦 |
| `services/signal/onchain/bridge.py` | Signal-service 集成指南 + consumer 模板 |

## 修改文件

### `shared/events/streams.py`
- 新增 `STREAM_ONCHAIN_EVENTS = "onchain.events"`
- 新增 `CG_ONCHAIN = "onchain-sentinel"`
- `MAXLEN` 加入 `onchain.events: 50_000`

### `services/signal/factors/composite.py`
- 末尾追加 `get_onchain_factor(symbol)` 异步函数
- 末尾追加 `get_onchain_composite(symbol)` 异步函数
- **`MultiFactorScorer` 本身零改动**，`onchain` 权重槽（10%）原本就存在

### `services/gateway/main.py`
- 新增 `_on_onchain` WebSocket 广播 handler
- lifespan 加入 `STREAM_ONCHAIN_EVENTS` 消费者
- 新增 5 个 REST 路由：
  - `GET /api/v4/onchain/state` — 所有 symbol 状态
  - `GET /api/v4/onchain/state/{symbol}` — 单 symbol 状态
  - `GET /api/v4/onchain/stats/{symbol}` — 历史胜率查询
  - `GET /api/v4/onchain/wallet/{address}` — 聪明钱包画像
  - `GET /api/v4/onchain/summary/{symbol}` — 日度活跃度汇总

### `services/notification/hub.py`
- `_on_alert` 加入 `onchain_signal` 分支处理
- STRONG 级别直接推 Telegram + DingTalk

### `docker-compose.yml`
- 新增 4 个服务：`onchain-sentinel` / `onchain-btc-worker` / `onchain-eth-worker` / `onchain-sol-worker`
- 新增 volume：`onchain_history`（SQLite 持久化）

### `.env.example`
- 新增：`INFURA_KEY` / `HELIUS_KEY` / `WX_WEBHOOK` / `FEISHU_WEBHOOK`
- 新增：`THRESHOLD_BTC/ETH/SOL/MINER` / `PUSH_THRESHOLD_STRONG/WATCH`

---

## 数据流全景

```
外部数据源                    cw4 内部 Redis Streams
─────────────────────────────────────────────────────────────────

Mempool.space WSS ──→ btc/worker ──┐
Infura WSS        ──→ eth/worker ──┤──→ [onchain.events]
Helius WSS        ──→ sol/worker ──┘         │
                                             ▼
                                   onchain/main.py
                                   ├─ OnchainScorer.ingest()
                                   │    EWMA + magnitude + sev_mult
                                   │    → score [-1, +1]
                                   │
                                   ├─ historian.record_alert()
                                   │    SQLite: 价格背景 + 触发时评分
                                   │
                                   ├─ SET onchain:state:{sym}
                                   │    ← signal-service 读 onchain factor
                                   │    ← gateway /api/v4/onchain/state 读
                                   │
                                   ├─ XADD [signal.raw]
                                   │    source=onchain-sentinel
                                   │    → 触发 signal-service 重新评分
                                   │
                                   └─ 读 [signal.scored]
                                        regime + win_probability 反馈
                                              │
                                        fused_prob = 0.7×cw4 + 0.3×onchain
                                              │
                              ┌───────────────┴──────────────────┐
                              │ ≥ 0.62 STRONG                    │ 0.56~0.62 WATCH
                              ▼                                   ▼
                    XADD [system.alerts]               XADD [system.alerts]
                    + 企微/飞书直推                     （Telegram 普通消息）
                              │
                    NotificationHub 消费
                    → Telegram + DingTalk

signal-service 评分循环：
  onchain_score = await get_onchain_factor(symbol)  ← 从 onchain:state 读
  factors = FactorScores(..., onchain=onchain_score, ...)
  scored = scorer.score("LONG", factors)             ← onchain 占 10% 权重
  XADD [signal.scored]  win_probability + regime + CI

gateway WebSocket：
  订阅 [onchain.events] → broadcast type:"onchain" → 前端实时预警流
  GET /api/v4/onchain/stats/BTCUSDT?class=whale_inflow_exchange&window=24
  → {"n":47, "win_rate":0.638, "avg_ret_pct":1.8, "summary":"过去90天 47次 | 胜率64% | 均值+1.8%"}
```

---

## 启动命令

```bash
# 完整启动（首次）
docker compose up -d

# 仅启动 onchain 相关服务（已有服务运行时）
docker compose up -d onchain-sentinel onchain-btc-worker onchain-eth-worker onchain-sol-worker

# 验证
curl http://localhost:8020/health
curl http://localhost:5000/api/v4/onchain/state | jq .
curl "http://localhost:5000/api/v4/onchain/stats/BTCUSDT?class=whale_inflow_exchange"

# 查看 Redis Stream 写入
docker compose exec redis redis-cli -a changeme XLEN onchain.events
docker compose exec redis redis-cli -a changeme XREVRANGE onchain.events + - COUNT 3
```

---

## signal-service 接入（最后一步）

找到你 signal-service 里组装 `FactorScores` 的位置，做两处改动：

```python
# 1. import（加在文件顶部）
from services.signal.factors.composite import get_onchain_factor

# 2. 在组装 FactorScores 前加一行
onchain_score = await get_onchain_factor(symbol)

# 3. 填入 FactorScores（原来是 0.0）
factors = FactorScores(
    technical_rsi  = score_rsi(rsi),
    technical_ema  = score_ema_alignment(...),
    funding_zscore = score_funding_zscore(...),
    oi_momentum    = score_oi_momentum(...),
    lsr_divergence = score_lsr_divergence(...),
    onchain        = onchain_score,   # ← 这里
    social         = 0.0,
    orderbook      = 0.0,
)
```

如需 onchain factor 实时触发重评（而不是等下次循环），
参考 `services/signal/onchain/bridge.py` 中的 `consume_onchain_factor_updates`。
