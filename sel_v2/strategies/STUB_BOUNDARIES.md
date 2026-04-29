# Strategy STUB Boundaries Registry

Single reference for every intentional placeholder across sel_v2 strategy code.
Update this file whenever a STUB is added or removed.

---

## Strategy 2 STUBs (Wave 2 origin)

| STUB 项 | 当前替代实现 | 真实实现需要 | Wave 几接入 |
|---|---|---|---|
| **OFI 持续性** (`ofi_persistent_same_direction`) | `evaluate()` 参数，默认 `None`。`None` 或 `False` → Type B 不触发 → ABORT | LOB collector 实时 OFI 方向 60s 窗口 | Wave 5 (LOB 30 天后) |
| **Sweep / Absorption / Crowding 识别** (`inverse_vocab`) | 外部字符串列表参数，无自动识别 | 反推词汇引擎（Wave 5） | Wave 5 |
| **liq_pulse** (`strategy2_entry`) | 布尔参数，默认 `False` | `v2_liquidations` 表 + 5min 滚动强度 | 无数据源，暂无时间表 |
| **cross_spread_pct** (`strategy2_entry`) | 浮点参数，默认 `None` | 多交易所 ticker | 无数据源，暂无时间表 |
| **H1 Hawkes 参数** | Wave 1 H2 MLE 值作为 cold-start | 7 天 tick 数据后 fit_gmm() | Wave 6/7 (paper Month 3+) |

---

## Strategy 1 STUBs (Wave 4 origin)

| STUB 项 | 文件/参数 | 当前替代实现 | 真实实现需要 | 激活条件 |
|---|---|---|---|---|
| **Step 4a: funding_pctile** | `strategy1_entry.py` — `funding_pctile` | `None` → 跳过 funding 过滤 | helixa.derivatives_snapshots (GRANT 后接入) | kanpan 执行 helixa_grants.sql |
| **Step 4b: oi_direction** | `strategy1_entry.py` — `oi_direction` | `None` → 跳过 OI 方向降级 | helixa.open_interest_history (GRANT 后接入) | kanpan 执行 helixa_grants.sql |
| **Step 4c: liquidation_pulse_1h** | `strategy1_entry.py` — `liquidation_pulse_1h` | `False` → 跳过清算脉冲检查 | `v2_liquidations` 表 | 无数据源，暂无时间表 |
| **Step 5: on-chain** | `strategy1_entry.py` — `large_withdrawal_active` | `None` → observation-only 跳过 | `v2_onchain_exchange_flows` | 无数据源（Glassnode/CryptoQuant 付费） |
| **Step 6: divergence** | `strategy1_entry.py` — `cross_spread_pct` | `None` → 跳过跨所价差校验 | 多交易所 ticker collector | 无数据源，暂无时间表 |
| **Step 7: vocab** | `strategy1_entry.py` — `vocab` | `None` → size_modifier=1.0 | 反推词汇引擎（Wave 5） | Wave 5 |

---

## Kelly Sizing STUBs (Wave 4 origin)

| STUB 项 | 文件 | 当前替代实现 | 激活条件 |
|---|---|---|---|
| **Phase 2 切换** | `kelly_sizing.py` — `KellySizer.phase` | Phase 0 固定 base_size | Wiki 审批 + paper Month 3 ≥ 30 笔交易 |
| **Phase 3 切换** | `kelly_sizing.py` — `KellySizer.phase` | 维持 Phase 2 | Wiki 审批 + paper Month 6 评估 |

---

## Exit STUBs (Wave 4 origin)

| STUB 项 | 文件 | 当前替代实现 | 激活条件 |
|---|---|---|---|
| **Absorption 反向减仓** (Strategy 1) | `strategy_exit.py` | 未实现（Wave 5 vocab 引擎后） | Wave 5 |
| **S2 batch exit: cusum_peak_since_entry** | `strategy_exit.py` | 调用方传入，SubAccount 追踪 | 已接入 SubAccount |

---

## Sub-Account STUBs (Wave 4 origin)

| STUB 项 | 文件 | 当前替代实现 | 真实实现需要 |
|---|---|---|---|
| **真实 OKX 子账户 API** | `sub_account.py` | paper 内存模拟，无 HTTP 调用 | paper trading 决策后接入 OKX API |
| **fees_paid 计算** | `db_writer.py` — `write_trade_exit` | 写入 0.0 | 真实 OKX 成交回报 |

---

## 依赖关系总结

```
立即可做（无需等待）:
  → helixa GRANT → Step 4a/4b 真实数据激活
  → Wave 4 策略引擎当前可用，所有 STUB 保守跳过

需等待 collector 30 天:
  → LOB entropy → check_coiling 可靠识别
  → OFI → check_surging 真实流量信号
  → Wave 5: Sweep/Absorption/Crowding 词汇引擎

无数据源（需付费或自采集）:
  → v2_liquidations (liq_pulse)
  → v2_onchain_exchange_flows (on-chain filter)
  → 跨所价差 (divergence filter)
```

---

## Wave 5 Observation Tool STUBs

| STUB 项 | 工具 | 当前替代实现 | 真实实现需要 | 激活条件 |
|---|---|---|---|---|
| **B1 pymc/numpyro** | `bayesian_hmm.py` | 自实现 Baum-Welch EM（2-state frequentist HMM）| pymc 或 numpyro 安装 | 环境加 pymc 后 |
| **B1 特征: entropy/OI** | `bayesian_hmm.py` | 仅 return + σ | LOB entropy、OI 数据 | LOB/OI collector 30 天后 |
| **B2 Drifting-Charged** | `hmm_boundary_arbiter.py` | Drifting-Charged 永远不识别（OI/funding STUB）| helixa GRANT + OI 数据 | kanpan helixa_grants.sql |
| **T2 因果关系** | `transfer_entropy_rolling.py` | funding_rate STUB → NO_DATA | funding/OFI/链上/跨所价差数据 | helixa GRANT + LOB |
| **H3 liquidation 事件流** | `hawkes_cascade_warning.py` | bar-level proxy（价格方向+成交量）| v2_liquidations 表数据 | liquidation collector 接入 |
| **H3 buy/sell 事件流** | `hawkes_cascade_warning.py` | bar-level proxy | tick-level LOB 买卖事件流 | LOB collector 30 天后 |
| **周报 DB 查询** | `reports/scheduler.py` | 静态 stub 数据（Drifting_Calm 98%）| v2_state_history / v2_cusum_events / v2_strategy_trades 查询 | paper trading 启动后 |
| **ToolEvaluator DB** | `evaluation/tool_evaluator.py` | 空列表（无 DB）| v2_inverse_vocab_events + v2_state_history | paper trading Month 3 评估 |

---
*维护责任: 每次新增 STUB 必须在此文件登记*
*最后更新: Wave 5 实施*
