# TASK 1.8.1 — Candidate B Impact Analysis

**决策日期**：2026-04-28  
**实装 commit**：`699f219`  
**关联**：EC-07 RESOLVED

---

## 背景

Task 1.8 为 Rule 2 signal_lag 引入了 `rule_2_subtype`（`missing_data` vs `no_match`），但动作仍为候选 A（两种情形均 CLOSE）。Task 1.8.1 实装候选 B 逻辑：

- `MISSING_DATA` → `NO_ACTION`（HOLD）+ 运营告警
- `NO_MATCH` → `CLOSE`（保留候选 A 行为）

---

## 行为变更对照

### Rule 2 触发路径

| 情形 | 候选 A（变更前） | 候选 B（变更后） |
|------|-----------------|-----------------|
| lag > 2H，subtype = MISSING_DATA | CLOSE（强制平仓） | HOLD（保留仓位）+ redis.xadd system.alerts |
| lag > 2H，subtype = NO_MATCH | CLOSE | CLOSE（不变） |
| Cascade state（Rule 1） | CLOSE（Rule 1，不受影响） | CLOSE（Rule 1 优先级不变） |

### 告警路径

| 条件 | 行为 |
|------|------|
| `alert_required=True` + redis 可用 + 距上次告警 > 6H | 发布 `system.alerts` xadd，type=risk_alert |
| `alert_required=True` + 距上次告警 ≤ 6H | 抑制（dedup） |
| `alert_required=False`（NO_MATCH 或其他规则） | 不发布 |

---

## 合成场景数据

### 场景 1：collector 临时停服 3H（missing_data）

假设仓位 $10,000 LONG，入场价 $60,000，当前价 $61,000（浮盈 +$167）：

**候选 A（变更前）**：
- 3H 无状态 → lag = 3H > 2H → CLOSE 触发
- 平仓执行，浮盈锁定（或在更差价格平仓）
- 若 collector 恢复后状态仍为进场信号 → 需重新开仓，支付 2× fee

**候选 B（变更后）**：
- 3H 无状态 → lag = 3H > 2H → MISSING_DATA → HOLD
- 仓位保留，告警发出（system.alerts）
- 运营人员收到告警，检查 collector 状态，确认是重启导致
- Collector 恢复后状态更新，下一 bar 正常决策

**结论**：候选 B 在 collector 临时故障场景下避免 1 次不必要的平仓 + 2× fee。

### 场景 2：collector 长期停服 > 2H（missing_data，持续）

**候选 B 行为**：
- 每 bar（每 1H）lag 递增
- 每 6H（dedup 间隔）触发一次 risk_alert
- 仓位在完全无状态保护下持续持仓
- 若 collector 停服 24H：仓位持仓 24H，期间 4 次告警；状态引擎不更新

**风险敞口**：collector 停服期间，仓位承担完整市场风险，无 Rule 2 保护。
**缓解措施**：运营人员必须响应首次 risk_alert 并手动评估。

### 场景 3：真实无状态（no_match，市场异常）

**候选 A 和 B 行为相同**：lag > 2H + NO_MATCH → CLOSE。

---

## 实装细节

### 告警字段

`system.alerts` xadd 消息：
```python
{
    "type": "risk_alert",
    "reason": "[Selene Risk Alert] Rule 2 - Missing Data Lag\n\nBar: ...",
    "bar_time": "2026-04-28T12:00:00+00:00",
    "symbol": "BTCUSDT",
    "rule_2_subtype": "missing_data",
}
```

### Dedup 机制

- `runner._last_alert_time`：上次告警发出时间（内存状态，进程重启归零）
- `missing_data_alert_dedup_hours: 6`：config YAML 中，不来自 v1.0 spec
- 进程重启后首次 missing_data 必然触发告警（`_last_alert_time = None`）

### Rule 1 优先级不变

`RiskGate.check()` 中 Cascade 检查先于 Rule 2：
```python
if current_state == "Cascade" and self.config.cascade_always_overrides:
    return self._fire("cascade", ..., DecisionAction.CLOSE, ...)
# Rule 2 在此之后
```

---

## 测试覆盖

| 测试文件 | 测试类 | 覆盖点 |
|---------|--------|--------|
| `tests/paper_trading/test_risk.py` | `TestCandidateB` | missing_data→NO_ACTION+alert_required; no_match→CLOSE; cascade 优先覆盖候选 B |
| `tests/paper_trading/test_runner.py` | `TestRule2AlertDedup` | 首次告警触发; 6H 内抑制; 6H 后再触发; stream/type/subtype 字段验证 |

全套测试：321 passed（2026-04-28）。

---

## 残余风险记录

1. **长期故障持仓风险**：collector 停服 > 24H 时，仓位在无状态下持续持仓。运营 SLA 需规定响应时间上限。
2. **进程重启 dedup 归零**：进程重启后 `_last_alert_time = None`，下次 missing_data bar 立即告警（合理，但会在重启后多发一条）。
3. **`signal_lag_max_hours = 2H` 值未变**：候选 B 不修改触发阈值，仅改变 missing_data 子路径动作。
