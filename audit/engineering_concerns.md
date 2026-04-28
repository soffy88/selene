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

### EC-01 调查记录（Task 2.1，2026-04-28）

**Healthcheck 命令**（`docker inspect selene-sel-orderbook-1 .Config.Healthcheck`）：
```json
{"Test":["CMD-SHELL","curl -fsS http://localhost:8000/health || exit 1"],
 "Interval":30000000000,"Timeout":3000000000,"StartPeriod":10000000000,"Retries":3}
```

**Healthcheck 失败日志**（每 30s 一次，已连续 487 次，约 4H）：
```
ExitCode: 1
Output: "curl: (7) Failed to connect to localhost port 8000 after 0 ms: Could not connect to server"
```

**根因确认**：`ghcr.io/helios-plat/helios/python-api-base:0.1.0` 基础镜像内置了 `curl -fsS http://localhost:8000/health` 的 HEALTHCHECK。三个 sel collector 的 Dockerfile 均未覆盖（`HEALTHCHECK NONE`），直接继承基础镜像的检查。Collector 进程是纯 Python 事件循环，不暴露任何 HTTP 端口，因此每次检查都立即返回 connection refused。

**影响评估**：
- 数据写入：✅ 不受影响（sel-orderbook 4H 内已写入 230 行，OI 47 行，均无中断）
- 容器调度：✅ 稳定运行（`restart: unless-stopped` + 无 `condition: service_healthy` 依赖）——unhealthy 状态不触发 docker 重启
- 上游影响：无（无其他 service 依赖 sel-* 容器的 `service_healthy` 条件）

**推荐修复方案**（不实施，等决策）：在三个 sel collector 的 Dockerfile 中加一行 `HEALTHCHECK NONE` 覆盖基础镜像的 healthcheck，或添加符合实际的 healthcheck（如检查进程 PID 或 Redis key 写入时间）。

**Status**：PENDING_DECISION。不阻塞 EC-04 + EC-09 部署（容器本身运行正常）。

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

## EC-04：`sel_features` 表无数据 — 状态引擎 Bar-Close 写入路径未启动（~~A = 高优先度~~ **RESOLVED**）

**修复日期**：2026-04-28（Task 2.2）  
**Resolution**：新增 `sel-bar-runner` Docker 服务（`services/sel_bar_runner/`）。每小时 UTC 整点 +30s 触发，从 DB + Redis 读取数据 → `FeatureCalculator.compute()` → `StateEngine.process()` → upsert `sel_features` + `sel_state_sequence`。Cold start 计时从首次成功写入开始。  
**Commit**：见 Task 2.2 commits。

~~**现象**：`sel_features` 表行数 = 0（部署后 73 分钟内无写入）。~~

~~**根因**：`FeatureCalculator` 和 `StateEngine` 未被任何 bar-close 事件触发。~~

---

## EC-05：MISSING_DATA 检测采用后验检查而非条件函数三值返回（C = 设计记录）

**现象**：TASK 1.7 引入 `StateNoneReason.MISSING_DATA`。检测逻辑实现为：在 `recognize()` 的 no-match 路径调用 `_none_reason_for_no_match(qr, fv)`，检查 `_HARD_SHORT_CIRCUIT_QR` 中的量化特征是否为 None。

**为何不用三值返回**：备选方案是将所有条件函数签名改为返回 `Optional[bool]`（`True=matched`, `False=no_match`, `None=missing_data`），约 60 处测试断言需同步修改，且条件函数本身职责更单纯时可读性更好。

**当前方案限制**：
- `_HARD_SHORT_CIRCUIT_QR` 集合须与实际条件代码手动保持同步；若新增条件用到新的 WIKI 特征，需记得同步更新该集合
- 极端情况：若所有 WIKI 特征都存在但某条件的第一个 gate 恰好失败（非 None 原因），仍返回 NO_MATCH，正确
- 当前集合覆盖：`H_24h_mean`、`abs_tf_24h_sum`、`oi_change_rate_24h`、`tf_dp_ratio_24h`、`abs_oi_change_rate_24h`、`tf_directional_ratio_6h`（fv 字段直接检查）

**相关文件**：`sel_engine/states/recognizer.py::_HARD_SHORT_CIRCUIT_QR`、`recognizer.py::_none_reason_for_no_match()`

**Defense in depth**：Task 1.8.2 新增 `test_hard_short_circuit_qr_covers_all_wiki_required_features` 和 `test_no_match_only_when_all_wiki_features_present`，防止未来 PR 新增 WIKI 特征时忘记更新 frozenset 而导致静默错分。

---

## EC-06：`state_rates` 分母选 `active_bars`（非 `total_bars`）未在 §10.5 文档化（**RESOLVED**）

**决策日期**：2026-04-28  
**Resolution**：Documentation update in `docs/sel-lang-v1.0.md §10.5` to explicitly state denominators — `active_bars` for state rates, `transition_count` for legality rate.  
**Code change**：None（实现已正确，仅文档补全）。

~~**现象**：`compute_state_distribution()` 和 `HealthMonitor.generate()` 中，`state_rates` 分母为 `active_bars`（即已确认状态的 bar 数），而非 `total_bars`。~~

~~**文档缺口**：`v1.0.md §10.5` 中 EXPECTED_RATE_RANGES 表格给出了速率期望范围，但未说明「速率分母为 active_bars」。~~

**相关文件**：`docs/sel-lang-v1.0.md §10.5`（已补全说明）、`sel_engine/states/recognizer.py::compute_state_distribution()`

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

## EC-08：trade_flow sz 单位待确认（B = 中优先度，数据质量）

**发现日期**：2026-04-28（Task 2.0 审计）

**现象**：`sel_engine/collectors/trade_flow_collector.py:75` 计算 `notional = size * px`，其中 `size = float(trade.get("sz", 0))`。OKX BTC-USDT-SWAP trades API 的 `sz` 字段单位需确认：若为 lots（1 lot = 0.01 BTC）而非 BTC，则 TF notional 高估 100×。

**实测 TF 量级**：最近 3H 净流量为 ±260M～+1,183M USDT/bar。BTC 永续合约 1H 净流量在 1B+ USDT 属于偏大但并非不可能。需对照 OKX API 文档确认。

**数据流影响**：
- TF 分位数排名：**不受影响**（所有 bar 同等偏移，rank 一致）
- `tf_dp_ratio_24h`（§4.1 Cond4，Coiling 条件之一）：若 TF 高估 100×，该比率会高估 100×，使 Coiling 的实际触发阈值与 WIKI 语义不符
- 冷启动期（30 天）如以错误单位积累分位数窗口，未来修正后历史窗口失效

**不处理后果**：Coiling §4.1 Cond4 在真实数据上可能始终不满足或始终满足。

**相关文件**：`sel_engine/collectors/trade_flow_collector.py:75`

### EC-08 Resolution Investigation（Task 2.1，2026-04-28）

**OKX API documented behavior**（via web_fetch，`GET /api/v5/public/instruments?instType=SWAP&instId=BTC-USDT-SWAP`）：
```json
{"ctVal": "0.01", "lotSz": "0.01", "minSz": "0.01", "ctValCcy": "BTC"}
```
`ctValCcy=BTC`：sz 字段单位为 **BTC**（= num_contracts × ctVal = num_contracts × 0.01）。1 lot = 0.01 BTC，sz=0.87 = 87 contracts × 0.01 BTC。

**Verdict**：✅ **CORRECT** — `notional = sz × px` 公式正确，sz 已经是 BTC 单位，无 100× 偏差。

**Empirical comparison**（Redis 实测，5 分钟窗口，100+ trades）：

| sz 样本 | 价格 (USDT) | notional (USDT) | lots 数 |
|---------|-------------|-----------------|---------|
| 0.01 | ~76,000 | ~760 | 1 |
| 0.03 | ~76,000 | ~2,280 | 3 |
| 0.87 | ~76,830 | ~66,842 | 87 |
| 23.96 | ~76,000 | ~1,820,960 | 2396 |

- sz 最小值为 0.01（= 1 lot × 0.01 BTC），与 OKX 规格完全吻合
- 所有样本均为 0.01 的整数倍，符合 BTC 单位约束
- 若 sz 为 lots，则最小 sz 应为 1（不是 0.01）——实测排除 lots 假设

**Impact on sel_engine**：
- `tf_dp_ratio_24h`：Coiling §4.1 Cond4（`sel_engine/states/conditions.py:116,135,142`）——分位数排名不受影响，绝对值正确
- `abs_tf_24h_sum`：Drifting-Calm §4.3 Cond3 + Drifting-Charged §4.4 Cond3（`conditions.py:216,261`）——同上
- 30 天 cold start 期间分位数窗口：不受污染，数据已正确

**Recommended fix**：None — 公式无误，collector 代码无需修改。

**Status**：RESOLVED（公式验证通过）。不阻塞 EC-04 + EC-09 部署。

---

## EC-09：`sel_state_sequence` 表缺失（~~**A = 高优先度**~~ **RESOLVED**）

**修复日期**：2026-04-28（Task 2.2）  
**Resolution**：
- 迁移文件：`sel_engine/db/migrations/001_create_sel_state_sequence.sql`
- `sel_engine/db/schema.sql` 已补全表定义（新部署通过 migrations.py 自动建表）
- `sel_engine/db/writer.py` 新增 `write_state_record()` upsert 函数
- 表已在生产 DB 成功建立（TimescaleDB hypertable，chunk 7 days，unique index on (symbol, time)）

**Commit**：见 Task 2.2 commits。

~~**现象**：`sel_engine/db/schema.sql` 中无 `sel_state_sequence` 定义。~~

---

## 汇总表

| ID | 描述 | 优先级 | 数据流影响 | 不处理后果 |
|---|---|---|---|---|
| EC-01 | Collector healthcheck 误报 | A（低） | 无 | 告警误报（运维噪音） |
| EC-02 | orderbook_samples 多行/bucket | B（中） | DB 回溯分析多计 5× | 回溯分析结果错误 |
| EC-03 | writer.py 缺 delta_H 字段 | B（中） | 状态引擎无影响 | Cascade 历史记录不完整 |
| EC-04 | sel_features 无写入（bar-close 未启动） | ~~**A（高）**~~ **RESOLVED** | — | — |
| EC-05 | MISSING_DATA 后验检查而非三值返回 | C（设计记录） | 无 | 新增 WIKI 特征时需手动同步集合 |
| EC-06 | state_rates 分母 active_bars 未文档化 | ~~C（设计记录）~~ **RESOLVED** | §10.5 已补全分母说明 | — |
| EC-07 | Rule 2 对 collector 故障 vs 无状态命中动作未区分 | ~~A（高，待决策）~~ **RESOLVED** | 候选 B 已实装（HOLD+alert for MISSING_DATA） | — |
| EC-08 | TF collector sz 单位待确认（lot vs BTC） | ~~B（中）~~ **RESOLVED** | 公式正确，无污染 | — |
| EC-09 | sel_state_sequence 表缺失 | ~~**A（高）**~~ **RESOLVED** | — | — |
| EC-10 | StateEngine 内存状态在容器重启时丢失 | C（设计记录） | 重启后数个 bar Dwell/Cooling 状态可能不准确 | cold start 期内无影响；post-warmup 重启偶发伪 no_match |
| EC-11 | 调度器失败 bar 无持久记录 | B（中） | 失败 bar 不可事后追溯，gap 不可审计 | 运营不知哪些 bar 失败，cold start 时序不透明 |
| EC-12 | candles schema 错配：backfill.py + validate_e2e.py 使用 open_time（表实际为 time）| B（中） | backfill 和 e2e 验证脚本运行即失败 | backfill 功能完全失效；validate_e2e 无法运行 |
| EC-13 | asyncpg pool 无 acquire/command timeout，长空闲后 pool.acquire() 可能无限挂起 | B（中） | 调度器在触发时静默挂起，无 log 无异常，直到 SIGTERM | cold start 期间触发即挂起 → bar 丢失 → cold start 延迟 |
| EC-14 | funding_rate Redis 值为 JSON（`{"rate":0.0}`），flow.py 用 float() 直接解析 → ValueError | C（低） | funding_rate 始终为 None，不参与 WIKI_REQUIRED 检查 | funding_rate 特征永远缺失；warn 持续出现 |
| EC-15 | paper_trading/db/trail_store.py 大小写访问：row["H/TF/OI/LV"] 与实际列名不符 | B（中，延迟触发） | cold start 期间 state=None → paper_trading 不写 trail → 不触发 | cold start 结束后首次非 None state 时 trail 写入崩溃 |
| EC-16 | paper_interface/store.py schema 版本不匹配：SELECT H/TF/OI/LV FROM sel_state_sequence，这些列不存在 | A（高，延迟触发） | cold start 期间 paper_interface 不被调用 | cold start 结束后 paper_trading 读取状态时 SQL 级失败（"column H does not exist"） |

## EC-10：StateEngine 重启后内存状态丢失（C = 设计记录，低紧急度）

**发现日期**：2026-04-28（Task 2.2 部署分析）

**现象**：`sel-bar-runner` 容器重启后，`StateEngine` 的 `DwellFilter`（候选计数）和 `CascadeCooling`（冷却期截止时间）重置为初始值，丢失重启前的状态。

**根因**：`StateEngine` 状态保存在进程内存中，未持久化到 Redis 或 DB。重启时无法恢复上次的 `_last_confirmed`、`_candidate`（DwellFilter）、`_cascade_end_time`（CascadeCooling）。

**数据流影响**：
- **Cold start 期间（前 720 bar）**：无影响——`StateRecognizer` 直接返回 `cold_start=True`，`DwellFilter` 和 `CascadeCooling` 在 cold_start 路径下直接 pass through
- **Post-warmup 重启**：重启后约 `max(DWELL_TIMES)` = 12 bar（12H）内，DwellFilter 状态不准确，可能提前确认应被抑制的状态；CascadeCooling 在重启时恰好处于冷却期时会失效（6H 最长影响）

**不处理后果**：post-warmup 偶发重启后，`sel_state_sequence` 中 12H 内的 dwell 状态略有偏差。概率低（容器稳定不频繁重启），影响期短。

**相关文件**：`sel_engine/states/engine.py::StateEngine`（`_last_confirmed`）、`sel_engine/states/transition.py::DwellFilter`、`CascadeCooling`

**推荐修复方向**（不在当前范围）：于 bar 处理完成后将 `_last_confirmed`、`_cascade_end_time` 写入 Redis；启动时从 Redis 恢复。

---

## EC-11：调度器失败 bar 无持久记录（B = 中优先度，可观测性缺失）

**发现日期**：2026-04-28（Task 2.2.1 事后审查）

**现象**：Task 2.2 Fix 2.2 明细中要求"失败 bar 记录到 `sel_engine_errors` 表或日志聚合系统"，但实际未实装：
- `sel_engine_errors` 表不存在（DB 确认）
- `sel:scheduler:consecutive_failures` Redis 键仅记录连续失败次数，不记录哪个 bar、何种错误、何时发生
- 容器重启后旧日志丢失，失败证据无法事后查询

**直接影响（Task 2.2.1 发现）**：2026-04-28T06:00Z bar 在首次触发时失败（UndefinedColumnError: open_time），容器重启后该 traceback 已无法从 DB 或 Redis 恢复——仅存于本次会话的临时文件 `/tmp/…/bwnsej30o.output`。

**数据流影响**：失败的 bar 不写入 `sel_state_sequence`（正상적 동작），但运营人员无法：
1. 得知 cold start 期间哪些 bar 被跳过（形成时序 gap）
2. 事后重现失败原因
3. 统计连续失败 bar 数并触发自动告警

**相关文件**：`sel_engine/scheduler/bar_close_runner.py`（`except` 块只 incr Redis 计数器）

**推荐修复方向**（等决策）：
- 方向 A：建 `sel_engine_errors` 表，失败 bar 写一行（time, symbol, error_type, traceback_excerpt）
- 方向 B：将失败 bar 信息写入 Redis stream `sel:bar_errors`（轻量、TTL 7d）
- 方向 C：接入现有 `system.alerts` Redis stream 发送 risk_alert（复用 Task 1.8.1 告警路径）

---

## EC-12：candles 表 schema 错配——backfill.py 和 validate_e2e.py 使用 open_time（B = 中优先度）

**发现日期**：2026-04-28（Task 2.2.1 事后审查，768f887 披露调查）

**现象**：生产数据库 candles 表（DDL 在 `infra/timescaledb/schema.sql`）主时间戳列名为 `time`，但以下文件使用了不存在的 `open_time` 列：
- `sel_engine/backfill.py:25` — `INSERT INTO candles (symbol, interval, open_time, ...)` + `ON CONFLICT (symbol, interval, open_time)`
- `sel_engine/scripts/validate_e2e.py:65` — `SELECT open_time, close FROM candles ORDER BY open_time DESC`

**根因**：`infra/timescaledb/schema.sql`（平台团队）与 `sel_engine/` 模块各自独立开发，接口契约未文档化。`sel_engine/db/reader.py` 中同类错误已由 commit 768f887 修复（但此修复本身未经授权，见 Task 2.2.1 审查）。

**数据流影响**：
- **backfill.py**：若执行历史 K 线回填，INSERT 会因 `open_time` 列不存在立即失败；现有数据由 data-service 写入，不受影响
- **validate_e2e.py**：e2e 验证脚本无法运行，但该脚本是离线诊断工具，不在 critical path 上

**不处理后果**：backfill 功能完全失效（如需补历史数据无法运行）；e2e 验证脚本形同虚设。

**相关文件**：`sel_engine/backfill.py`、`sel_engine/scripts/validate_e2e.py`、`infra/timescaledb/schema.sql`

**推荐修复方向**（等决策）：将 backfill.py 和 validate_e2e.py 中的 `open_time` 改为 `time`，与实际 schema 对齐。同时在 `sel_engine/db/` 中建立 candles 表的 schema 常量，防止未来再次错配。

---

## EC-13：asyncpg pool 无超时配置——长空闲后触发可能静默挂起（B = 中优先度）

**发现日期**：2026-04-28（Task 2.2.1 事后分析 + Task 2.2.2 Phase 1 诊断）

**现象**：`sel-bar-runner` 在 2026-04-28T08:00:30Z 触发后，进程存活但无任何日志输出，持续 62 分钟直至 SIGTERM。DB 和 Redis 零写入，健康检查持续失败。

**根因**：`shared/db/connections.py::get_pg()` 调用 `asyncpg.create_pool()` 时未设置：
- `command_timeout`（单条 SQL 命令无超时，默认 None = 永不超时）
- `timeout`（`pool.acquire()` 等待连接无超时，默认 None = 永不超时）
- `max_inactive_connection_lifetime`（连接在 pool 中空闲无限久，等同于 server 关闭后 pool 不感知）

当 pool 空闲约 57 分钟后，postgres server 可能已关闭空闲连接。触发时 `pool.acquire()` 尝试重连，若 TCP 握手卡住则永远挂起，且不抛出异常、不输出日志。

**Task 2.2.2 Phase 1 诊断结果**：加入 run_bar 入口日志后，12:00:30Z 触发（51 分钟空闲）成功执行且无挂起（148ms 完成）。推测 08:00:30Z 挂起为单次偶发，可能与 pool 初始化时机或当时 DB 负载有关。但根因未消除，下次长空闲后仍可复现。

**数据流影响**：触发时 `run_bar()` 卡在 `pool.acquire()`，当前 bar 完全丢失。`consecutive_failures` 不递增（异常未传播），外部无感知。cold start 期间丢失一个 bar = 时序 gap，cold start 时钟不受影响但 sel_features 有空洞。

**不处理后果**：长空闲后（如维护窗口、DB 重启）触发静默挂起 → bar 丢失 → cold start gap。无可观测性，需 SIGTERM + 重启才能恢复。

**相关文件**：`shared/db/connections.py::get_pg()`

**推荐修复方向**（Phase 2，待授权）：
```python
pool = await asyncpg.create_pool(
    dsn,
    min_size=2,
    max_size=4,
    command_timeout=30,
    timeout=10,
    max_inactive_connection_lifetime=300,
)
```
同时为 Redis 客户端加 `socket_timeout=10, socket_connect_timeout=5, health_check_interval=60`。

---

## EC-14：funding_rate Redis 值为 JSON 格式，flow.py float() 解析失败（C = 低优先度）

**发现日期**：2026-04-28（Task 2.2.2 Phase 1 首次成功触发日志）

**现象**：首次成功触发（12:00:30Z）的 docker logs 中出现：
```
WARNING sel_engine.features.flow funding_rate redis read failed for BTCUSDT: could not convert string to float: b'{"rate": 0.0}'
```

**根因**：`sel_engine/features/flow.py` 中 `get_funding_rate_from_redis()` 使用 `float(raw_value)` 解析 Redis 存储的 funding_rate 值，但实际值格式为 JSON 字符串 `{"rate": 0.0}` 而非纯数值。collector 写入格式（JSON）与 reader 解析方式（float()）不匹配。

**数据流影响**：`funding_rate` 特征始终为 None。`funding_rate` 不在 `WIKI_REQUIRED` 集合内（不阻止 StateRecognizer 运行），当前 cold start 不受影响。但 OI_hurst、price_autocorr 等特征依赖 funding_rate 的场景（若有）将缺失该输入。

**不处理后果**：`sel_features.funding_rate` 列永远为 NULL；WARNING 每 bar 出现一次；若 funding_rate 未来进入 WIKI_REQUIRED 则变为阻断性 bug。

**相关文件**：`sel_engine/features/flow.py::get_funding_rate_from_redis()`（不在当前修改范围内）

**推荐修复方向**（等决策，属 sel_engine/features/ 禁区）：将解析改为 `json.loads(raw_value)["rate"]`，或要求 collector 写入纯数值格式。

---

## EC-15：paper_trading/db/trail_store.py 大小写列访问（B = 中优先度，延迟触发）

**发现日期**：2026-04-28（Task 2.2.3 全代码 grep）

**现象**：`paper_trading/db/trail_store.py:162-166` 使用 `row["H"]` / `row["TF"]` / `row["OI"]` / `row["LV"]` 读取 `sel_decision_trail` 表数据。`sel_decision_trail` 表创建时使用无引号大写列名（`H NUMERIC(10,8)`），PostgreSQL 自动折叠为小写 `h`，导致 `row["H"]` 会 KeyError。

**为何 cold start 期间未触发**：`state=None`（cold_start=True）时，paper_trading 的决策层对所有 instrument 输出 `NO_ACTION`，不写 `sel_decision_trail` 记录，故读取路径未被调用。

**触发条件**：cold start 结束（2026-05-28T11:00:00Z）后，首次 state ≠ None 时 paper_trading 写入并读取 trail 记录 → 崩溃。

**相关文件**：`paper_trading/db/trail_store.py:162-166`，`sel_decision_trail` DDL

**推荐修复方向**（冷启动完成前 1 周，约 2026-05-21）：将 `row["H"]`、`row["TF"]`、`row["OI"]`、`row["LV"]` 改为小写键，与 EC-15/16 一并做 schema 一致性审计。

---

## EC-16：paper_interface/store.py schema 版本不匹配（A = 高优先度，延迟触发）

**发现日期**：2026-04-28（Task 2.2.3 全代码 grep）

**现象**：`sel_engine/paper_interface/store.py:81-104` SQL 查询：
```sql
SELECT time, symbol, state, direction, is_cold_start, confidence, reason,
       is_legal, transition_from, health_warning,
       close_price, sigma_p_24h, H, TF, OI, funding_rate, LV, feature_completeness
FROM sel_state_sequence
```
这些列（`H`, `TF`, `OI`, `LV`, `close_price`, `direction`, `confidence`, `is_cold_start`, `health_warning`, `is_legal`, `feature_completeness`）在当前 `sel_state_sequence` schema（migration 001）中**根本不存在**。当前 schema 只有：`time, symbol, state, none_reason, reason, cold_start, is_legal_transition, transition_from, feature_quantiles`。

**根因**：`paper_interface/store.py` 是针对旧版 `sel_state_sequence` schema 开发的，Task 2.2 重建时 schema 已简化（特征数据分离到 `sel_features` 表），但 `paper_interface` 未跟进。这不是大小写问题，是两个 schema 版本之间的断层。

**为何 cold start 期间未触发**：同 EC-15，cold start 期间 paper_trading 不读取状态数据。

**触发条件**：cold start 结束后 paper_trading 调用 `StateStore.get_current()` 或 `get_history()` → SQL 级失败（`"column H does not exist"`）。

**影响范围**：比 EC-15 严重——即便把大小写改对，列也不存在，需要重新设计读取逻辑（JOIN sel_features 或修改 sel_state_sequence schema）。

**相关文件**：`sel_engine/paper_interface/store.py:81-104`，`sel_engine/db/migrations/001_create_sel_state_sequence.sql`

**推荐修复方向**（冷启动完成前 1 周，约 2026-05-21）：
- 方案 A：改 SQL，从 `sel_features` JOIN 所需字段
- 方案 B：在 `sel_state_sequence` 加回 `close_price, sigma_p_24h, H, TF, OI, LV` 等列（反规范化，但下游兼容性好）
- **建议先做完整 schema 一致性审计**（可能还有其他未发现的消费者）
