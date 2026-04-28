# Collector 部署现状审计

**审计日期**：2026-04-28  
**审计人**：Claude Code (Task 2.0)  
**前置状态**：sel_engine 层 Task 1.5–1.9 全部完成；三个 collector 均已在跑。

---

## 总览

| Collector | 代码完整 | Schema | Compose | 在跑 | 数据写入中 | 建议 |
|---|---|---|---|---|---|---|
| orderbook_collector | ✅ | ✅ | ✅ | ✅ (unhealthy=EC-01) | ✅ 59行/1H | 已就绪，确认 EC-02 后可直接用 |
| trade_flow_collector | ✅ | N/A (Redis-only) | ✅ | ✅ (unhealthy=EC-01) | ✅ 3bar/Redis | 已就绪 |
| oi_persister | ✅ | ✅ | ✅ | ✅ (unhealthy=EC-01) | ✅ 12行/1H | 已就绪 |

**结论：三个 collector 全部就绪在跑，数据正在写入。阻塞数据进入 sel_engine 的是 EC-04（bar-close 调度器缺失），而非 collector 本身。**

---

## orderbook_collector

### 代码完整度

**文件路径**：`sel_engine/collectors/orderbook_collector.py`（单文件，115 行）  
**入口**：`services/sel_collectors/run_orderbook.py`

**实现状态**：✅ 完整实装

- 连接 OKX REST API：`GET /api/v5/market/books?instId=BTC-USDT-SWAP&sz=20`
- 每 60s 采样一次盘口（SAMPLE_INTERVAL_SECS = 60）
- 调用 `compute_orderbook_entropy(bids)` 计算 H
- 调用 `compute_depth_features(bids, asks)` 计算 top_5_bid/ask, spread_bps
- 结果写入 Redis（`sel:h_samples:{symbol}:{bar_ts}` list + `sel:depth_samples:...`）
- 每 5 min bucket 写入 DB（`sel_orderbook_samples` via `write_orderbook_sample()`）
- 异常处理：HTTP 错误 / 网络异常均 warn+skip，不崩溃

**关键 TODO**：无

**依赖**：`aiohttp`（Dockerfile 中已包含），`sel_engine.features.liquidity`, `sel_engine.features.orderbook`

**配置项**：`SEL_SYMBOL`（默认 BTCUSDT），`SEL_INST_ID`（默认 BTC-USDT-SWAP），OKX 公共 endpoint（无 API Key 要求）

### Schema

**表名**：`sel_orderbook_samples`  
**类型**：TimescaleDB hypertable  
**chunk_time_interval**：7 天（604,800,000,000 µs）  
**迁移文件**：`sel_engine/db/schema.sql`（通过 `sel_engine/db/migrations.py` 应用）

| 列名 | 类型 |
|------|------|
| time_bucket | TIMESTAMPTZ (hypertable key) |
| symbol | VARCHAR(20) |
| H_sample | NUMERIC(10,8) |
| top_5_bid | NUMERIC(20,8) |
| top_5_ask | NUMERIC(20,8) |
| spread_bps | NUMERIC(10,4) |
| sample_count | INTEGER DEFAULT 1 |

**压缩**：schema.sql 中未定义压缩策略。

**注意**：`sel_orderbook_samples` 无 UNIQUE(time_bucket, symbol) 约束 → EC-02（多行/bucket，已记录）。

### Compose

**service 名**：`sel-orderbook`  
**Dockerfile**：`services/sel_collectors/Dockerfile`（与其他两个 collector 共用）  
**命令**：`python -m services.sel_collectors.run_orderbook`  
**网络**：helios-net ✅  
**环境变量**：`SEL_SYMBOL`, `SEL_INST_ID`, `TIMESCALE_URL`, `REDIS_URL`（无 API Key）  
**资源限制**：0.2 CPU / 128MB  
**Healthcheck**：未定义（继承基础镜像 :8000/health → EC-01 误报）

### 运行状态

**状态**：Up 4 hours (unhealthy = EC-01 基础镜像误报，进程本身正常)  
**最近写入**：`max(time_bucket) = 2026-04-28T05:30:00Z`（约当前时间，正常）  
**总行数**：230 行（4H 运行；EC-02 多行/bucket 原因导致高于预期）  
**最近 1H 写入**：59 行  
**日志错误**：无（仅启动信息 + schema 应用成功）

**Redis 实测数据**：
- 3 个活跃 bar 的 H samples key 在 Redis 中
- 最新 key 长度 = 60 条（满足 ≥60 次/bar 文档要求 ✅）
- H 值范围：0.069 ~ 1.524（Shannon entropy，合理）

### 数据质量

- **采样频率**：60 条/bar ✅（文档要求 ≥60 次/1H bar）
- **top_5_bid/ask**：均有值（46~795 BTC bid, 130~1252 BTC ask）
- **H 计算**：在 collector 内完成，bar close 时 FeatureCalculator 从 Redis `lrange` 取平均值
- **spread_bps**：所有样本固定 0.0130 bps ⚠️ — 疑似 OKX 返回的最小档 spread，数值可靠性待确认

### 阻塞项

- 无启动阻塞（已在跑）
- EC-02（多行/bucket）影响历史回溯，不影响实时 H 计算（H 从 Redis 读）

### 预计工作量

- **启动**：N/A（已在跑）
- **EC-02 修复**（可选）：添加 UNIQUE 约束 + ON CONFLICT DO UPDATE，约 2H

---

## trade_flow_collector

### 代码完整度

**文件路径**：`sel_engine/collectors/trade_flow_collector.py`（单文件，94 行）  
**入口**：`services/sel_collectors/run_trade_flow.py`

**实现状态**：✅ 完整实装

- 连接 OKX REST API：`GET /api/v5/market/trades?instId=BTC-USDT-SWAP&limit=100`
- 每 5s 轮询一次（POLL_INTERVAL_SECS = 5）
- 按 tradeId 去重（Redis set `sel:tf_trade_ids:{symbol}:{bar_ts}`）
- 计算 notional = sz × px；side == "buy" → +delta，side == "sell" → -delta
- 累积写入 Redis：`sel:tf_accum:{symbol}:{bar_ts}`（`INCRBYFLOAT`）
- Bar 过期：2H（BAR_EXPIRE_SECS = 7200）

**架构说明**：trade_flow 为 Redis-only 设计。TF 是 ephemeral 累积值，FeatureCalculator 在 bar close 时从 Redis 读取，无需 DB 持久化。

**关键 TODO**：无

**依赖**：`aiohttp`，无额外依赖

**配置项**：`SEL_SYMBOL`, `SEL_INST_ID`，OKX 公共 endpoint（无 API Key）

### Schema

**无 DB 写入**（架构设计）。TF 数据存在 Redis，FeatureCalculator 在 bar close 时读取 `sel:tf_accum:{symbol}:{bar_ts}`。

### Compose

**service 名**：`sel-trade-flow`  
**Dockerfile**：`services/sel_collectors/Dockerfile`（共用）  
**命令**：`python -m services.sel_collectors.run_trade_flow`  
**网络**：helios-net ✅  
**环境变量**：`SEL_SYMBOL`, `SEL_INST_ID`, `REDIS_URL`（无 TIMESCALE_URL，无 API Key）  
**资源限制**：0.2 CPU / 128MB

### 运行状态

**状态**：Up 4 hours (unhealthy = EC-01)  
**日志**：启动成功，无错误

**Redis 实测数据**：
```
sel:tf_accum:BTCUSDT:2026-04-28T03:00:00+00:00 → +1,183,397,334 USDT (净买方)
sel:tf_accum:BTCUSDT:2026-04-28T04:00:00+00:00 →   -259,486,299 USDT (净卖方)
sel:tf_accum:BTCUSDT:2026-04-28T05:00:00+00:00 →   -720,906,160 USDT (净卖方)
```
3 个活跃 bar，数值有正有负（方向正确）。

### 数据质量

- **taker 方向分类**：buy/sell 分开，净值计算正确 ✅
- **去重机制**：Redis set 按 tradeId 去重 ✅
- **1H 聚合**：在 collector 内实时累积，bar close 时直接读取 ✅
- ⚠️ **sz 单位待确认（EC-08）**：OKX BTC-USDT-SWAP 合约 trades API 的 `sz` 字段单位可能是 lots（1 lot = 0.01 BTC），非 BTC 本身。当前代码 `notional = sz × px` 若 sz 为 lots，则高估 100×。对分位数排名无影响（一致偏移），但 `tf_dp_ratio_24h` 绝对值会偏大 100×，影响 §4.1 Cond4 条件触发阈值的经济含义。见 EC-08。

### 阻塞项

- 无启动阻塞（已在跑）
- EC-08（sz 单位待确认）需在 bar-close 调度器部署前验证

---

## oi_persister

### 代码完整度

**文件路径**：`sel_engine/collectors/oi_persister.py`（单文件，58 行）  
**入口**：`services/sel_collectors/run_oi.py`

**实现状态**：✅ 完整实装

- 连接 OKX REST API：`GET /api/v5/public/open-interest?instId=BTC-USDT-SWAP&instType=SWAP`
- 每 5 分钟轮询（PERSIST_INTERVAL_SECS = 300）
- 优先取 `oiUsd`，fallback `oiCcy`, `oi`
- 写入 DB：`sel_oi_history` via `write_oi_snapshot()`

**关键 TODO**：无

**依赖**：`aiohttp`，无额外依赖

**配置项**：`SEL_SYMBOL`, `SEL_INST_ID`，OKX 公共 endpoint（无 API Key）

### Schema

**表名**：`sel_oi_history`  
**类型**：TimescaleDB hypertable  
**chunk_time_interval**：7 天  
**迁移文件**：`sel_engine/db/schema.sql`

| 列名 | 类型 |
|------|------|
| time | TIMESTAMPTZ (hypertable key) |
| symbol | VARCHAR(20) |
| oi_value | NUMERIC(20,8) NOT NULL |

UNIQUE INDEX：`sel_oi_history_time_symbol` ✅（无 EC-02 问题）

### Compose

**service 名**：`sel-oi`  
**Dockerfile**：`services/sel_collectors/Dockerfile`（共用）  
**命令**：`python -m services.sel_collectors.run_oi`  
**环境变量**：`SEL_SYMBOL`, `SEL_INST_ID`, `TIMESCALE_URL`（无 API Key）  
**资源限制**：0.1 CPU / 64MB

### 运行状态

**状态**：Up 4 hours (unhealthy = EC-01)  
**最近写入**：`max(time) = 2026-04-28T05:30:54Z`（约 5 min 前，正常）  
**总行数**：47 行（4H × 12条/H ≈ 48，符合 5 min 间隔）  
**最近 1H 写入**：12 行 ✅  
**日志错误**：无

**最新 OI 值**：2,530,364,834 USDT（约 25.3 亿美元）✅ BTC 永续合约 OI 量级合理

### 数据质量

- **写入间隔**：5 min ✅
- **数据缺口**：无（47行/4H，连续）
- **oi_value 取值**：优先 `oiUsd`（已含 USD 换算）✅

### 阻塞项

无。

---

## 总体判断

### 现状分类

| 分类 | 是/否 |
|---|---|
| 全部就绪在跑 | **是** — 三个 collector 均运行 4H，数据连续写入 |
| 全部就绪未启动 | 不适用 |
| 部分就绪 | 不适用 |
| 全部缺失 | 否 |

**现状**：`全部就绪在跑`。

**但关键路径阻断**：`sel_features` 表 0 行（EC-04）——bar-close 调度器从未启动，collector 数据虽在积累，sel_engine 从未消费过任何实时数据。`sel_state_sequence` 表在 schema.sql 中完全缺失（DB 中不存在）。

### 30 天数据攒齐预计时间

**前置条件（必须全部完成）**：
1. EC-04 修复：部署 bar-close 调度器
2. `sel_state_sequence` 表补全：在 schema.sql 中添加，re-apply migrations
3. EC-08 确认：验证 TF sz 单位，必要时修正 collector（若不修正则 COLD_START 期的分位数窗口会以错误单位积累）

假设上述前置条件于 **2026-04-29** 完成：
- 2026-04-29：bar-close 开始运行，COLD_START 阶段开始（720 bar warmup）
- **2026-05-29**：720 bar 完成，分位数窗口满，状态开始正常触发

**首次有效状态产出预期日期：2026-05-29**（前置修复 ≤1 天）

每推迟 1 天修复 EC-04，数据攒齐日期推迟 1 天。

### 关键路径风险

| 风险 | 影响 | 优先级 |
|------|------|--------|
| EC-04：bar-close 调度器缺失 | **彻底阻断**，collector 数据无法进入 sel_engine | 最高 |
| `sel_state_sequence` 表缺失 | bar-close 启动后状态写入立即失败（UndefinedTableError） | 最高（与 EC-04 同批修复） |
| EC-08：TF sz 单位待确认 | tf_dp_ratio_24h 可能偏大 100×，影响 §4.1 条件 | 高（需在分位数窗口填充前确认） |
| EC-02：orderbook_samples 多行/bucket | 历史 H 回溯分析多计 5×；实时 H 不受影响（从 Redis 读） | 中 |
| EC-03：delta_H 未写 DB | Cascade Cond4 历史审计不完整；实时不受影响 | 低 |
| H/TF 无历史回填 | cold_start 30 天不可压缩，实时采集从启动起算 | 信息（非风险） |

---

## 工程顾虑（新增）

### EC-08（新）：TF sz 单位待确认（B = 中优先度，数据质量）

**现象**：`trade_flow_collector.py:75` 计算 `notional = sz × px`，但 OKX BTC-USDT-SWAP trades API 的 `sz` 字段单位可能是 lot（1 lot = 0.01 BTC），而非 BTC 本身。

**根因**：OKX 合约 `sz` 的语义依赖 instType 和具体合约规格，未经验证。

**数据流影响**：
- TF 绝对值若高估 100×，分位数排名内部仍一致（all bars 同等偏移），state 触发率不受影响
- `tf_dp_ratio_24h = sum|TF| / sum|ΔP%|`（§4.1 Cond4）的绝对量级会偏差 100×，使该条件的触发阈值与 WIKI 的物理含义不符
- 冷启动期（30 天）如果以错误单位积累分位数窗口，未来修正 sz 会使历史窗口失效

**不处理后果**：Coiling §4.1 Cond4 在实际数据上可能始终不满足或始终满足，偏离 WIKI 语义。

**相关文件**：`sel_engine/collectors/trade_flow_collector.py:75`

### `sel_state_sequence` 表缺失

**现象**：`sel_engine/db/schema.sql` 中无 `sel_state_sequence` 定义。DB 确认该表不存在（`UndefinedTableError`）。

**根因**：schema.sql 包含 `sel_features`, `sel_oi_history`, `sel_funding_history`, `sel_orderbook_samples`，但遗漏了状态序列持久化表。该表对应 Task 1.7 引入的 StateRecord 历史写入路径。

**不处理后果**：bar-close 调度器启动后，状态写入立即以 `UndefinedTableError` 失败，状态历史无法积累。

**相关文件**：`sel_engine/db/schema.sql`（需补全），`sel_engine/db/writer.py`（写入逻辑）

---

## 通知服务检查（EC-08 前置）

**EC-08 原有记录**：无（本任务新建，见上节）

**services/notification/ 实装状态**：
- `hub.py`：✅ 完整，处理 `STREAM_SYSTEM_ALERTS = "system.alerts"` 和 `STREAM_ORDER_LIFECYCLE`
- `risk_alert` 路由：✅ `alert_t == "risk_alert"` → `text = f"⚠️ *风控告警*\n{data.get('reason', '')}"`
  — `data.get('reason')` 与 Task 1.8.1 payload 字段完全匹配
- Telegram + DingTalk 双渠道
- 运行状态：Up 20 hours (unhealthy = EC-01)，进程本身正常
- Consumer group 确认：`notify-alerts` 正在监听 `system.alerts` ✅（日志可见）

**Task 1.8.1 alert_required 接入路径**（端到端验证）：
```
runner._emit_rule2_alert_if_needed()
  → redis.xadd("system.alerts", {"type": "risk_alert", "reason": ..., ...})
  → notification-service hub._on_alert()
  → TelegramChannel.send() + DingTalkChannel.send()
```
路径完整，无告警缺口。
