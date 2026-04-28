# P0 Fix Impact Report

**日期**：2026-04-28  
**基于**：`audit/sel_v1_compliance_report.md` P0 偏差修复（4 个 commit：3c2375c → b440e44 → 3ad136b → 6fb75e8）

---

## 测试配置

真实 DB 仅有 217 条 1H 蜡烛（Apr 18–27），全部在 720 bar 冷启动窗口内，无法产生任何 active 状态。因此，本报告使用与 `state_inspection.py` fallback 一致的合成数据集：

```
种子：numpy.random.seed(42)
总 bar 数：1008（720 warmup + 288 display = 12 天）
价格模型：70000 * (1 + 0.0002i) + 3500·sin(2πi/168) + 600·sin(2πi/24) + N(0,80)
单次崩盘：每 300 bar 一次，幅度 -5000 USDT
可用特征：close, delta_p_pct, sigma_p_24h, price_autocorr_12h/24h/48h, sigma_p_d2
WIKI_REQUIRED（未采集）：H, TF, LV, OI, abs_funding_rate, delta_H
```

---

## 状态分布对照

| 状态 | BEFORE（4 个 fix 前） | AFTER（4 个 fix 后） | 变化 |
|---|---|---|---|
| Drifting_Calm | 226（78.5%） | 288（100%） | ＋62 bars |
| Critical | 62（21.5%） | 0（0%） | −62 bars |
| Coiling | 0（0%） | 0（0%） | — |
| Cascade | 0（0%） | 0（0%） | — |
| Surging_Up | 0（0%） | 0（0%） | — |
| Surging_Down | 0（0%） | 0（0%） | — |
| Drifting_Charged | 0（0%） | 0（0%） | — |
| **合计** | **288** | **288** | |

---

## 逐 Fix 分析

### Fix 1：Cascade 主门控（`abs_delta_p_pct` 替换 `sigma_p_24h`）

**Before**：Cascade 主门控为 sigma_p_24h > 97th pct。合成数据波动率相对稳定，sigma_p_24h 从未超过 97th pct → Cascade = 0 bars。

**After**：主门控改为 abs_delta_p_pct > 97th pct（单 bar 绝对价格涨跌幅）。每 300 bar 有一次 -5000 USDT 崩盘，abs_delta_p_pct 确实超过 97th pct。但 Cond3（LV）和 Cond4（delta_H）均依赖 WIKI_REQUIRED 数据（orderbook depth、H entropy），在当前数据下为 None → 次级条件均为 False → Cascade 仍 = 0 bars。

**结论**：Cascade 在合成数据下 Before/After 均为 0，但原因完全不同：
- Before = sigma_p_24h 从未够高
- After = 主门控可以触发，但次级条件等待 WIKI 数据

当 orderbook collector 数据积累后（LV 和 delta_H 可用），Fix 1 的 Cascade 将有机会正常触发。

---

### Fix 2：Coiling H 方向（`H > 0.30` 替换 `H < 0.70`）

**Before/After Coiling = 0 bars**——两者均为 0，但原因不同：

- Before：H 为 None（WIKI_REQUIRED），H 检查被跳过；但 Coiling 同时需要 OI ≥ 50th pct（OI 也为 None）→ 失败于 OI 门控
- After：同上。H 修正方向后，None 时仍跳过；OI 仍为 None

**真正影响（将在 H 数据可用后显现）**：

| 场景 | Before | After |
|---|---|---|
| H 高（盘口分散，OI 也高） | ✗ 错误触发 Coiling | ✓ 正确拒绝（高 H 意味着无法 Coiling） |
| H 低（盘口集中，OI 也高） | ✓ 触发，但 H ≥ 70th 逻辑是反的（此时 H < 30th，code 在 h_qr < 0.70 处通过，但 H 本应 < 30th 才是有效 Coiling） | ✓ 正确触发（H < 30th pct 确认盘口结构性聚集） |

物理含义修正：Coiling 要求"盘口熵低"（能量积累、挂单结构集中）。旧代码要求"盘口熵高"（挂单分散），与 §4.1 定义完全相反。

---

### Fix 3：Critical Cond1（autocorr 单调上升趋势）

**核心数据**：

| Critical gate | 通过/失败（288 display bars） | 占比 |
|---|---|---|
| Cond1（autocorr_12h > 24h > 48h）失败 | 261 bars | 90.6% |
| Cond1 通过，Cond2（sigma_d2 > 0 且 > 80th）失败 | 20 bars | 6.9% |
| Cond1+2 均通过，Cond3/4 无 WIKI 数据 | 7 bars | 2.4% |
| 合计 active Critical（After） | **0 bars** | 0% |

**Before**：Critical = 62 bars，触发条件为 sigma_p_d2 > 80th pct AND autocorr_24h ≤ 20th pct。这是错误的：§4.5 Cond1 要求自相关在 12H 内**单调上升**（CSD 的核心信号），而旧代码检查的是自相关**水平低**（恰恰相反的含义）。

**After**：Critical = 0 bars。62 bars 的"伪 Critical"被全部正确消除：
- 90.6% 在 Cond1（autocorr 单调性）就被拦截——说明这些 bar 自相关并无上升趋势，不是真正的 CSD 预警
- 6.9% Cond1 通过但 sigma_p_d2 无上升加速
- 2.4%（7 bars）Cond1+2 均通过，仅因缺少 H/OI WIKI 数据而暂时不触发

**结论**：Critical 62→0 是**正确修正**，不是误压制。当 orderbook collector 和 oi_persister 积累足够数据后，7 bars 的真实 CSD 候选将被正确标记。

---

### Fix 4：3 条非法转移标记

Legal transitions 变更不影响状态分布（LegalityChecker 只注释不抑制）。影响仅在 `sel_state_sequence.is_legal_transition` 字段：

| 转移 | Before | After |
|---|---|---|
| Drifting-Calm → Surging | `is_legal=True` | `is_legal=False` + 计入 `illegal_transition_count` |
| Coiling → Drifting-Calm | `is_legal=True` | `is_legal=False` + 计入 `illegal_transition_count` |
| Surging → Coiling | `is_legal=True` | `is_legal=False` + 计入 `illegal_transition_count` |

§7.1 健康度指标（非法转移率 > 20% → 状态定义有问题）现在可以正确统计这 3 种转移。

---

## 下游 Coiling 可疑点（Fix 2 衍生）

H 方向反转后，历史和未来 Coiling 标记的含义发生根本变化。以下引用点需要人工审视：

| 文件 | 位置 | 可疑原因 |
|---|---|---|
| `paper_trading/scripts/validate_runner.py` | `_STATE_CYCLE` (L27-42) | 合成状态序列以 Coiling 作为"能量积累"前置态，该假设在旧代码下实际是"高熵期"，修复后语义对齐，但合成序列的决策逻辑（下方 PaperTradingRunner）可能依赖旧行为 |
| `paper_trading/scripts/replay_cli.py` | L28-36 | `coiling_after_drifting_prepare`、`surging_up_from_coiling` 规则测试用例：如果测试假设是旧的错误 Coiling（高熵），测试现在会验证一个和历史不一致的世界 |
| `services/signal/regime/detector.py` | L101-105 | 注释"coiling"但实际用 `close < low_30 * 1.05` 判断积累区，与 sel 语言 Coiling 无直接关联，语义独立，**低风险** |
| `reports/generator.py` | L19 | Coiling 出现在报告标签列表，仅展示层，**无风险** |
| `sel_engine/states/health.py` | L17 | 健康度目标区间 `(0.10, 0.25)` 是否仍合理？旧代码高估了 Coiling（因为错误触发），修复后实际触发率可能更低（需要真实 H 数据才能评估）|

---

## 总结

| 修复 | Before 影响 | After 影响 | WIKI 数据可用后预期 |
|---|---|---|---|
| Fix 1 Cascade | 主门控错误（sigma_p_24h）；合成数据 0 次触发 | 主门控正确（abs_delta_p_pct）；次级条件待 WIKI | LV + delta_H 可用后 Cascade 可正常触发 |
| Fix 2 Coiling H | H 方向完全相反；H=None 时跳过，OI=None 时失败 | H 方向正确；H=None 时仍跳过，OI=None 时仍失败 | H 低熵期（盘口集中）+ OI 可用时 Coiling 将首次正确触发 |
| Fix 3 Critical | 62 bars 伪 CSD（autocorr 水平低被误判为 CSD） | 0 bars；真实 CSD 窗口收窄至 7 bars pending WIKI | H_change_rate_std 或 OI_hurst 可用时 7 bars 候选触发 |
| Fix 4 Transitions | 3 条 illegal 转移被错标为 legal | 正确注释为 illegal，健康度监控可正常工作 | 无状态分布影响 |
