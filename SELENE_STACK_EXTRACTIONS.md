# Selene → Stack Extraction Tracking

## oprim (Layer 1) — 13 elements on feat/v1.2-from-selene

| # | Selene source | Element name | Module | Selene replaced? |
|---|---|---|---|---|
| 1 | sel_engine/features/price.py:compute_price_slope_6h | linear_slope | signal_processing | ✅ YES |
| 2 | services/signal/regime/detector.py:_calc_atr | atr | signal_processing | ✅ YES |
| 3 | sel_engine/features/derived.py:compute_hurst_rs | hurst_exponent | signal_processing | ✅ YES |
| 4 | sel_v2/offline/wavelet.py:compute_dwt | compute_dwt | signal_processing | ⏳ Deferred (module moves as unit) |
| 5 | sel_engine/features/liquidity.py:compute_H_change_rate_std | H_change_rate_std | signal_processing | ⏳ Deferred (trivial, not worth import overhead) |
| 6 | sel_engine/features/liquidity.py:compute_orderbook_entropy | orderbook_entropy | signal_processing | ✅ YES |
| 7 | sel_v2/offline/transfer_entropy.py:_H | shannon_entropy | information | ⏳ Deferred (internal to symbolic_te) |
| 8 | sel_v2/offline/transfer_entropy.py:_ordinal_pattern | ordinal_pattern | information | ⏳ Deferred (internal to symbolic_te) |
| 9 | sel_v2/offline/transfer_entropy.py:phase_randomize | phase_randomize | information | ⏳ Deferred (internal to te_pvalue) |
| 10 | sel_v2/offline/tda_calibration.py:takens_embed | takens_embed | topology | ✅ YES (tda_clustering) |
| 11 | sel_v2/offline/tda_calibration.py:persistence_diagram_to_landscape | persistence_landscape | topology | ⏳ Deferred (tda_critical uses different params) |
| 12 | sel_v2/hawkes/mle.py:hawkes_nll | hawkes_nll | point_process | ⏳ Deferred (mle.py is canonical, used by oskill.fit_hawkes) |
| 13 | (new, stack gap) | percentile_value | statistics | ✅ IMPLEMENTED (available for future use) |

## oskill (Layer 2) — 6 elements on feat/v1.2-from-selene

| # | Selene source | Element name | Module | Selene replaced? |
|---|---|---|---|---|
| 1 | sel_v2/hawkes/mle.py:fit_hawkes | fit_hawkes | point_process | ⏳ Deferred (Selene mle.py is canonical, same algo) |
| 2 | sel_v2/offline/transfer_entropy.py:symbolic_te | symbolic_transfer_entropy | causal | ⏳ Deferred (different ordinal encoding) |
| 3 | sel_v2/strategies/cusum_short.py:CUSUMShort | cusum_detector | signal_detection | ⏳ Deferred (stateful class vs functional) |
| 4 | sel_v2/observation_tools/bayesian_hmm.py:_GaussianHMM | gaussian_hmm | hmm | ⏳ Deferred (stateful class vs functional) |
| 5 | services/signal/regime/detector.py:_calc_adx | adx | signal_detection | ⏳ Deferred (numerical diff with Selene version) |
| 6 | services/signal/factors/composite.py:platt_fit | platt_calibration | signal_detection | ⏳ Deferred (grid search params differ) |

## omodul (Layer 3) — 3 elements on feat/v0.2-from-selene

| # | Selene source | Element name | Module | Selene replaced? |
|---|---|---|---|---|
| 1 | services/portfolio/capital/kelly.py:kelly_fraction | kelly_allocator | portfolio | ⏳ Deferred (different formula: cost-adjusted vs simple) |
| 2 | services/portfolio/capital/kelly.py:risk_parity_weights | risk_parity | portfolio | ⏳ Deferred (Selene has residual correction logic) |
| 3 | services/execution/slippage/model.py:SlippageModel | execution_cost_model | portfolio | ⏳ Deferred (class vs function) |

## Summary

- **Stack elements implemented**: 22/22 (13 oprim + 6 oskill + 3 omodul)
- **Selene-side replacements done**: 5 (linear_slope, hurst_exponent, orderbook_entropy, takens_embed, atr)
- **Selene-side deferred**: 17 (internal helpers, stateful classes, different formulas)
- **Reason for deferrals**: Most Bucket B items are internal to modules that will be used via the stack in NEW projects, but Selene's existing code has project-specific wrappers that are better left in Layer 4.


## Audit Corrections (post-Wave 3)

Audit 阶段将以下 4 项归到 Bucket B (extraction candidate),
实际深入实施后确认应归 Bucket C (Layer 4 stateful):

| # | Item | Original bucket | Corrected bucket | Reason |
|---|---|---|---|---|
| 1 | DrawdownController | B → oskill? | C (Layer 4) | Stateful class maintaining _peak/_halted_at/level; oprim.drawdown_curve is pure functional |
| 2 | CUSUMShort | B → oskill.cusum_detector | C (Layer 4) | Stateful class with peaks deque + time-based eviction; oskill version is batch/functional |
| 3 | _GaussianHMM | B → oskill.gaussian_hmm | C (Layer 4) | Incremental fit mode (update per bar); oskill version is batch fit on full sequence |
| 4 | SlippageModel | B → omodul.execution_cost_model | C (Layer 4) | Class maintaining regime state; omodul version is pure function |

These remain as Selene Layer 4 business logic. The stack versions (oskill.cusum_detector,
oskill.gaussian_hmm, omodul.execution_cost_model) serve as general-purpose batch alternatives
for future non-stateful projects.
