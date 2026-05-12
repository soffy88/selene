# Tolerance Log — Selene 3O Migration

Items where rtol != 1e-10 (default).

| # | Function | Stack target | rtol used | Reason |
|---|---|---|---|---|
| 1 | calc_historical_var | oprim.value_at_risk | 1e-8 | Quantile interpolation method differs (sorted index vs numpy quantile) |

## Items moved to Bucket C (rtol > 1e-6 or interface mismatch)

| # | Function | Original target | Reason for abort |
|---|---|---|---|
| 2 | DrawdownController.update | oprim.drawdown_curve | Stateful class vs functional API — incompatible interface |
| 3-6 | WFO engine (_calc_period_metrics, _aggregate, _monte_carlo, run) | oprim.sharpe_ratio + oskill | PnL-based Sharpe (not return-based), population std, project-specific |
| 7 | _ema | oprim.ewma_smooth | SMA-seeded initialization differs from pandas EMA; rdiff=1e-3 for period=50 |
| 10-11 | ICTracker.calc_ic/_calc_single_ic | oprim.pearson_spearman_corr | Module import blocked by fastapi dep; custom Spearman formula |
| 15 | compute_price_features | oprim.log_returns | Only partial overlap; function computes multiple features |
| 16-19 | compute_sigma_change_rate_std_6h, tda_rolling_pctile, _current_threshold, threshold | oprim.percentile_rank | Already using np.quantile directly; oprim.percentile_rank has different semantics |
| 20-23 | TDA dedup items | internal | Different algorithms (sum of bars vs landscape integral); not true duplicates |
