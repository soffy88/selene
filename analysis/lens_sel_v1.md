# sel 视角 v1 — 状态机自述(三视角对比基准)

- 生成:`python -m sel_v2.offline.lens_study`(确定性:seed=42,整文件覆写)
- 数据:`v2_bars_4h ⋈ v2_state_annotation`,BTC-USDT,2024-07-03 → 2026-07-11,4432 bars(逐 bar 精确对齐)
- 纪律:observation-only;不碰 `states/**`/`strategies/**`;三视角同数据可并排对比

> **样本量诚实声明**:2 年标注中 Surging 腿仅 13 条(11 Exhaustion + 2 Stress 收尾),
> 稀有状态 bar 数:**Coiling=12,Critical=16,Cascade=0**(2026-07-11 Coiling 判据放宽后,Coiling 仅出现于
> 特征齐全的近期窗口;降级历史仍为 None 路径)。凡涉及腿级/事件级检验均为小样本,
> 功效有限;bar 级检验存在序列相依,p 值偏乐观(均已在对应小节标注)。

## 状态分布(2 年标注)

| state | bars | share |
|---|---:|---:|
| Drifting_Calm | 3105 | 70.1% |
| Surging | 1265 | 28.5% |
| Drifting_Charged | 34 | 0.8% |
| Critical | 16 | 0.4% |
| Coiling | 12 | 0.3% |

- degraded bars(特征缺失回退):3616 / 4432 = 81.6%
- 稀有状态:Coiling=12、Cascade=0(降级历史条件不可判)、Release 转移=0

## Surging 腿明细(13 条)

| leg | start | end | bars | direction | net ret | end via |
|---:|---|---|---:|---:|---:|---|
| 0 | 2024-11-06 | 2024-12-07 | 191 | +1 | +29.8% | Exhaustion |
| 1 | 2024-12-31 | 2025-01-03 | 21 | +1 | +2.8% | Exhaustion |
| 2 | 2025-01-17 | 2025-01-17 | 2 | +1 | +1.7% | Exhaustion |
| 3 | 2025-01-21 | 2025-01-22 | 5 | -1 | -1.8% | Exhaustion |
| 4 | 2025-02-03 | 2025-02-14 | 65 | -1 | -5.0% | Exhaustion |
| 5 | 2025-03-02 | 2025-03-27 | 147 | -1 | -6.4% | Exhaustion |
| 6 | 2025-06-03 | 2025-06-07 | 26 | -1 | -0.8% | Exhaustion |
| 7 | 2025-08-22 | 2025-09-09 | 111 | -1 | -4.2% | Exhaustion |
| 8 | 2025-10-12 | 2025-11-26 | 273 | -1 | -22.4% | Stress |
| 9 | 2025-11-28 | 2025-12-01 | 17 | -1 | -6.5% | Stress |
| 10 | 2025-12-02 | 2025-12-13 | 67 | -1 | -0.8% | Exhaustion |
| 11 | 2026-02-02 | 2026-03-07 | 199 | -1 | -15.0% | Exhaustion |
| 12 | 2026-06-10 | 2026-07-03 | 141 | -1 | -0.1% | Exhaustion |

- 腿驻留(bars):median=67,min=2,max=273
- Critical bars:16(2025-11-27, 2025-11-27, 2025-11-27, 2025-11-27, 2025-11-27, 2025-11-27, 2025-11-28, 2025-11-28, …)

## 现读(最近 30 bar)

- 状态序列:Charged×18 → Coiling×12
- 最新 bar:2026-07-11 12:00 UTC,state=Coiling,close=64,170
