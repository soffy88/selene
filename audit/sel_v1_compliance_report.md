# sel v1.0 合规审计报告

**审计日期**：2026-04-28  
**被审计文档**：`docs/sel-lang-v1.0.md`  
**被审计代码**：`sel_engine/states/` — `conditions.py`, `transition.py`, `thresholds.py`, `recognizer.py`  
**审计范围**：6 节（Feature Layer / Threshold Layer / State Priority / Time Filters / Legal Transitions / Cold Start）  
**声明**：本报告仅记录偏差，不修复任何偏差。偏差标注三类：`一致` / `偏差` / `未实装`

---

## 第 1 节：Feature Layer（状态触发条件）

> 对照 doc §4.1–4.6，按状态逐条审计。

### 1.1 Cascade（雪崩态）— 对照 doc §4.6

文档要求：
```
条件 1：1H 价格变化幅度 |ΔP|/P > 30 天 97 分位  [主门控]
条件 2：1H 内成交量 > 30 天 95 分位
条件 3：LV（流动性真空）> 30 天 95 分位
条件 4：H 变化幅度 |ΔH| > 30 天 95 分位
满足条件 1 + 至少一个其他条件 → Cascade
```

| 文档要求 | 代码实际（`conditions.py:13`） | 偏差 |
|---|---|---|
| 主门控：`abs_delta_p_pct`（\|ΔP\|/P）> 97th pct | 主门控：`sigma_p_24h`（24H 波动率）> 97th pct | **偏差** — 主门控特征完全不同；code 用 σ_24h，doc 用单 bar 价格变化幅度 |
| 次级 Cond2：1H 成交量 > 95th pct | `volume` 不在 `FeatureVector` 中，无任何 volume 字段 | **未实装** — FeatureVector (`schema.py`) 无 volume；quantile window 无 `volume` 键 |
| 次级 Cond3：LV > 95th pct | `lv_qr >= 0.95` ✓ | **一致** |
| 次级 Cond4：`\|ΔH\|`（熵变化幅度）> 95th pct | `abs_TF >= 0.95`（绝对成交流）代替 | **偏差** — 次级 Cond4 用 `\|TF\|` 替代 `\|ΔH\|`；两者含义完全不同 |
| 结构：Cond1 AND (Cond2 OR Cond3 OR Cond4) | 结构：sigma_p_24h AND (abs_delta_p OR LV OR abs_TF) | **偏差** — doc 的 Cond1 (`\|ΔP\|/P`) 在代码中变成了次级；doc 的次级 volume 未实装 |

---

### 1.2 Critical（临界态）— 对照 doc §4.5

文档要求：
```
条件 1：lag-1 自相关在过去 12H 上升 且 autocorr(12H) > autocorr(24H) > autocorr(48H)
条件 2：σ(P) 二阶差分 > 0 且 > 30 天 80 分位
条件 3：H 变化率 |ΔH/H| 12H 滚动 std > 30 天 80 分位
条件 4：任一变量恢复时间显著变长（任一替代指标）
满足：条件 1 + 条件 2 + (条件 3 OR 条件 4)
```

| 文档要求 | 代码实际（`conditions.py:50`） | 偏差 |
|---|---|---|
| Cond1：autocorr lag-1 在 12H 内单调上升（三段序列验证） | 无此检验；代码没有任何 autocorr 趋势序列比较 | **偏差** — CSD 的核心指标（autocorr 上升趋势）在代码中完全缺失 |
| Cond2：σ(P) 二阶差分（加速度）> 80th pct | `sigma_p_d2` > 80th pct ✓ | **一致**（`sigma_p_d2` = σ 的二阶差分，但在代码中作为主门控而非 Cond2） |
| Cond3：`H_change_rate_std_12h` > 80th pct | `H_change_rate_std_12h` 在 FeatureVector 中存在但未出现在 `check_critical` 的任何条件 | **未实装** — 字段存在，但未被 `check_critical` 读取 |
| Cond4：任一变量恢复时间显著变长 | 以 `OI_hurst >= 70th pct` 代替 | **偏差** — OI Hurst 指数是一种持续性指标，但文档 Cond4 要求的是系统动力学恢复时间，两者不等价 |
| 结构：Cond1 AND Cond2 AND (Cond3 OR Cond4) | 结构：sigma_p_d2(80th) AND [OI_hurst(70th) if available] AND (LV≥70th OR autocorr_24h≤20th) | **偏差** — 结构完全不同；Cond1（autocorr 单调性）缺失；Cond3（H_change_rate_std）未实装；次级条件以 LV/autocorr_24h 替代 |
| 补充：autocorr_24h 用于 Cond1（trending 检验） | `autocorr_24h ≤ 20th pct` 作为 Cond3 替代（level 而非趋势） | **偏差** — doc 检验自相关的上升趋势；code 检验自相关的低分位 level |

---

### 1.3 Coiling（蜷缩态）— 对照 doc §4.1

文档要求：
```
条件 1：σ(P) 过去 24H 均值 < 30 天滚动 30 分位
条件 2：H（盘口熵）过去 24H 均值 < H 30 天滚动 30 分位
条件 3：OI 过去 24H 累计变化率 > OI 变化率 30 天 70 分位
条件 4：|TF|/|ΔP| 比值 > 该比值 30 天滚动 80 分位
满足全部 4 条 → Coiling
```

| 文档要求 | 代码实际（`conditions.py:104`） | 偏差 |
|---|---|---|
| Cond1：σ(P)_24h < 30th pct（低于 30 分位） | `sigma_p_24h ≤ 30th pct` ✓ | **一致** |
| Cond2：H < 30th pct（低熵，盘口集中） | `H ≥ 70th pct`（H 大于 70 分位） | **偏差** — 方向完全相反；doc 要 H 低（盘口集中），code 要 H 高（盘口分散） |
| Cond3：OI 变化率 > 70th pct（OI 快速积累） | `price_autocorr_24h ≥ 60th pct` | **偏差** — 特征完全不同；doc 用 OI 变化率，code 用价格自相关 |
| Cond4：\|TF\|/\|ΔP\| 比值 > 80th pct（吸收力强） | `OI ≥ 50th pct`（OI level 分位） | **偏差** — 特征完全不同；doc 用流-力比值，code 用 OI 原始分位 |
| 结构：全部 4 条 AND | 结构：sigma AND autocorr AND OI AND (H if available) | **偏差** — 门控特征错误（见上三行）；H 在 code 中可选（None 时跳过），doc 要求强制 |

---

### 1.4 Surging（涌动态）— 对照 doc §4.2

文档要求：
```
条件 1：过去 6H 价格线性回归斜率绝对值 > 30 天滚动 80 分位
条件 2：过去 6H TF 同向比例 > 70%
条件 3：σ(P) 上升中，但 σ 变化率本身稳定（σ 在上升但加速度接近 0）
满足全部 3 条 → Surging；方向由 TF 同向比例符号决定
```

| 文档要求 | 代码实际（`conditions.py:148`） | 偏差 |
|---|---|---|
| Cond1：6H 线性回归斜率绝对值 > 80th pct | `abs_delta_p_pct ≥ 70th pct`（单 bar 价格变化率绝对值） | **偏差** — 6H 回归斜率（多 bar 趋势强度）被单 bar 价格幅度替代；分位阈值也不同（80 vs 70） |
| Cond2：6H TF 同向比例 > 70%（有界 0~1） | `sigma_p_24h ≥ 60th pct`（24H 波动率分位） | **偏差** — 特征完全不同；doc 要 TF 方向一致性，code 要 σ 高于均值 |
| Cond3：σ(P) 上升但 σ 变化率（二阶导）稳定 | `price_autocorr_12h ≥ 60th pct`（12H 价格自相关） | **偏差** — 特征和含义完全不同 |
| 方向判定：TF 同向比例符号 | `delta_p_pct` 符号（单 bar 涨跌） | **偏差** — doc 用 6H TF 流向符号确定方向，code 用最新 bar 涨跌符号 |

---

### 1.5 Drifting-Calm（平静漂浮）— 对照 doc §4.3

文档要求：
```
条件 1：σ(P) 24H ∈ [30 分位, 60 分位]
条件 2：H 24H ∈ [40 分位, 80 分位]
条件 3：|TF| 24H 累计 < 50 分位
条件 4：OI 24H 变化率 ∈ [-50 分位, +50 分位]
满足全部 4 条 → Drifting-Calm
```

| 文档要求 | 代码实际（`conditions.py:231`） | 偏差 |
|---|---|---|
| Cond1：σ(P)_24h ∈ [30th pct, 60th pct]（区间） | σ(P)_24h ≤ 50th pct（单侧上界，无下界） | **偏差** — 无下界（σ 极低时也匹配）；上界 50th vs 60th |
| Cond2：H 24H ∈ [40th pct, 80th pct] | H 条件：不存在 | **未实装** |
| Cond3：\|TF\| < 50th pct | TF 条件：不存在 | **未实装** |
| Cond4：OI 变化率 ∈ [-50th, +50th pct] | OI 条件：不存在 | **未实装** |
| 结构：全部 4 条 AND | 仅 σ ≤ 50th，且 σ 不可用时仍触发（fallback） | **偏差** — 代码将此作为最终兜底 catch-all，实质是"无条件 fallback" |

---

### 1.6 Drifting-Charged（蓄能漂浮）— 对照 doc §4.4

文档要求：
```
条件 1：σ(P) 24H ∈ [40 分位, 70 分位]
条件 2：H 24H < 50 分位
条件 3：|TF| 累计 ∈ [30 分位, 70 分位]（中等成交流）
条件 4：OI 持续单向变化（Hurst 指数 > 0.6 风格持续性）
满足全部 4 条 → Drifting-Charged
```

| 文档要求 | 代码实际（`conditions.py:189`） | 偏差 |
|---|---|---|
| Cond1：σ(P)_24h ∈ [40th pct, 70th pct]（区间） | σ(P)_24h ≤ 50th pct（单侧上界，无下界） | **偏差** — 无下界；上界 50th vs 70th；σ 极低时也匹配（与 Drifting-Calm 边界重叠） |
| Cond2：H < 50th pct | H 条件：不存在 | **未实装** |
| Cond3：\|TF\| ∈ [30th pct, 70th pct] | TF 条件：不存在 | **未实装** |
| Cond4：OI Hurst 风格持续性（趋势性） | `OI ≥ 70th pct`（OI level 原始分位） | **偏差** — doc 要求 OI 的时间序列持续性（Hurst > 0.6），code 用 OI 当前 level 替代 |
| 补充：无资金费率条件 | `abs_funding_rate ≥ 60th pct` 作为额外门控 | **偏差** — doc §4.4 无资金费率条件；code 独立添加了此门控 |

---

## 第 2 节：Threshold Layer（分位数阈值）

> 对照 doc §10.1–10.3 的标定原则。

| 文档要求 | 代码实际 | 偏差 |
|---|---|---|
| 所有阈值基于 [t−720h, t−1h] 严格过去窗口计算 | `RollingQuantileCalculator`：`quantile_rank()` 在 `add()` 前调用，窗口只含过去值 ✓ | **一致** |
| 窗口长度：720H（30 天） | `RollingQuantileCalculator.WINDOW = 720` ✓ | **一致** |
| Cascade Cond1：`\|ΔP\|/P` 97th pct | sigma_p_24h 97th pct（PLACEHOLDER 注释） | **偏差** — 特征替换（见第 1 节 1.1） |
| Cascade Cond2：Volume 95th pct | 无 volume 特征 | **未实装** |
| Cascade Cond3：LV 95th pct | LV 95th pct ✓ | **一致** |
| Cascade Cond4：`\|ΔH\|` 95th pct | abs_TF 95th pct | **偏差** |
| Coiling Cond1：σ 30th pct | σ 30th pct ✓ | **一致** |
| Coiling Cond2：H 30th pct（上界） | H 70th pct（下界，方向反转） | **偏差** |
| Coiling Cond3：OI 变化率 70th pct | autocorr_24h 60th pct（特征替换） | **偏差** |
| Coiling Cond4：\|TF\|/\|ΔP\| 80th pct | OI 50th pct（特征替换） | **偏差** |
| Critical Cond2：σ 二阶差分 80th pct | sigma_p_d2 80th pct ✓ | **一致** |
| Critical Cond3：H 变化率 std 80th pct | 未使用（字段存在但未读取） | **未实装** |
| Surging Cond1：6H 斜率 80th pct | abs_delta_p_pct 70th pct（特征 + 阈值双偏差） | **偏差** |
| Drifting-Calm Cond1：σ ∈ [30th, 60th pct] | σ ≤ 50th pct | **偏差** |
| 全部阈值均为 PLACEHOLDER 注释 | 代码注释明确标注 `# PLACEHOLDER — calibrate with v1.0.md when available` | **已知** — 代码承认阈值待标定，非静默偏差 |

---

## 第 3 节：State Priority（状态识别优先级）

> doc §4 未显式定义优先级顺序；`recognizer.py:69` 的 checks 列表定义了实际优先级。

| 文档要求 | 代码实际（`recognizer.py:69`） | 偏差 |
|---|---|---|
| 文档未定义显式优先级顺序（仅按 §4.1–4.6 排列） | 代码优先级：Cascade > Critical > Coiling > Surging > Drifting-Charged > Drifting-Calm | **偏差（隐式）** — 文档排列顺序为 Coiling(4.1) > Surging(4.2) > Drifting-Calm(4.3) > Drifting-Charged(4.4) > Critical(4.5) > Cascade(4.6)，与代码执行顺序不同 |
| Drifting-Calm 作为普通状态（4 条件全满足） | Drifting-Calm 作为最终 catch-all（σ ≤ 50th 或 σ 不可用） | **偏差** — 优先级最低且条件降为单条，文档无此设计 |
| Cascade 须由 σ_p 二阶变化触发（doc 未指定） | Cascade 作为最高优先级 | **一致**（紧急状态优先级最高符合设计意图） |
| Critical 在 Coiling/Surging 之前检测 | Critical 排第 2（Cascade 之后） | **合理实现** — 文档语境中 Critical 是前置于 Cascade 的高优先级状态 |

---

## 第 4 节：Time Filters（最小停留时间 / DwellFilter）

> 对照 doc §7.4。

| 状态 | 文档要求（§7.4） | 代码实际（`transition.py:16`） | 偏差 |
|---|---|---|---|
| Coiling | 6H | `DWELL_TIMES[COILING] = 6` | **一致** |
| Surging（Up + Down） | 3H | `DWELL_TIMES[SURGING_UP] = 3` / `SURGING_DOWN = 3` | **一致** |
| Drifting-Calm | 12H | `DWELL_TIMES[DRIFTING_CALM] = 12` | **一致** |
| Drifting-Charged | 6H | `DWELL_TIMES[DRIFTING_CHARGED] = 6` | **一致** |
| Critical | 1H | `DWELL_TIMES[CRITICAL] = 1` | **一致** |
| Cascade | 1H | `DWELL_TIMES[CASCADE] = 1` | **一致** |
| 判定逻辑：连续 N 个 bar 满足才确认 | `DwellFilter.apply()`：连续计数，未达 N 时 re-emit 上一个 confirmed state | **一致** |
| Cascade 冷却：Cascade 后 6H 内禁用 Critical（doc §7.5） | `CascadeCooling(cooldown_bars=6)`：Cascade 后 6H 内将 CRITICAL 替换为 last_confirmed | **一致** |
| doc §7.5："直接进入未定义状态" | 代码替换为 `last_confirmed`（而非 `None`/未定义） | **偏差（轻微）** — 文档说"未定义"，代码用上一个已知状态；语义略不同，但 None 在无 prior state 时会破坏下游 |

---

## 第 5 节：Legal Transitions（合法转移）

> 对照 doc §7.1。

### 5.1 文档明确列出的合法转移

| 文档要求（§7.1 合法转移） | 代码实际（`transition.py:29`） | 偏差 |
|---|---|---|
| Drifting-Calm → Drifting-Charged | `DRIFTING_CALM` 合法集含 `DRIFTING_CHARGED` ✓ | **一致** |
| Drifting-Charged → Coiling | `DRIFTING_CHARGED` 合法集含 `COILING` ✓ | **一致** |
| Coiling → Surging（Up/Down） | `COILING` 合法集含 `SURGING_UP`, `SURGING_DOWN` ✓ | **一致** |
| Coiling → Cascade（异常释放） | `COILING` 合法集含 `CASCADE` ✓ | **一致** |
| Surging → Cascade（突发反转） | `SURGING_UP/DOWN` 合法集含 `CASCADE` ✓ | **一致** |
| 任意 → Critical（文档允许） | 所有状态合法集均含 `CRITICAL`（除 CASCADE，其合法集为 {DRIFTING_CALM, COILING}） | **偏差（轻微）** — CASCADE → CRITICAL 未列入 CASCADE 合法集；doc "任意→Critical"暗示包含 CASCADE→Critical |
| Critical → Cascade | `CRITICAL` 合法集含 `CASCADE` ✓ | **一致** |
| Critical → Drifting-Calm（假警报） | `CRITICAL` 合法集含 `DRIFTING_CALM` ✓ | **一致** |
| Surging → Drifting-Charged（衰竭） | `SURGING_UP/DOWN` 合法集含 `DRIFTING_CHARGED` ✓ | **一致** |

### 5.2 文档明确列出的非法转移（应警告，不禁止）

| 文档要求（§7.1 非法转移） | 代码实际 | 偏差 |
|---|---|---|
| Drifting-Calm → Surging：非法（应警告） | `DRIFTING_CALM` 合法集含 `SURGING_UP`, `SURGING_DOWN`（视为**合法**） | **偏差** — doc 标记为 illegal，代码标记为 legal；不会触发 `is_legal_transition=False` 注释 |
| Coiling → Drifting-Calm：非法（应警告） | `COILING` 合法集含 `DRIFTING_CALM`（视为**合法**） | **偏差** — doc 标记为 illegal，代码标记为 legal |
| Surging → Coiling：非法（应警告） | `SURGING_UP/DOWN` 合法集含 `COILING`（视为**合法**） | **偏差** — doc 标记为 illegal，代码标记为 legal |

### 5.3 LegalityChecker 行为

| 文档要求 | 代码实际（`transition.py:201`） | 偏差 |
|---|---|---|
| "非法转移应被警告，不是禁止"（§7.1） | `LegalityChecker.check()` 仅设置 `is_legal_transition=False`，**从不抑制**转移 | **一致** |
| 非法转移频率 > 20% → 状态定义有问题 | `illegal_transition_count` 和 `illegal_transition_types` 有记录 | **一致** |

---

## 第 6 节：Cold Start（冷启动）

> 对照 doc §10.1 原则 3。

| 文档要求 | 代码实际 | 偏差 |
|---|---|---|
| 冷启动期 = 30 天 = 720 个 1H 单位 | `RollingQuantileCalculator.WINDOW = 720`；`bar_count < 720` → cold_start | **一致** |
| 冷启动期内：不输出状态判定，只记录数据 | `StateRecord(state=None, cold_start=True, reason="COLD_START")` | **一致** |
| 参照窗口：[t−720h, t−1h]（不含当前） | `quantile_rank()` 在 `add()` 之前调用，窗口严格不含当前 bar | **一致** |
| MIN_VALUES 阈值 | `MIN_VALUES = 10`（窗口内非 None 值 < 10 时返回 None） | **文档未规定** — doc 无对应规定，属于实现细节 |
| `bar_count < 720` 使用 `_bar_count` 计数 | `recognizer.py:57`：`self._bar_count < RollingQuantileCalculator.WINDOW` | **一致** |

---

## 汇总偏差统计

| 节 | 文档条目数 | 一致 | 偏差 | 未实装 |
|---|---|---|---|---|
| 1. Feature Layer（Cascade） | 5 | 1 | 3 | 1 |
| 1. Feature Layer（Critical） | 5 | 1 | 4 | 1 |
| 1. Feature Layer（Coiling） | 5 | 1 | 4 | 0 |
| 1. Feature Layer（Surging） | 4 | 0 | 4 | 0 |
| 1. Feature Layer（Drifting-Calm） | 5 | 0 | 1 | 3 |
| 1. Feature Layer（Drifting-Charged） | 5 | 0 | 3 | 2 |
| 2. Threshold Layer | 15 | 4 | 7 | 2 |
| 3. State Priority | 4 | 1 | 2 | 0 |
| 4. Time Filters | 8 | 7 | 1 | 0 |
| 5. Legal Transitions | 12 | 9 | 3 | 0 |
| 6. Cold Start | 5 | 5 | 0 | 0 |
| **合计** | **73** | **29 (40%)** | **32 (44%)** | **9 (12%)** |

---

## 重大偏差快查

以下偏差在实证阶段会直接导致状态标记与文档语义不符：

1. **Cascade 主门控**：代码用 σ_24h，doc 用 |ΔP|/P — 雪崩的核心判据完全不同
2. **Coiling H 方向反转**：代码 H ≥ 70th pct（高熵），doc 要求 H < 30th pct（低熵）— 触发条件物理含义相反
3. **Coiling Cond3/Cond4 特征替换**：OI 变化率 + |TF|/|ΔP| 被 price_autocorr + OI_level 替代
4. **Critical Cond1 缺失**：autocorr lag-1 单调上升检验（CSD 的核心指标）在代码中完全不存在
5. **Surging 全部 3 条特征替换**：6H 线性斜率、TF 同向比例、σ 稳定性均被替换为不同含义的特征
6. **Drifting-Calm 降为 catch-all**：doc 要求 4 条件严格 AND，代码仅单条件兜底
7. **3 条 doc-illegal 转移被代码标记为 legal**：Drifting-Calm→Surging、Coiling→Drifting-Calm、Surging→Coiling

**完全一致的子系统**：Dwell times（全部 6 个值）、CascadeCooling（6H）、LegalityChecker 行为（仅注释不抑制）、Cold start（720 bars）、滚动分位数的因果性（先 rank 后 add）
