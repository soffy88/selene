# Wave 3 State Machine — STUB Boundaries Registry

Single reference for every intentional placeholder in the Wave 3 state machine.
Consult before starting Wave 4+ implementation.

| STUB 项 | 当前替代 | 真实实现需要 | Wave 几接入 |
|---|---|---|---|
| ~~**LOB Shannon 熵** (`entropy_4h` / `entropy_pctile`)~~ | ~~`None`~~ **已接入**(audit follow-up B):`v2_lob_snapshots.entropy`(iris md-adapter,P3 采集层合并)→ `paper_engine._load_microstructure_series` 每 4H 聚合 → `BarRunner` 滚动分位 | — | 已完成 |
| ~~**熵变化率方差** (`entropy_variance_rising`)~~ | ~~`None`~~ **已接入**(GL1 T0.1,2026-07-08):`sel_v2/features/lob_entropy.py::rolling_entropy_variance` 对 `entropy_4h` 序列取滚动方差(窗口 6 bar=24h)+ 3-bar 单调上升判定;`states/critical_logic.py` A2 现返回真实 True/False/None。生产数据验证:34 根 bar 中 31 根 entropy_variance 非 None,7 根 entropy_variance_rising=True | — | 已完成 |
| **OI 累计变化率** (`oi_change_rate`) | `None` | `v2_derivatives_snapshots` 每 4H 聚合 OI | Wave 4 |
| **OI 加速度** (`oi_acceleration`) | `None` | OI change rate 二阶差分 > 0 | Wave 4 |
| **Funding rate** (`funding_rate` / `funding_pctile`) | `None` | `v2_derivatives_snapshots` 8H funding 点 | Wave 4 |
| **Funding 持续方向** (`funding_persistent`) | `None` | 6+ 连续 bar 同号 funding | Wave 4 |
| **LOB 深度分位** (`lob_depth_pctile`) | `None` | LOB snapshot → 7 天深度滚动 5 分位 | Wave 4 |
| **清算脉冲** (`liquidation_pulse`) | `None` | `v2_liquidations` 滚动均值 × 5 倍阈值 | Wave 4 |
| **跨所价差** (`cross_exchange_spread`) | `None` | 多所 ticker collector → 实时价差 % | Wave 4+ |
| **OFI 累计分位** (`ofi_cumulative_pctile`) | `None` | LOB OFI collector → 7 天 90 分位 | Wave 4 |
| **Cascade 条件 1 全满** (σ + LOB) | σ 条件可计算, LOB=None → 总条件 = None | LOB depth 接入 | Wave 4 |
| **Cascade 条件 2** (清算脉冲) | `None` | `v2_liquidations` 接入 | Wave 4 |
| **Cascade 条件 3** (跨所价差) | `None` | 多所价差接入 | Wave 4+ |
| **inverse_vocab 自动识别** | Strategy 2 `evaluate()` 外部传入 | LOB OFI + 逐笔 → Sweep/Absorption 识别 | Wave 4 |
| **v2_inverse_vocab_events W2/B1/I1** | 接口占位 (`write_inverse_vocab_event`) | Wave 5 工具实施 | Wave 5 |

## 影响分析

Wave 3 replay 中因 STUB 导致的已知偏差：

1. **Cascade = 0%**: 三个触发条件均需 STUB 数据。§5.7 设计预期 < 1% 真实占比。
   已知局限，等 Wave 4 LOB + liquidation 接入后重跑。

2. ~~**Critical A_full = 0%**: A2 (LOB 熵方差) 永远 None，Critical 只能经 Path 2
   (A_partial + B + C)。等 LOB 接入后才能测试 Path 1。~~ **已解除(GL1 T0.1)**:A2
   现基于真实 entropy_variance_rising，Path 1 (A_full AND (B OR C)) 端到端可达，
   已用真实生产数据验证（详见上表）。Wave 3 replay 历史数据仍反映旧状态，需等
   paper_epoch（GL1 T0.5）启动后的新纪元重跑才能观察 A_full 实际触发率。

3. **Drifting-Charged 可能低估**: OI 和 funding 两个核心条件均 STUB，
   该状态仅凭 σ 中间区间判断，缺少 OI 蓄能信号。

4. **Surging 可能低估或高估**: OFI 和 OI 加速度缺失，只靠价格突破 + σ 跳升，
   可能放过 OFI 不足的伪突破，也可能漏掉 OFI 强但 σ 未及时跳升的真突破。

## Wave 4 接入优先级

```
Priority 1 (Cascade 激活):
  LOB depth collector → lob_depth_pctile  [已接入]
  v2_liquidations 接入 → liquidation_pulse

Priority 2 (Critical Path 1 激活):
  LOB entropy collector → entropy_4h, entropy_variance_rising  [已接入,GL1 T0.1]

Priority 3 (Surging/Coiling 改善):
  OFI collector → ofi_cumulative_pctile
  OI/funding collector → oi_change_rate, funding_rate
```

---
*生成时间: Wave 3*
*维护责任: 每次新增 STUB 必须在此文件登记*
