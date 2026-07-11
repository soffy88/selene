# ICT 视角 v1 — ICT-2 结构 + ICT-1 VPIN pilot

- 生成:`python -m sel_v2.offline.lens_study`(确定性:seed=42,整文件覆写)
- 数据:`v2_bars_4h ⋈ v2_state_annotation`,BTC-USDT,2024-07-03 → 2026-07-11,4429 bars(逐 bar 精确对齐)
- 纪律:observation-only;不碰 `states/**`/`strategies/**`;三视角同数据可并排对比

> **样本量诚实声明**:2 年标注中 Surging 腿仅 13 条(11 Exhaustion + 2 Stress 收尾),
> Critical 16 bar,**Coiling=0、Cascade=0、Release=0**。凡涉及腿级/事件级检验均为小样本,
> 功效有限;bar 级检验存在序列相依,p 值偏乐观(均已在对应小节标注)。

## ICT-2 swing 结构(1.5×ATR zigzag,与 CHAN-3 共享;全部事件取确认时刻,无前视)

| 结构态 | bars | share |
|---|---:|---:|
| UP | 1461 | 33.0% |
| DOWN | 1500 | 33.9% |
| RANGE | 1468 | 33.1% |

- 事件普查:BOS_UP 33 / BOS_DOWN 36 / CHOCH_UP 17 / CHOCH_DOWN 19

### H-ICT2a 结构方向 vs sel Surging 腿方向(腿方向=累计收益符号,sel Surging 无方向字段)

| leg | 方向 | 众数结构态 | 一致 |
|---:|---:|---|---|
| 0 | +1 | UP | ✓ |
| 1 | +1 | RANGE | ✗ |
| 2 | +1 | DOWN | ✗ |
| 3 | -1 | RANGE | ✗ |
| 4 | -1 | DOWN | ✓ |
| 5 | -1 | RANGE | ✗ |
| 6 | -1 | RANGE | ✗ |
| 7 | -1 | DOWN | ✓ |
| 8 | -1 | DOWN | ✓ |
| 9 | -1 | DOWN | ✓ |
| 10 | -1 | RANGE | ✗ |
| 11 | -1 | DOWN | ✓ |
| 12 | -1 | DOWN | ✓ |

- RANGE 计不一致:**7/13 = 54%**,CP 95% CI [25%, 81%]
- 剔除 RANGE 众数腿:7/8,CI [47%, 100%]
- bar 级(辅助,序列相依):44.6%(n=1265)
- **n=13 的 CI 宽 ±~25pp——>70% 只能以点估计评估,不作显著性声明**

### H-ICT2b CHoCH vs CHAN-2 背驰:对 Exhaustion 腿终结的 lead(bar 数)

| leg | 方向 | CHoCH lead | 背驰 lead |
|---:|---:|---:|---:|
| 0 | +1 | — | — |
| 1 | +1 | — | — |
| 2 | +1 | — | — |
| 3 | -1 | — | — |
| 4 | -1 | — | 1 |
| 5 | -1 | 46 | 1 |
| 6 | -1 | — | — |
| 7 | -1 | 111 | — |
| 10 | -1 | — | — |
| 11 | -1 | 61 | — |
| 12 | -1 | 4 | 1 |

- 配对腿 n=2:median CHoCH lead = 25,median 背驰 lead = 1(配对 <6 → descriptive only)
- FP(Surging 内信号,6 bar 内未跟腿终结):CHoCH 5/6,背驰 163/181;Fisher p=0.479

## ICT-1 VPIN pilot(**H-ICT1a/1b:DATA-INSUFFICIENT-PENDING**)

> v2_ticks starts 2026-07-06 (no retention drop) — first honest 30d window ≈ 2026-08-05; re-run `python -m sel_v2.offline.lens_study` then.

- tick:7,021,582 笔,07-06 → 07-11;完成桶 258,VPIN 点 209
- V_bucket 引导:30d bar 量基线 → 3,311.1(coin 口径);tick/bar 量比 = 56.456(∉[0.5,2] → tick size 与 bar volume 非同一单位,**已按比值换算到 tick 口径**)→ **V_bucket = 186,928.4**(tick 口径)
- 分布:p50=0.128 p90=0.145 p95=0.153 p97=0.156 max=0.163
- 桶时长(分钟):median=23.5 min=0.1 max=145.9
- lag-1 自相关 = 0.982;side-VPIN vs BVC-VPIN 相关 = 0.157(side 为主分类,BVC 为无 side 场景的对照验证)
- 滚动分位 warmup(100 桶):已达;p95=0.153 p97=0.156

## 现读(最新 bar)

- 结构态 = RANGE;最近事件:CHOCH_DOWN @ 2026-07-08
