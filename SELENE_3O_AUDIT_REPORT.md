# Selene 3O Migration Audit Report

**Date**: 2026-05-12
**Branch**: audit/3o-migration-survey
**Total LOC** (excluding tests/venv): 28,813
**Total functions**: 579
**Total classes**: 162
**Total test functions**: 722
**Baseline coverage**: 92%
**Baseline tests**: 829 passed, 33 warnings, 4.90s

## Summary

| Bucket | Count | LOC (est.) | % of total |
|---|---|---|---|
| A (direct replacement) | 25 | ~550 | 1.9% |
| B (extraction candidate) | 35 | ~2,400 | 8.3% |
| C (Layer 4 retention) | 516 | ~25,100 | 87.1% |
| D (dead code) | 3 | ~60 | 0.2% |
| Tests (untouched) | 722 | (separate) | — |


## Stack Coverage Analysis

### oprim v1.1.0 (Layer 1 — 40 elements)

Selene 可立即使用 (Bucket A):
- `oprim.value_at_risk` → 替换 `services/risk/portfolio/var_engine.py:calc_historical_var` (×18 callers)
- `oprim.drawdown_curve` → 替换 `services/risk/portfolio/var_engine.py:DrawdownController.update` (×21 callers)
- `oprim.sharpe_ratio` → 替换 `backtest/engine.py:_calc_period_metrics` (×1 caller, internal)
- `oprim.cumulative_returns` → 替换 `backtest/engine.py:_aggregate_metrics` (×1 caller)
- `oprim.ewma_smooth` → 替换 `services/signal/regime/detector.py:_ema` (×7 callers)
- `oprim.pearson_spearman_corr` → 替换 `sel_engine/features/price.py:compute_autocorr` (×9 callers)
- `oprim.pearson_spearman_corr` → 替换 `services/signal/regime/hmm_detector.py:_autocorr` (×1 caller)
- `oprim.pearson_spearman_corr` → 替换 `services/signal/main.py:ICTracker.calc_ic` (×2 callers)
- `oprim.zscore_normalize` → 替换 `services/signal/factors/composite.py:score_funding_zscore` (×6 callers)
- `oprim.percentile_ci` → 替换 `services/signal/factors/composite.py:_ci_half` (×1 caller)
- `oprim.percentile_rank` → 替换 `services/signal/main.py:_rank` (×3 callers)
- `oprim.percentile_rank` → 替换 `sel_v2/states/tda_critical.py:compute_tda_rolling_pctile` (×2 callers)
- `oprim.percentile_rank` → 替换 `sel_v2/strategies/cusum_short.py:_current_threshold` (×1 caller)
- `oprim.percentile_rank` → 替换 `sel_v2/strategies/hawkes_intensity.py:RollingIntensityThreshold.threshold` (×1 caller)
- `oprim.log_returns` → 替换 `sel_engine/features/price.py:compute_price_features` (partial, ×10 callers)
- `oprim.realized_vol` → 替换 `sel_engine/features/price.py:compute_sigma_change_rate_std_6h` (×1 caller)

未覆盖但 Selene 需要 (候选抽取 → Bucket B):
- Hurst exponent (R/S analysis) → 建议 `oprim.hurst_exponent`
- Shannon entropy → 建议 `oprim.shannon_entropy`
- Permutation entropy → 建议 `oprim.permutation_entropy`
- Ordinal pattern encoding → 建议 `oprim.ordinal_pattern`
- Phase randomization surrogate → 建议 `oprim.phase_randomize`
- Linear regression slope → 建议 `oprim.linear_slope`

### oskill v1.0.0 (Layer 2 — 16 elements)

Selene 可立即使用 (Bucket A):
- `oskill.bootstrap_sharpe` → 替换 `backtest/engine.py:_monte_carlo` (×1 caller)
- `oskill.walk_forward_optimization` → 替换 `backtest/engine.py:WFOEngine.run` WFO split logic (×1 caller)
- `oskill.psr_dsr` → 可增强 `backtest/engine.py:_check_pass` 的 Sharpe 验证

未覆盖但 Selene 需要 (候选抽取 → Bucket B):
- Hawkes process MLE fitting → 建议 `oskill.hawkes_mle`
- Gaussian HMM (Baum-Welch) → 建议 `oskill.gaussian_hmm`
- Symbolic Transfer Entropy → 建议 `oskill.symbolic_transfer_entropy`
- Page CUSUM detector → 建议 `oskill.cusum_detector`
- DWT energy decomposition → 建议 `oskill.wavelet_energy`
- TDA persistence landscape → 建议 `oskill.persistence_landscape`
- Platt scaling calibration → 建议 `oskill.platt_calibration`
- ATR / ADX indicators → 建议 `oskill.atr` / `oskill.adx`

### omodul v0.1.0 (Layer 3 — 17 elements)

Selene 可立即使用:
- `omodul.strategy_backtest_report` → 可增强 `backtest/engine.py` 的报告输出
- `omodul.strategy_decay_monitor` → 可增强 monitoring 的策略衰退检测
- `omodul.tail_risk_analyzer` → 可增强 risk service 的尾部风险分析

未覆盖但 Selene 需要 (候选抽取 → Bucket B):
- Slippage model (sqrt-impact) → 建议 `omodul.execution_cost_model`
- Kelly criterion (cost-adjusted + phased) → 建议 `omodul.kelly_allocator`
- Risk parity weights → 建议 `omodul.risk_parity`
- Volatility targeting → 建议 `omodul.vol_targeting`


## Bucket A — Direct stack replacement (25 items)

| # | Selene location | Function/Class | Stack target | Callers | Risk |
|---|---|---|---|---|---|
| 1 | services/risk/portfolio/var_engine.py:21 | calc_historical_var | oprim.value_at_risk | 18 | High |
| 2 | services/risk/portfolio/var_engine.py:49 | DrawdownController.update | oprim.drawdown_curve | 21 | High |
| 3 | backtest/engine.py:334 | _calc_period_metrics | oprim.sharpe_ratio + drawdown_curve | 1 | High |
| 4 | backtest/engine.py:365 | _aggregate_metrics | oprim.sharpe_ratio + cumulative_returns | 1 | High |
| 5 | backtest/engine.py:388 | _monte_carlo | oskill.bootstrap_sharpe | 1 | High |
| 6 | backtest/engine.py:127 | WFOEngine.run (split logic) | oskill.walk_forward_optimization | 1 | High |
| 7 | services/signal/regime/detector.py:130 | _ema | oprim.ewma_smooth | 7 | Medium |
| 8 | sel_engine/features/price.py:33 | compute_autocorr | oprim.pearson_spearman_corr | 9 | Medium |
| 9 | services/signal/regime/hmm_detector.py:165 | _autocorr | oprim.pearson_spearman_corr | 1 | Medium |
| 10 | services/signal/main.py:153 | ICTracker.calc_ic | oprim.pearson_spearman_corr | 2 | Medium |
| 11 | services/signal/main.py:183 | ICTracker._calc_single_ic | oprim.pearson_spearman_corr | 1 | Medium |
| 12 | services/signal/main.py:194 | _rank | oprim.percentile_rank | 3 | Low |
| 13 | services/signal/factors/composite.py:101 | score_funding_zscore | oprim.zscore_normalize | 6 | Medium |
| 14 | services/signal/factors/composite.py:223 | _ci_half | oprim.percentile_ci | 1 | Medium |
| 15 | sel_engine/features/price.py:10 | compute_price_features (log-ret part) | oprim.log_returns | 10 | Medium |
| 16 | sel_engine/features/price.py:84 | compute_sigma_change_rate_std_6h | oprim.realized_vol | 1 | Low |
| 17 | sel_v2/states/tda_critical.py:129 | compute_tda_rolling_pctile | oprim.percentile_rank | 2 | Low |
| 18 | sel_v2/strategies/cusum_short.py:161 | CUSUMShort._current_threshold | oprim.percentile_rank | 1 | Low |
| 19 | sel_v2/strategies/hawkes_intensity.py:236 | RollingIntensityThreshold.threshold | oprim.percentile_rank | 1 | Low |
| 20 | sel_v2/states/tda_critical.py:36 | takens_embed (dedup) | internal → tda_calibration.py | 4 | Medium |
| 21 | sel_v2/states/tda_critical.py:44 | _persistence_landscape (dedup) | internal → tda_calibration.py | 2 | Medium |
| 22 | sel_v2/states/tda_critical.py:58 | compute_pl_l1 (dedup) | internal → tda_calibration.py | 2 | High |
| 23 | sel_v2/observation_tools/tda_clustering.py:38 | _persistence_landscape_l1 (dedup) | internal → tda_calibration.py | 1 | Medium |
| 24 | sel_v2/offline/hawkes_calibration.py:53 | hawkes_nll (dedup) | internal → hawkes/mle.py | 2 | High |
| 25 | sel_v2/offline/hawkes_calibration.py:98 | fit_hawkes (dedup) | internal → hawkes/mle.py | 2 | High |

**Total Bucket A**: 25 items, ~550 LOC
**Estimated Wave 2 effort**: 5-7 days (avg 4 items/day with regression testing)


## Bucket B — Extraction candidates (35 items)

按 §8.1 准则评分 (E1=layer fit, E2=reuse ≥2, E3=math clarity, E4=test verifiability):

| # | Selene location | Function | Target layer | E1 | E2 | E3 | E4 | Verdict | Effort |
|---|---|---|---|---|---|---|---|---|---|
| 1 | sel_v2/hawkes/mle.py:29 | hawkes_nll | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 2d |
| 2 | sel_v2/hawkes/mle.py:54 | fit_hawkes | oskill | ✓ | ✓ | ✓ | ✓ | EXTRACT | 3d |
| 3 | sel_v2/observation_tools/bayesian_hmm.py:43 | _GaussianHMM (full class) | oskill | ✓ | ✓ | ✓ | ✓ | EXTRACT | 5d |
| 4 | sel_v2/offline/transfer_entropy.py:60 | symbolic_te | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 3d |
| 5 | sel_v2/offline/transfer_entropy.py:31 | _ordinal_pattern | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 1d |
| 6 | sel_v2/offline/transfer_entropy.py:47 | _H (Shannon entropy) | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 1d |
| 7 | sel_v2/offline/transfer_entropy.py:106 | phase_randomize | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 1d |
| 8 | sel_v2/offline/transfer_entropy.py:115 | te_pvalue | oskill | ✓ | ✓ | ✓ | ✓ | EXTRACT | 2d |
| 9 | sel_v2/offline/wavelet.py:59 | compute_dwt | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 2d |
| 10 | sel_v2/offline/tda_calibration.py:50 | takens_embed | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 1d |
| 11 | sel_v2/offline/tda_calibration.py:95 | persistence_diagram_to_landscape | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 3d |
| 12 | sel_v2/offline/tda_calibration.py:128 | compute_pl_l1 | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 2d |
| 13 | sel_v2/offline/tda_calibration.py:64 | estimate_tau | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 1d |
| 14 | sel_v2/strategies/cusum_short.py:57 | CUSUMShort.update | oskill | ✓ | ✓ | ✓ | ✓ | EXTRACT | 3d |
| 15 | sel_v2/strategies/hawkes_intensity.py:97 | HawkesIntensityTracker | oskill | ✓ | ✓ | ✓ | ✓ | EXTRACT | 3d |
| 16 | sel_v2/strategies/hawkes_intensity.py:158 | fit_gmm | oskill | ✓ | ✓ | ✓ | ✓ | EXTRACT | 2d |
| 17 | sel_v2/observation_tools/permutation_entropy.py:40 | _permutation_entropy | oprim | ✓ | ✗ | ✓ | ✓ | REJECT | — |
| 18 | sel_v2/observation_tools/wavelet_multifractal.py:32 | _energy_ratio | oprim | ✓ | ✗ | ✓ | ✓ | REJECT | — |
| 19 | sel_engine/features/derived.py:47 | compute_hurst_rs | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 2d |
| 20 | sel_engine/features/liquidity.py:16 | compute_orderbook_entropy | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 1d |
| 21 | sel_engine/features/price.py:59 | compute_price_slope_6h | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 1d |
| 22 | services/portfolio/capital/kelly.py:15 | kelly_fraction | omodul | ✓ | ✓ | ✓ | ✓ | EXTRACT | 3d |
| 23 | services/portfolio/capital/kelly.py:81 | risk_parity_weights | omodul | ✓ | ✓ | ✓ | ✓ | EXTRACT | 2d |
| 24 | services/portfolio/capital/kelly.py:118 | volatility_targeting | omodul | ✓ | ✗ | ✓ | ✓ | REJECT | — |
| 25 | services/execution/slippage/model.py:45 | SlippageModel.estimate | omodul | ✓ | ✓ | ✓ | ✓ | EXTRACT | 3d |
| 26 | services/signal/regime/detector.py:140 | _calc_atr | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 1d |
| 27 | services/signal/regime/detector.py:153 | _calc_adx | oskill | ✓ | ✓ | ✓ | ✓ | EXTRACT | 2d |
| 28 | services/signal/factors/composite.py:265 | platt_fit | oskill | ✓ | ✓ | ✓ | ✓ | EXTRACT | 2d |
| 29 | services/signal/factors/composite.py:219 | _sigmoid (Platt inference) | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 1d |
| 30 | services/signal/regime/hmm_detector.py:121 | _build_features | oskill | ✓ | ✗ | ✓ | ✓ | REJECT | — |
| 31 | services/signal/regime/hmm_detector.py:175 | _fit_and_predict | oskill | ✓ | ✓ | ✓ | ✓ | EXTRACT | 4d |
| 32 | sel_v2/states/hawkes_critical.py:32 | compute_hawkes_branching_ratio | oskill | ✓ | ✓ | ✓ | ✓ | EXTRACT | 2d |
| 33 | sel_v2/strategies/kelly_sizing.py:98 | KellySizer.compute | omodul | ✓ | ✓ | ✓ | ✓ | EXTRACT | 3d |
| 34 | sel_engine/features/liquidity.py:40 | compute_H_change_rate_std | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 1d |
| 35 | sel_v2/offline/transfer_entropy.py:54 | _joint_symbols | oprim | ✓ | ✓ | ✓ | ✓ | EXTRACT | 0.5d |

**Total Bucket B accepted**: 31 items (4 rejected → Bucket C)
- → oprim: 17 items (primitives: entropy, TDA, wavelet, Hurst, ATR, slope)
- → oskill: 10 items (HMM, Hawkes, CUSUM, TE, ADX, Platt, WFO)
- → omodul: 4 items (Kelly, risk parity, slippage, Kelly sizer)

**Total Bucket B rejected** (→ Bucket C): 4 items (single caller or composite builder)
**Estimated Wave 3 effort**: 45-55 days (含 stack release 协调)


## Bucket C — Layer 4 retention (516 items, sampled)

按类别归类:
- **I/O & DB**: ~85 items (services/data/, shared/db/, sel_v2/db/, sel_engine/db/, paper_trading/db/)
- **Event streaming**: ~20 items (shared/events/, services/gateway/)
- **Scheduling & orchestration**: ~45 items (sel_engine/scheduler/, sel_v2/scheduler/, reports/scheduler.py)
- **State machine & business logic**: ~120 items (sel_engine/states/, sel_v2/states/, paper_trading/, decision/)
- **Config & CLI**: ~25 items (decision/config.py, services/*/Dockerfile, scripts/)
- **Monitoring & notification**: ~40 items (services/monitoring/, services/notification/)
- **Execution adapters**: ~35 items (services/execution/adapters/, routing/, statemachine/)
- **Signal orchestration**: ~50 items (services/signal/main.py handlers, services/signal/weight_learner.py)
- **Onchain**: ~45 items (services/onchain/)
- **Report rendering**: ~30 items (sel_v2/reports/, reports/generator.py)
- **ObservationTool wrappers**: ~21 items (sel_v2/observation_tools/ .update/.reset/.is_ready methods)

Wave 4 (optional refactor) 候选: ~15 items
- 这些 Bucket C 项目可选择性用 stack 重写内部逻辑, 但不强制
- 例: `services/monitoring/report.py:analyze_ic` 可用 `oprim.pearson_spearman_corr` 简化内部
- 例: `services/risk/main.py:check_var` 可用 `oprim.value_at_risk` 替换内部调用

## Bucket D — Dead code (3 items)

| Location | Function | Last commit | Reason | Callers |
|---|---|---|---|---|
| sel_v2/offline/wavelet.py:69 | _reconstruction_energy_fraction | 2026-04-29 | Internal helper, never called after refactor | 0 |
| sel_v2/reports/scheduler.py:112 | schedule_weekly_report_blocking | 2026-04-29 | Entry point never wired to any runner/Dockerfile | 0 |
| services/portfolio/capital/kelly.py:118 | volatility_targeting | 2026-04-27 | Defined but never imported or called | 0 |

**Total LOC to delete**: ~60
**Estimated Wave 1 effort**: 0.5 days


## Numerical Risk Assessment

预估各 Bucket A 项数值差异风险:

**Low (纯加减乘除, 一次性公式)** — 8 items, 期望 rtol=1e-12 通过:
- `_rank`, `score_funding_zscore`, `compute_sigma_change_rate_std_6h`
- `compute_tda_rolling_pctile`, `_current_threshold`, `RollingIntensityThreshold.threshold`
- `compute_price_features` (log-return part), `_ema`

**Medium (含 floating-point 累积 / 不同 API 语义)** — 10 items, 期望 rtol=1e-10:
- `compute_autocorr`, `_autocorr`, `ICTracker.calc_ic`, `_ci_half`
- `score_funding_zscore` (edge: empty array handling)
- Internal dedup items (takens_embed, _persistence_landscape, _persistence_landscape_l1)
- `_calc_period_metrics` (Sharpe annualization convention)

**High (数值算法不同实现 / random seed 依赖)** — 7 items, 可能需 rtol=1e-8:
- `calc_historical_var` — oprim uses sorted percentile vs numpy quantile
- `DrawdownController.update` — stateful vs functional API mismatch
- `_monte_carlo` — random seed handling differs from oskill.bootstrap_sharpe
- `_aggregate_metrics` — compound vs simple return convention
- Internal dedup: `hawkes_nll`, `fit_hawkes` (optimizer convergence), `compute_pl_l1` (ripser vs gudhi)

→ High-risk 项目可能需要 Wiki 决策是否接受差异

## Stack Gap Identification

Selene 需要但 stack 无法覆盖, 也不符合抽取标准的:
- `compute_LV` (liquidity vacuum): 项目专属复合指标, 无通用数学定义
- `check_cascade/critical/coiling/surging/drifting_*`: 状态识别业务逻辑, 非数学
- `RegimeDetector._classify`: 阈值组合逻辑, 非可复用算法
- `fuse_regimes`: 映射表, 非数学
- `_bar_to_intensity_proxy`: 项目专属启发式
- `generate_recommendation`: 业务规则引擎
- 所有 ObservationTool `.update()` wrappers: 胶水代码, 调用底层数学但自身非数学

这些保留在 Selene Layer 4.


## Estimated Total Migration Effort

| Phase | Effort | Wall clock | Dependencies |
|---|---|---|---|
| Wave 1 (delete D) | 0.5 days | 0.5 days | Wiki confirm 3 items |
| Wave 2 (replace A) | 5-7 days | 1-1.5 weeks | oprim/oskill API stable |
| Wave 3 (extract B) | 45-55 days | 8-10 weeks | stack release coordination |
| Wave 4 (optional C refactor) | 5-10 days (optional) | — | post-Wave 3 |
| Verification & regression | 3-4 days | 1 week | all waves |
| **Total** | **~60-75 days** | **~12-14 weeks** | — |

注: Wave 3 是最大工作量, 因为需要:
1. 在 oprim/oskill/omodul 中实现新 element
2. 编写 element 级测试 (scipy/statsmodels 对照)
3. 发布 stack 新版本
4. 在 Selene 中替换调用
5. 回归测试

## Decisions Needed from Wiki

按重要性排序, Wiki 需要做以下决策才能启动 Wave 1:

### 1. [HIGH] Bucket B 抽取范围
- 全部 31 项都做? 还是只做高复用前 15 项 (Hawkes, HMM, TDA, TE, CUSUM, Kelly), 其它转 Bucket C?
- 建议: 优先抽取 callers ≥ 4 的项目 (hawkes_nll, fit_hawkes, compute_hurst_rs, symbolic_te, takens_embed, kelly_fraction)

### 2. [HIGH] 数值容差策略
- 默认 rtol=1e-10 (建议)
- High-risk 项目 (7 items) 可放宽到 1e-8? 或必须达 1e-10 否则 abort?
- 特别关注: `calc_historical_var` (quantile interpolation), `_monte_carlo` (seed handling)

### 3. [MEDIUM] Bucket A 中边界模糊项 (5 items)
以下项目 stack 已有但语义略不同, 需 Wiki 确认是否替换:
- `DrawdownController.update`: oprim.drawdown_curve 是 functional, 而 Selene 是 stateful class
- `WFOEngine.run`: oskill.walk_forward_optimization 只返回 splits, 不含 simulate 逻辑
- `compute_price_features`: 只有 log-return 部分可替换, rolling std 部分需保留
- Internal dedup items (#20-25): 是否先内部去重再等 stack 抽取?

### 4. [MEDIUM] Wave 3 节奏
- 串行 (一个 element 入栈后立即在 Selene 使用) — 风险低但慢
- 批量 (积累 5-8 个一次发布) — 快但 review 压力大
- 建议: 按 layer 批量 (先 oprim batch, 再 oskill batch, 最后 omodul batch)

### 5. [LOW] Bucket D 中可疑项 (3 items)
- `schedule_weekly_report_blocking`: 可能是未来 cron 入口, 需确认是否真删
- `volatility_targeting`: 可能是 planned feature, 需确认
- `_reconstruction_energy_fraction`: 确认已被 `_energy_ratio` 替代

## Internal Dedup Opportunities (Pre-Wave 2)

以下函数在项目内有重复实现, 建议 Wave 2 前先内部去重:

| Canonical location | Duplicates | Action |
|---|---|---|
| sel_v2/hawkes/mle.py | sel_v2/offline/hawkes_calibration.py:53-150 | 删除 calibration.py 中的副本, import from mle.py |
| sel_v2/offline/tda_calibration.py:50-180 | sel_v2/states/tda_critical.py:36-100, sel_v2/observation_tools/tda_clustering.py:38-65 | 统一到 tda_calibration.py, 其它 import |

去重后 Bucket A #20-25 自动消失, 减少 Wave 2 工作量.

