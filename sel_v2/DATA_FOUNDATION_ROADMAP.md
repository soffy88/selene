# sel_v2 数据底层路线图

**生成日期**: 2026-04-29  
**状态**: 事实记录，不含产品决策  
**适用范围**: Wave 4-5 工程启动前的数据可获取性评估

---

## 数据库现状总览

| 表 | 当前行数 | 期望行数（2024-01-01起2年）| 状态 |
|----|---------|--------------------------|------|
| `v2_bars_4h` | **5097** | ~5100 (4H bar × 2.3年) | ✅ 已回填 |
| `v2_ticks` | 0 | ~100亿+ (每日数百万笔) | ❌ 空 |
| `v2_lob_snapshots` | 0 | ~50亿+ (每分钟快照) | ❌ 空 |
| `v2_derivatives_snapshots` | 0 | ~6000 (8H funding点 × 2年) | ❌ 空 |
| `v2_liquidations` | 0 | ~数十万 (事件驱动) | ❌ 空 |
| `v2_onchain_exchange_flows` | 0 | ~数万 (大额链上流入/出) | ❌ 空 |
| `v2_4h_bars` | 不存在 | — | schema 中无此表，与 `v2_bars_4h` 是同一张 |

---

## 各表逐张分析

---

### 1. `v2_bars_4h`

**字段**: time / symbol / open / high / low / close / volume / source

**当前行数**: 5097  
**期望行数**: ~5100 (BTC-USDT 4H bar, 2024-01-01 → 当前)  
**状态**: ✅ 已在 Wave 3 收尾修正 1 中通过 `okx_backfill --mode db` 回填

**数据源可获取性**:  
- OKX `/api/v5/market/history-candles` — **公开 REST，无需 API key**  
- 回填命令已存在: `python -m sel_v2.data.okx_backfill --mode db --start 2024-01-01`  
- 历史深度: 完整，可追溯至合约上线

**历史回填可行性**: ✅ 可回填，已完成

**当前依赖模块**:
- Wave 1: 所有离线分析（TDA1、Hawkes 训练集）
- Wave 3: BarRunner 的 close price → σ / breakout 计算基础
- Wave 3: replay.py 主数据源
- Wave 4+: live runner 实时 4H bar 基础

**数据缺失对状态机的影响**: 无（已有数据）

---

### 2. `v2_ticks`

**字段**: timestamp / price / volume / side / exchange

**当前行数**: 0  
**期望行数**: BTC-USDT-SWAP 每日约 50-200 万笔成交，2年约 10-70 亿行

**数据源可获取性**:
- OKX `/api/v5/market/trades` — **公开 REST（实时最新）**，无需 API key  
- OKX `/api/v5/market/history-trades` — **公开 REST**，无需 API key；  
  实测深度: **最多 3 个月**（OKX 官方限制），且每页 100 条 × 每秒数千笔，  
  全量回填 2024-01-01 起不可能（数据量过大 + API 深度不足）  
- OKX WebSocket `trades` channel — 实时逐笔推送，自录需持续运行

**历史回填可行性**:  
- 2024-01-01 起: ❌ **不可回填**（REST 深度 3 个月，2 年前数据无 API 途径）  
- 2026-01-29 起（3个月内）: 技术上可用 history-trades 分批拉取，但数据量极大  
  (每日 ~100 万笔 × 90 天 = ~9000 万行，拉取时间 > 10 小时)  
- **实际路径**: 部署 WebSocket 实时 collector，从启动时间开始积累  
  最低可用历史（用于 OFI 计算）: **需要 collector 运行 ≥ 30 天**

**当前依赖模块**:
- Wave 4: OFI（Order Flow Imbalance）计算基础 → `ofi_cumulative_pctile`
- Wave 5: Sweep / Absorption 逆推词汇识别（逐笔买卖压差异）
- Wave 5: B1（体量不对称 Bayesian 工具）

**数据缺失对状态机的影响**:
- `ofi_cumulative_pctile` = None → Surging 条件 `ofi_90pct` 无法评估（met=None）
- `check_surging` 保守原则：ofi + oi_acceleration 全 None → met=None → Surging=0%
- Wave 5 Sweep/Absorption 识别无法运行

---

### 3. `v2_lob_snapshots`

**字段**: timestamp / bids (JSONB) / asks (JSONB) / exchange

**当前行数**: 0  
**期望行数**: 每分钟 1 次快照，2 年约 10.5 亿行（压缩后 TimescaleDB 估算 ~50-200 GB）

**数据源可获取性**:
- OKX `/api/v5/market/books` 或 `books-full` — **公开 REST（当前快照）**，无需 API key  
- OKX WebSocket `books` / `books5` channel — **实时推送，自录**，无需 API key  
- **无任何历史 LOB endpoint**（OKX 不提供 LOB 历史数据）

**历史回填可行性**:  
- ❌ **完全不可历史回填**  
- LOB 数据是瞬时状态，行情结束即消失，任何第三方也不提供 LOB 历史  
- **实际路径**: 部署 WebSocket LOB 自录 collector，从启动时间开始积累  
  最低可用历史（用于 Shannon 熵 + 熵方差）: **需要 collector 运行 ≥ 30 天**

**当前依赖模块**:
- Wave 4: LOB Shannon 熵 → `entropy_4h`, `entropy_pctile`
- Wave 4: 熵变化率方差 → `entropy_variance_rising` (Critical A2 子条件)
- Wave 4: LOB 深度分位 → `lob_depth_pctile` (Cascade 条件 1)
- Wave 4: OFI = LOB 差价加权买卖差 → `ofi_cumulative_pctile`
- Wave 5: Absorption / Sweep 识别（深度与价格对应关系）

**数据缺失对状态机的影响**:
- `entropy_pctile` = None → Coiling `entropy_lt_30pct` 无法评估
- `entropy_variance_rising` = None → Critical A2 = None → A_full 永远 False → Path 1 永远 0
- `lob_depth_pctile` = None → Cascade 条件 1 = None → Cascade met=None（3 条件全 None → Cascade=0%）
- `ofi_cumulative_pctile` = None → Surging OFI 门槛无法评估

---

### 4. `v2_derivatives_snapshots`

**字段**: timestamp / instrument / oi / funding_rate / next_funding

**当前行数**: 0  
**期望行数**: OI 每 4H 一个点 = ~5000 行；funding 每 8H 一个点 = ~2500 行（2年）

**数据源可获取性**:

**OI (Open Interest)**:
- OKX `/api/v5/public/open-interest` — **实时当前值**，公开，无需 API key  
- OKX `/api/v5/rubik/stat/contracts/open-interest-volume` — **历史**，公开，无需 API key  
  - 1H 粒度: 实测最大深度 ~**30 天**（14400 条 / 页面机制异常，实际最多 720 条 = 30天）  
  - 1D 粒度: 实测最大深度 ~**180 天**（分页 after 参数失效，实际 180 天窗口固定）  
  - ❌ **无法回填 2024-01-01 起的 OI 数据**

**Funding Rate**:
- OKX `/api/v5/public/funding-rate` — **实时当前值**，公开  
- OKX `/api/v5/public/funding-rate-history` — **历史**，公开，无需 API key  
  - 实测深度: **最多 273 条 ≈ 91 天（~3 个月）**  
  - 最早可达: **2026-01-28**（今日 2026-04-29 往前 91 天）  
  - ❌ **无法回填 2024-01-01 起的 funding 数据**

**历史回填可行性**:
- 2024-01-01 起 OI: ❌ 不可行（REST 最深 180 天，缺口约 20 个月）
- 2024-01-01 起 funding: ❌ 不可行（REST 最深 91 天，缺口约 25 个月）
- 2025-11-01 起 OI (180天内): ✅ 可用 1D rubik 端点，工程时间 < 1 小时
- 2026-01-28 起 funding (91天内): ✅ 可用 history 端点，工程时间 < 30 分钟
- **第三方回填选项** (决策留 Wiki):  
  - Coinalyze: BTC 期货 OI + funding 历史，付费，可达 2020+  
  - CryptoQuant: 同上，付费  
  - Glassnode: OI 有限，付费  
  - 如需覆盖 2024-01-01 起，第三方是唯一途径
- **实时 collector 路径**: 部署定时任务（每 4H）拉 OI + funding，从启动时间积累；  
  最低可用历史: **需运行 ≥ 30 天**（计算 oi_change_rate 需要 6 bar 滑窗）

**当前依赖模块**:
- Wave 3: `oi_change_rate`, `oi_change_rate_pctile` → Coiling + Drifting-Charged 核心条件
- Wave 3: `funding_rate`, `funding_pctile`, `funding_persistent` → Coiling + Drifting-Charged
- Wave 4: K1 凯利仓位（背景市场情绪参考）
- Wave 5: 周报衍生品市场结构分析

**数据缺失对状态机的影响**:
- `oi_change_rate` = None + `oi_change_rate_pctile` = None → Coiling `oi_positive` = None，Drifting-Charged `oi_elevated` = None
- `funding_persistent` = None → Drifting-Charged `accumulation` = None → met=None
- 结果: Coiling 保守原则 → met=None → Coiling=0%；Drifting-Charged met=None → Drifting_Charged=0%
- 当前 replay: Drifting_Calm=98%，根因是这两张表空

---

### 5. `v2_liquidations`

**字段**: timestamp / instrument / side / size_usd / price

**当前行数**: 0  
**期望行数**: 事件驱动，BTC 行情平静期约 100-500 事件/小时，极端行情可达数万/小时；  
2 年估算 ~1000 万行

**数据源可获取性**:
- OKX `/api/v5/public/liquidation-orders` — **公开 REST**，无需 API key  
  必须传 `instFamily=BTC-USDT`（实测 `instId` 不被接受）  
  实测: 可返回当前批次的清算事件（含 bkPx / size / side）  
  **无历史翻页能力**（每次只返回当前窗口内事件，无 `after` 翻页）  
- OKX WebSocket `liquidation-orders` channel — **实时推送**，公开  

**历史回填可行性**:  
- ❌ **完全不可历史回填**（API 无历史端点）  
- **实际路径**: WebSocket 实时 collector 自录，从启动时间积累  
  最低可用历史（用于 liquidation_pulse 分位计算）: **需运行 ≥ 30 天**

**当前依赖模块**:
- Wave 3: `liquidation_pulse` → Cascade 条件 2（当前 STUB）
- Wave 4: liq_pulse 逻辑激活 → Strategy 2 `evaluate()` Step 4 ABORT 条件
- Wave 5: 逆推词汇 Squeeze / Liquidation Cascade 识别

**数据缺失对状态机的影响**:
- `liquidation_pulse` = None → Cascade 条件 2 = None（OR 逻辑下不阻塞，但无法触发）
- 目前 Cascade = 0% 的三个原因之一（条件 2 = None）

---

### 6. `v2_onchain_exchange_flows`

**字段**: timestamp / exchange / direction / amount_btc / block_height

**当前行数**: 0  
**期望行数**: 事件驱动，大额链上流入/出（定义阈值待定），约 10-100 事件/天；  
2 年约 7000-70000 行

**数据源可获取性**:
- OKX **无链上数据 API**（OKX 是 CEX，不提供链上流向数据）  
- **必须依赖第三方数据源**（以下均为付费）:  
  - Glassnode: 交易所净流入/出，BTC 历史可达 2009+，付费订阅  
  - Nansen: 智能资金追踪，标注交易所热/冷钱包，付费  
  - CryptoQuant: 交易所 reserve + netflow，付费  
  - Dune Analytics: 链上自定义查询，部分公开但需要自建查询  
- 无公开免费 API 能提供精度足够的链上交易所流向数据

**历史回填可行性**:
- 历史回填: ✅ 付费第三方可覆盖 2024-01-01 起  
- 实时接入: 需要付费 API key + 自建 collector  
- 成本未知（需各平台报价）

**当前依赖模块**:
- Wave 5: 周报链上宏观分析（目前为 STUB，未在 v2.0 §5 条件中直接使用）
- 非 v2.0 状态机直接依赖项，属于"观察性"工具层

**数据缺失对状态机的影响**:
- **当前 0 影响**：v2.0 6 状态机不直接依赖链上流向数据  
- 间接：Wave 5 周报中的宏观市场结构分析无法完成

---

## OKX 公开 Endpoint 实测摘要

| Endpoint | 需要 API key | 历史深度 | 实测结论 |
|----------|------------|---------|---------|
| `/market/history-candles` | 否 | 全量 | ✅ 4H OHLCV 回填完整 |
| `/public/funding-rate-history` | 否 | **~91 天**（273条） | ⚠️ 不足2年 |
| `/public/funding-rate` | 否 | 实时 | ✅ 当前值 |
| `/rubik/stat/contracts/open-interest-volume` 1D | 否 | **~180 天**（分页失效） | ⚠️ 不足2年 |
| `/rubik/stat/contracts/open-interest-volume` 1H | 否 | **~30 天** | ❌ 严重不足 |
| `/public/open-interest` | 否 | 实时 | ✅ 当前值 |
| `/public/liquidation-orders` | 否 | 无历史（当前窗口） | ❌ 无历史 |
| `/market/trades` | 否 | 实时 | ✅ 当前逐笔 |
| `/market/history-trades` | 否 | **~3 个月**（数据量极大） | ⚠️ 有限 |
| `/market/books` | 否 | 实时快照 | ✅ 当前 LOB |
| LOB 历史 | — | **不存在** | ❌ 无任何历史端点 |
| 链上交易所流向 | — | **不存在** | ❌ OKX 无此数据 |

---

## 数据可获取性分类

### 类别 A：可通过 OKX REST 历史回填

| 数据 | 端点 | 深度限制 | 2024-01-01 回填 |
|------|------|---------|----------------|
| 4H OHLCV | `/market/history-candles` | 全量 | ✅ 已完成 |
| Funding rate (2026-01-28后) | `/public/funding-rate-history` | 91天 | ❌ 不完整 |
| OI daily (2025-11-01后) | `/rubik/.../open-interest-volume` 1D | 180天 | ❌ 不完整 |

### 类别 B：可通过实时 Collector 自录（OKX 公开 WS，无需 API key）

| 数据 | OKX WS Channel | 最低积累时长 | 优先级 |
|------|---------------|------------|-------|
| OI / Funding (定时 REST 轮询) | N/A（每 4H 调 REST） | 30天 | **P1** |
| 清算事件 | `liquidation-orders` | 30天 | **P1** |
| 逐笔成交 | `trades` | 30天 | P2 |
| LOB 快照 | `books` / `books5` | 30天 | P2 |

### 类别 C：需要付费第三方

| 数据 | 典型数据源 | 注意事项 |
|------|---------|---------|
| OI 历史 2024+ | Coinalyze / CryptoQuant | 价格未知，需调研 |
| Funding 历史 2024+ | 同上 | 同上 |
| 链上交易所流向 | Glassnode / Nansen / CryptoQuant | 价格未知 |

### 类别 D：永久无法历史回填（只能从 Collector 启动时起）

| 数据 | 原因 |
|------|------|
| LOB 快照（`v2_lob_snapshots`） | 历史 LOB 不存在于任何 API 或第三方 |
| 逐笔成交历史 2024+ | OKX REST 仅 3 个月；其余数据商一般不提供原始 tick |
| 实时清算（精确时间戳级别） | OKX 仅保留近期窗口 |

---

## 状态机 STUB 解封路径（数据视角）

以下对应 `STATE_STUB_BOUNDARIES.md` 的 15 项 STUB：

| STUB 字段 | 解封需要 | 数据类别 | 最短可达时间 |
|---------|---------|---------|------------|
| `oi_change_rate` / `oi_change_rate_pctile` | v2_derivatives_snapshots OI 数据 | B（实时轮询）或 C（历史） | Collector 启动后 30 天 |
| `funding_pctile` / `funding_persistent` | v2_derivatives_snapshots funding 数据 | B（实时轮询）或 C（历史） | Collector 启动后 30 天 |
| `liquidation_pulse` | v2_liquidations 数据 | B（WS 实时） | Collector 启动后 30 天 |
| `lob_depth_pctile` | v2_lob_snapshots 数据 | D（只能自录） | Collector 启动后 30 天 |
| `entropy_4h` / `entropy_pctile` | v2_lob_snapshots 数据 | D（只能自录） | Collector 启动后 30 天 |
| `entropy_variance_rising` | 同上 + 滚动方差计算 | D | Collector 启动后 30 天 |
| `ofi_cumulative_pctile` | v2_ticks 或 LOB 数据 | D（只能自录） | Collector 启动后 30 天 |
| `cross_exchange_spread` | 多所 ticker collector | B（多所实时） | Collector 启动后立即 |
| `oi_acceleration` | 同 oi_change_rate，需二阶差分 | B 或 C | 同 OI |

**Cascade 解封最短路径**（需同时满足 3 条件之一）:
- 条件 1: lob_depth + σ → LOB Collector 运行 ≥ 30 天
- 条件 2: liquidation_pulse → Liquidations Collector 运行 ≥ 30 天  
- 条件 3: cross_exchange_spread → 多所 ticker collector 运行后立即可用

**Critical Path 1 解封最短路径**:
- 需 entropy_variance_rising (A2) → LOB Collector 运行 ≥ 30 天

---

## 依赖关系汇总（按 Wave）

### Wave 1 / Wave 2（已完成）
- 依赖: `v2_bars_4h` ✅
- 不直接依赖任何当前为空的表

### Wave 3（已完成，状态分布受 STUB 影响）
- 依赖: `v2_bars_4h` ✅, `v2_strategy_params` ✅
- 受影响: `v2_derivatives_snapshots` ❌ → Coiling=0%, DCharged=0%
- 受影响: `v2_lob_snapshots` ❌ → Cascade=0%, Critical Path1=0%
- 受影响: `v2_liquidations` ❌ → Cascade 条件 2 无法触发

### Wave 4（待启动）
- 硬性依赖（无则无法测试关键功能）:
  - `v2_derivatives_snapshots` — Coiling / Drifting-Charged 识别
  - `v2_liquidations` — Cascade / Strategy 2 abort gate
- 软性依赖（有则更完整）:
  - `v2_lob_snapshots` — Cascade 条件 1 + Critical A2

### Wave 5（待启动）
- 依赖: `v2_ticks`, `v2_lob_snapshots` — Sweep / Absorption 逆推词汇
- 依赖: `v2_onchain_exchange_flows` — 链上宏观周报（第三方必须）

---

## 关键判断点（决策权留 Wiki）

以下均为事实陈述，不含建议：

1. **v2_derivatives_snapshots 历史回填方案**: OKX REST 仅覆盖 90-180 天，2024-01-01 起的 OI + funding 历史需要第三方数据源（付费）或接受"从 collector 启动时间起"作为历史起点。

2. **Wave 4 最早启动条件**: 若要在 Wave 4 中看到 Coiling / Drifting-Charged > 0%，在 replay 层面需要 OI + funding 历史数据；在 live 层面需要 OI/funding collector 已运行 ≥ 30 天。

3. **LOB collector 工程投入**: v2_lob_snapshots 是体量最大、工程最重的表（2年约 50-200 GB），且无历史回填可能。启动该 collector 的决策影响 Critical A2 / Cascade 条件 1 / OFI / Wave 5 全部工具。

4. **链上数据是否引入**: `v2_onchain_exchange_flows` 当前对状态机无直接影响，属 Wave 5 观察性工具。引入与否纯属产品优先级决策。

5. **replay 历史质量 vs 实时质量权衡**: 当前 replay 使用 parquet 价格数据 + STUB 衍生数据，结果（Coiling=0%）反映的是"数据缺失下的保守机器行为"，不代表系统在有完整数据时的真实状态分布。

---

*本文档基于 2026-04-29 实测数据。OKX API 端点行为可能随版本变化。*  
*参考: sel_v2/states/STATE_STUB_BOUNDARIES.md*
