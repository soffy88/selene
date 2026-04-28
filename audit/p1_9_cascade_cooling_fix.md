# TASK 1.9 — CascadeCooling DRIFTING_CALM Fallback Fix

**修复日期**：2026-04-28  
**文档依据**：`docs/sel-lang-v1.0.md §7.5`  
**决策选项**：选项 Y（last_confirmed 存在时 hold prior，None 时返回 None）

---

## 问题描述

`sel_engine/states/transition.py::CascadeCooling.apply()` 在压制 CRITICAL 时：

```python
# 修复前（错误）
held_state = last_confirmed if last_confirmed is not None else StateLabel.DRIFTING_CALM
```

当 `last_confirmed=None`（系统从未确认过任何非 None 状态），fallback 到 `DRIFTING_CALM`。

这违反了 `v1.0.md §7.5`：
> Cascade 后 6H 内，禁用 Critical 判定，**直接进入"未定义"状态**。

"未定义"= `state=None`，不是 `DRIFTING_CALM`。

---

## 触发路径分析

理论触发路径：
1. 系统启动，`_last_confirmed = None`
2. Cascade 作为首个确认状态触发（在 warmup 后首次出现极端行情）
3. 6H 内 CRITICAL 候选出现，CascadeCooling 压制时 `last_confirmed` 被显式传入 `None`

**当前缓解**：P1 数据缺失（collector 未部署）时 Cascade 无法触发，bug 隐藏。collector 部署后首次 Cascade 前有真实触发风险。

---

## 修复内容

```python
# 修复后（正确）
if last_confirmed is None:
    # Per v1.0 §7.5: "undefined" state, not DRIFTING_CALM
    return StateRecord(
        state=None,
        none_reason=StateNoneReason.NO_MATCH,
        reason="CASCADE_COOLING_SUPPRESSED_CRITICAL_NO_PRIOR(...)",
        ...
    )
# last_confirmed is not None: hold prior state
return StateRecord(
    state=last_confirmed,
    none_reason=StateNoneReason.NOT_APPLICABLE,
    reason=f"CASCADE_COOLING_HOLD_PRIOR:{last_confirmed.value}(...)",
    ...
)
```

**StateNoneReason 选择**：使用 `NO_MATCH`（不新增枚举值）。CascadeCooling 压制是主动抑制，语义上类似"条件不成立"。如后续需统计 cooling 引发的 None 数量，可从 `reason` 字段过滤 `"CASCADE_COOLING_SUPPRESSED_CRITICAL_NO_PRIOR"`。

---

## 行为变更对照（场景 C — Cascade 注入）

合成场景：720 bar warmup + 288 bar post-warmup，每 48H 注入一次 CASCADE，CASCADE 后 2H 注入 CRITICAL 候选，且系统为首次 Cascade（`last_confirmed=None` 时刻发生）。

| 指标 | Before T1.9 | After T1.9 |
|------|-------------|------------|
| Cascade bars | 6 | 6（不变） |
| cooling 窗口内伪 DRIFTING_CALM bars | N | 0 |
| cooling 窗口内 state=None bars | M | N+M |
| no_match_bars（来自 cooling 新增） | 0 | N |

**含义**：cooling 期间不再产生伪 DRIFTING_CALM bar，state 序列更诚实，`state_rates["Drifting_Calm"]` 不被污染。

---

## last_confirmed 非 None 路径（选项 Y 的持仓行为）

当 `last_confirmed = CASCADE`（系统已有确认状态），CRITICAL 被压制时持仓 CASCADE state。这是合理的：系统刚经历 Cascade，抑制 Critical 的意图是防止立即再次开仓，而非放弃已确认的市场状态。

---

## 测试覆盖

| 测试 | 覆盖点 |
|------|--------|
| `test_cascade_cooling_no_prior_returns_none` | last_confirmed=None → state=None, none_reason=NO_MATCH, reason 含 CASCADE_COOLING |
| `test_cascade_cooling_with_prior_holds_state` | last_confirmed=CASCADE → state=CASCADE, not CRITICAL |
| 所有原有 TestCascadeCooling 测试 | 无需改动（原测试均以 DRIFTING_CALM 或 CASCADE 为 prior，不覆盖 None 路径） |

全套测试：325 passed（2026-04-28）。

---

## 不处理的残余问题（未纳入本任务）

- 选项 X（cooling 期间一律 state=None）：破坏信息连续性，在 last_confirmed 存在时没有意义，不采用
- 选项 Z（新 CASCADE_COOLING StateLabel）：需文档级修订，不在 P1 范围
- DwellFilter 在 `last_confirmed=None` 时的类似 fallback：已在 Task 1.7 代码中正确处理（`return raw`，不假设 DRIFTING_CALM）
