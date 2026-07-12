# STATUS — Selene / Helios

⚠️ **Data constraint (2026-07-11, forensics below):** `v2_state_history` contains a
one-time full-history backfill for `timestamp < '2026-06-15'` (4272 rows, all sharing
a single write transaction — see the v2_state_history backfill forensics note). Any
query treating this table as "what the live deployment observed in real time" **must
explicitly bound `timestamp >= '2026-06-15'`** or it will silently mix backfilled rows
into a live/operational metric. Clean, purpose-built historical state annotation
(2yr, degraded-feature-aware) lives in `v2_state_annotation` instead — use that for
any offline/historical analysis.
*(2026-07-11 instrumentation)* v2_state_history 为当前代码全历史视图,非逐时存档;
首写时刻 `first_written_at`(NULL=仪表化前),漂移标记 `rewritten_at`(非空=判定被
改写)。漂移检查:`SELECT count(*) FROM v2_state_history WHERE rewritten_at IS NOT NULL;`

Canonical task board. Source: trader-view audit (2026-06-30, 7-subsystem deep read).
Numbering is **this audit's** backlog, distinct from the earlier 24-item / 5-subsystem
rounds (see memory `opt-pr-3`).

---

## 🔒 Never

- **Never enable live mainnet trading.** `EXEC_MODE` MUST stay in a non-live mode
  (`NOTIFY_ONLY` or `PAPER` — PAPER is pure simulation: no exchange adapter calls,
  fills priced off `cw4:prices`). No deployed strategy has out-of-sample alpha
  evidence yet. Do NOT set `AUTO_EXEC` / `CONFIRM_THEN_EXEC` with
  `ENVIRONMENT=production`, and do NOT remove the `_assert_safe_exec_mode` boot
  guard or the `I_UNDERSTAND_LIVE_AUTO_EXEC` ack requirement.
  *(2026-07-10: wording amended by explicit user decision — original said "MUST stay
  NOTIFY_ONLY"; user instructed enabling paper order placement for the v4 chain.)*
- Never weaken a risk gate to make a test pass. Gates fail closed.
- Never treat a missing/None feature as a confirmed signal (three-state discipline).
- Never silently smooth a *signal* in the sel_v2 path (state-machine dwell is OK).

---

## 🔄 In Progress

- **v2.2 lens batch (2026-07-11) — 实施完成,进入 Month-3 采集期**:三视角同数据实证
  (sel/缠论/ICT,`analysis/lens_{sel,chan,ict,verdict}_v1.md`)→ CHAN-1 触发失败标准
  **废弃**(A/C 前向收益无差异,p=0.44/0.79);CHAN-2/CHAN-3/ICT-2 接入 paper engine
  obs refresh(vocab `chan_divergence`/`chan_pivot`/`swing_structure`),ICT-1 VPIN 独立
  服务 `v2-vpin-monitor`(vocab `vpin`)。全部 observation-only,零 `states/**`/
  `strategies/**` 改动(注:同日稍后 Coiling 判据经用户授权放宽并重置 epoch → a335521f,见 Done)。
  **待办**:H-ICT1a/b 数据不足,~2026-08-05 tick 满 30 天后重跑
  `python -m sel_v2.offline.lens_study`;Month-3 评估 vpin 须按 metadata.history_days
  剔除 <30d(tool_evaluator 已含 5 个新映射)。出池记录见候选池文档
  `analysis/v2_2_candidate_pool_v1.md`。
- P2 cleanups (P2-4 tie-aware Spearman, P2-5 vwap, P2-3 router split). P2-1 done (see below).
- SEL2-SPEC-GL1 Phase 0 (T0.1–T0.6) **done and deployed** (see below). Validation epoch:
  first epoch (e678e8fb, started 2026-07-08 23:17) was **reset on 2026-07-09** per R1 —
  the CUSUM threshold-deadlock fix (below) changes `strategies/cusum_short.py`, which
  invalidates the running epoch by design; human approval = explicit user instruction
  ("调整使其能开仓", 2026-07-09). New epoch **fe4782f9 started 2026-07-09 23:52 UTC**
  (status CLEAN) — 30-day clock restarted; no further `states/**`/`strategies/**` change
  without another explicit reset.

---

## 📋 Backlog — P0 (live-safety / signal-trust; must precede any live ambition)


## 📋 Backlog — P1 (core capability gaps)


## 📋 Backlog — P2 (cleanup / UX)

- [ ] **P2-2** Misleading observation-tool names — deferred (docstrings already self-disclaim;
      rename is high-import-churn for low value).
- [ ] **P2-3** smart_router cross-venue split not executed — deferred (single-venue +
      NOTIFY_ONLY make it low-value now; needs live multi-venue validation).

---

## ✅ Done

### C 批次:状态机校准三件套(2026-07-12,用户授权"授权你处理")

- **C1 Calm 判据空隙修复**:`check_drifting_calm` 去掉 σ 下界([30,60)→<60 分位)——
  "安静但无蓄能证据"的 bar 有了真归属,不再靠仲裁回退粘在旧状态(07-11 Charged×30
  粘性的另一半根因)。副效应良性:Surging 腿的衰竭对 σ 单调(此前 σ 直接跌破 30 分位
  的腿反而被粘更久,是 artifact)。权威标注影响温和:Calm 3105→3110、Coiling 12→10、
  Charged 43→34,**13 条腿完整不变**,CHAN 检验结果不变
- **C3 Surging 方向字段**:recognizer 填 `sub_state`=Up/Down(入场 bar 用突破方向,
  后续 bar 用 close 对入场价符号,因果)——V22 系列"腿方向全靠事后推断"的老账关闭;
  db_writer upsert 守卫补 `sub_state`(否则历史行永不回填);历史 Surging 行经 live
  重放自动补方向,rewritten_at 全程留痕
- **C2 CUSUM-Short 核查:不改参**。触发管道健康(离线复刻与 live v2_cusum_events 完全
  一致:678 次/2yr ≈6.4/周,最后触发 06-25;当前静默=σ p10 极静市场的正确行为)。
  旧结论"CUSUM 稀疏是 S2 瓶颈"已过时(那是 07-09 已修的死锁)。**留档发现**:自适应
  阈值在 4H 节奏下结构性不激活(7 天/20 峰窗口为 1 秒 bar 设计)→ 实际恒为 static
  h=2.0;实测该 static 值触发率合理,真自适应会令 h 掉向噪声峰、触发暴增,故按
  "改参需证据"纪律**有意保留现状**。若未来要真自适应需专项评估
- **仓库-线上一致性闭环**:已部署未提交的 07-09/10 工作(CUSUM 死锁修复、GL1 T0.1
  熵方差布线)补提交(769b313)——epoch 指纹不再依赖未提交工作树
- **epoch**:a335521f → 1c8a86ee → **31bccad5 CLEAN**(同一授权批次两步,reason 全记录;
  30 天时钟 2026-07-12 03:55 UTC 起)

### 优化批次 A+B1:CI/nginx/月度评估/08-05自动化 + SMT 实证废弃 (2026-07-12)

- **A1** CI 修复(python 3.11→3.12,此前依赖装不上,CI 死于出生)
- **A2** nginx resolver+变量 proxy_pass:gateway recreate 不再打断前端(已验证换 IP 后不重启即通)
- **A3** `v2-tool-eval-monthly` 服务(每月 1 日 01:00 UTC 全工具评估→v2_tool_evaluation_results
  +decision_trail 摘要);tool_evaluator 增 vpin history_days≥30 过滤(pilot 期正确剔除)
- **A4** 08-05 补跑全自动化:H-ICT1a 正式检验与 ICT-7 清算-sweep 已实现(数据守卫,
  现打 PENDING),host cron `23 9 5 8 *` 跑 `sel_v2/tools/rerun_20260805.sh`(自删,
  不自动 commit,verdicts 留人工按池纪律裁决);venv=~/.venvs/selene-analytics
- **A5** platform-postgres-backup unhealthy 诊断:selene 每日备份正常✓;根因=备份清单含
  不存在的 infisical 库(每日 Exit 1);且 marketdata(iris 原始数据)不在备份清单
  ——修复在 helios-platform repo,待人工定夺(见 Needs Human)
- **B1** SMT divergence(BTC/ETH)实证 **fail** 废弃(bear 方向反,p=0.73;详
  `analysis/smt_v1.md`);ETH-USDT 4H 深史已回填 v2_bars_4h(4380 bars,一次性快照)

### ICT"最新技术"扫描 + 实证:四中取一,Killzones 通过并接入 (2026-07-11)

用户指令实证 ICT 最新技术。扫描(SMC 生态零同行评审;清算级联学术有据但自有数据仅数日)
→ 四候选预注册实证(`analysis/ict_advanced_v1.md`):
- **ICT-3 Killzones PASS**(KW p=5.8e-26/6e-145,对半稳健,峰值 12:00 UTC)→ 实施为
  时段调整异动工具 `killzone_anomaly`(ObservationRunner 第 11 工具,同时段滚动 p90
  联合越限);案例库案例 9 关闭为 VALIDATED;图表加 🟦"时段异动"标记
- **ICT-4 Sweep / ICT-5 FVG / ICT-6 OB 全部废弃**:sweep-up 实为延续(+0.27%,p=0.81);
  FVG 回补率 73% 与零售宣称一致但无 null、首触支撑不显著;OB bull 回访反向(-0.16%)
- **清算-sweep 关联 PENDING**:与 VPIN 同期 ≈2026-08-05 补跑(v2_liquidations 07-06 起)
- 出池记录入候选池文档;tests 636 passed(唯 5 既有环境失败)

### 缠论"最新技术"扫描 + 实证:双 NO-GO,无新增入池 (2026-07-11)

用户指令实证缠论最新技术。扫描(czsc/chan.py/社区,2024-2026 学术零同行评审)后
可机械化真空白仅二,均与既有实现做**受控对照**(`analysis/chan_orthodox_v1.md`):
- **CHAN-4 正统分型/笔分割:废弃**——腿方向一致率 5/13 劣于 zigzag 基线 7/13;
  笔中枢前向波动分离 p=0.96 fail 劣于 zigzag p≈0(pass)
- **CHAN-5 MACD 面积背驰:废弃**——p=0.23/FP 94% 劣于 CHAN-2 动量比 p=0.067/FP 90%
- ML 买卖点管线(样本差 2-3 量级)/区间套(违 4H 单锚点)/一二类买卖点等维持排除
- **净结论:既有 CHAN-2/CHAN-3 不劣于甚至优于缠论正统实现,维持现状**;
  出池记录入 `analysis/v2_2_candidate_pool_v1.md`;`sel_v2/offline/chan_orthodox*.py`
  留作后续对照工具(offline-only)

### Coiling 判据放宽(2-of-3)+ epoch 重置 a335521f (2026-07-11,用户授权)

用户质疑"两年 Coiling/Cascade 未出现"→ 调查确认双层原因(历史=结构性特征缺失,
现在=四条件 AND 在真实数据上近乎不可满足,逐 bar 被单一条件否决)→ 用户授权放宽。

- `states/conditions.py check_coiling`:σ<30pct 必要 + 蓄能篮子 2-of-3(熵/OI/funding);
  <2 个可判信号 → met=None(三态纪律不破;降级历史 None 路径不变,Calm 3105 不动)
- 效果:2yr 标注 Coiling 0→**12**(Charged 43→34),live 最新 bars 判 Coiling;
  Coiling→Surging Release 通路首次可用;Charged 粘性段自然解开(未动仲裁回退——
  该回退同时是 Surging 腿持续的机制,不可乱改)
- **epoch 重置**:af6f7d3d(DIRTY)→ **a335521f CLEAN**,reason 含用户授权原文,
  合并处置 db_writer 仪表化 DIRTY;30 天验证时钟重启 2026-07-11 12:55 UTC
- 漂移仪表首次真实工作:v2_state_history 12 行改写全部带 `rewritten_at` 戳
- 2yr 标注已重跑刷新;lens 报告重生成(报告文案改数据驱动,新增 Coiling lift 域:
  首批 12 个 Coiling bar 与缠论高中枢重叠不重合,n 极小谨慎解读);Cascade=0 维持
  (真实级联事件本就稀有 + 历史无清算数据,属预期,未放宽)
- 测试:617 passed(新增 2-of-3 判定用例;唯 5 个既有环境失败不变)

### v2_state_history 回填溯源 + 消费方审计 (2026-07-11)

跟进 V22-D 的"06-15起显式截止"假设——溯源确认、复发排查、消费方逐一核对。

- **溯源**:`timestamp < '2026-06-15'` 的 4272 行 xmin(PG 事务ID)**全部相同**
  (940363),证明一次性单事务写入,非逐bar累积。该事务ID远低于本库其他所有已知
  锚点(如 epoch e678e8fb 插入时 xmin=29,180,933,对应 2026-07-08 23:17 UTC),
  说明写入发生在数据库生命周期很早期。对照 git log:commit `bf80c6e`
  (2026-06-30 23:28:15 +08,"BTC candlestick chart...")提交信息明确写道
  "Repopulated v2_state_history with current-code states so markers are
  meaningful"。repo 内唯一具备"全量覆盖写入 v2_state_history"能力的代码是
  `sel_v2/scheduler/replay.py`(手动 CLI,`--reset` 可先 TRUNCATE 再重放;数据源
  是本地 parquet 快照而非 v2_bars_4h,Wave 1 遗留)——判定为该次回填的执行工具,
  在 bf80c6e commit 附近手动跑的(未作为服务部署,git 无记录具体调用时刻)。
- **复发排查**:全 repo grep 确认写 v2_state_history 只有 2 条代码路径——①
  `sel_v2/paper/paper_engine.py`(live 部署,`v2-paper-engine` 服务,每周期把当前
  code 重算的全量 state 提交 upsert,`WHERE ... IS DISTINCT FROM` 令未变化的
  行不产生新 xmin,故老 bar 长期原地不动);②`sel_v2/scheduler/replay.py`
  (手动 CLI,未接入任何 docker-compose 服务)。TRUNCATE 只经 replay.py 的
  `--reset`。无第三方写入路径。
- **消费方审计**(7个查询点,4个文件):`services/healthcheck/main.py`
  (dwell检测 LIMIT 200≈33天,当前不越界但 Drifting_Calm p90 dwell 达94天时可能
  跨06-15)、`sel_v2/paper_interface/api.py` 的 5 处(24h/LIMIT100/LIMIT1 天然安全;
  30天窗口越界4天;`/sel/chart` 默认300bar≈50天,常规越界但该端点用途是人工看图,
  不区分 live/回填不影响可用性)、`sel_v2/evaluation/tool_evaluator.py`
  (`lookback_days=90` 默认值,**无 06-15 护栏**,若被调用会把回填期数据当作
  live 观测——该工具尚未在生产跑过)、`sel_v2/tools/golive_report.py`
  (epoch CLEAN 时用 epoch 起始时间,当前安全;NO_EPOCH 回退 30 天窗口越界4天,
  ~07-15 后自然消解)。STATUS.md 顶部已加数据约束提示。

### Wave V22-D:Live 捕获率周监控 + V22 收尾裁决 (2026-07-11)

V22-A/C 收尾:历史捕获率(29-39%)被特征降级污染不可外推,建立前瞻性周监控。

- `sel_v2/offline/capture_monitor.py`:复用 leg_census.py 三档阈值(未改动),对近
  90天 v2_bars_4h 比对 v2_state_history(比对域=2026-06-15起显式截止,因
  v2_state_history 已被 07-01 SEL live-ops 回填全history,行存在与否不再能区分
  "真live"vs"回填",故用显式日期而非行存在判定域)。写 `v2_capture_rate_weekly`
  表 + 追加 `sel_v2/reports/capture_rate_weekly.md`。
- **部署**:新增独立轻量服务 `v2-capture-monitor`(docker-compose.yml + 复用
  sel_v2/Dockerfile,零接触任何 live 策略/状态容器),每周一 00:30 UTC 自动跑,
  失败 ERROR 日志不崩溃(同 healthcheck 惯例)。已 build+部署+手动触发首轮,
  下次调度经容器日志确认:**2026-07-13 00:30 UTC**。
- 首轮(样本仅~4周,预期之内):3×ATR 15腿/1规格/0入域;5×ATR 5腿/3规格/1入域/
  1捕获/100%;8pct 5腿/3规格/1入域/1捕获/100%。
- `v2_decision_trail` 写入 `decision_type='v22_verdict'` 收尾三条裁决:①Wiki周期
  模型对一半(规格腿~9条/年真实存在,但总腿量2-3倍模型且更短更杂);②父状态机历史
  漏检为真但归因被污染,live捕获率待本监控满12周(约09-15前)判定;③分支B(V22-A
  pyramid)**封存**(非废弃)——重开需≥12周live数据 +(若确认漏检)states/**修复
  (超出范围,需另行授权)+ 离线门重跑;S1 定位低频补充,S2/CUSUM-Short 为现行主力
  引擎。

### Wave V22-C:趋势腿普查 (2026-07-11)

V22-A 门失败归因:2年 BTC-USDT OHLC 里,符合 Wiki 规格(时长10-35天、push 3-6)的
趋势腿到底存不存在,父状态机 Surging 标注捕获了多少?纯计数,零策略参数/门槛/仿真。
`sel_v2/offline/leg_census.py`(唯一新文件,按红线),三档粗层阈值(3×ATR/5×ATR/
固定8%价格)独立全跑全报,细层 push 计数与 substate.py 同参(1.5×ATR)。

`sel_v2/reports/trend_leg_census_v1.md` 原始裁决(不下结论):

| 阈值 | 总腿数 | 符合Wiki规格 | 其中被父状态机捕获(≥50%重叠) |
|---|---:|---:|---:|
| 3×ATR | 139 | 8 | 2 |
| 5×ATR | 61 | 18 | 7 |
| 8%价格 | 49 | 17 | 5 |

漏检腿的 bar 里 86.9%-96.8% 被标注叫 Drifting_Calm(而非 Surging)。数字上报,
按 Wave 明示条款不做归因结论。

### Wave V22-A:v2.2 离线验证门 (2026-07-11)

D1-D5(v2.2 设计草案拍板)的离线 gate,三门裁决,**不进 V22-B**(按本 Wave 明示条款,
无论过没过都停):

- **Part 1** `sel_v2/offline/substate.py`:Surging 子状态机(Impulse/Pullback/
  Consolidation + RE_PUSH/STRUCTURE_BREAK/TERMINAL_FLAG),纯函数,offline-only,
  7 单测(容器内手动跑绿,本地无 pytest)。
- **Part 2** `sel_v2/offline/branch_b_sim.py`:分支 B 金字塔仿真器(D1/D2 30/45/25%
  分批、D3 移动止损、D4 terminal 减半、D5 30天时间止损,taker+滑点成本),10 单测绿。
- **Part 3** `sel_v2/offline/v22_gate_report.py` + `sel_v2/reports/v22_offline_gate_v1.md`:
  对 2 年 4429 根 BTC-USDT 4H 真实数据跑出裁决 —— **H-V22-1(触发次数≥20)= 9,FAIL**
  (2年仅13段Surging,与 Wave S2C 基线一致的结构性稀缺);**H-V22-2(每腿净期望>0)=
  -0.708%,FAIL**;**H-V22-3(止损假阳率<40%)= 0%(0/4),PASS(小样本)**。
  `v2_decision_trail` 已写入 `decision_type='v22_design_ruling'` 一行(D1-D5 原文 +
  裁决数字)。
- **过程中发现并修的真 bug**(非调参):跑真实数据时发现原方向推断(3根bar回看)把全部
  13段都判成"多头",含数段实际跌超15-20%的段——换更长回看窗口(6/12/24/48)依然如此,
  说明 Surging 进入条件本身可能在触发瞬间总是伴随短线上跳,与该段真实趋势无关。改为用
  segment 自身实际净变动定方向(离线全量回放语境下合法),修后 9 腿里 8 空 1 多。已作为
  Wiki 层面的发现记入报告(暗示 live 状态机本身可能缺 Down 触发路径,而非仅子状态机的
  bug)——不在本 Wave 范围内动 states/**。
- 红线全程遵守:未碰 live 代码路径 / epoch / v2_state_history / v2_trades;仅动
  `sel_v2/offline/**`。

### V4 信号链复活:market-scanner 小币趋势发现 (2026-07-10)

v4 链(signal→portfolio→risk→execution)架构完整但两年"无头"——`market.candles`/
`market.raw` 全仓库零生产者,signal.scored 恒 0;且不存在任何选币/扫描组件。本轮补齐:

- **新服务 `services/scanner/`(market-scanner)**:①发现循环——一次 REST 拉全市场
  24h ticker(Binance USD-M,与 execution adapter 同 venue,经 helios-proxy),按
  流动性下限($20M)×24h 涨幅(≥5%)×量能激增(vs 上轮快照,截断 [0.5,3])排序取
  top-5,写 `cw4:moonshots`(gateway `/api/v4/moonshots` 由死端点复活);②喂送循环——
  watchlist(核心 BTC/ETH/SOL + 发现币)新符号回填 249 根 1h K 线热身 regime 检测器,
  之后每根新收盘 K 线发 `market.candles`(RSI/EMA20/50/200 由扫描器算好),每 3min 发
  `market.raw`(funding/OI 变化/多空比)。REST 客户端 3 次重试(实测 helios-proxy 上游
  socks5 阵发抖动,iris 同款重试稳定多日)。15 个纯函数/契约单测全绿。
- **signal 服务 `backfill=true` 契约**:回填 K 线只热身 detector/缓存不评分(否则对
  历史价格发信号 + 占用 1h 冷却窗)。
- **修复两个数据一来就炸的潜伏依赖**:signal(`regime/detector.py` 的 `oprim.atr`、
  HMM 的 `pearson_spearman_corr`)与 risk(`var_engine` 的 `value_at_risk`)镜像缺
  vendored `oprim`——此前 `market.candles` 永远无消息,这些懒加载路径从未执行过。
  两个 Dockerfile 照 sel_v2 模式补 `COPY vendor/oprim` + eager deps。
- **实测端到端(2026-07-10)**:扫描器发现 TAC(+100%)/US(+56%)/TAG(+31%)等 5 个真实
  趋势小币 → 种子回放 regime 迁移正常(检测出 TRENDING_UP/DOWN)→ **signal.scored 首次
  非零**(TACUSDT SHORT_SETUP win_p=0.67 actionable、SOLUSDT SHORT win_p=0.61)→
  portfolio 完成 Kelly 定仓(SIZED ... kelly=0.229 notional=$1000)→ execution 按
  NOTIFY_ONLY 设计停在门口(🔒 Never:EXEC_MODE 不得离开 NOTIFY_ONLY,已遵守)。
  注:首批信号方向是 SHORT(对 +100% 抛物线币,composite 的 RSI/funding 反挤压逻辑
  判定超买回落)——这是评分器的既有设计,趋势跟随型(LONG_SETUP/TREND_CONFIRM)在
  TRENDING_UP regime 下会被加权放行,未为"演示好看"而改因子方向。
- 已知降级(非阻塞):HMM 对新表冷启动 range、动态权重回退基础权重、risk 动态相关性门
  在 candles 数据积累前走静态 CORR_GROUPS(小币不在组内=放行,相关性聚集暂无守卫);
  onchain 因子仅覆盖 BTC/ETH/SOL,小币恒 0。

**PAPER 开单落地(2026-07-10,用户拍板"一定要让能开单,小币种衰竭开空也可以")**:
- `EXEC_MODE=PAPER` 在 compose 写死(🔒 Never 同步修订为"禁 live 模式";`.env:34`
  成死配置待人工清理,见 Needs Human)。PAPER = 纯模拟撮合,零 exchange adapter 调用。
- 落地过程中排掉的四个连环障碍:①v4 全套 PG 表(orders/signals/candles/audit_log 等)
  从未建过——`infra/timescaledb/schema.sql` 首次应用到 selene 库 + selene_app 授权
  (表+sequence);②`cw4:prices` 无人写(PAPER 成交定价 + monitoring_loop 的 SL/TP
  监控全靠它)——market-scanner 每 60s 写入 watchlist 实时价;③`cw4:execution:halt`
  熔断键是 2026-07-03 的陈年遗留(deadman_heartbeat_stale,链还是死的年代 healthcheck
  设的,永不过期),经操作员端点 `/execution/halt/clear` 清除;④risk 的 VaR/相关性门
  确认对缺表优雅降级(不 fail-closed),放行安全。
- **首笔 PAPER 成交实证(2026-07-10 10:07 UTC)**:VELVETUSDT(发现的趋势小币)
  SHORT_SETUP win_p 达标 → Kelly 定仓 $1000 → risk.check→risk.approved 全程过门 →
  FSM 走完 SLIPPAGE_ESTIMATE→ROUTING→SUBMITTING→PENDING_ACK→OPEN→FILLED→**MONITORING**,
  `[PAPER] filled VELVETUSDT SELL qty=2103.05 price=0.4903`,orders 表落库,
  order.lifecycle 事件发布,SL/TP 监控读实时价运行中。SOL/TAC 空单信号随后跟进。

### 策略开仓能力修复 (2026-07-09) — S1/S2 为何两年零开仓,及处置

Evidence-first diagnosis (decision-trail audit + full-history simulation over 4,422 bars):

- **S2 从未启用**:`v2_strategy_params` 空表 → `HawkesParams.from_h2_reference()` 抛错 →
  S2 整体禁用。**处置**:跑了设计好的 Wave 1 校准 `calibrate_all --from-db`(η=0.556 次临界,
  h2_mu/alpha/beta_ref + branching_ratio_threshold + tda1_l1_p90/95/97 全部落库),S2 现已启用
  ("Loaded 7 strategy parameters",disabled 警告消失)。零代码改动。
- **CUSUM 自适应阈值死锁(真 bug,已修)**:`cusum_short.py` 只在**触发时**记录峰值,但阈值
  要 ≥20 个峰值样本才从冷启动 2.0 切到"滚动 p95 峰值"——没触发就没样本,没样本阈值就永远
  卡 2.0,而 4H bar 上 C 在状态门开着的 bar 里最高只到 1.26 → 鸡生蛋死锁,自适应机制从未
  激活。**修复**:excursion 自然衰减归零时记录峰值(每段 excursion 恰好采样一次,触发分支
  不再重复记录)。4 个新测试(无触发也能热身/对自适应阈值触发/不重复采样/无时间戳保持旧行为),
  原 15 个测试全过。
- **S1 结构性稀缺(设计属性,未改)**:全历史联合仿真显示 CUSUM 触发(9.2% bars)全部落在
  Surging(S1 禁入态)——趋势推高 CUSUM 的同时把状态机推进 Surging,两个条件天然互斥;
  Coiling 两年出现 0 次,Drifting_Charged 36 次且全在 2026-07(OI/funding/entropy 数据可用
  之后)。修复自适应阈值后 S1 联合条件仍为 0 次(阈值在趋势市自适应地更高)。**不为强行
  开仓魔改 S1**——它的开仓能力取决于数据完备时代积累出的蓄能状态,7 月起 near-miss
  (C=1.26 vs h=1.69)已在出现。S2(仅 Cascade 禁入,趋势里可交易)才是实际交易主力。
- **paper 引擎校准阈值接线**(`paper/paper_engine.py`,壳层):`_reprocess_inner` 现从
  v2_strategy_params 读 `h2_branching_ratio_threshold`/`tda1_l1_threshold_p95` 传给引擎
  (replay.py 早有同款,paper 路径一直漏接,校准后仍跑硬编码默认值)。
- 顺手修复:`_s1/_s2_trail` 在 `__post_init__` 初始化(修好存量失败
  `test_s2_opens_when_all_conditions_align`——恰是"S2 全条件对齐会开仓"的端到端证明);
  `test_nav_is_consistent_with_realized_pnl` 的"结束时必无持仓"是冻结阈值年代的场景假设,
  改为对开仓情形也成立的 NAV 守恒式。510 passed,存量失败 6→5。
- **R1**:cusum_short.py 属 `strategies/**` 冻结层,依用户明示指令修改并重置 epoch(见上)。

### GL1 T0.1–T0.4 (2026-07-08) — `docs/SEL2-SPEC-GL1.md`

- **T0.1** LOB entropy variance de-STUB'd: `sel_v2/features/lob_entropy.py` computes rolling
  variance of `entropy_4h` (6-bar/24h window) + 3-bar monotone rise, wired through `BarRunner`
  into `states/critical_logic.py` A2 (was permanently `None`). Verified against real production
  data: 31/34 bars non-None, 7 bars `entropy_variance_rising=True`; Critical Path 1 (A_full)
  confirmed reachable end-to-end (`tests/sel_v2/test_lob_depth_pctile_wiring.py`).
- **T0.2** Sampling-density analysis (`docs/reports/GL1-T0.2-lob-sampling.md`) — **resolved,
  no longer NEEDS-HUMAN**: initial stride-thinning test showed degradation (Pearson 1.0→0.89
  at 120s), inconclusive on its own. Self-verified with two follow-ups on the full 28,167-
  snapshot series: (1) ACF of the raw entropy series drops to 0.094 at lag=1 (60s) — already
  near-memoryless at 60s, so there's no slow structure a coarser regular sample could miss;
  (2) 200-trial random-subsample control at matched sample counts reproduces the same
  degradation as regular-stride thinning (120s: stride Pearson=0.904 vs random 0.915±0.025;
  300s: stride=0.791 vs random 0.745±0.080) — stride and random subsampling are statistically
  indistinguishable, which is what you'd see if the degradation is pure estimator noise from
  fewer samples, not systematic aliasing of real fast dynamics. **Conclusion: current ~60s
  cadence is adequate for entropy_variance; no WS depth-diff follow-up spec needed.**
- **T0.3** P2-1 (D1) landed: `v2_strategy_decision.decision_trail` JSONB column (schema.sql)
  now carries the full numeric snapshot (OFI/CUSUM/funding/OI/entropy/hawkes/tda + thresholds)
  each bar's decision actually used, written by `StrategyEngine._bar_snapshot` /
  `DBWriter.write_decision_trail_bulk`. `ofi_persister` removed from compose;
  `v2_ofi_features` marked DEPRECATED (kept, not dropped) — was a write-only duplicate of the
  same data the engine already computes inline (see prior Needs-Human entry, now resolved).
- **T0.4** Staleness matrix landed: `sel_v2/runtime/staleness.py` is the single point that
  decides ticks(90s)/funding_oi(300s)/bar_4h(missing boundary)/lob(300s) freshness →
  enforcement, per the GL1 T0.4 matrix (17 unit tests, one per matrix cell). Wired as
  shell-layer guards in `StrategyEngine` (blocks S1/S2 new entries, suppresses only the
  CUSUM-reversal exit via its existing `priority` field — drawdown/time/Cascade exits
  untouched, `strategies/**` itself unmodified per R1) and only applied to the *current* bar
  of each replay (staleness is a live/now concept; historical bars in the same replay aren't
  retroactively flagged). `PaperEngine._compute_staleness` queries real freshness each cycle
  and logs transitions to `v2_staleness_events`; verified live against the running stack (all
  4 sources currently fresh, ages 4.7s–3.8h, events table populated).
- **T0.5** `sel_v2/tools/epoch.py`: fingerprint = sha256 over every `.py` file under
  `sel_v2/states/`+`sel_v2/strategies/` (sorted, content-hashed) + git HEAD (degrades to
  `"unknown"` when `.git` isn't present — containers exclude it via `.dockerignore`, not
  faked). `v2_paper_epochs` table (append-only, current = latest `started_at`); CLI
  `python -m sel_v2.tools.epoch {start --reason "..."|status}` reports CLEAN/DIRTY/NO_EPOCH.
  11 unit tests incl. DIRTY-on-edit for both roots and non-.py/`__pycache__` exclusion;
  live-smoked against the real repo + DB (start/status round-trip verified, smoke-test row
  removed afterward — no epoch has actually started yet, that's a G0-gated human action).
- **T0.6** `python -m sel_v2.tools.golive_report --gate G0`: epoch status, None-prone field
  fill rates (entropy_variance_rising/oi_change_rate/funding_persistent, read straight from
  T0.3's `decision_trail` — no second recompute), 6-state distribution, Cascade/Critical
  counts, P2-1 liveness, staleness (current + 30d transitions), RED/GREEN per check + overall.
  **T0.1–T0.5 redeployed** to make the numbers real (not a code exercise): rebuilt
  `selene-v2-paper-engine`, restarted (old `v2-ofi-persister` container stopped/removed per
  T0.3), full historical replay (4414 bars, TDA+Hawkes+entropy_variance+decision_trail+
  staleness all live) completed cleanly, zero errors. `golive_report` verified twice —
  pre-redeploy (`P2-1=0/180`, fill rates 0%, proving RED-with-detail works) and post-redeploy
  with real numbers: `P2-1=179/179` (decision_trail genuinely live), fill rates 16–18%
  (entropy_variance_rising/oi_change_rate/funding_persistent — real, evidence-based gaps: these
  windows need 4–6 consecutive bars to fill, only ~5.5 days of history exist since the P3/P1
  migrations), `epoch=NO_EPOCH` (correct — no epoch started yet, that's the G0-gated human
  step). **OVERALL: RED**, as it correctly must be — G0 will stay RED until a real 30-day
  epoch accrues (D3, GL1's own framing, not a bug) and someone runs `epoch.py start`.

### Full-health audit + fixes (2026-07-03) — see `audit/2026-07-03_full_health_audit.md`

Live-stack体检发现 docker `healthy` 是假象：**23 张表全空**（OKX 在本环境被全局封锁，
采集器 13h 断供）、v4 `signal.scored`=0、healthcheck 对空表隐形。已修 + 实证：
- **P0-a** OKX 全局不可达 / Binance 经 `helios-proxy:2080` 可达；compose proxy 默认值改为可用代理。
- **P0-c** 新增 `sel_v2/data/binance_backfill.py`，`v2_bars_4h` 从 Binance 回填 **4380 bars（2yr，已落库）**。
- **P0-d** healthcheck 新增空表检测（STALE 或 EMPTY 都告警）——补上让 13h 全断供隐形的盲区。
- **P1-a** onchain→signal 桥修 2 个 import bug 并接线；`signal.raw` 不再是孤儿流（已实证消费）。
- **P1-b** `/metrics` 误用未初始化的 `redis_client.health_check` → 改 `connections.redis_health`（6 服务）。
- **P1-d** composite `EFFECTIVE_WEIGHTS`：social/orderbook 死权重置零 + 重归一化，score 不再被稀释。
- **P1-e** `shared/db/connections.py` 加 asyncpg/redis 超时（治 EC-13 静默挂起 + DNS 抖动）。
- **P2-c** `backtest/costs.py` 增 per-symbol 成本档 + `cost_params_for()`，engine 按 symbol 取值。

**OKX→Binance 采集器迁移(2026-07-03,已实证)**:实证 OKX 与 Binance WS 均不可达,Binance REST 可达
(代理抖动),故迁为 REST 轮询。新增 `sel_v2/data/binance_rest.py`;`v2_derivatives_snapshots`
(premiumIndex+openInterest,30s)、`v2_lob_snapshots`(depth,60s)、`v2_bars_4h`(--loop 前向轮询)
**均已实测写入** → 解锁 Coiling/Drifting-Charged(OI/funding)+ Cascade/Critical(LOB)。
`v2_ticks`(REST 有损)未迁、`v2_liquidations`(仅 WS)不可迁 → 见 Needs-Human。

⚠️ **P1-6 已回退**：`e0e1cbc` 删了自带 prometheus/grafana，改由平台中央 `prometheus-agent` 采集
（本审计的 `/metrics` item #12 即为此适配）。STATUS 早前「P1-6 done」记录作废。
⚠️ **部署持久性**：v4 服务源码打进镜像，本次热补丁经 `docker cp`+restart，**需 `compose build` 才持久**。

### SEL live-ops rounds (2026-06-30 → 07-01) — commits `d9297ec`…`ebb636b`

Live-deployment debugging + optimization of the sel_v2 (SEL) subsystem, driven against the
running docker-compose stack (real DB/collectors/paper engine). Distinct from the audit
backlog above. All verified live; all tests green (suite ~1148+).

**Frontend / observability (SEL tab now a real cockpit):**
- `f9949da` — gateway had **no `DB_URL`**, so EVERY `/api/v2/sel/*` PG endpoint 500'd — the
  real reason "S1/S2 were invisible". Added DB_URL to the gateway env. (Root infra bug.)
- `a13ce52` — S1/S2 strategy panel + `GET /sel/strategy/summary` (open/closed/PnL/win-rate
  from `v2_trades`, current state, no Redis).
- `74f3b07` — per-bar "why no entry": engine captures the latest S1/S2 `EntryDecision`,
  persisted to `v2_paper_latest_decision`, shown on the panel (action·step·reason).
- `bf80c6e` — **BTC candlestick chart + regime-state annotation** on the SEL tab (vendored
  TradingView Lightweight Charts, no CDN; `GET /sel/chart` joins bars⋈state). Repopulated
  `v2_state_history` with current-code states so markers are meaningful.
- `d86074c` — chart legend lists all **6 states**, dims the 2 that never occur
  (Coiling / Drifting-Charged — the OI/entropy-gated states, same root as S1 not trading).
- `d76f770` — state-history table columns fixed: the blank 方向/置信度 (no source — the
  state machine is deterministic, `sub_state` always NULL) replaced by real
  from/via/duration; feature-completeness + cold-start derived from `state_features`.
- `6ec73f4` — **counterfactual S1 overlay** on the chart (toggle): assuming the unavailable
  OI/entropy/funding gates pass, S1 had ~68 entries over 2yr (40% win, +11k USDT) — shown as
  ▲/▼/○ markers with a loud "NOT a validated backtest" banner. `v2_counterfactual_trades` +
  `GET /sel/counterfactual`.
- `fde3eee` (#2) — **full per-bar decision trail** persisted to `v2_strategy_decision`
  (self-healing upsert), joined into state-history as an "S1决策" column (per-bar action·step).
- `ebb636b` (#3) — the **7 observation-only tools** (HMM regime/boundary, TDA clustering,
  permutation/transfer entropy, wavelet, Hawkes cascade) had a runner but NO caller — now run
  over the recent window, persisted to `v2_observation_latest` (throttled to new-bar),
  `GET /sel/observations` + a SEL observation panel.

**Signal correctness / data:**
- `d9297ec` — **Coiling/Drifting-Charged never formed** because `entropy_pctile` /
  `funding_pctile` / `oi_change_rate_pctile` were 100% null. Wired LOB entropy into BarFeatures
  and made the rolling-percentile window **adaptive** (emit once ≥30 obs) so a recently-started
  feed produces a percentile instead of waiting for the full 360-bar window.
- `2d90d24` — **liquidation collector filtered the wrong field**: OKX puts `instId` on the
  outer item (details[].instId is None), so `v2_liquidations` was **永远 0** and the Cascade
  liquidation-pulse defense was dead. Now filters on item.instId (captured from the live
  channel; pure `extract_liquidation_rows` + tests).
- `708903d` — `okx_backfill` fetched **spot** candles (historical bars were spot while the live
  feed is perp). Now defaults to the perp `{symbol}-SWAP`. NOTE: measured basis is only ~0.05%,
  so re-backfilling the existing 2yr is **low-value** — code fixed for future, existing data
  left as-is by choice.
- `d837b4a` (P1-4) — `write_states_bulk` now `ON CONFLICT DO UPDATE` (was DO NOTHING) with a
  WHERE guard, so `v2_state_history` **self-heals** on recompute instead of keeping stale
  first-written states (no more manual TRUNCATE+repopulate).

**Performance:**
- `97035f6` (#4) — the full-history replay ran on every tick, recomputing σ/Hawkes/**TDA(ripser
  over ~4500 bars)** each time. Now cached by a closes signature and reused when no new 4H bar
  sealed (the dominant case). Engine output unchanged (verified state_counts identical).

**Diagnosis that did NOT need a code change (documented for the record):**
- **S1/S2 have 0 trades** — verified NOT a bug: the only ~15 days with OI/LOB data were a
  sustained high-vol regime, so S1's entry states (Coiling/Drifting-Charged, which need low/mid
  vol) never formed. Pipeline is wired; S1 will trade when the market consolidates.
- **OI history is unbackfillable** — OKX caps `open-interest-history` at ~16 days (pagination
  no-ops). So a faithful historical S1/S2 OOS is impossible from OKX; a 3rd-party OI source
  (Coinglass/…) is the only path. See Needs-Human.

**Deferred SEL follow-ups (not yet done):** 3rd-party historical OI (for real OOS);
`v2_ofi_features` orphan store (table doesn't even exist — decide wire-or-drop); Cascade
cond-2 needs live liquidation data to actually flow (collector now fixed, awaiting events);
Surging Up/Down direction (sub_state unused); S2 counterfactual (needs tick-driven Hawkes).

- **P2-5** VWAP now NULL (unknown) instead of fake 0.0 where it can't be computed (REST
      backfill + zero-volume bars), so a reader can't mistake a placeholder for a real VWAP.
      `sel_v2/data/{okx_backfill,v2_bar_aggregator}.py`. (P2-6 done with P0-6.)
- **P2-4** Online IC is now tie-correct: ICTracker uses average ranks + Pearson-on-ranks
      (`_spearman`, equals `scipy.stats.spearmanr` under ties) instead of the
      1−6Σd²/(n(n²−1)) shortcut that assumed no ties — discretised scores / flat bars no
      longer bias the IC used for sizing throttle. 5 new tests. `services/signal/main.py`.
- **P1-6** Prometheus + Grafana now deployed in docker-compose (were absent): prometheus
      scrapes the gateway `/metrics` (target verified to match `gateway:5000`) with 30d
      retention, grafana on :3000, both on helios-net with persistent volumes. Compose +
      prometheus.yml validated as parseable. Container bring-up needs a real Docker host →
      Needs-Human. `docker-compose.yml`.
- **P1-7** Rich decision-trail read API: `GET /sel/decision-trail/full` exposes the per-bar
      `sel_decision_trail` (feature snapshot, state+reason, proposed-vs-final action, matched
      rule, risk veto+details, fill, config_hash) — the Helios moat that was persisted but had
      no read surface. Degrades to [] when the table is absent. 2 new tests.
      `sel_v2/paper_interface/api.py`. (Frontend trail tab still pending — see P0-6 Needs-Human.)
- **P1-4** Bar-aggregator gap recovery: an empty 4H bar (WS outage lost its trades) is no
      longer silently skipped — it's recovered from the official OKX perp candle
      (tick_count=0 marks REST-recovered) so the 4H series stays contiguous; only an
      unrecoverable bar logs an explicit GAP. Pure `build_bar_row`/`parse_rest_candle`
      extracted for testing. 7 new tests. `sel_v2/data/v2_bar_aggregator.py`.
- **P1-3** Cascade cond-1 reachable: BarRunner now derives `lob_depth_pctile` (rolling
      7-day rank of total top-of-book bid+ask depth) from the collected perp LOB and wires it
      into BarFeatures; paper engine aggregates `AVG(bid_depth+ask_depth)` per bar. A thin book
      now yields a low pctile so "σ extreme AND thin book" can fire. 3 new tests.
      `sel_v2/scheduler/bar_runner.py`, `sel_v2/paper/{strategy_engine,paper_engine}.py`.
- **P1-8** CPCV now purges label overlap: `run_cpcv` threads `label_horizon` through to
      `oskill.cpcv_pipeline` and the engine passes `MAX_HOLD_HOURS` (trades hold up to 24
      hourly bars), so train samples whose labels overlap the test window are purged instead
      of leaking future info and flattering PBO/path-Sharpe. 2 tests (incl. forwarding).
      `backtest/cpcv.py`, `backtest/engine.py`.
- **P1-5** Correlation static-fallback side bug fixed: `check_corr_exposure` now normalises
      LONG/SHORT vs BUY/SELL to a sign (matching the dynamic path) before summing same-
      direction exposure — the gate previously matched nothing and silently passed all
      correlated concentration at cold start. 3 new tests. `services/risk/main.py`.
- **P1-2** Verified already code-complete + tested (not a code gap): `_micro_vocab_series`
      derives Sweep/Absorption/Crowding vocab and `_maybe_open_s2` derives
      `ofi_persistent_same_direction` from real microstructure, and Type A/B entries are
      exercised in `test_strategy2_entry`. The audit's "S2 inert" reflected the empty-data
      runtime; the residual is deploy-data availability (advanced by P0-2). No code needed.
- **P1-1** Gated sel_v2 → live execution bridge: `decision_to_scored_signal` translates a
      deployed S1/S2 entry decision into the canonical ScoredSignal (protective stop from the
      REAL drawdown-stop pct, regime mapped from 4H state), and `LiveBridge.emit` publishes
      it onto `signal.scored` so the paper-validated strategy reaches the same Kelly-sizing +
      risk-gate (incl. P0-1 liq guard) + P0-3 native-stop path. Default OFF
      (`SEL_V2_LIVE_BRIDGE`); only controls *reachability*, loosens no gate. 7 new tests.
      `sel_v2/paper_interface/live_bridge.py`, `tests/sel_v2/test_live_bridge.py`.
- **P0-6** Observe-only iron law (backend language): the mode-switch surface no longer
      *advises* — "建议切换到 AUTO_EXEC" → neutral threshold-status with an explicit
      "是否切换为人工决策，系统不作建议" disclaimer (report.py advisor + rendered §⑧ +
      monitoring Telegram push). Endpoints renamed `/monitor/recommendation` →
      `/monitor/mode-thresholds` with deprecated aliases + back-compat keys (also closes
      P2-6), in both monitoring and gateway. 3 new tests. NOTE: the v4 *execute-UI*
      (confirm/reject/execute buttons) is a product decision → Needs-Human.
- **P0-5** Backtest verdict is now binding at two real boundaries:
      `enforce_oos_gate()` *raises* `BacktestRejected` on a failing/absent OOS slice (the
      verdict can't be computed-then-ignored), and the live boot guard
      (`_assert_safe_exec_mode`) now also requires `I_HAVE_OOS_EVIDENCE=yes` so no live
      mode can start without proven out-of-sample evidence — guilty until proven innocent.
      NOTIFY_ONLY/PAPER/dev unaffected. 2 new tests + updated guard tests.
      `services/execution/main.py`, `backtest/v2_strategy_backtest.py`.
- **P0-4** Real-strategy backtest DSR no longer degenerate: `n_trials` defaults to
      `effective_calibration_trials()` (product of the calibration knobs the deployed
      config was selected over, = 81), so DSR deflates for selection bias instead of
      collapsing to PSR-vs-0. Documented `CALIBRATION_KNOBS`, overridable. 1 new test.
      `backtest/v2_strategy_backtest.py`, `tests/backtest/test_v2_strategy_backtest.py`.
- **P0-3** Exchange-native protective stops: `place_stop_order` on the adapter
      interface (base default = unsupported; real Binance `STOP_MARKET` + OKX algo
      `conditional`); live fills now place a reduce-only native stop that survives a
      service/feed outage or gap, with a high-severity alert + in-process backstop when
      placement fails; cancelled on close. `websockets` lazy-imported so adapters are
      unit-testable. 7 new tests. `services/execution/adapters/{base,okx,binance}.py`,
      `services/execution/main.py`, `tests/services/test_native_stop_orders.py`.
- **P0-2** Microstructure feed now matches the traded instrument: tick + LOB
      collectors subscribe to the perp `BTC-USDT-SWAP` (was spot), storing the shared
      base symbol so downstream joins are unchanged. Liquidation/derivatives made
      symmetric+configurable; `websockets_proxy` lazy-imported so the modules are now
      unit-testable. 8 new tests. `sel_v2/data/v2_{tick,lob,liquidation,derivatives}_collector.py`,
      `tests/sel_v2/test_collector_instrument.py`.
- **P0-1** Perp liquidation-distance guard in RiskGate (`check_liquidation_distance`,
      Gate 5b in `approve()`): rejects when post-trade cross-leverage sits within
      MIN_LIQ_BUFFER_PCT of liquidation, or when the protective stop is at/beyond the
      liquidation price (would liquidate before the stop fills). 7 new tests, 44
      existing risk tests still green. `services/risk/main.py`,
      `tests/unit/test_risk_liquidation_gate.py`.

---

## 🚨 Needs Human

- **备份清单修复(helios-platform repo,2026-07-12)**:platform-postgres-backup 的
  `POSTGRES_DB=helios,helixa,selene,tide,infisical`(infrastructure/docker-compose.yml:58)
  中 infisical 库不存在 → 每日备份 job Exit 1 + 容器 unhealthy(selene 等四库备份本身
  正常)。建议:(a) 移除 infisical 或补建库;(b) 决定是否把 marketdata(iris 采集层,
  ETH/SOL/BTC 原始 tick)加入备份清单(体积权衡);(c) healthcheck 8080 未监听,顺带查。
  跨项目 repo,未擅动。
- ~~Coiling 四条件 AND 过严 + Charged 粘性回退~~ → **已裁决执行 (2026-07-11 用户授权
  "授权放宽"):** `check_coiling` 放宽为 σ 必要 + 蓄能篮子 {熵<30pct, OI增>0,
  |funding|<80pct} **2-of-3**(单个 False 不再一票否决;可判信号 <2 个时 met=None,
  三态纪律保持,降级历史仍为 None 路径不变)。效果:2yr 重放 Coiling 0→12 bar(全在
  特征齐全近期),最新 live bars 已判 Coiling,Charged 粘性段自然解开(Coiling 优先级
  更高,无需动仲裁回退);Coiling→Surging Release 通路打开。v2_state_history 12 行
  历史判定被改写,`rewritten_at` 漂移仪表首次真实触发并全部留痕。详见 Done 区。
- ~~Epoch af6f7d3d DIRTY 待人工~~ → **已随上述授权一并解决 (2026-07-11):** 放宽 Coiling
  本身即动 `states/**`,按 R1 以用户授权重置 epoch——新 epoch **a335521f**
  (2026-07-11 12:55 UTC 起,status CLEAN,30 天时钟重启),reason 记录了授权与
  db_writer 仪表化 DIRTY 的合并处置。注意:新指纹包含工作区内**未提交**的
  `states/schema.py`/`strategies/cusum_short.py` 既有改动——提交或还原它们都会再次
  DIRTY,处理前先确认。
- **`.env:34` 的 `EXEC_MODE=NOTIFY_ONLY` 已过时 (2026-07-10)**:EXEC_MODE=PAPER 已按用户
  决定在 compose 写死(不再 ${} 插值),.env 该行现为死配置——受保护文件,请人工删除或
  改为 PAPER 以免误导。(同一文件 30-31 行的失效 proxy IP 也还挂着,见下方 07-03 条目。)
- **数据源迁移 OKX→Binance (2026-07-03)**: OKX 在本部署环境永久不可达（每代理 403/SSL-EOF，
  平台 `*_OKX_FLAG=0`）；Binance 仅经 `helios-proxy:2080` 可达。live WS 采集器（tick/lob/deriv/liq）
  是 OKX-WS 专用，恢复实时微结构数据需将其移植到 Binance WS——核心大改且本地无法跑测试
  （无 pytest/oprim/oskill wheel），不宜盲改。已用 Binance REST 救活 `v2_bars_4h` 作过渡。
  同时决定 v4 signal 链存废：接 `v2_bars_4h`→`market.candles`（无生产者），或全押 sel_v2+LiveBridge。
  `.env:30-31` 的 proxy（受保护文件）仍指向失效 IP，需人工改为 `helios-proxy:2080`。
- **回测严谨性（须先于任何 live 野心，需 CI 私有 wheel 验证）**: (a) PBO 现为 Sharpe 符号代理而非
  真 CSCV（vendored `oskill` 已有实现未用）；(b) 真 CPCV 路径 `test_cpcv_wiring.py:147` 硬 skip、
  生产调未导出的 `oskill.cpcv_pipeline` → 静默 `cpcv=None`；(c) `I_HAVE_OOS_EVIDENCE` 仅 env 荣誉检查，
  应绑定 committed OOS artifact。本地无 oskill/pytest，改后无法自证 → 留 CI。
- **SEL historical OOS is blocked by data, not code** (2026-07-01): S1/S2 entry states need OI,
  and OKX only serves ~16 days of OI history (LOB/entropy: no history at all). So a faithful
  "where would S1/S2 have traded" backtest is impossible from OKX — decision needed on a
  **3rd-party historical OI source** (Coinglass/Laevitas/Amberdata; some paid). Until then S1
  can only accrue evidence *forward* (it trades once the market consolidates). The chart's
  "反事实成交" toggle shows the OI-gates-assumed upper bound, clearly labelled as not-a-backtest.
- **P0-6 frontend product decision**: the only shipped UI is the v4 recommendation/
  execution dashboard (signal cards with entry/SL/TP + confirm/reject/execute buttons).
  Backend advisory *language* is now neutralized, but the execute-UI itself is structurally
  at odds with the Helios observe-only doctrine. Decision needed: keep the v4 execution
  dashboard as a separate product, or build/ship an observe-only Helios UI (decision-trail,
  regime, observation tools) as the primary surface? I did not delete a working feature on
  a judgment call. (Execution remains NOTIFY_ONLY regardless, so the buttons are inert today.)
- **P1-1 bridge end-to-end live validation**: the translator + gated publisher are unit-
  tested, but the full sel_v2-decision → signal.scored → portfolio → risk → execution loop
  needs a real Redis/services run to validate (call-site wiring in the paper/strategy engine
  to actually invoke `LiveBridge.emit` is intentionally NOT added yet — that is the live
  cut-over step and must be a deliberate human action with OOS evidence in hand).
- **P1-3 remaining Cascade conditions**: cond-1 (thin book) is now wired; cond-2
  (`liquidation_pulse`) needs a `v2_liquidations`→per-bar aggregation (tractable, separate)
  and cond-3 (`cross_exchange_spread`) is structurally N/A while single-venue (OKX only) —
  a second venue feed (P2: 2nd exchange) is required. Validate cond-1 firing with real LOB
  data on deploy.
- **P1-6 observability bring-up**: prometheus+grafana are declared in compose but need a real
  Docker host to verify containers start and scraping works; add Grafana dashboards/datasource
  provisioning once running. Other FastAPI services still need to adopt `shared/metrics.py` to
  appear (scrape jobs already declared).
- **P0-3 live bracket lifecycle** needs real-exchange verification before any live use
  (cannot be integration-tested here): native stop placement on the *WebSocket* fill
  path (currently wired only on the immediate-FILLED branch), OCO pairing with take-profit,
  partial-fill stop resizing, and de-duplication against the in-process monitor so a
  fired native stop and the monitor don't both try to close. Capability + unit tests are
  in place; the live wiring is best-effort and gated (never runs under NOTIFY_ONLY).
