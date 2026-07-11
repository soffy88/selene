# 缠论视角 v1 — CHAN-1/2/3 实证

- 生成:`python -m sel_v2.offline.lens_study`(确定性:seed=42,整文件覆写)
- 数据:`v2_bars_4h ⋈ v2_state_annotation`,BTC-USDT,2024-07-03 → 2026-07-11,4429 bars(逐 bar 精确对齐)
- 纪律:observation-only;不碰 `states/**`/`strategies/**`;三视角同数据可并排对比

> **CHAN-1 偏差横幅**:sel 的 Release(价格突破 + OFI 跃升 + OI 加速)2 年标注与 live
> 均为 **0 次触发**,本节用**纯几何突破代理**(盘整区间收盘突破)实证 breakout-retest
> 假设本身,**不等于**检验 sel Release 语义(用户裁决,候选池记录)。
> 前向收益取 t+6→t+12(分类窗之后),与池原文"后续 24H"不同——避免分类窗与结果窗重叠的循环论证。

## CHAN-1 盘整/突破/回踩普查(双参数组,都报,不挑参)

### 参数组 K18

- 突破事件:58(A:11 B:32 C:15,尾部截断 0)
- A 类均值前向收益:-0.00%;C 类:-0.14%
- Welch 单侧 t=0.15,p=0.4403;Mann-Whitney p=0.4178
- bootstrap 90% CI(mean_A − mean_C):[-1.30%, +1.56%](seed=42)
- **verdict(K18):fail**(α=0.10,池原文标准)

### 参数组 K30

- 突破事件:58(A:17 B:30 C:11,尾部截断 0)
- A 类均值前向收益:-0.15%;C 类:+0.37%
- Welch 单侧 t=-0.80,p=0.7858;Mann-Whitney p=0.7881
- bootstrap 90% CI(mean_A − mean_C):[-1.55%, +0.50%](seed=42)
- **verdict(K30):fail**(α=0.10,池原文标准)

## CHAN-2 背驰(标注 Surging 腿内)

- 腿总数 13,可测腿(有同向前腿且前腿>1 bar)11,背驰候选 bar 181
- 2×2(bar 级,序列相依 → p 偏乐观):[[9, 172], [22, 855]]
- Fisher 单侧 p=0.0672 → **verdict:pass**

- 腿级描述(不做检验,n 太小):

| leg | 候选数 | 末 3 bar 内命中 |
|---:|---:|---|
| 1 | 0 | ✗ |
| 2 | 0 | ✗ |
| 4 | 49 | ✓ |
| 5 | 15 | ✓ |
| 6 | 0 | ✗ |
| 7 | 0 | ✗ |
| 8 | 26 | ✗ |
| 9 | 0 | ✗ |
| 10 | 0 | ✗ |
| 11 | 0 | ✗ |
| 12 | 91 | ✓ |

## CHAN-3 中枢重叠度

- overlap_ratio 有值 bar:4397/4429;**p70 = 2.328**(全样本内,live 冻结值)
> **H-CHAN3a 原判据 UNTESTABLE**:标注中 Coiling = 0 bar。以下为改编检验(lift,
> 非裸重合率——Drifting_Calm 基率 70.1%,裸重合率无意义):

| 替代域 | 高 overlap 命中率 | 低 overlap 命中率 | Fisher p |
|---|---:|---:|---:|
| Drifting_Calm | 62.8% | 72.9% | 1.0000 |
| σ_pctile<30 | 43.9% | 37.9% | 0.0001 |

- **verdict(3a,改编):adapted-pass**

### H-CHAN3b 分歧样本(高 overlap 且 sel 处趋势态)

- X(n=491)前向 6-bar 波动 median=0.00681;同态对照(n=827)=0.00799;Calm 基准=0.00646
- MW 单侧 p(波动)=0.0000;p(|收益|)=0.0134;CI90(X−对照)= [-0.00158, -0.00075]
- **verdict(3b):pass**

## 现读(最新 bar)

- overlap_ratio = 1.665
- 高于 p70?否/未知
