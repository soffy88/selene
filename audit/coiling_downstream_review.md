# Coiling 下游影响审计

**背景**：P0 Fix 2（commit b440e44）将 `check_coiling` 的 H 条件方向从 `H ≥ 70th pct`（高熵）改为 `H < 30th pct`（低熵）。本文对所有 Coiling 引用按照影响类别分类。

**前置清单位置**：`audit/p0_fix_impact.md` §"下游 Coiling 可疑点" 有简版列表，本文为完整展开版。

---

## 引用总览

| 文件 | 行号 | 类别 | 说明 | 修复需求 |
|---|---|---|---|---|
| `sel_engine/states/schema.py` | 11, 23 | A | enum 定义 + 优先级分值 | 无 |
| `sel_engine/states/__init__.py` | 7, 25 | A | 函数导出 | 无 |
| `sel_engine/states/conditions.py` | 109–150 | A | 函数本体（已由 Fix 2 修复） | 无 |
| `sel_engine/states/recognizer.py` | 14, 74 | A | import + dispatch | 无 |
| `sel_engine/states/transition.py` | 16, 31–76 | A | DWELL_TIMES + LEGAL_TRANSITIONS（已由 Fix 4 修复） | 无 |
| `sel_engine/paper_interface/schema.py` | 14, 19 | A | 注释中的 example 字符串 | 无 |
| `sel_engine/diagnostics/state_inspection.py` | 39 | A | ALL_STATES 展示列表 | 无 |
| `reports/generator.py` | 19 | A | ALL_STATES 展示列表 | 无 |
| `sel_engine/states/health.py` | 17 | B | Coiling 期望触发率区间 | 等 H 数据 30 天后重标 |
| `configs/sel-decision-rules.yaml` | 29–34 | B | `coiling_after_drifting_prepare`（no_action） | 等实证数据 |
| `configs/sel-decision-rules.yaml` | 36–46 | B | `surging_up/down_from_coiling`（open_long/short） | 等实证数据 |
| `configs/sel-decision-rules.yaml` | 64–76 | B | `surging_up/down_not_from_coiling`（no_action） | 等实证数据 |
| `services/signal/regime/detector.py` | 102 | A | 注释中"coiling"是普通英语描述，无逻辑依赖 | 无 |
| `paper_trading/scripts/validate_runner.py` | 27–41 | A | 合成状态序列，纯标签 | 无 |
| `paper_trading/scripts/replay_cli.py` | 28–36 | A | 合成状态序列，纯标签 | 无 |
| `tests/sel_engine/test_states.py` | 343–411 | **C** | TestCheckCoiling 测试了旧的 H 语义（高熵通过）| **需要修复** |
| `tests/decision/test_config.py` | 41–92 | A | 规则查找测试，不涉及 H 逻辑 | 无 |
| `tests/paper_trading/test_engine.py` | 110–121 | A | state label 触发测试，不涉及 H | 无 |
| `tests/paper_trading/test_trail.py` | 59, 93, 105, 186–504 | A | DecisionTrail 测试，state 为字符串常量 | 无 |
| `tests/reports/test_generator.py` | 91, 120, 177, 230–332 | A | 报告生成测试，state 为字符串常量 | 无 |
| `tests/sel_engine/test_transitions.py` | 94–506 | A | DwellFilter/LegalityChecker 测试，不涉及 H | 无 |
| `tests/sel_engine/test_paper_interface.py` | 182–500 | A | StateOutputService 测试，state 为 enum | 无 |

---

## A 类引用（标签传递，不受影响）

以下引用仅使用 `"Coiling"` 字符串或 `StateLabel.COILING` 枚举值作为标识符，不依赖 Coiling 的物理条件（特别是 H 方向），因此 Fix 2 对其无影响：

- `sel_engine/states/schema.py` — enum 定义和优先级分值
- `sel_engine/states/__init__.py` — 函数导出
- `sel_engine/states/recognizer.py` — dispatch 入口
- `sel_engine/paper_interface/schema.py` — 注释示例字符串
- `sel_engine/diagnostics/state_inspection.py`, `reports/generator.py` — ALL_STATES 展示列表
- `services/signal/regime/detector.py:102` — 注释用词"coiling"是普通英语，逻辑走 ADX/EMA 路径，与 sel 状态机完全独立
- `paper_trading/scripts/validate_runner.py` 和 `replay_cli.py` — 合成状态序列脚本，只是发射状态标签字符串，用于测试 paper trading 决策逻辑，不计算 H
- 所有 tests 中的 A 类：test_transitions.py、test_trail.py、test_engine.py、test_config.py、test_generator.py、test_paper_interface.py — 均使用状态标签常量测试状态机行为，不测试 check_coiling 条件本身

---

## B 类引用（参数可能需重调，当前不改）

### `sel_engine/states/health.py:17` — Coiling 期望触发率

```python
"Coiling": (0.10, 0.25),  # PLACEHOLDER — verify with v1.0.md §10.5 when available
```

**当前逻辑**：每周报告检查 Coiling 实际触发率是否在 [10%, 25%] 区间，超出则生成健康警告。

**H 反转后的潜在影响**：
- Fix 2 前：Coiling 条件为"高熵期"（H ≥ 70th pct），H 在 BTC 盘口中高熵相对常见 → 触发率可能偏高
- Fix 2 后：Coiling 条件为"低熵期"（H < 30th pct，盘口集中），doc §4.1 预期触发率 5~15%
- **潜在结果**：H 数据到位后，Coiling 实际触发率可能低于当前区间下限 10%，触发"BELOW"警告
- doc §10.5 的目标区间是 [5%, 15%]，而代码里是 [10%, 25%]，两者不重叠的下界部分需要调整

**建议处理时机**：等 H collector 数据积累 30 天后重新标定，**现在不改**。

---

### `configs/sel-decision-rules.yaml:29–34` — `coiling_after_drifting_prepare`

```yaml
- id: "coiling_after_drifting_prepare"
  current_state: "Coiling"
  previous_state_pattern: "Drifting_*"
  action: "no_action"
  position_multiplier: 0.0
  notes: "Record only, wait for Release (Surging). [Wiki: using Claude default]"
```

**当前逻辑**：Drifting → Coiling 转换时，执行 no_action，等待 Surging 信号。

**H 反转后的潜在影响**：
- 语义层面：规则含义不变（Drifting 后进入积累态，等待方向）— 逻辑正确
- 频率层面：Fix 2 前，高熵期被错误地标记为 Coiling；Fix 2 后，真正的低熵/盘口集中期才触发。该规则的触发次数将减少，但触发质量更高
- `no_action`（不开仓）是保守行为，频率下降不会造成损失

**建议处理时机**：等实证数据确认真实 Coiling 频率后评估是否需要调整，**现在不改**。

---

### `configs/sel-decision-rules.yaml:36–46` — `surging_up/down_from_coiling`

```yaml
- id: "surging_up_from_coiling"
  current_state: "Surging_Up"
  previous_state: "Coiling"
  action: "open_long"
  position_multiplier: 1.0
  notes: "Primary open window. [Wiki: confirm base_size]"
```

**当前逻辑**：Coiling → Surging_Up 是主要开仓窗口，使用 1.0x 仓位（即 base_size_pct = 20% NAV）。

**H 反转后的潜在影响**：
- 这是最重要的 B 类引用——它是系统的**主要盈利逻辑**
- Fix 2 前：Coiling 是"高熵后的 Surging"——高熵期盘口分散，不一定对应能量积累
- Fix 2 后：Coiling 是"低熵后的 Surging"——低熵期盘口集中（资金结构性积累）后突破，更接近 Wyckoff Spring/Release 的实证形态
- `position_multiplier: 1.0` 是 Wiki 待确认的占位参数（notes 里明确标注）
- 真实 Coiling 频率下降 → 开仓信号频率下降 → 年化交易次数减少。这对 paper trading 的统计功效有影响（样本更少）

**建议处理时机**：等 H 数据 30 天后确认 Coiling 频率和 Coiling→Surging 转换率，再与 Wiki 确认 position_multiplier，**现在不改**。

---

### `configs/sel-decision-rules.yaml:64–76` — `surging_up/down_not_from_coiling`

```yaml
- id: "surging_up_not_from_coiling"
  current_state: "Surging_Up"
  previous_state_pattern: "(?!Coiling).*"   # NOT from Coiling
  action: "no_action"
  position_multiplier: 0.0
  notes: "Surging without Coiling setup = suspicious. [Wiki: decision point 5 — may be too strict]"
```

**当前逻辑**：非 Coiling 前置的 Surging 不开仓（认为缺乏能量积累支撑）。

**H 反转后的潜在影响**：
- Fix 2 后 Coiling 触发频率预计下降 → "not from Coiling"的 Surging 占比上升 → **no_action 的执行频率增加**
- 效果：系统将跳过更多 Surging 机会，整体开仓次数进一步减少
- notes 已标注"may be too strict"，在实证数据下这可能成为主要问题
- 负面影响：如果真实 BTC Surging 通常直接从 Drifting 爆发（而非从 Coiling 进入），该规则将系统性错过机会

**建议处理时机**：等 H 数据 30 天后查看 Surging 前置状态分布，再决定是否放宽，**现在不改**。

---

## C 类引用（语义颠倒，需逐条审视）

### `tests/sel_engine/test_states.py:343–411` — TestCheckCoiling

**位置**：`tests/sel_engine/test_states.py`，第 343–411 行

这是当前唯一一处**语义已颠倒**的 C 类引用。Fix 2 使以下测试用例产生了结果变化：

#### `test_matches_all_conditions`（L344–354）— **现在失败**

```python
def test_matches_all_conditions(self):
    fv = make_fv(H=3.5)
    qr = {
        "sigma_p_24h": 0.20,
        "H": 0.80,          # ← 80th pct（高熵）
        "price_autocorr_24h": 0.70,
        "OI": 0.60,
    }
    matched, reason, _ = check_coiling(fv, qr)
    assert matched          # ← Fix 2 后：H=0.80 > 0.30 → 条件拒绝 → matched=False → 断言失败
```

**根因**：测试假设 Coiling 需要高熵（旧代码 H ≥ 70th pct 通过），Fix 2 后高熵被拒绝，测试数据不符合文档 §4.1 定义的 Coiling（低熵）。

---

#### `test_fails_when_h_present_but_low`（L401–411）— **结论正确，语义表达错误**

```python
def test_fails_when_h_present_but_low(self):
    """If H is available but below threshold, should fail (not relaxed)."""
    fv = make_fv(H=1.0)
    qr = {
        "sigma_p_24h": 0.20,
        "H": 0.40,          # ← 40th pct
        "price_autocorr_24h": 0.70,
        "OI": 0.60,
    }
    matched, _, _ = check_coiling(fv, qr)
    assert not matched  # ← 旧代码：0.40 < 0.70 → 失败 ✓；新代码：0.40 > 0.30 → 失败 ✓
```

测试用例**仍然通过**，但 docstring 和参数含义已经颠倒：
- 旧含义：H=0.40 "低于 0.70 阈值"→ 失败
- 新含义：H=0.40 "高于 0.30 阈值"→ 失败（原因从"H 不够高"变成了"H 不够低"）

---

#### `test_matches_without_h`（L356–366）— **仍然通过，语义正确**

```python
def test_matches_without_h(self):
    """H is WIKI_REQUIRED — should match even if H is None."""
    qr = {"sigma_p_24h": 0.20, "H": None, "price_autocorr_24h": 0.70, "OI": 0.60}
    matched, _, _ = check_coiling(fv, qr)
    assert matched  # H=None → 跳过检查 → 仍然通过 ✓
```

语义和结果均正确，无需改动。

---

#### `test_fails_high_sigma`（L368–377）、`test_fails_low_autocorr`（L379–388）、`test_fails_low_oi`（L390–399）

这三个测试不涉及 H 条件（它们测试 sigma/autocorr/OI 失败路径，H 的值是 0.80 但被 sigma/autocorr/OI 门控先拦截）。

Wait — 实际情况取决于执行顺序。这些测试都有 `"H": 0.80`，在 Fix 2 后 H=0.80 会被新的 H 门控拒绝（`h_qr > 0.30 → return False`），但这发生在 sigma/autocorr/OI 检查之后，因为在 `check_coiling` 中的执行顺序是：

```python
# 1. sigma_qr 检查（先于 H）
if sigma_qr > 0.30: return False

# 2. H 检查（在 sigma 之后）
if h_qr is not None and h_qr > 0.30: return False

# 3. autocorr 检查
if autocorr_qr < 0.60: return False

# 4. OI 检查
if oi_qr < 0.50: return False
```

在 `test_fails_high_sigma` 中，sigma=0.60 > 0.30 → 第 1 步就返回 False，H 检查不执行。结果：`assert not matched` 仍然通过 ✓

在 `test_fails_low_autocorr` 中，sigma=0.20 通过，H=0.80 > 0.30 → 第 2 步返回 False。结果：`assert not matched` 仍然通过 ✓（但因为是 H 失败而非 autocorr 失败，测试名称产生误导）

在 `test_fails_low_oi` 中，sigma=0.20 通过，H=0.80 > 0.30 → 第 2 步返回 False。结果：`assert not matched` 仍然通过 ✓（同上，实际原因是 H，不是 OI）

**总结**：`test_fails_low_autocorr` 和 `test_fails_low_oi` 仍然 pass，但**失败原因已从预期条件变成了 H 条件**。测试名称与实际失败路径不匹配，会误导未来调试。

---

## 总结

| 类别 | 引用数 | 建议处理时机 |
|---|---|---|
| A 类（标签传递，无影响） | 17 处 | 无需处理 |
| B 类（参数可能需重调） | 4 处 | H 数据积累 30 天后（2026-05-28）重新评估 |
| C 类（语义颠倒，需修复） | 1 处（`tests/sel_engine/test_states.py:TestCheckCoiling`） | **需要修复** |

**是否需要追加修复任务**：是——`TestCheckCoiling` 测试套件需要按 Fix 2 新语义重写，目前 `test_matches_all_conditions` 断言失败（H=0.80 应该失败，测试却期望通过）。

---

## 工程顾虑

以下工程顾虑**不做决策，供审阅**：

1. **`surging_up_not_from_coiling` 的严格性**：该规则在 Fix 2 后会影响更多 Surging 事件（更多 Surging 将因"非 Coiling 前置"而 no_action）。notes 已标注"may be too strict"，但还没有实证数据说明多大比例的 BTC Surging 确实由 Coiling 前置。如果 BTC 习惯于从 Drifting 直接爆发（不经过 Coiling），整个决策矩阵将系统性不开仓。

2. **TestCheckCoiling 修复范围**：`test_fails_low_autocorr` 和 `test_fails_low_oi` 测试虽然仍然 pass，但实际失败原因变成了 H（而非测试名称暗示的 autocorr/OI）。修复时需要决定：是改 H 值让真正的失败原因发生（设 H=None 或 H < 30th pct），还是接受当前通过但原因不对的状态。两种选择有不同的测试文档价值。

3. **健康度阈值与文档不一致**：`sel_engine/states/health.py` 的 Coiling 期望区间是 `(0.10, 0.25)`，而 doc §10.5 的健康分布是 5~15%（`(0.05, 0.15)`）。这与其他状态一起需要校准，但这是在有实证数据之前就已存在的偏差，不是 Fix 2 引入的新问题。
