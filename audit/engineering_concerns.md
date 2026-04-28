# 工程顾虑清单

**审计日期**：2026-04-28  
**来源**：TASK 2.0 — collector 部署现状审计 + Coiling 下游审查  
**纪律**：此文件仅列问题供决策，不含任何修复建议。每条顾虑附 A/B/C 优先级标注，并说明"不处理的后果"。

---

## EC-01：Collector Healthcheck 误报（A = 低紧急度，需告警屏蔽）

**现象**：`sel-orderbook`、`sel-trade-flow`、`sel-oi` 三个容器持续报 `unhealthy`（`docker ps` 可见）。

**根因**：基础镜像 `ghcr.io/helios-plat/helios/python-api-base:0.1.0` 内置了对 `:8000/health` 的 `HEALTHCHECK` 指令。三个 collector 均无 HTTP 服务，端口 8000 不可达 → 每次 healthcheck 超时 → Docker 标记 `unhealthy`。

**数据流影响**：**无**。容器进程正常运行，数据写入正常（详见 `audit/collector_status.md` 各节数据质量表）。

**不处理的后果**：
- 运维告警系统（如 Grafana/Alertmanager）若基于 `docker inspect` 健康状态触发告警，会产生持续误报
- `docker-compose.yml` 中 `depends_on: condition: service_healthy` 模式无法被 collector 满足（当前 compose 无此依赖，不受影响）
- 不影响任何业务功能

**相关文件**：`docker-compose.yml`（`sel-orderbook`、`sel-trade-flow`、`sel-oi` service 定义）

---

## EC-02：`sel_orderbook_samples` 每 5-min Bucket 多行写入（B = 中优先度，数据冗余）

**现象**：`sel_orderbook_samples` 表中，同一 `(time_bucket, symbol)` 组合存在多行（每 60s 写一行 → 5-min bucket 内最多 5 行）。

**根因**：
- `sel_engine/db/schema.sql`：`sel_orderbook_samples` 表无 `UNIQUE(time_bucket, symbol)` 约束
- `sel_engine/db/writer.py::write_orderbook_sample()`：使用 plain `INSERT`，无 `ON CONFLICT DO UPDATE`

**文档声明**（RUNBOOK §"orderbook_samples schema"）：期望"1 row per 5-min bucket"。

**数据流影响**：
- **H 计算无影响**：H 在 bar close 从 Redis 读取（`sel:h_samples:{symbol}:{bar_ts}`），不依赖 DB 行数
- **回溯分析影响**：任何直接查询 `sel_orderbook_samples` 做历史 H 均值/分位的分析，结果会被多计 5×（除非用 `AVG` 聚合）
- 表体积：73 行（1H 启动后），约 5× 超出预期

**相关文件**：`sel_engine/db/schema.sql`（`sel_orderbook_samples` DDL）、`sel_engine/db/writer.py::write_orderbook_sample()`

---

## EC-03：`writer.py::write_feature_vector()` 未持久化 `delta_H`（B = 中优先度，DB 数据缺失）

**现象**：`sel_features` 表无 `delta_H` 列；`write_feature_vector()` 的 INSERT 语句未包含该字段。

**根因**：P0 Fix 1 在 `sel_engine/features/schema.py` 新增了 `FeatureVector.delta_H`，在 `sel_engine/features/calculator.py` 计算赋值，但 `sel_engine/db/writer.py` 未同步更新，且 `sel_engine/db/schema.sql` 的 `sel_features` 表也无对应列。

**数据流影响**：
- **状态引擎运行时无影响**：`delta_H` 已正确计算并传入 `check_cascade()`（通过内存中的 `qr` dict）
- **DB 持久化缺失**：`sel_features` 中历史 `delta_H` 值无法查询，历史回溯分析无法复现 Cascade 的 Cond4 判断路径
- 如未来需要审计某个 Cascade 触发是否合理，DB 记录中将缺少 `delta_H` 证据

**相关文件**：`sel_engine/db/writer.py::write_feature_vector()`、`sel_engine/db/schema.sql`（`sel_features` DDL）

---

## EC-04：`sel_features` 表无数据 — 状态引擎 Bar-Close 写入路径未启动（A = 高优先度，端到端功能缺失）

**现象**：`sel_features` 表行数 = 0（部署后 73 分钟内无写入）。

**根因**：`FeatureCalculator` 和 `StateEngine` 未被任何 bar-close 事件触发。当前部署架构中：
- 三个 collector（orderbook/trade_flow/oi_persister）均正常写入各自存储（Redis / DB）
- **缺少 bar-close 调度器**：没有进程在每小时 bar 收盘时调用 `FeatureCalculator.calculate()` → `StateEngine.process()` → `write_feature_vector()` 写入 `sel_features`

**数据流影响**：
- `sel_features` 表持续为空
- `sel_state_sequence` 表无新记录（状态历史无法积累）
- 下游信号/交易决策引擎（`services/signal/`）若依赖 `sel_features` 中的 live 状态，实际接收不到任何数据
- **这是系统端到端功能缺失，不是性能或质量问题**

**相关文件**：`sel_engine/features/calculator.py`、`sel_engine/states/engine.py`、`sel_engine/db/writer.py::write_feature_vector()`  
**缺失组件**：bar-close 调度进程（scheduler service 或 cron job），调用链：`on_bar_close(ts, symbol)` → `FeatureCalculator` → `StateEngine` → `writer`

---

## 汇总表

| ID | 描述 | 优先级 | 数据流影响 | 不处理后果 |
|---|---|---|---|---|
| EC-01 | Collector healthcheck 误报 | A（低） | 无 | 告警误报（运维噪音） |
| EC-02 | orderbook_samples 多行/bucket | B（中） | DB 回溯分析多计 5× | 回溯分析结果错误 |
| EC-03 | writer.py 缺 delta_H 字段 | B（中） | 状态引擎无影响 | Cascade 历史记录不完整 |
| EC-04 | sel_features 无写入（bar-close 未启动） | **A（高）** | **端到端断路** | 状态历史无法积累，下游信号无数据 |
