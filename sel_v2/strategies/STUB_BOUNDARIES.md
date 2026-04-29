# Strategy 2 — STUB Boundaries Registry

Single reference for every intentional placeholder in Wave 2 strategy code.
Consult this file before starting Wave 3+ implementation to ensure no stub is missed.

| STUB 项 | 当前替代实现 | 真实实现需要 | Wave 几接入 |
|---|---|---|---|
| **OFI 持续性** (`ofi_persistent_same_direction`) | `evaluate()` 参数，默认 `None`。`None` 或 `False` → Type B 不触发 → ABORT，符合 §14.2 "类型未明 → abort" | LOB collector 实时逐笔委托流 → 滚动 60 s OFI 方向持续性检测 | Wave 3 |
| **Sweep / Absorption / Crowding 识别** (`inverse_vocab`) | `evaluate()` 字符串列表参数，由外部调用方传入（无自动识别逻辑） | 实时 LOB OFI + 逐笔成交 → 反推词汇引擎（Sweep/Absorption 判断模型） | Wave 3 |
| **liq_pulse** | `evaluate()` 布尔参数，默认 `False`。`True` → Step 4 ABORT | `v2_liquidations` 表 + 滚动 5 min 清算强度 > 95 分位触发 | Wave 3 |
| **cross_spread_pct** | `evaluate()` 浮点参数，默认 `None`。`> 0.5%` → Step 5 ABORT | 多交易所 ticker collector（Binance / Bybit / OKX） → 实时价差计算 | Wave 3 后 |
| **DB 写入** (`v2_cusum_events` / `v2_decision_trail`) | `evaluate()` 纯内存函数，不写任何 DB | 实时事件循环 + DB writer；每次 `evaluate()` 调用写 v2_cusum_events；ENTER 决策写 v2_decision_trail | Wave 3 |
| **H1 Hawkes 参数** (`HawkesParams.from_h2_reference()`) | 从 `v2_strategy_params` 读取 Wave 1 H2 4H-bar MLE 拟合值（`mu_ref=0.093136 / alpha_ref=0.023899 / beta_ref=0.043163`）作为 cold-start 占位。H2 是 4H 尺度过程，与 H1 秒级 tracker 时间尺度不同。 | 7 天+ tick 数据积累后调用 `fit_gmm()` 在线估计真实 per-second 参数 | Wave 3 (实时事件循环接入时启用 fit_gmm，fallback 路径继续从 v2_strategy_params 读) |
| **CUSUM 阈值** (`CUSUMShort` threshold) | 7 天滚动 95 分位（`< 20` 峰值时 cold-start 默认 `h=2.0`） | Month 1 纸交易后基于真实数据校准 drift_k 和 threshold_quantile（§11.4） | Wave 6 / Wave 7 (paper Month 3 / Month 6 评估后，按 v2.1 §11.4 / §13.1 节点) |

## 依赖关系

```
Wave 3 接入前提:
  LOB collector (sel-orderbook) → 稳定输出 Sweep / Absorption / OFI 信号
  v2_liquidations 表有数据    → liq_pulse 逻辑可激活
  tick 数据 7 天+              → fit_gmm() 可替换 H2 cold-start

Wave 3 实施顺序建议:
  1. OFI collector 接入 → ofi_persistent_same_direction 填充
  2. Sweep/Absorption 识别模型 → inverse_vocab 自动生成
  3. DB writer 接入 → evaluate() 结果持久化
  4. liq_pulse + cross_spread_pct → Step 4/5 真实数据激活
  5. fit_gmm() 定期重标定 → 替换 H2 cold-start
```

---
*生成时间: Wave 2 修正补丁*
*维护责任: 每次新增 STUB 必须在此文件登记*
