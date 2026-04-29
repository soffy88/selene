# sel Language v2.1 Patches — 数学工具增量补丁

**作者**:Wiki(决策)+ Claude(架构 + 调研)  
**日期**:2026-04-29  
**版本**:v2.1(增量补丁,不替换 v2.0 主文档)  
**配套**:`sel-language-v2.0.md`(冻结)

---

# 文档定位与生效边界

## 本文档与 v2.0 的关系

本文档是 sel v2.0 主文档(`sel-language-v2.0.md`)的**增量补丁集**。

```
关系结构
═══════════════════════════════════════════════════

sel-language-v2.0.md(冻结)
  ├─ 公理 + 状态机 + 反推词汇 + 双策略架构 + 运营机制
  └─ 架构层面:不可改

sel-language-v2.1-patches.md(本文档)
  ├─ 14 个数学工具增量补丁
  ├─ 实施优先级与工作量评估
  ├─ 评估节点(3/6/12/24 月)
  └─ 过度自信防御机制

═══════════════════════════════════════════════════
```

**冻结原则**:v2.0 架构层面已锁定,本文档仅增量增强,不重写。任何 patch 与 v2.0 冲突时,以 v2.0 为准。

## 本文档的产生过程

按规则 #17(设计前置调研),v2.1 patch 是**8 轮系统性数学工具调研**的产出:

```
8 轮调研过程
═══════════════════════════════════════════════════

Round 1: Hawkes 自激点过程         → 3 个 patch
Round 2: Transfer Entropy           → 2 个 patch
Round 3: Kelly 公式                 → 2 个 patch
Round 4: Bayesian HMM               → 2 个 patch
Round 5: Wavelet 多尺度分解          → 2 个 patch
Round 6: Permutation Entropy        → 2 个 patch
Round 7: Random Matrix Theory       → 0 个 patch(确认无单一资产应用)
Round 8: Topological Data Analysis  → 3 个 patch

═══════════════════════════════════════════════════
总计:14 个 v2.1 patch + 1 轮无产出确认(R7)
```

每一轮调研都基于:
- 数学定义清晰化
- BTC 实证文献扫描(规则 #13)
- 在 sel v2.0 中的具体落地点评估(规则 #14)
- 真实成本评估
- 不确定性显式标注

## 实施工程的范围(本次确认)

**本文档发布后立即可开始的工程**:
- v2.0 主体 + v2.1 patch 的 CC 工程实施
- 与 Helixa 实盘准备**并行**进行

**仍受门槛约束**:
- Selene paper trading 启动:等 Helixa 实盘稳定 ≥ 3 月

**不在本文档范围**:
- v2.2+ 工具(包括加入 8 轮未调研的工具:微观结构 / 链上深度 / 期权 / 行为金融 / 网络分析等)
- 这些是**长期演进方向**,paper 数据反馈后评估

---

# 第一部分:整体框架与原则

## 1. v2.1 patch 全景

### 1.1 14 个 patch 的层级分类

按 sel 架构层级(规则 #14):

```
Layer 1:数据 / 离线分析(paper 启动前必做)
═══════════════════════════════════════════════════
  T1  - Transfer Entropy 因果地图
  W1  - Wavelet 多尺度离线分析
  TDA1(离线部分)- TDA 历史 cascade 签名分析

Layer 2:状态机增强(实时但非主决策)
═══════════════════════════════════════════════════
  H2  - Hawkes branching ratio → Critical 主条件(新)
  TDA1(实时部分)- TDA L^1 范数 → Critical 主条件(新)
  H3  - Hawkes Cascade 早期预警(observation-only)
  B1  - Bayesian HMM 软验证层(observation-only)
  B2  - HMM 边界仲裁(Drifting-Calm/Charged)
  TDA2 - TDA + clustering 状态识别(observation-only)
  T2  - TE 滚动监控(observation-only)
  W2  - Wavelet 多分形谱宽 → Critical 观察(observation-only)
  I1  - Permutation Entropy 趋势预警(observation-only)

Layer 3:策略入场决策(主决策路径)
═══════════════════════════════════════════════════
  H1  - Hawkes 强度门槛(策略 2 前置筛选)

Layer 4:仓位决策
═══════════════════════════════════════════════════
  K1  - 分阶段动态 Kelly(策略 1+2)

Layer 5:推迟到 v2.2
═══════════════════════════════════════════════════
  K2  - 状态条件 Kelly
  TDA3 - Topological Persistence Norm 仓位
  I2  - PE 增强 Coiling 进入条件(条件性)

═══════════════════════════════════════════════════
```

### 1.2 价值与成本汇总

| Patch | 工具 | 价值 | 实施工作量 | 角色 |
|---|---|---|---|---|
| H1 | Hawkes | 高 | 1-2 Wave | 策略 2 入场过滤(主决策) |
| H2 | Hawkes | 高 | 0.5-1 Wave | Critical 主条件(新) |
| H3 | Hawkes | 中 | 1-2 Wave | Cascade 预警(observation) |
| T1 | TE | 中-高 | 0.5-1 Wave | 因果地图(离线) |
| T2 | TE | 中 | 1 Wave | 状态特征(observation) |
| K1 | Kelly | 高 | 1-2 Wave | 仓位分阶段(主决策) |
| B1 | HMM | 中 | 1.5-2 Wave | 软验证(observation) |
| B2 | HMM | 中 | 0.5-1 Wave | 边界仲裁 |
| W1 | Wavelet | 中 | 0.5-1 Wave | 多尺度分析(离线) |
| W2 | Wavelet | 低 | 1 Wave | Critical 辅助(observation) |
| I1 | PE | 低 | 0.5 Wave | 趋势预警(observation) |
| TDA1 | TDA | 高 | 2-3 Wave | Critical 主条件(新) + 离线 |
| TDA2 | TDA | 中 | 1-2 Wave | 状态识别(observation) |

**总实施工作量估计**:8-13 Wave。

### 1.3 估计的累积匹配度提升

按规则 #13 显式标注不确定性:

```
累积匹配度估计(去重叠后,paper 验证前)
═══════════════════════════════════════════════════
  v2.0 baseline: 75%
  
  + R1 Hawkes:        +3-5%(信号增强 + Critical)
  + R2 TE:             +2-3%(等价,设计验证)
  + R3 Kelly:          +2-3%(仓位纪律)
  + R4 HMM:            +2-4%(等价,软验证)
  + R5 Wavelet:        +1-2%(等价,离线分析)
  + R6 PE:             +1%(等价,趋势预警)
  + R7 RMT:            0%(无产出)
  + R8 TDA:            +2-4%(危机几何识别)
  
  简单相加:86-97%
  去重叠后估计:82-87%
  
  实际 paper 验证可能:
    悲观:75-80%(部分工具失效)
    预期:80-85%
    乐观:85-90%

═══════════════════════════════════════════════════
```

⚠️ **诚实标注**:
- 这些数字**没有真实数据基础**
- paper trading 前**全部是假设**
- 不能作为承诺 / 决策依据
- 真实匹配度只能由 paper 数据验证

## 2. 核心实施原则

### 2.1 Critical 状态条件择优(关键)

按规则 #11 不堆砌,**v2.1 Critical 状态判定的择优规则**:

```
v2.1 Critical 进入条件(择优后)
═══════════════════════════════════════════════════

主条件(继承 v2.0 + v1.1 修订):
  - σ 进入 90 分位 + 12h 单调上升
  - 熵变化率方差上升

新增主条件(v2.1):
  - Hawkes branching ratio > 0.85(R1 - H2)
  - TDA L^1 范数 > 95 分位 + 12h 单调上升(R8 - TDA1 实时)

辅助(log-only):
  - AR1 上升(继承 v1.1 修订)
  - Wavelet 多分形谱宽 > 90 分位(R5 - W2,observation-only)
  - PE 复杂度异常(R6,observation-only)
  - HMM 后验显示 Critical(R4 - B1,observation-only)

═══════════════════════════════════════════════════
进入逻辑:
  v2.0 主条件 完全满足  ✓
  + 新增主条件 任意 1 个 满足  ✓
  → Critical 进入

或者:
  v2.0 主条件 部分满足
  + 新增主条件 全部 2 个 满足
  → Critical 进入

避免:
  - 任一单工具假阳性 → 错误 Critical
  - 多工具堆砌 → 不可解释
═══════════════════════════════════════════════════
```

⚠️ **observation-only 工具不参与 Critical 决策**,只在数据库记录,3 月评估。

### 2.2 observation-only 工具评估纪律

按规则 #11 + ADR-038 "诚实到不舒服":

```
observation-only 工具的命运
═══════════════════════════════════════════════════

paper Month 0-3:
  - 全部 observation-only 工具运行
  - 数据写入 inverse_vocab_events / decision_trail
  - 不参与决策

paper Month 3 评估:
  - 每个 observation 工具生成评估报告
  - 评估指标:
    * lead time(领先 sel 主决策多久)
    * 假阳性率(信号触发但未发生事件)
    * 与现有工具的相关性
  - 三种命运:
    A. 数据强支持 → v2.2 升级为决策因子
    B. 数据弱支持 → 维持 observation-only 至 Month 6
    C. 数据明显无效 → 放弃,从代码中移除

paper Month 6 评估:
  - 仍在 observation 的工具二次评估
  - 仍弱支持 → 放弃
  - 强支持 → v2.2 升级

═══════════════════════════════════════════════════
```

⚠️ **明确放弃的纪律**:工具失败不是 sel 的失败,**反复看不到效果还不放弃才是 sel 的失败**。这是规则 #11 + 公理 5 的硬纪律。

### 2.3 工具间功能重叠的诚实标注

按规则 #11 + #14:

```
v2.1 工具的重叠分析
═══════════════════════════════════════════════════

测量"市场内生性 / 复杂度"的工具重叠:
  - Hawkes branching ratio    (微观,事件聚集)
  - TDA L^1 范数              (中观,几何形状)
  - Wavelet 多分形谱宽         (多尺度,能量分布)
  - Permutation Entropy 复杂度 (序列层面)
  - Bayesian HMM 后验          (分布层面)

虽测量维度不同,但都对应"系统接近临界":
  - paper 期间需统计相关性
  - 高相关 → 保留独立性强的(可能保留 Hawkes + TDA)
  - 其他降级或放弃

测量"因果 / 信息流"的工具重叠:
  - Transfer Entropy(R2)
  - Mutual Information(被 TE 覆盖,放弃)
  - Hawkes 多变量(局部因果)

测量"状态识别"的工具重叠:
  - sel 硬规则状态机(主)
  - Bayesian HMM 软验证(R4)
  - TDA + clustering(R8)
  - 三者形成"状态识别共识层"

═══════════════════════════════════════════════════
```

⚠️ **paper 6 月评估时的硬纪律**:
- 多个工具在 Critical 判定上分歧 > 50% → 触发 review
- 多个工具高度相关(> 0.7) → 必须合并或择一
- 不允许"工具越多越好"思维

## 3. 评估节点机制(3 / 6 / 12 / 24 月)

按规则 #11(替代"1-2 年再说"的消极延期),v2.1 引入**精确的评估节点**:

### 3.1 Month 0-1:观察期

```
活动:
  - K1 Phase 0(固定 base_size)
  - 全部 v2.1 patch 运行
  - observation-only 工具记录数据
  - 不调参
  - 每周报告但不调整

输出:
  - 第一批 trade 数据
  - 工具运行的工程稳定性确认
  - 异常事件日志

禁止:
  - 调整任何参数
  - 升级 / 放弃任何工具
  - 提前进入 Phase 1
```

### 3.2 Month 1-3:数据收集期

```
活动:
  - K1 Phase 1(收集 W/R 数据,不用)
  - observation 工具继续运行
  - 每月最多调一次参数(v2.0 第 28 节)

输出:
  - 30+ 笔交易数据(目标)
  - W/R 初步估计
  - 工具间相关性矩阵(初步)

允许:
  - 严重 bug 修复(不算调参)
  - paper 工程稳定性优化
```

### 3.3 Month 3:第一次系统评估(关键)

```
评估清单:
═══════════════════════════════════════════════════

(1) observation-only 工具评估:
   - B1 / T2 / H3 / TDA2 / I1 / W2
   - lead time + 假阳性率 + 相关性
   - 命运:升级 / observation 维持 / 放弃

(2) Critical 主条件评估:
   - σ + 熵(v2.0)/ Hawkes branching / TDA L^1
   - 4 个主条件的实际触发情况
   - 是否需要权重调整(不是放弃,只是优先级)

(3) 策略 2 入场质量:
   - H1 Hawkes 强度门槛是否真在过滤假信号
   - 触发率 + 胜率 + lead time

(4) Kelly 准备:
   - W/R 估计的稳定性
   - 进入 Phase 2 的可行性判断

(5) 健康分布:
   - 状态触发率 vs 占位符目标
   - 严重偏离 → 调整

═══════════════════════════════════════════════════

输出文档:
  ~/projects/selene/reports/3month_review_v1.md
  
决策权:
  - Wiki 拍板(决策)
  - Claude 提供数据 + 建议
  - 写入 decision_trail
```

### 3.4 Month 3-6:第一次迭代期

```
活动:
  - K1 Phase 2(启用 quarter Kelly,cap [5%, 25%])
  - 升级的工具进入决策
  - 放弃的工具从代码移除
  - 每月最多调一次参数

允许的调整:
  - CUSUM 阈值分位数
  - 状态进入/离开分位数
  - 反推词汇增强系数
  - Kelly fraction cap 范围
  - Hawkes branching ratio 阈值
  - TDA L^1 范数阈值

禁止的调整(架构层冻结):
  - 4H 主时间锚点
  - 双策略架构
  - 80/20 资金分配
  - 状态数量(6)
  - 风控红线
```

### 3.5 Month 6:第二次系统评估(关键)

```
评估清单:
═══════════════════════════════════════════════════

(1) 累积财务指标:
   - 累计 PnL / Sharpe / Sortino / 最大回撤
   - 与 Helixa 对照(同期)

(2) 架构层面 review:
   - 双策略架构是否有效?
   - 80/20 分配是否合理?
   - 协调层(同向独立 / 反向独立)是否产生过多对冲?

(3) Critical 系统评估:
   - 4 个主条件的真实贡献
   - 至少 1 个 Cascade 事件中的表现(如果发生)

(4) 自毁开关检查:
   - 连续 3 次调参无改善? → 触发停摆评估
   - sel 整体方向是否需要重大调整?

(5) v2.2 候选讨论:
   - 8 轮未调研的工具(微观结构 / 链上 / 期权)
   - 是否有 paper 数据强烈指向某个新方向?

(6) 实盘启动评估:
   - 数据强支持 → 进入实盘启动准备
   - 数据弱支持 → 继续 paper

═══════════════════════════════════════════════════

输出文档:
  ~/projects/selene/reports/6month_review_v1.md
  
这是 sel 的第一个"小突破"窗口
```

### 3.6 Month 6-12:K1 Phase 3 + 实盘评估

```
活动:
  - K1 Phase 3(cap 调整 [3%, 30%])
  - 实盘启动评估(如果 6 月数据强)
  - 12 个月数据资产形成

实盘启动条件(必须全部满足):
  - 6 月评估数据强支持
  - Helixa 实盘稳定 ≥ 3 月已达成
  - 无系统性 bug 累积
  - Wiki + Claude 双重 review 通过
  - 写入 decision_trail
```

### 3.7 Month 12:第三次系统评估(中突破窗口)

```
评估清单:
═══════════════════════════════════════════════════

(1) 1 年完整 paper 数据:
   - 12 个月 PnL 分布
   - 包含 BTC 多个 regime 的表现
   - 至少 5+ Cascade 事件样本

(2) 长期数据资产:
   - decision_trail 完整 12 个月
   - 60+ 笔交易(目标)
   - 工具有效性的统计显著性检验

(3) 中突破方向:
   - paper 数据反向归纳新工具(2D 选项)
   - 是否需要 v3.0 架构升级讨论
   - v2.2+ 的具体方向

(4) Helios 平台层面:
   - sel 在 Helios 平台的角色
   - 与 Helixa / Tide 的协同

═══════════════════════════════════════════════════
```

### 3.8 Month 24:长期评估(真突破窗口)

```
24 个月是真正考虑根本性突破的节点:
  - 跨 BTC 多个完整 cycle 的数据
  - sel 框架在长周期上的真实表现
  - 与 Helixa 长期对照(架构差异 vs 实施差异)
  - v6 候选评估(全资产 / 多账户 / 学习层)
  - 是否需要发明全新工具(2A 选项,谨慎评估)
```

⚠️ **关键纪律**(替代"1-2 年再说"):

```
"突破"的真实定义
═══════════════════════════════════════════════════

3 月节点 = 工具迭代(日常,不叫突破)
6 月节点 = 架构 review + 小突破
12 月节点 = 数据驱动的中突破
24 月节点 = 根本性方向,真突破

不叫"突破"的:
  - 调参数
  - 升级/放弃 observation 工具
  - paper 期间的常规优化

叫"突破"的:
  - 加入 8 轮未调研的全新工具维度(2B)
  - 基于 paper 数据反向归纳(2D)
  - 架构层面的范式转换
═══════════════════════════════════════════════════
```

## 4. 过度自信防御机制

按规则 #11 + 本对话已暴露的认知偏移风险,v2.1 显式增加**过度自信防御**段落。

### 4.1 警告 1:工具数量 ≠ 优势

```
错误心态:
  "sel 有 8 个数学工具,站在传统指标用户头顶"

正确认知:
═══════════════════════════════════════════════════
  - 8 个工具中有 6 个有功能重叠
  - paper 期间需要择优,不是堆砌
  - Renaissance Technologies 自己说"discarded 99%+ 信号"
  - 工具堆砌是失败者特征,不是赢家
  - sel 真实价值 = 工程 + 数据 + 内容 + 时间
  - **不是工具数量**

防御机制:
  - 6 月评估强制 review 工具间相关性
  - 高相关工具(> 0.7)必须合并或择一
  - 不允许"工具越多越好"思维进入决策
═══════════════════════════════════════════════════
```

### 4.2 警告 2:对手定位错误

```
错误心态:
  "我超越用 MA / MACD / ICT 的散户"

正确认知:
═══════════════════════════════════════════════════
  - 散户不是 sel 的真实对手
  - sel 服务于 Wiki 自己 + 后续用户
  - 不是与人比较,是工艺自我评估
  - 真正的"对手"是市场效率和不确定性

按记忆:
  - "BTC 市场 alpha = 零护城河,打不过 Jane Street 高频"
  - "真实护城河 = 工程 + 数据 + 内容 + 时间"
  - sel 是个人量化的"工艺水平",不是"超越机构"

防御机制:
  - 任何"超越 X"措辞 → 立即纠正为"工艺自我评估"
  - 报告中禁止"领先" / "超越" / "更强" 等比较性语言
  - 替代:"系统化" / "可证伪" / "诚实"
═══════════════════════════════════════════════════
```

### 4.3 警告 3:已穷尽错觉

```
错误心态:
  "8 轮调研已经覆盖所有重要工具"

正确认知:
═══════════════════════════════════════════════════
  - 8 轮只覆盖数学工具的一个切片
  - 未覆盖的领域:
    * 微观结构理论(Kyle / O'Hara / Hasbrouck)
    * 行为金融(prospect / herding / 情绪)
    * 链上数据深度(Glassnode / cohort 分析)
    * 网络分析(交易所 / 钱包网络)
    * 机器学习(transformer / RL)
    * 期权理论(volatility surface / dispersion)
    * 衍生品微观结构
    * 跨市场套利

防御机制:
  - 6/12 月评估强制提"未调研维度"
  - 不允许"sel 工具栈完成"的自满
  - paper 数据指向未调研维度时,必须严肃考虑
═══════════════════════════════════════════════════
```

### 4.4 警告 4:过度延期

```
错误心态:
  "测试 1-2 年再考虑突破"

正确认知:
═══════════════════════════════════════════════════
  - 这是消极延期,不是纪律
  - paper 3 月就有重要评估节点
  - 延期 = 错过 observation 工具的失败信号
  - 反复看不到效果还不调整 = sel 失败

防御机制:
  - 替代为 3/6/12/24 月精确节点
  - 每个节点都有强制评估清单
  - "等等再说" 不是合法回答
═══════════════════════════════════════════════════
```

### 4.5 警告 5:Kelly 仓位心理风险

```
错误心态:
  "Kelly 公式让我可以 full Kelly"

正确认知:
═══════════════════════════════════════════════════
  - Kelly 文献:full Kelly 50%+ 回撤是常态
  - 专业交易者**几乎都用 quarter Kelly(25%)**
  - 估计误差 10% → 仓位翻倍

防御机制:
  - K1 严格分阶段(0-1月固定 / 1-3月收集 / 3-6月quarter / 6+月稳定)
  - cap 永远不超过 30%
  - "我有边际,可以加仓位"思维 → 立即触发 review
═══════════════════════════════════════════════════
```

### 4.6 警告 6:实盘启动的诱惑

```
错误心态:
  "v2.1 完成,可以提前实盘"

正确认知:
═══════════════════════════════════════════════════
  - 实盘启动门槛(已记录):
    * Helixa 实盘稳定 ≥ 3 月
    * Selene paper 6 月评估强支持
    * 两者必须同时满足
  - "v2.1 文档完成"不是实盘启动条件
  - "我感觉准备好了"不是实盘启动条件

防御机制:
  - decision_trail 必须显式记录"实盘启动评估"
  - Wiki + Claude 双重 review
  - 任何缩短门槛的提议 → 严肃质疑
═══════════════════════════════════════════════════
```

⚠️ **过度自信防御的元原则**:

按规则 #25(首席顾问):
- 当 Wiki 出现"站在头顶" / "超越" / "已穷尽" / "提前实盘" 等表达
- Claude **必须立即拦截**,不软化
- 这是规则 #11 的硬执行


---

# 第二部分:14 个 v2.1 Patch 完整内容

## 5. Round 1 — Hawkes 自激点过程(3 个 patch)

### 5.1 Patch H1:Hawkes 强度门槛(策略 2 入场前置筛选)

**作用层级**:Layer 3(策略入场决策,主决策路径)

**目的**:
- 策略 2 当前用 CUSUM-Short 触发
- 加入 Hawkes 强度门槛作为**前置筛选**
- 只在"trade clustering 真实存在"时才相信 CUSUM 信号

**修改位置**:v2.0 第 14.2 节(策略 2 入场决策树)

**原 v2.0 决策树**:
```
Step 1: CUSUM-Short 触发判定
  - C+_t > h_t 或 |C-_t| > h_t
Step 2: 反推词汇增强
Step 3: 反向冲突检查
Step 4: 入场
```

**新 v2.1 决策树**:
```
Step 1a: CUSUM-Short 触发判定(同 v2.0)
  - C+_t > h_t 或 |C-_t| > h_t

Step 1b: Hawkes 强度门槛(新增)
  - 计算同期 Hawkes 强度 λ*(t)
  - λ*(t) > h_λ → 通过门槛
  - λ*(t) ≤ h_λ → 降级为 observation-only(记录但不入场)

Step 2: 反推词汇增强(同 v2.0)
Step 3: 反向冲突检查(同 v2.0)
Step 4: 入场(同 v2.0)
```

**Hawkes 参数**:
- 强度估计方法:GMM(Fonseca-Zaatour),实时计算 < 5 秒
- kernel:exponential(α exp(-β t))
- 阈值 h_λ:7 天滚动 70 分位(占位符,paper 标定)
- 计算窗口:同 CUSUM-Short(秒-分钟级)

**实施工作量**:1-2 个 CC Wave
- 实施 GMM 快速校准:约 200 行 Python
- 集成到策略 2 决策树:约 50 行
- 单元测试 + 集成测试:约 100 行

**预期效果**:
- 假信号减少(CUSUM 因单一异常 tick 触发的情况)
- 策略 2 信噪比提升
- 估计匹配度提升:+3-5%

**评估机制**:
- Month 3 评估:
  - 如果 H1 启用前后,策略 2 胜率提升 < 2% → 重新评估阈值
  - 如果阈值 h_λ 标定一直不稳定 → 考虑放弃
- Month 6 评估:
  - 累积胜率改善 < 3% → 阈值固定为最优值
  - 累积胜率改善 ≥ 5% → 验证成功

**不确定性标注**(规则 #13):
- BTC OKX 上 Hawkes kernel 的最优形式未实证(指数 vs 幂律 vs sum-of-3-exp)
- 70 分位阈值是参考值,不是 BTC 实证标定
- GMM vs MLE 校准在 sel 实时场景下的精度差异未知

---

### 5.2 Patch H2:Hawkes branching ratio → Critical 主条件

**作用层级**:Layer 2(状态机增强,新主条件)

**目的**:
- v2.0 Critical 进入条件依赖 σ + 熵
- 加入 Hawkes branching ratio 作为**新主条件**
- 直接量化"市场内生反馈强度"

**修改位置**:v2.0 第 5.6 节(Critical 状态)

**新 Critical 进入条件**(整合后):
```
Critical 进入条件(v2.1)
═══════════════════════════════════════════════════

主条件 A(继承 v2.0):
  - σ 进入 90 分位 + 12h 单调上升
  - 熵变化率方差上升

主条件 B(新增 - H2):
  - Hawkes branching ratio = ||h||_1 = α/β > 0.85

主条件 C(新增 - TDA1):
  - TDA L^1 范数 > 95 分位 + 12h 单调上升
  
进入逻辑:
  A 完全满足 + (B 或 C 任一满足) → Critical 进入
  或
  A 部分满足 + B 和 C 都满足 → Critical 进入

辅助(log-only):
  - AR1 上升(继承 v1.1)
  - W2/B1/I1 等 observation 工具

═══════════════════════════════════════════════════
```

**Hawkes branching ratio 数学**:
- 单变量 Hawkes:branching ratio = ∫₀^∞ h(t) dt = α/β(指数核)
- 经济意义:**一个事件平均触发多少后续事件**
- > 0.85:接近 1(near-unstable subcritical)
- > 1:explosive(数学上不允许,物理上 cascade)

**阈值 0.85 来源**:
- arxiv 2510.08085(2025-10):BTC LOB 数据上 near-unstable subcritical regime 是必要的
- Filimonov-Sornette:0.85 是经验阈值
- 但 BTC 4H 上的最优阈值需 paper 标定

**实施工作量**:0.5-1 个 CC Wave
- 滚动 Hawkes MLE 校准(用 tick 数据):约 200 行
- 集成到 Critical 判定逻辑:约 50 行

**评估机制**:
- Month 3 评估:branching ratio 阈值是否能区分真假 Critical
- Month 6 评估:与 σ-based 主条件的一致率
  - 高一致(> 80%)→ 工具冗余,简化保留一个
  - 中一致(40-80%)→ 互补,两者保留
  - 低一致(< 40%)→ 严肃 review

**不确定性标注**:
- 0.85 阈值在 BTC 4H 上未实证
- 估计窗口长度(滚动 30 天?60 天?)需 paper 验证

---

### 5.3 Patch H3:Hawkes 多变量 Cascade 早期预警(observation-only)

**作用层级**:Layer 2(observation-only)

**目的**:
- 多变量 Hawkes 在 LOB 事件(buy market / sell market / liquidation)上跑
- 当 λ_liquidation*(t) 跃升 + buy/sell 失衡 → Cascade 预警
- 在 sel 现有 Cascade 触发前提供早期信号

**修改位置**:v2.0 新增"Cascade 早期监测"模块

**实施细节**:
```
多变量 Hawkes(三维)
═══════════════════════════════════════════════════
事件流:
  N_buy(t)         市场买单到达
  N_sell(t)        市场卖单到达
  N_liq(t)         强平事件到达

强度函数:
  λ_buy*(t) = μ_buy + Σ_j ∫ h_buy_j(t-s) dN_j(s)
  λ_sell*(t) = ...
  λ_liq*(t) = ...

Cascade 早期预警条件:
  λ_liq*(t) > 95 分位(7 天滚动)
  + λ_buy*(t) / λ_sell*(t) 严重失衡(> 5 或 < 0.2)
  + 两者持续 > 30 秒
═══════════════════════════════════════════════════
```

**作用方式**:
- 触发 → 写入 inverse_vocab_events 表
- **不直接干预决策**(observation-only)
- Month 3 评估:与 sel 现有 Cascade 触发的 lead time

**实施工作量**:1-2 个 CC Wave
- 多变量 Hawkes 实时估计:约 500 行
- 多事件流提取:约 200 行
- 数据库记录:约 50 行

**评估机制**:
- Month 3:lead time 平均值
  - lead time > 60 秒 → 工具有真实价值,v2.2 升级为决策因子
  - lead time < 30 秒 → 价值边际,维持 observation
  - 假阳性率 > 50% → 放弃
- Month 6:Cascade 事件样本(估计 5-15 次)
  - 统计显著性检验

**不确定性标注**:
- 多变量 Hawkes 的 cross-excitation kernel 标定复杂
- BTC liquidation 数据的事件密度与传统市场不同
- "5 倍失衡"阈值是占位符

---

## 6. Round 2 — Transfer Entropy(2 个 patch)

### 6.1 Patch T1:Transfer Entropy 因果地图(离线工具)

**作用层级**:Layer 1(数据 / 离线分析)

**目的**:
- 验证 sel 设计中"X 前置 / Y 后置"是否符合实际因果方向
- 解决 v2.0 公认盲区 2(多源数据因果不明)
- 不进入实时引擎

**修改位置**:v2.0 第 19 节(实证方法论)增加 19.4 节

**实施工作流**:
```
TE 因果地图工作流
═══════════════════════════════════════════════════

paper 启动前必做:

(1) 数据收集
   - 过去 6 个月 BTC 小时级数据
   - 提取 sel v2.0 用到的所有信号:
     * 价格 returns
     * OFI(15min 聚合)
     * OI 变化率
     * funding rate
     * liquidation 数据
     * 链上大额提现/充值
     * 跨所价差

(2) STE 估计
   - 用 Symbolic Transfer Entropy(STE)
   - 优势:样本量需求小于直接 TE
   - 库:idtxl / pyinform
   - 双向 TE:每两个信号都计算 X→Y 和 Y→X

(3) 显著性检验
   - Surrogate test(打乱时序计算 null 分布)
   - p < 0.05 显著
   - 不显著的边 → 不画

(4) 输出
   - TE 矩阵(对角空,off-diagonal 为 STE 值)
   - 因果地图(有向图)
   - 写入 ~/projects/selene/analysis/causal_map_v1.md

═══════════════════════════════════════════════════
```

**用途**:
- 验证 sel 设计中信号的"前置/后置"位置
- 例:如果 TE(price → funding) > TE(funding → price)
  → funding 作为"前置 Crowding 信号"的设计需要重新评估
- 例:如果某信号 TE ≈ 0(在所有方向上),说明无信息贡献
  → sel 可以省略该信号

**实施工作量**:0.5-1 个 CC Wave
- STE 估计器:约 200 行(用开源库,不从零写)
- 离线分析脚本:约 100 行
- 可视化(networkx + matplotlib):约 100 行

**评估机制**:
- 一次性输出,不需要持续评估
- 但 6 月可重跑(post-paper-data)对比是否变化
- 因果关系不稳定 → sel 设计需要适应 regime

**不确定性标注**:
- BTC OKX 数据上 STE 的最优 lag 未实证
- 6 个月数据(hourly = 4380 bar)是否够 STE 估计的边缘
- post-ETF 后 BTC 因果可能不稳定

---

### 6.2 Patch T2:TE 滚动监控(observation-only)

**作用层级**:Layer 2(observation-only)

**目的**:
- 4H 尺度的 TE 滚动估计
- 监控关键因果关系的时变性
- 写入数据库,observation-only

**实施细节**:
```
TE 滚动监控
═══════════════════════════════════════════════════

监控的因果关系:
  - TE(OFI → price)
  - TE(funding → price)
  - TE(链上 → price)
  - TE(跨所价差 → price)

滚动窗口:
  - 30 天 4H 数据(180 bar)
  - 边缘但可计算

更新频率:
  - 每周一次(不需实时)

写入:
  - inverse_vocab_events 表
  - 字段:vocab='TE_X_to_Y', intensity=TE 值

═══════════════════════════════════════════════════
```

**评估机制**:
- Month 3:
  - TE 显著变化是否预示状态转换
  - 与 sel 状态机的 lead/lag
- Month 6:
  - 是否纳入决策因子
  - 通常不纳入(TE 的最大价值是离线分析)

**实施工作量**:1 个 CC Wave
- 滚动 TE 计算:约 300 行
- 数据库记录:约 50 行

**不确定性标注**:
- 4H 尺度 + 30 天窗口 = 180 bar,STE 估计边缘
- 这个 patch 价值不高,可能 6 月评估时放弃

---

## 7. Round 3 — Kelly 公式(2 个 patch)

### 7.1 Patch K1:动态 Kelly base_size(策略 1 + 策略 2)

**作用层级**:Layer 4(仓位决策,主决策)

**目的**:
- v2.0 当前仓位是固定 base_size(策略 1 = 20% / 策略 2 = 10%)
- 引入分阶段动态 Kelly,但严格保守化
- 避免主观仓位决策

**修改位置**:v2.0 第 13.4 节(策略 1)+ 14.4 节(策略 2)

**核心 Kelly 公式**:
```
连续收益版本(适用于 sel):
  f* = μ / σ²
     = SR / σ
     
其中:
  μ = 超额收益均值(W·R - (1-W) 等价)
  σ² = 收益方差
  SR = Sharpe ratio

历史 trade 版本(更直接):
  f* = (W·R - (1-W)) / R
  
其中:
  W = 胜率
  R = 平均盈利 / 平均亏损
```

**严格分阶段实施**(关键):
```
═══════════════════════════════════════════════════

Phase 0(paper 启动 0-1 月):
  - base_size 完全固定(继承 v2.0)
  - 策略 1 base_size = 20% 子账户
  - 策略 2 base_size = 10% 子账户
  - 不启用 Kelly(无历史数据)
  - trades 表完整记录每笔的 PnL/胜负/盈亏比

Phase 1(paper 1-3 月):
  - base_size 仍固定
  - 每周报告中计算并展示 rolling Kelly fraction(诊断用)
  - 不进入决策
  - 目的:观察 Kelly 估计的稳定性

Phase 2(paper 3-6 月,启用 fractional Kelly):
  - 计算 rolling 60 天的 W 和 R(策略 1)/ 30 天(策略 2)
  - f_kelly = (W·R - (1-W)) / R
  - **base_size = max(5%, min(25%, 0.25 × f_kelly × 子账户余额))**
    - 0.25 = quarter Kelly(保守化)
    - cap [5%, 25%]:防止 Kelly 公式输出极端值
  - 策略 1 和策略 2 各自独立计算

Phase 3(paper 6 月以后,稳定):
  - 同 Phase 2,但 cap 范围调整为 [3%, 30%]
  - 增加状态条件(K2,推迟到 v2.2)

═══════════════════════════════════════════════════
```

**估计纪律**:
```
W 和 R 的估计:
  - 滚动窗口 = 60 天(策略 1)/ 30 天(策略 2)
  - 最少样本 = 30 笔交易,否则不切换 Phase
  - 排除被 Cascade 强制清仓的交易(避免污染统计)

数据来源:
  - 严格用 trades 表的 closed positions
  - 不包括 paper 内的"if ran"模拟交易

Kelly fraction 输出限制:
  - f_kelly < 0(负边际)→ 暂停该策略
  - f_kelly > 1 → cap 到 25% / 30% 上限
```

**Kelly 不替换的风控**:
- 浮亏止损(策略 1 -3% / 策略 2 -2%)
- Cascade 红线
- Critical 减仓 50%
- Time Stop

⚠️ **Kelly 只调整 base_size,其余风控不变**。

**实施工作量**:1-2 个 CC Wave
- W/R 滚动估计:约 100 行
- Kelly fraction 计算:约 50 行
- Phase 划分逻辑:约 100 行
- 集成到仓位计算:约 50 行
- 安全边界(cap / 负边际暂停):约 50 行

**评估机制**:
- Month 1 末:Phase 0 → 1 切换条件检查
- Month 3:Phase 1 → 2 切换决策
  - 30+ 笔交易样本是否达成
  - W/R 估计稳定性
  - 切换前必须 Wiki + Claude 双重 review
- Month 6:Phase 2 → 3 切换决策
  - Phase 2 期间是否有改善
  - 改善显著 → 切 Phase 3
  - 改善不显著 → 维持 Phase 2 或回退 Phase 0

**不确定性标注**:
- BTC 4H 趋势策略下 Kelly fraction 真实数值范围未知
- post-ETF 后 W/R 分布是否稳定
- 策略 1 + 2 在 cascade 下的相关性放大效应未量化

⚠️ **核心警告**:
- Kelly 是"放大器",不是"信号生成器"
- 如果策略本身没有正边际,Kelly 让你**更快亏完**
- Phase 1 诊断阶段的关键作用:**显示负边际 → 直接停掉策略,不要用 Kelly 救**

---

### 7.2 Patch K2:状态条件 Kelly(v2.2 范围)

**作用层级**:Layer 4(仓位决策)

**目的**:
- 不同状态下用不同 Kelly fraction
- 例:Coiling → Surging 时 Kelly 高
- Drifting-Calm → Surging 时 Kelly 低

**推迟原因**:
- 需要 paper 6+ 月数据
- 每个状态需要至少 20 笔交易样本
- 当前样本量不足

**v2.1 不实施**,记录为 v2.2 候选。

**触发条件**:
- paper 6 月评估时,如果各状态样本量充足
- 且 K1 Phase 3 稳定运行
- 则启动 K2 设计

---

## 8. Round 4 — Bayesian HMM(2 个 patch)

### 8.1 Patch B1:Bayesian HMM 软验证层(observation-only)

**作用层级**:Layer 2(observation-only)

**目的**:
- sel 硬规则状态机继续主决策
- 后台并行跑 Bayesian HMM
- HMM 后验与 sel 状态分歧时触发 review
- 类似"第二意见"

**修改位置**:v2.0 新增"HMM 软验证"模块

**实施工作流**:
```
═══════════════════════════════════════════════════

离线训练阶段(paper 启动前):
  1. 收集 2-3 年 BTC 4H 数据
  2. 训练 Bayesian HMM(状态数 = 6,与 sel 对齐)
     - 输入特征:return / σ / 熵 / OI 变化率
     - Bayesian MCMC 推断(用 pymc 或 numpyro)
  3. 训练状态映射函数(HMM 状态 → sel 6 状态)
     - 基于状态特征均值的相似度
  4. 验证状态映射在历史数据上的一致性

在线推断阶段(paper 期间):
  1. 每 4H bar 收盘,计算 HMM 状态后验 γ_t
  2. 取 argmax γ_t = HMM 主状态
  3. 应用映射函数 → sel 状态等价
  4. 与 sel 硬规则状态机的输出对比

告警条件:
  - HMM 后验和 sel 状态完全不同 → 高优先级警告
  - HMM 后验最大值 < 0.4(高不确定性)→ 低优先级警告
  - 警告写入 decision_trail 的"建议"字段

不影响主决策:
  - HMM 警告**仅作 observation**
  - sel 主决策仍按硬规则
═══════════════════════════════════════════════════
```

**实施工作量**:1.5-2 个 CC Wave
- Bayesian HMM 训练(用 pymc / hmmlearn):约 200 行
- 状态映射:约 100 行
- 分歧检测 + 告警:约 50 行

**评估机制**:
- Month 3:
  - HMM 与 sel 一致率
  - 分歧时哪方更准(回看后续 24h 实际行为)
- Month 6:
  - 累积分歧数据
  - HMM 是否在某些 regime 下更准
  - 决定:升级 / 维持 / 放弃

**不确定性标注**:
- BTC 4H 上 HMM 训练的最优状态数未必是 6(BIC 可能不支持)
- 状态映射函数的"无标签"问题(实证看映射准确率)
- post-ETF 数据上 HMM 训练的稳定性
- 训练数据 = 2024-01 之后(只用 ETF 时代),约 2 年,可能不够

⚠️ **post-ETF 警告**(继承 v2.0):
- HMM 训练在 2024+ 数据上可能与 2024- 数据不一致
- 建议训练数据 = 2024-01 之后
- 但这意味着 HMM 训练数据约 2 年,可能不足

---

### 8.2 Patch B2:HMM 边界仲裁(Drifting-Calm/Charged)

**作用层级**:Layer 2(状态机增强)

**目的**:
- 解决 v1.0 已知缺陷 2:Drifting-Calm/Charged 边界模糊
- 在 sel 硬规则在两个状态间频繁抖动时
- 用 HMM 后验仲裁

**修改位置**:v2.0 第 5.4 节(Drifting-Calm)+ 5.5 节(Drifting-Charged)

**判定逻辑**:
```
═══════════════════════════════════════════════════

边界状态触发条件:
  当 sel 硬规则在最近 6 个 4H 中
  出现 ≥ 3 次 Drifting-Calm ↔ Drifting-Charged 切换:
    1. 视为"边界状态"(boundary state)
    2. 调用 HMM 后验
    3. 取 P(Calm) vs P(Charged) 多者为准
    4. 持续 4 个 4H bar
    5. 解除边界状态后回归 sel 硬规则

═══════════════════════════════════════════════════
```

**实施工作量**:0.5-1 个 CC Wave(基于 B1)
- 边界检测逻辑:约 100 行
- HMM 仲裁:约 50 行(基于 B1 已有代码)

**评估机制**:
- Month 3:边界状态触发频率
  - 频繁触发(> 月 3 次)→ 工具有用
  - 罕见触发(< 月 1 次)→ Drifting 边界本就清晰,工具冗余

**不确定性标注**:
- HMM 在 Drifting 状态上的样本量足够
- 但 HMM 仲裁的"准确率"需要后验验证

---

## 9. Round 5 — Wavelet 多尺度分解(2 个 patch)

### 9.1 Patch W1:Wavelet 离线多尺度分析(paper 启动前)

**作用层级**:Layer 1(数据 / 离线分析)

**目的**:
- 验证 sel 时间架构选择(4H 主锚点 + CUSUM 双尺度是否覆盖关键尺度)
- 不进入实时引擎(避开边界效应陷阱)
- 类似 T1 因果地图的角色

**修改位置**:v2.0 第 19 节增加 19.5 节

**实施工作流**:
```
═══════════════════════════════════════════════════

paper 启动前必做(与 T1 因果地图同期):

1. 数据准备:
   - BTC 2-3 年历史数据(tick / 1min / 4H)
   - 必须涵盖多个 regime(2024-2025+)

2. DWT 多尺度分解:
   - 母小波:db4(实战常用,信号-边界平衡)
   - 分解层数:6 层(覆盖 4H bar 的多倍尺度)
   - 库:PyWavelets

3. 每个尺度的能量分布:
   - Level 1-6 detail 能量
   - approximation 能量
   - 时间演化曲线

4. 关键问题分析:
   - 哪些尺度对应 BTC 真实 regime 变化?
   - sel 4H 主时间锚点是否在"信噪比最优"尺度?
   - cascade 事件在哪个尺度上最显著?

5. 输出:
   - ~/projects/selene/analysis/wavelet_multiscale_v1.md
   - 包含:能量谱图、时频图、cascade 事件签名

用途:
  - 验证 v2.0 时间架构选择
  - 不进入实时引擎

═══════════════════════════════════════════════════
```

**实施工作量**:0.5-1 个 CC Wave
- DWT 分析脚本:约 200 行
- 可视化:约 100 行

**评估机制**:
- 一次性输出
- 6 月可重跑确认 regime 变化

**不确定性标注**:
- 4H bar 不规则采样(BTC 24/7,但用 4H 锚点)
- DWT 在边界处的处理(多种 padding 方案)
- 不同母小波的结果差异

---

### 9.2 Patch W2:Wavelet 多分形谱宽 → Critical 观察(observation-only)

**作用层级**:Layer 2(observation-only)

**目的**:
- 滚动 30 天 4H 数据上跑 wavelet leaders
- 多分形谱宽度 Δh 作为"市场内生性"度量
- 与 Hawkes branching ratio + TDA L^1 共同观察

**实施细节**:
```
═══════════════════════════════════════════════════

并行(observation-only)条件:
  - 滚动 30 天 4H 数据上跑 wavelet leaders 算法
  - 计算多分形谱宽度 Δh
  - Δh > 历史 90 分位 → 多分形高度内生
  - 写入 inverse_vocab_events 表

═══════════════════════════════════════════════════
```

**实施工作量**:1 个 CC Wave
- Wavelet leaders 算法:约 300 行(复杂)
- 多分形谱估计:约 200 行
- 集成:约 50 行

**评估机制**:
- Month 3 评估:
  - Δh > 90 分位时,后续 24h 进入 Cascade 的频率
  - 与 sel 现有 Critical 条件 + Hawkes/TDA 的相关性
  - 高相关(> 0.7)→ 工具冗余,放弃
  - 中等独立 → 维持 observation
  - 低相关 → 升级为决策因子(但谨慎)

**不确定性标注**:
- BTC 4H 数据上 wavelet leaders 估计的稳定性未验证
- 0.85/0.90 阈值都是占位符
- 计算复杂度高,实时性能可能受限

⚠️ **本 patch 价值评级低**:
- 与 R1 Hawkes branching ratio 高度功能重叠
- 与 R8 TDA L^1 范数也重叠
- paper 6 月评估时大概率放弃

---

## 10. Round 6 — Permutation Entropy(2 个 patch)

### 10.1 Patch I1:PE 趋势早期预警(observation-only)

**作用层级**:Layer 2(observation-only)

**目的**:
- 滚动计算 PE
- PE 突然下降 = 市场进入 non-random 模式 = 趋势可能开始
- 与 sel Coiling/Drifting → Surging 转换对比

**修改位置**:v2.0 第 5.x 节增加 PE 监测

**实施工作流**:
```
═══════════════════════════════════════════════════

PE 趋势预警:
  - 滚动 30 天 4H 数据上计算 PE
  - 嵌入维度 d = 4(BTC 文献常用)
  - 当 PE 在 12h 内下降超过 1 个标准差(序列变 non-random)
  - 视为"趋势可能开始"
  - 写入 inverse_vocab_events 表

不影响主决策。
═══════════════════════════════════════════════════
```

**实施工作量**:0.5 个 CC Wave
- PE 算法(用 antropy 或 entropyhub):约 50 行
- 滚动计算 + 写入:约 100 行

**评估机制**:
- Month 3 评估:
  - PE 下降信号 vs sel Release 转换的领先/滞后
  - PE 下降信号的假阳性率
- 数据强支持 → v2.2 启用为 Release 转换的辅助确认
- 数据弱支持 → 维持 observation 或放弃

**不确定性标注**:
- BTC 4H 数据上 PE 嵌入维度 d 的最优值未验证
- PE 与 LOB 熵(v2.0 已有)的相关性可能很高,导致信号冗余

---

### 10.2 Patch I2:PE 增强 Coiling 进入(条件性,v2.2 范围)

**作用层级**:Layer 2(条件性)

**条件**:I1 在 Month 3 评估中表现强(假阳性率 < 30% + lead time > 24h)

**实施细节**:
```
═══════════════════════════════════════════════════

如果 Patch I1 在 3 月评估中表现强:
  - Coiling 进入条件增加 PE 维度:
    - 原条件:LOB 熵 < 30 分位
    - 新条件:LOB 熵 < 30 分位 OR PE 下降到 30 分位
  - 即:LOB 静默 OR 价格序列开始有结构

═══════════════════════════════════════════════════
```

**v2.1 不实施**,记录为 v2.2 候选。

---

## 11. Round 7 — Random Matrix Theory(无 patch)

### 11.1 调研结论(诚实记录)

按规则 #13,Round 7 RMT 调研结论:

```
═══════════════════════════════════════════════════
RMT 在 sel v2.1 中的状态:不纳入

原因:
  1. 工具本质与 sel 架构不匹配
     - RMT 是多资产相关矩阵工具
     - sel 是单一 BTC 资产
     - 80/20 物理隔离子账户已是产品决策
  
  2. 无 BTC 单一资产 RMT 的同行评议文献
     - 所有 BTC RMT 文献都是 89-140 个币种 portfolio
     - 没有"单一资产 RMT"的应用先例
  
  3. 创造性落地点全部失效:
     - LOB 跨档相关:N=40 太小,且各档高度相关
     - 跨所价差矩阵:N 个交易所价差几乎完全相关
     - 多时间尺度收益:N=6 太小,RMT 失效
     - 状态特征矩阵:N=6-10,被 PCA 替代
  
  4. 强行纳入 = 违反规则 #13(基于事实,不是直觉)

═══════════════════════════════════════════════════
```

### 11.2 长期记录

**未来扩展时重新评估**:
- 如果 sel 扩展到多资产(v6 候选)
- 触发条件:paper 6+ 月稳定 + 100+ 付费用户 > 60% 要求
- 届时 RMT 重新成为相关工具
- v2.1 不实施

---

## 12. Round 8 — Topological Data Analysis(3 个 patch)

### 12.1 Patch TDA1:Persistence Landscape L^p 范数 → Critical 主条件

**作用层级**:Layer 2(状态机增强,新主条件)+ Layer 1(离线训练)

**目的**:
- 利用 BTC 时间序列的几何结构
- 在相空间重构中识别危机几何
- Persistence Landscape L^1 范数作为 Critical 状态主条件

**修改位置**:v2.0 第 5.6 节(Critical 状态)

**离线训练阶段**(paper 启动前):
```
═══════════════════════════════════════════════════

1. 数据收集
   - 2-3 年 BTC 4H log returns

2. 对每个滑动窗口(W = 50-100 bar):
   a. Takens 嵌入:
      y(t) = (x(t), x(t-τ), x(t-2τ), x(t-3τ))
      - d = 4(嵌入维度)
      - τ = 通过 mutual information 第一极小估计
   b. 构造 Vietoris-Rips 复形
   c. 计算 persistence diagram(用 ripser 或 giotto-tda)
   d. 转换为 persistence landscape
   e. 计算 L^1 范数

3. 输出 L^1 范数时间序列

4. 在历史 cascade 事件上验证:
   - L^1 范数在 cascade 前是否显著上升?
   - 提前多少时间?(目标:数小时到数天)
   - 假阳性率是多少?

5. 标定阈值:
   - L^1 范数的滚动 90 / 95 / 97 分位
   - 选择 paper 启动初始阈值(可调)

输出:
  ~/projects/selene/analysis/tda_calibration_v1.md

═══════════════════════════════════════════════════
```

**实时阶段**(paper 期间):
```
═══════════════════════════════════════════════════

每个 4H bar 收盘:
  1. 取最近 W bar 的 returns
  2. Takens 嵌入 → 点云
  3. Persistent Homology(ripser)
  4. Persistence Landscape L^1 范数
  5. 滚动 90 天分位

判定逻辑:
  Critical 进入条件增加 TDA 维度(已在第 2.1 节列出):
    - σ-based 主条件(继承)
    - Hawkes branching ratio > 0.85(R1 - H2)
    - **TDA L^1 范数 > 95 分位 + 12h 单调上升**(本 patch)
  
  三者**任意 2 个满足** → Critical 进入
  
  这避免了任一单工具的假阳性

═══════════════════════════════════════════════════
```

**实施工作量**:2-3 个 CC Wave
- Takens 嵌入实施:约 100 行
- Persistent Homology(用 ripser/giotto-tda):约 100 行
- Persistence Landscape 计算:约 100 行
- 滚动窗口管理:约 100 行
- 集成到 Critical 状态判定:约 50 行
- 离线训练 + 阈值标定脚本:约 200 行

**评估机制**:
- Month 3 评估:
  - TDA 信号 vs sel 现有 Critical 条件的 lead/lag
  - TDA 信号的假阳性率
  - 与 Hawkes branching ratio 的相关性
- Month 6 评估:
  - Cascade 事件中 TDA 的真实表现
  - 假阳性率 < 30% + lead time > 24h → 验证成功
  - 否则降级为 observation 或放弃

**不确定性标注**:
- BTC 4H 数据上 Takens 嵌入的最优 d 和 τ 未实证
- ripser / giotto-tda 在生产环境的稳定性
- TDA 信号在 post-ETF 时代是否仍有效(原文献多在 2018 数据)
- TDA 计算延迟是否能在 4H bar 收盘后及时完成
- 超参数对结果的真实敏感性

---

### 12.2 Patch TDA2:TDA + clustering 状态识别(observation-only)

**作用层级**:Layer 2(observation-only)

**目的**:
- 用 TDA 的几何视角对状态进行独立分类
- 与 sel 硬规则状态 + Bayesian HMM 软验证形成"三方共识层"

**实施细节**:
```
═══════════════════════════════════════════════════

并行运行 TDA-based 状态识别:
  1. 滚动 persistence landscape
  2. k-means clustering(k=6,与 sel 状态数对齐)
  3. cluster 与 sel 状态映射(基于特征均值)
  4. 与 sel 硬规则状态对比

写入 inverse_vocab_events 表
与 Bayesian HMM 软验证(B1)叠加

3 月评估:
  - 三方共识(sel + HMM + TDA 一致)→ 高置信度
  - 三方分歧 → 高优先级 review

═══════════════════════════════════════════════════
```

**实施工作量**:1-2 个 CC Wave(基于 TDA1)
- Clustering 算法:约 200 行
- 状态映射:约 100 行
- 与 sel/HMM 对比逻辑:约 100 行

**评估机制**:
- Month 3:三方共识率
  - 高一致率(> 80%)→ 系统稳健,但 TDA 价值边际
  - 中一致率(40-80%)→ TDA 提供独立视角,有价值
  - 低一致率(< 40%)→ 严肃 review,可能某方有问题

---

### 12.3 Patch TDA3:Topological Persistence Norm 仓位调节(v2.2 范围)

**作用层级**:Layer 4(仓位决策)

**推迟原因**:
- Santana & Ramirez 2026 是 2026-04 新出 paper
- 实证强度尚需验证
- 与 K1 Kelly 已经处理仓位,叠加可能过度复杂

**v2.1 不实施**,记录为 v2.2 候选。

**触发条件**:
- paper 6+ 月,K1 Kelly 稳定
- TDA1 在 Critical 判定上验证成功
- 同时这两个条件满足 → 启动 TDA3 设计


---

# 第三部分:实施时间表与工程要点

## 13. CC 实施 Wave 划分建议

按规则 #12 的 prompt 内容边界 + Wave 切分按"可见产出+失败可隔离+回滚成本低":

### 13.1 Wave 总览

```
═══════════════════════════════════════════════════

Wave 1:数据底层 + 离线分析
  - T1 Transfer Entropy 因果地图
  - W1 Wavelet 多尺度离线分析
  - TDA1 离线训练 + 阈值标定
  
Wave 2:策略 2 增强(Layer 3 主决策)
  - H1 Hawkes 强度门槛
  
Wave 3:Critical 状态主条件(Layer 2 决策)
  - H2 Hawkes branching ratio
  - TDA1 实时部分(L^1 范数 + Critical 集成)
  
Wave 4:仓位 Phase 0/1(Layer 4)
  - K1 Phase 0 固定 base_size 实施
  - K1 Phase 1 W/R 数据收集逻辑
  
Wave 5:observation-only 工具组(Layer 2 observation)
  - B1 Bayesian HMM 软验证
  - B2 HMM 边界仲裁
  - TDA2 TDA + clustering
  - I1 Permutation Entropy 趋势预警
  - T2 TE 滚动监控
  - W2 Wavelet 多分形谱宽
  - H3 Hawkes Cascade 早期预警
  
Wave 6(条件):K1 Phase 2 切换
  - paper Month 3 评估通过后实施
  - quarter Kelly 启用
  
Wave 7(条件):Phase 3 + observation 工具升级
  - paper Month 6 评估通过后实施
  - K1 Phase 3
  - observation 工具升级为决策因子(若数据强支持)

═══════════════════════════════════════════════════
```

### 13.2 Wave 间的依赖关系

```
依赖图
═══════════════════════════════════════════════════

Wave 1 ─┐
        ├─ Wave 2(策略 2 增强,需 Hawkes 校准结果)
        ├─ Wave 3(Critical 主条件,需 Hawkes + TDA 离线训练)
        └─ Wave 5(observation 工具,部分依赖 W1)

Wave 2 ─ 独立(策略 2 入场)
Wave 3 ─ 独立(Critical 状态)
Wave 4 ─ 独立(仓位 Phase 0/1)

Wave 5 ─ 部分依赖 Wave 1 + 3(B1/TDA2 用 HMM/TDA 组件)

Wave 6 ─ 依赖 Wave 4 完成 + paper Month 3 评估
Wave 7 ─ 依赖 Wave 6 + paper Month 6 评估

═══════════════════════════════════════════════════
```

⚠️ Wave 1-5 是 paper 启动**前**的实施工作。
⚠️ Wave 6-7 是 paper 启动**后**的迭代工作。

### 13.3 paper 启动前的实施时间估算

```
═══════════════════════════════════════════════════

Wave 1(数据 + 离线):2-3 周
Wave 2(策略 2):1-2 周
Wave 3(Critical):2-3 周
Wave 4(仓位 Phase 0/1):1-2 周
Wave 5(observation 工具):3-4 周

总实施时间:9-14 周(约 2-3.5 月)

并行情况下:
  - Wave 2/3/4 可并行 → 总时间压缩到 6-9 周
  - 但需要 Wiki 在多 Wave 间切换 review

依赖 Helixa:
  - 实施可与 Helixa 实盘准备并行
  - 不互相阻塞

═══════════════════════════════════════════════════
```

⚠️ **真实总时间**:
- Selene 工程实施 = 6-14 周(并行 / 串行依赖人手)
- + Helixa 实盘准备时间
- + Helixa 实盘稳定 ≥ 3 月
- = paper 启动需要 6-9 月

## 14. CC 实施 prompt 编写要点

按规则 #12,每个 Wave 的 prompt 必须包含:

### 14.1 必备元素

```
═══════════════════════════════════════════════════

(1) FULL AUTO 头(顶部固定):
   执行模式:FULL AUTO - 不要中途问问题/请求确认/等待输入;
   遇判断点自决按最常见正确做法前进;
   只在真正无法继续时记录到最终汇报;
   全部跑完后一次性汇报所有结果

(2) 目标(具体可验收):
   例:实施 Hawkes 强度门槛作为策略 2 入场前置筛选

(3) 验收标准:
   例:H1 单元测试通过 + 集成测试在历史数据上跑通 + 
       与 v2.0 决策树对比文档输出

(4) 约束(路径 / 端口 / 容器名):
   - ~/projects/selene/...
   - 容器:selene-strategy-2
   - 数据库:platform-postgres selene
   - Redis:helios-redis DB 3

(5) 边界(Wave / FULL AUTO / 停点):
   - 单 Wave 完成即停
   - 不自决进入下一 Wave
   - 失败必须停而非自决继续

(6) ADR 锚点:
   - sel-language-v2.0.md(架构基础)
   - sel-language-v2.1-patches.md(具体 patch)
   - 引用具体 patch ID(H1 / H2 / TDA1 等)

═══════════════════════════════════════════════════
```

### 14.2 内容边界(规则 #12)

**只给目标 + 验收 + 约束 + 边界 + ADR 锚点**

**不写**:
- 代码
- 函数签名
- SQL 具体语句
- 配置内容

**理由**:CC 自己读代码 + 文档,Claude 写反而限制 CC 判断 + 浪费 token。

### 14.3 协作循环(规则 #15)

```
═══════════════════════════════════════════════════

每个 Wave 的协作循环:

1. Claude(本对话)写 Wave 的 prompt
2. Wiki 把 prompt 贴给 CC
3. CC 跑(FULL AUTO,不中断)
4. CC 完成后输出报告
5. Wiki 把 CC 原始输出贴回 Claude
6. Claude review + 决定下一步:
   - 通过验收 → 写下一个 Wave prompt
   - 部分通过 → 写修复 prompt
   - 失败 → 停下来 review 设计

═══════════════════════════════════════════════════
```

⚠️ **Wiki 角色**(规则 #14):
- Wiki 不写代码
- Wiki 拍板架构 / 验收 / 进度
- Wiki 在 Wave 间切换 review

⚠️ **Claude 角色**:
- Claude 写 prompt
- Claude review CC 输出
- Claude 不直接接终端

⚠️ **CC 角色**:
- CC 实施
- CC 不参与架构决策
- CC 不修改 sel 词汇定义

## 15. 数据库 schema 变更

按 v2.0 第 32 节,v2.1 需要的 schema 变更:

### 15.1 新增字段(decision_trail)

```sql
-- 追加到 v2.0 的 decision_trail 表
ALTER TABLE decision_trail ADD COLUMN tool_evaluation JSONB;
-- 用于记录工具评估状态(observation/decision/deprecated)

ALTER TABLE decision_trail ADD COLUMN evaluation_phase TEXT;
-- 用于标记评估阶段(month_3 / month_6 / month_12 / month_24)
```

### 15.2 新增字段(inverse_vocab_events)

```sql
-- 追加到 v2.0 的 inverse_vocab_events 表
ALTER TABLE inverse_vocab_events ADD COLUMN tool_source TEXT;
-- 标识信号来源:hawkes/transfer_entropy/hmm/tda/wavelet/permutation_entropy

ALTER TABLE inverse_vocab_events ADD COLUMN observation_only BOOLEAN DEFAULT TRUE;
-- 标识是否影响决策

ALTER TABLE inverse_vocab_events ADD COLUMN tool_metadata JSONB;
-- 工具特定元数据(如 Hawkes branching ratio 值,TDA L^p 范数)
```

### 15.3 新表:tool_evaluation_results

```sql
-- 工具评估结果表(每月 / 每 3 月评估时填充)
CREATE TABLE tool_evaluation_results (
    id              UUID PRIMARY KEY DEFAULT uuidv7(),
    timestamp       TIMESTAMPTZ NOT NULL,
    
    tool_id         TEXT NOT NULL,        -- 'H1' / 'TDA1' / etc
    tool_name       TEXT NOT NULL,
    evaluation_phase TEXT NOT NULL,        -- 'month_3' / 'month_6' / etc
    
    -- 关键指标
    lead_time_seconds   NUMERIC,
    false_positive_rate NUMERIC,
    correlation_with_others JSONB,        -- 与其他工具的相关性
    sample_size         INTEGER,
    
    -- 决定
    decision        TEXT NOT NULL,         -- 'upgrade' / 'maintain' / 'deprecate'
    decision_reason TEXT,
    
    -- 元信息
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_tool_eval_phase ON tool_evaluation_results (evaluation_phase, tool_id);
```

### 15.4 新表:strategy_phase_history

```sql
-- Kelly Phase 切换历史
CREATE TABLE strategy_phase_history (
    id              UUID PRIMARY KEY DEFAULT uuidv7(),
    timestamp       TIMESTAMPTZ NOT NULL,
    
    strategy        TEXT NOT NULL,         -- 'strategy_1' / 'strategy_2'
    from_phase      TEXT,                   -- 'phase_0' → 'phase_1' etc
    to_phase        TEXT NOT NULL,
    
    -- 切换条件(必须留痕)
    rolling_W       NUMERIC,                -- 切换时的 W
    rolling_R       NUMERIC,                -- 切换时的 R
    sample_size     INTEGER,
    
    -- 决策
    kelly_fraction_estimated NUMERIC,
    kelly_cap_lower NUMERIC,
    kelly_cap_upper NUMERIC,
    
    decision_id     UUID REFERENCES decision_trail(id),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

# 第四部分:评估机制与长期演进

## 16. Month 3 评估清单(完整版)

```
═══════════════════════════════════════════════════
Month 3 系统评估清单
═══════════════════════════════════════════════════

(1) observation-only 工具评估
   工具:B1 / B2 / T2 / H3 / TDA2 / I1 / W2
   
   评估指标(每个工具):
     - 触发频率
     - lead time(领先 sel 主决策多久)
     - 假阳性率
     - 与现有工具的相关性
     - 样本量是否足够统计推断
   
   命运决定:
     A. 数据强支持(lead time > 24h + 假阳性率 < 30%)→ 升级
     B. 数据弱支持(lead time 0-24h 或假阳性率 30-60%)→ 维持
     C. 数据明显无效(假阳性率 > 60% 或 lead time < 0)→ 放弃

(2) Critical 主条件评估
   工具:σ + 熵 / Hawkes branching / TDA L^1
   
   评估指标:
     - 4 个主条件的实际触发情况
     - 触发的一致性(多少时候多个条件同时满足)
     - 触发后的实际 Critical 行为(是否真的接近 Cascade)
   
   决策:
     - 高一致(> 80%)→ 工具冗余,可能简化
     - 中一致(40-80%)→ 互补
     - 低一致(< 40%)→ 严肃 review

(3) 策略 2 入场质量(H1 评估)
   评估指标:
     - 触发率(H1 启用前后对比)
     - 胜率(同上)
     - lead time 改善
   
   决策:
     - 胜率提升 > 5% → 验证成功
     - 胜率提升 < 2% → 阈值需调整
     - 胜率下降 → review 阈值或考虑放弃 H1

(4) Kelly 准备(K1 Phase 1 → 2)
   评估指标:
     - 样本量是否 ≥ 30 笔
     - W 和 R 的稳定性(滚动估计变化幅度)
     - W·R - (1-W) 是否显著正
   
   决策:
     - 全部满足 → Phase 2 启用
     - 部分满足 → 维持 Phase 1
     - 完全不满足 → 严肃 review,可能策略本身有问题

(5) 健康分布
   状态触发率 vs 占位符目标:
     - Coiling:目标 20-30%
     - Drifting:目标 40-60%
     - Surging:目标 10-20%
     - Critical:目标 1-5%
     - Cascade:目标 < 1%
   
   严重偏离(差 > 50%)→ 调整阈值

═══════════════════════════════════════════════════

输出文档:
  ~/projects/selene/reports/3month_review_v1.md

决策权:
  - Wiki 拍板
  - Claude 数据 + 建议
  - 全部决策写入 decision_trail + tool_evaluation_results
```

## 17. Month 6 评估清单(完整版)

```
═══════════════════════════════════════════════════
Month 6 系统评估清单
═══════════════════════════════════════════════════

(1) 累积财务指标
   - 累计 PnL(USDT)
   - Sharpe ratio
   - Sortino ratio
   - 最大回撤
   - Calmar ratio
   - 与 Helixa 同期对照(架构差异 vs 实施差异)

(2) 架构层面 review
   - 双策略架构是否有效?
     * 策略 1 单独 vs 策略 2 单独 vs 双策略
     * 哪个贡献了主要 PnL
   - 80/20 分配是否合理?
     * 实际产生 PnL 的比例
     * 子账户余额漂移
   - 协调层(同向独立 / 反向独立)
     * 反向对冲发生频率(目标 < 月 5 次)
     * 对冲后的实际效果

(3) Critical 系统评估
   - 4 个主条件的真实贡献
   - 至少 1 个 Cascade 事件中的表现(如发生)
   - Cascade 立即清仓的代价 / 收益
   - Critical 减仓 50% 是否过早或过晚

(4) 自毁开关检查
   - 是否连续 3 次调参无改善?
   - 如果是 → 触发停摆评估
   - 严肃质疑 sel 整体方向

(5) v2.2 候选讨论
   - 8 轮未调研的工具(微观结构 / 链上 / 期权)
   - 是否有 paper 数据强烈指向某个新方向?
   - 例:策略 2 持续表现差 → 考虑微观结构 OFI 增强
   - 例:链上信号在 cascade 前显著 → 链上深度

(6) 实盘启动评估(关键)
   实盘启动条件(必须全部满足):
     * 6 月评估数据强支持
     * Helixa 实盘稳定 ≥ 3 月
     * 无系统性 bug 累积
     * Wiki + Claude 双重 review 通过
     * 写入 decision_trail
   
   数据强支持 → 进入实盘启动准备
   数据弱支持 → 继续 paper

═══════════════════════════════════════════════════

输出文档:
  ~/projects/selene/reports/6month_review_v1.md
```

⚠️ **6 月评估是 sel 的第一个"小突破"窗口**(替代"1-2 年再说")。

## 18. Month 12 评估清单(中突破窗口)

```
═══════════════════════════════════════════════════
Month 12 系统评估清单
═══════════════════════════════════════════════════

(1) 1 年完整 paper 数据
   - 12 个月 PnL 分布
   - 包含 BTC 多个 regime 的表现
   - 至少 5+ Cascade 事件样本
   - 季节性 / 周期性效应

(2) 长期数据资产
   - decision_trail 完整 12 个月
   - 60+ 笔交易(目标)
   - 工具有效性的统计显著性检验

(3) 中突破方向
   - paper 数据反向归纳新工具(2D 方向)
     * 数据中是否暗示某个未调研维度?
     * 例:某些状态在某些 regime 下错误率高 → 需新工具
   - 是否需要 v3.0 架构升级讨论
     * 严肃讨论(不轻易触发)
     * 自毁开关已多次触发?
     * 是否 sel 框架本身有根本问题?
   - v2.2+ 的具体方向

(4) Helios 平台层面
   - sel 在 Helios 平台的角色
   - 与 Helixa / Tide 的协同
   - 跨系统决策学习层(v6 候选)是否触发

═══════════════════════════════════════════════════

输出文档:
  ~/projects/selene/reports/12month_review_v1.md
```

## 19. Month 24 评估(真突破窗口)

```
═══════════════════════════════════════════════════
Month 24 系统评估清单(真突破窗口)
═══════════════════════════════════════════════════

24 个月是真正考虑根本性突破的节点:
  - 跨 BTC 多个完整 cycle 的数据
  - sel 框架在长周期上的真实表现
  - 与 Helixa 长期对照(架构差异 vs 实施差异)

可考虑的真突破方向:
  (a) 全资产扩展(v6 候选)
      * 触发条件:6+ 月稳定 + 100+ 付费用户 > 60% 要求
      * 包括美股 / ETF / 黄金 / 石油 / 大宗
  
  (b) 多账户支持(v6 候选)
  
  (c) 跨系统决策学习层
      * sel + Helixa 数据池
      * 学习层提取共性
  
  (d) 加入未调研维度的全新工具
      * 微观结构(Kyle / O'Hara / Hasbrouck)
      * 链上数据深度(cohort 分析)
      * 期权理论(volatility surface)
      * 行为金融
      * 机器学习(transformer / RL)
      * 网络分析
  
  (e) 发明 BTC 新工具(谨慎评估)
      * 必须有 paper 数据支持
      * 必须可证伪
      * 必须有清晰物理类比
      * 不是浪漫化的"创新"

═══════════════════════════════════════════════════
```

⚠️ **24 月才考虑根本性突破**,这与 v2.0 第 28.4 节"自毁开关"配合:
- 24 月内 sel 持续无效 → 停摆评估(可能放弃 sel)
- 24 月内 sel 有效 → 真突破方向评估

## 20. v2.1 核心纪律总结

按规则 #11 + #14,v2.1 的核心纪律(必须显式记录):

```
═══════════════════════════════════════════════════

v2.1 核心纪律(冻结)
═══════════════════════════════════════════════════

(1) 工具不堆砌:
   - Critical 主条件最多 4 个(σ+熵/Hawkes/TDA)
   - observation-only 工具必须 3 月内有结论
   - 不允许"工具越多越好"思维

(2) 评估节点强制:
   - Month 3 / 6 / 12 / 24 评估清单不可跳过
   - 每个节点产出文档 + decision_trail 记录
   - "等等再说"不是合法回答

(3) 工具命运三选一:
   - 升级 / 维持 / 放弃
   - 没有第四选项
   - 反复看不到效果还不放弃 = 失败

(4) Kelly 严格分阶段:
   - Phase 0 → 1 → 2 → 3 必须按时间顺序
   - 不允许跳阶段
   - 不允许 full Kelly

(5) 实盘启动门槛严格:
   - Helixa 实盘稳定 ≥ 3 月
   - Selene 6 月评估数据强支持
   - Wiki + Claude 双重 review
   - "v2.1 完成"不是实盘启动条件

(6) 过度自信防御:
   - "站在头顶" / "超越" / "已穷尽" / "提前实盘" 立即拦截
   - Claude 不软化
   - 工艺自我评估,不与人比较

(7) 自毁开关:
   - 连续 3 次调参无改善 → 项目停摆评估
   - sel 是工具,不是信仰

═══════════════════════════════════════════════════
```

---

# 附录

## 附录 A:14 个 patch 速查表

| ID | 工具 | Layer | Wave | 价值 | 状态 |
|---|---|---|---|---|---|
| H1 | Hawkes 强度门槛 | 3 主决策 | 2 | 高 | 主决策 |
| H2 | Hawkes branching ratio | 2 状态机 | 3 | 高 | Critical 主条件 |
| H3 | Hawkes Cascade 预警 | 2 obs | 5 | 中 | observation |
| T1 | TE 因果地图 | 1 离线 | 1 | 中-高 | 离线工具 |
| T2 | TE 滚动监控 | 2 obs | 5 | 中 | observation |
| K1 | 动态 Kelly base_size | 4 仓位 | 4 | 高 | 主决策(分阶段) |
| K2 | 状态条件 Kelly | 4 仓位 | v2.2 | - | 推迟 |
| B1 | HMM 软验证 | 2 obs | 5 | 中 | observation |
| B2 | HMM 边界仲裁 | 2 状态机 | 5 | 中 | 决策(条件性) |
| W1 | Wavelet 多尺度离线 | 1 离线 | 1 | 中 | 离线工具 |
| W2 | Wavelet 多分形 | 2 obs | 5 | 低 | observation |
| I1 | PE 趋势预警 | 2 obs | 5 | 低 | observation |
| I2 | PE 增强 Coiling | 2 状态机 | v2.2 | - | 推迟 |
| TDA1 | TDA L^1 范数 | 2 状态机 + 1 离线 | 1+3 | 高 | Critical 主条件 |
| TDA2 | TDA + clustering | 2 obs | 5 | 中 | observation |
| TDA3 | Topological Persistence Norm | 4 仓位 | v2.2 | - | 推迟 |

## 附录 B:Round 7 RMT 调研记录

按规则 #13 的诚实记录:

```
═══════════════════════════════════════════════════
Round 7 - Random Matrix Theory 调研记录

调研日期:2026-04-29
调研深度:同行评议文献扫描 + 创造性落地点评估

调研结论:不纳入 v2.1

理由汇总:
  1. RMT 本质是多资产工具(N >> 10)
  2. sel 是单一 BTC 资产
  3. 无 BTC 单一资产 RMT 文献支持
  4. 五个创造性落地点全部失效或被替代

未来重新评估触发:
  - sel 扩展到多资产(v6 候选)
  - 触发条件:paper 6+ 月稳定 + 100+ 付费用户 > 60% 要求

═══════════════════════════════════════════════════
```

⚠️ **诚实记录的价值**:
- 知道哪些工具不适合 = sel 知识资产的一部分
- 不是失败,是边界确认
- 未来扩展时不会重复调研

## 附录 C:文献调研关键引用

### Round 1 - Hawkes
- Decisions in Economics and Finance 2026 — BTC LOB MHP 预测
- arxiv 2510.08085(2025-10)— Binance BTCUSDT 校准 + near-unstable subcritical
- arxiv 2502.17723(2025-02)— 半参数 MHP 应用于 LOB
- jheusser 2013 — Mt.Gox trade Hawkes 早期实证
- Filimonov-Sornette — branching ratio 经济意义

### Round 2 - Transfer Entropy
- MDPI Entropy 2019(Jang & Lee)— BTC 与其他资产 TE
- Royal Society Open Science 2020(Keskin & Aste)— BTC sentiment ↔ price
- Journal of Futures Markets 2023(Barak et al.)— TE-based feature selection

### Round 3 - Kelly
- Kelly 1956 — 原始论文
- Thorp(实战派代表)
- Frontiers 2020 — Kelly 在多市场实证
- arxiv 2508.16598(2025-08)— Kelly + VIX hybrid

### Round 4 - Bayesian HMM
- MDPI Mathematics 2025 — Bitcoin Price Regime Shifts via Bayesian MCMC
- Preprints 2026 — Markov and HMM for Bitcoin Regime Detection
- Academia 2025 — Two-State Gaussian HMM on BTC

### Round 5 - Wavelet
- ScienceDirect 2026 — Multi-scale decomposition for Bitcoin forecasting
- ScienceDirect 2019 — Wavelet leaders in high-frequency BTC
- Pontiggia 2025 — BTC multifractal

### Round 6 - Permutation Entropy
- Nature Scientific Reports 2019(Sigaki et al.)— PE + 437 cryptos
- Wiley 2025 — BTC heists and PE
- MDPI Entropy 2019 — High-Frequency Entropy for BTC VaR

### Round 7 - RMT
- Laloux et al 1999 / Plerou 2002 — 经典金融 RMT
- arxiv 2510.19130(2025-12)— 89 cryptos RMT + ResNet
- arxiv 2512.06473(2025-12)— 140 cryptos detrended RMT

### Round 8 - TDA
- Gidea et al 2020(ScienceDirect)— BTC critical transitions
- Ismail et al 2020(IEEE)— BTC crashes early warning
- Santana & Ramirez 2026(arxiv 2604.13311)— Topological Persistence Norm
- Springer 2024 — BTC critical transitions + clustering

## 附录 D:与 v2.0 主文档的对应关系

```
═══════════════════════════════════════════════════
v2.0 章节 → v2.1 patch 修改

v2.0 第 5.2 节(Coiling) → I2(条件性,v2.2)
v2.0 第 5.4 节(Drifting-Calm) → B2 边界仲裁
v2.0 第 5.5 节(Drifting-Charged) → B2 边界仲裁
v2.0 第 5.6 节(Critical) → H2 + TDA1 + W2/B1/I1(observation)
v2.0 第 13.4 节(策略 1 仓位) → K1
v2.0 第 14.2 节(策略 2 入场决策树) → H1
v2.0 第 14.4 节(策略 2 仓位) → K1
v2.0 第 19 节(实证方法论) → 19.4 T1 + 19.5 W1
v2.0 第 28 节(调参纪律) → 整体扩展(评估节点)
v2.0 第 32 节(数据库 schema) → 第 15 节 schema 变更

═══════════════════════════════════════════════════
```

## 附录 E:实施风险清单

按规则 #11 + #13 显式列出:

```
═══════════════════════════════════════════════════
v2.1 实施风险

(1) 工程风险
  - Hawkes / TDA 实时计算延迟可能超 4H bar 收盘窗口
  - Bayesian HMM 训练时间长(数小时 MCMC)
  - PyWavelets / ripser / giotto-tda 在生产环境稳定性

(2) 数据风险
  - 2-3 年训练数据可能不够(post-ETF 时代仅 2 年+)
  - paper 期间数据漂移(regime 变化)
  - Cascade 事件样本严重不足(估计 5-15 次/年)

(3) 设计风险
  - Critical 主条件 4 选 2 的实证依据未充分
  - observation 工具的 3 月评估纪律是否能严格执行
  - Kelly Phase 切换的"30 笔交易"门槛是否合理

(4) 心理风险
  - Wiki "工具数量错觉"可能复发
  - paper 早期数据噪声大,容易过度调参
  - "实盘启动诱惑"在 6 月评估时严重

(5) Helixa 阻塞风险
  - Helixa 实盘准备延期 → Selene paper 启动也延期
  - Selene 完成 v2.1 实施后处于"等待"状态
  - Wiki 注意力分散

═══════════════════════════════════════════════════
```

⚠️ **每个风险的缓解措施**已在对应 patch 中标注。

---

# 文档结束声明

本文档(`sel-language-v2.1-patches.md`)是 sel v2.1 的**完整 patch 集**。

**冻结范围**:
- v2.0 主文档:**架构层冻结**(继续有效)
- v2.1 patch 列表:**冻结**(14 个 patch + 1 个无产出 R7)
- 评估节点机制:**冻结**(3/6/12/24 月)
- 过度自信防御:**冻结**(规则 #11 硬执行)
- 实施 Wave 划分:**指导性,可微调**

**修改规则**:
- 参数层面 → 通过 decision_trail,Wiki 决策
- 架构层面 → 仅 paper 数据强烈不利时考虑 v3.0
- 评估节点 → 不可跳过,可调整内容(不可跳过时间)

**长期演进路径**(替代"1-2 年再说"):
```
═══════════════════════════════════════════════════

实施期(目前 → paper 启动):
  - v2.0 主体 + v2.1 patch CC 实施
  - 与 Helixa 实盘准备并行

paper Month 0-3:观察期 + 数据收集
paper Month 3:第一次评估(工具命运决定)
paper Month 3-6:第一次迭代
paper Month 6:第二次评估(实盘启动决策)
paper Month 6-12:实盘考虑期
paper Month 12:中突破窗口
paper Month 12-24:长期数据积累
paper Month 24:真突破窗口

═══════════════════════════════════════════════════
```

**下一步**(本文档发布后):
1. CC 实施 prompt 包编写(下一轮对话)
2. CC 按 Wave 1-5 实施(几月)
3. Helixa 实盘启动(并行,关键路径)
4. Helixa 实盘 ≥ 3 月 → Selene paper 启动
5. Month 3/6/12/24 评估循环

---

**作者**:Wiki(决策)+ Claude(架构 + 调研)  
**日期**:2026-04-29  
**字数**:约 30000 中文字  
**版本**:v2.1(冻结)  
**配套文档**:sel-language-v2.0.md(冻结)

