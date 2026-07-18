# 三视角对比与裁决 v1(lens verdict)

- 生成:`python -m sel_v2.offline.lens_study`(确定性:seed=42,整文件覆写)
- 数据:`v2_bars_4h ⋈ v2_state_annotation`,BTC-USDT,2024-07-03 → 2026-07-12,4435 bars(逐 bar 精确对齐)
- 纪律:observation-only;不碰 `states/**`/`strategies/**`;三视角同数据可并排对比

> **样本量诚实声明**:2 年标注中 Surging 腿仅 13 条(11 Exhaustion + 2 Stress 收尾),
> 稀有状态 bar 数:**Coiling=10,Critical=16,Cascade=0**(2026-07-11 Coiling 判据放宽后,Coiling 仅出现于
> 特征齐全的近期窗口;降级历史仍为 None 路径)。凡涉及腿级/事件级检验均为小样本,
> 功效有限;bar 级检验存在序列相依,p 值偏乐观(均已在对应小节标注)。

## 逐假设结果(裸 p 判 pass/fail 按池原文;BH q 同列,FDR 下失守者标注)

| 假设 | 统计量 | 裸 p | BH q | 判定 |
|---|---|---:|---:|---|
| H-CHAN1[K18] | Welch t=0.15 | 0.4403 | 0.7706 | fail |
| H-CHAN1[K30] | Welch t=-0.80 | 0.7858 | 1.0000 | fail |
| H-CHAN2 | Fisher | 0.0672 | 0.1568 | pass ⚠FDR失守 |
| H-CHAN3a[calm] | Fisher lift | 1.0000 | 1.0000 | fail(Coiling n=10,判据放宽后首批样本) |
| H-CHAN3a[low_sigma] | Fisher lift | 0.0001 | 0.0005 | pass(Coiling n=10,判据放宽后首批样本) |
| H-CHAN3a[coiling] | Fisher lift | 1.0000 | 1.0000 | fail(Coiling n=10,判据放宽后首批样本) |
| H-CHAN3b | MW(fwd vol) | 0.0000 | 0.0003 | pass |
| H-ICT2a | 7/13,CP CI [25%,81%] | — | — | 点估计(n=13 不作显著性) |
| H-ICT2b | 配对 n=2 | None | nan | descriptive |
| H-ICT1a/1b | VPIN | — | — | **PENDING(tick history 6.0d < 30d)** |

## 每条 Surging 腿的三视角对照

| leg | sel(方向/收尾) | 缠论(背驰候选) | ICT(众数结构/lead) |
|---:|---|---|---|
| 0 | +1/Exhaustion | 不可测(无前腿) | UP✓ |
| 1 | +1/Exhaustion | 0 个候选 | RANGE✗ |
| 2 | +1/Exhaustion | 0 个候选 | DOWN✗ |
| 3 | -1/Exhaustion | 不可测(无前腿) | RANGE✗ |
| 4 | -1/Exhaustion | 49 个候选 | DOWN✓ |
| 5 | -1/Exhaustion | 15 个候选 | RANGE✗,CHoCH lead 46 |
| 6 | -1/Exhaustion | 0 个候选 | RANGE✗ |
| 7 | -1/Exhaustion | 0 个候选 | DOWN✓,CHoCH lead 111 |
| 8 | -1/Stress | 26 个候选 | DOWN✓ |
| 9 | -1/Stress | 0 个候选 | DOWN✓ |
| 10 | -1/Exhaustion | 0 个候选 | RANGE✗ |
| 11 | -1/Exhaustion | 0 个候选 | DOWN✓,CHoCH lead 61 |
| 12 | -1/Exhaustion | 91 个候选 | DOWN✓,CHoCH lead 4 |

## Live 接入 go/no-go(规则:仅离线明确触发失败标准者不接;UNDERPOWERED/PENDING 接入继续积累)

| 候选 | 接入 | 依据 |
|---|---|---|
| CHAN-1 (chan_retest) | **NO-GO(废弃)** | K18:fail; K30:fail |
| CHAN-2 (chan_divergence) | **GO** | pass |
| CHAN-3 (chan_pivot) | **GO** | 3a:adapted-pass / 3b:pass |
| ICT-2 (swing_structure) | **GO** | 一致率 7/13 |
| ICT-1 (vpin) | **GO** | PENDING(数据不足) |

## Live 冻结参数(唯一出处;观察工具引用本文档)

- CHAN-1 盘整参数组:主 `K18`(18 bar, 3.0×ATR),敏感性 `K30`(30 bar, 4.0×ATR)——live 用主参数组
- CHAN-3 overlap 阈值:**p70 = 2.326**(2yr 全样本,in-sample,live 冻结)
- ICT-2 zigzag:1.5×ATR(14)(与 CHAN-3 共享,substate 同参)
- ICT-1 VPIN:V_bucket **自适应**(监控服务启动时按 min(30d, 可用) tick 量自举,= 日均 tick 量/50;本次研究日取值 180,390.7 tick 口径),信号阈值 = 滚动 p95,warmup 100 桶;`history_days` 入 metadata(Month-3 评估剔除 <30d)

> v2_ticks starts 2026-07-06 (no retention drop) — first honest 30d window ≈ 2026-08-05; re-run `python -m sel_v2.offline.lens_study` then.
