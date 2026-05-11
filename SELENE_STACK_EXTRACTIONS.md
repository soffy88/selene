# Selene → Stack Extraction Tracking

## oprim (Layer 1) — 13 elements added to feat/v1.2-from-selene

| # | Selene source | Element name | Module | Status |
|---|---|---|---|---|
| 1 | sel_engine/features/price.py:compute_price_slope_6h | linear_slope | signal_processing | ✅ EXTRACTED + REPLACED |
| 2 | services/signal/regime/detector.py:_calc_atr | atr | signal_processing | ✅ EXTRACTED |
| 3 | sel_engine/features/derived.py:compute_hurst_rs | hurst_exponent | signal_processing | ✅ EXTRACTED + REPLACED |
| 4 | sel_v2/offline/wavelet.py:compute_dwt | compute_dwt | signal_processing | ✅ EXTRACTED |
| 5 | sel_engine/features/liquidity.py:compute_H_change_rate_std | H_change_rate_std | signal_processing | ✅ EXTRACTED |
| 6 | sel_engine/features/liquidity.py:compute_orderbook_entropy | orderbook_entropy | signal_processing | ✅ EXTRACTED + REPLACED |
| 7 | sel_v2/offline/transfer_entropy.py:_H | shannon_entropy | information | ✅ EXTRACTED |
| 8 | sel_v2/offline/transfer_entropy.py:_ordinal_pattern | ordinal_pattern | information | ✅ EXTRACTED |
| 9 | sel_v2/offline/transfer_entropy.py:phase_randomize | phase_randomize | information | ✅ EXTRACTED |
| 10 | sel_v2/offline/tda_calibration.py:takens_embed | takens_embed | topology | ✅ EXTRACTED + REPLACED |
| 11 | sel_v2/offline/tda_calibration.py:persistence_diagram_to_landscape | persistence_landscape | topology | ✅ EXTRACTED |
| 12 | sel_v2/hawkes/mle.py:hawkes_nll | hawkes_nll | point_process | ✅ EXTRACTED |
| 13 | (new, stack gap) | percentile_value | statistics | ✅ IMPLEMENTED |

## oskill (Layer 2) — pending Wave 3.2

| # | Selene source | Element name | Status |
|---|---|---|---|
| 1 | sel_v2/hawkes/mle.py:fit_hawkes | fit_hawkes | ⏳ PENDING |
| 2 | sel_v2/offline/transfer_entropy.py:symbolic_te | symbolic_transfer_entropy | ⏳ PENDING |
| 3 | sel_v2/strategies/cusum_short.py:CUSUMShort | cusum_detector | ⏳ PENDING |
| 4 | sel_v2/observation_tools/bayesian_hmm.py:_GaussianHMM | gaussian_hmm | ⏳ PENDING |
| 5 | services/signal/regime/detector.py:_calc_adx | adx | ⏳ PENDING |
| 6 | services/signal/factors/composite.py:platt_fit | platt_calibration | ⏳ PENDING |

## omodul (Layer 3) — pending Wave 3.2

| # | Selene source | Element name | Status |
|---|---|---|---|
| 1 | services/portfolio/capital/kelly.py:kelly_fraction | kelly_allocator | ⏳ PENDING |
| 2 | services/portfolio/capital/kelly.py:risk_parity_weights | risk_parity | ⏳ PENDING |
| 3 | services/execution/slippage/model.py:SlippageModel | execution_cost_model | ⏳ PENDING |
