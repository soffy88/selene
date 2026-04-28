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

## EC-05：MISSING_DATA 检测采用后验检查而非条件函数三值返回（C = 设计记录）

**现象**：TASK 1.7 引入 `StateNoneReason.MISSING_DATA`。检测逻辑实现为：在 `recognize()` 的 no-match 路径调用 `_none_reason_for_no_match(qr, fv)`，检查 `_HARD_SHORT_CIRCUIT_QR` 中的量化特征是否为 None。

**为何不用三值返回**：备选方案是将所有条件函数签名改为返回 `Optional[bool]`（`True=matched`, `False=no_match`, `None=missing_data`），约 60 处测试断言需同步修改，且条件函数本身职责更单纯时可读性更好。

**当前方案限制**：
- `_HARD_SHORT_CIRCUIT_QR` 集合须与实际条件代码手动保持同步；若新增条件用到新的 WIKI 特征，需记得同步更新该集合
- 极端情况：若所有 WIKI 特征都存在但某条件的第一个 gate 恰好失败（非 None 原因），仍返回 NO_MATCH，正确
- 当前集合覆盖：`H_24h_mean`、`abs_tf_24h_sum`、`oi_change_rate_24h`、`tf_dp_ratio_24h`、`abs_oi_change_rate_24h`、`tf_directional_ratio_6h`（fv 字段直接检查）

**相关文件**：`sel_engine/states/recognizer.py::_HARD_SHORT_CIRCUIT_QR`、`recognizer.py::_none_reason_for_no_match()`

---

## EC-06：`state_rates` 分母选 `active_bars`（非 `total_bars`）未在 §10.5 文档化（C = 文档缺失）

**现象**：`compute_state_distribution()` 和 `HealthMonitor.generate()` 中，`state_rates` 分母为 `active_bars`（即已确认状态的 bar 数），而非 `total_bars`。

**设计意图**：速率表示「有状态时各状态的相对频率」，分母含 cold_start/missing_data/no_match bar 会稀释速率使其失去可比性。

**文档缺口**：`v1.0.md §10.5` 中 EXPECTED_RATE_RANGES 表格给出了速率期望范围，但未说明「速率分母为 active_bars」。若后续校准工作以 `total_bars` 为分母计算期望范围，阈值会不匹配。

**不处理后果**：校准人员若未查看代码，可能以 `total_bars` 为基准设置错误的期望范围（如 `Cascade: [0.001, 0.03]`，若用 `total_bars` 分母则需换算）。

**相关文件**：`sel_engine/states/recognizer.py::compute_state_distribution()`、`sel_engine/states/health.py::HealthMonitor.generate()`

---

## EC-07：Rule 2 signal_lag 对「collector 故障」vs「无状态命中」动作未区分（**RESOLVED — 候选 B 已实装**）

**决策日期**：2026-04-28  
**决策**：采用候选 B — MISSING_DATA → HOLD + 告警；NO_MATCH → CLOSE  
**实装 commit**：`699f219`（Task 1.8.1）

**实装内容**：
- `paper_trading/risk.py::_fire_rule_2()` — subtype 分支，`missing_data` 触发 `NO_ACTION` + `alert_required=True`
- `paper_trading/runner.py::_emit_rule2_alert_if_needed()` — 发布 `risk_alert` 到 `system.alerts` Redis stream，6H dedup
- `paper_trading/risk.py::RiskCheckResult.alert_required` + `paper_trading/trail.py::DecisionTrail.alert_required`
- `decision/config.py::RiskConfig.missing_data_alert_dedup_hours = 6`（工程便利值，非 spec）

**行为对照（实装后）**：

| 情形 | subtype | 动作 | 告警 |
|------|---------|------|------|
| 状态识别器健康，条件 24H 无命中 | NO_MATCH | CLOSE | 无 |
| Collector 故障导致 WIKI 特征缺失 | MISSING_DATA | HOLD（NO_ACTION） | system.alerts risk_alert |

**Rule 1 优先级不变**：Cascade state 仍在 Rule 2 之前触发 CLOSE，候选 B 不影响 Rule 1。

**残余风险**：collector 长期故障（数天）期间仓位持续持仓，无状态保护；运营人员需响应 risk_alert 告警手动干预。详见 `audit/p1_8_1_candidate_b_impact.md`。

---

## 汇总表

| ID | 描述 | 优先级 | 数据流影响 | 不处理后果 |
|---|---|---|---|---|
| EC-01 | Collector healthcheck 误报 | A（低） | 无 | 告警误报（运维噪音） |
| EC-02 | orderbook_samples 多行/bucket | B（中） | DB 回溯分析多计 5× | 回溯分析结果错误 |
| EC-03 | writer.py 缺 delta_H 字段 | B（中） | 状态引擎无影响 | Cascade 历史记录不完整 |
| EC-04 | sel_features 无写入（bar-close 未启动） | **A（高）** | **端到端断路** | 状态历史无法积累，下游信号无数据 |
| EC-05 | MISSING_DATA 后验检查而非三值返回 | C（设计记录） | 无 | 新增 WIKI 特征时需手动同步集合 |
| EC-06 | state_rates 分母 active_bars 未文档化 | C（设计记录） | 无 | 校准时分母误用导致阈值错误 |
| EC-07 | Rule 2 对 collector 故障 vs 无状态命中动作未区分 | ~~A（高，待决策）~~ **RESOLVED** | 候选 B 已实装（HOLD+alert for MISSING_DATA） | — |
