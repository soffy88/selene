## [2.0.0] - 2026-05-12

### Changed - BREAKING
- Migrated to 3O stack (oprim 1.2.0, oskill 1.2.0, omodul 0.2.0)
- 11 functions now imported from oprim instead of local implementations
  (value_at_risk, hurst_exponent, orderbook_entropy, atr, linear_slope,
   percentile_value ×3 locations, pearson_spearman_corr ×2, takens_embed)
- Hawkes calibration deduplicated (mle.py is now single canonical source)
- 3 dead code items removed

### Added
- 22 new elements available in stack (for Selene internal use + cross-project)
  - oprim 1.2.0: +13 (percentile_value, hawkes_nll, ordinal_pattern, shannon_entropy,
    phase_randomize, takens_embed, persistence_landscape, compute_dwt, hurst_exponent,
    orderbook_entropy, linear_slope, atr, H_change_rate_std)
  - oskill 1.2.0: +6 (fit_hawkes, symbolic_transfer_entropy, cusum_detector,
    gaussian_hmm, adx, platt_calibration)
  - omodul 0.2.0: +3 (kelly_allocator, risk_parity, execution_cost_model)
- Migration regression tests in tests/migration/ (15 tests)

### Removed
- _reconstruction_energy_fraction (dead code, wavelet.py)
- schedule_weekly_report_blocking (dead code, scheduler.py)
- volatility_targeting (dead code, kelly.py)
- Duplicate hawkes_nll/fit_hawkes in hawkes_calibration.py (replaced with import)

### Architecture
- Project now follows 3O Layer 4 pattern: business/service code remains in Selene,
  reusable computational primitives moved to / extracted from stack
- See SELENE_STACK_EXTRACTIONS.md for complete mapping
- See TOLERANCE_LOG.md for numerical tolerance decisions

### Migration Metrics
- LOC: 28813 → 28634 (-179 net)
- Tests: 829 → 844 (+15 migration regression tests)
- Coverage: 92% → 91% (within tolerance)
- Stack imports: 0 → 11 (all oprim Layer 1)
- Numerical regressions: 0 (all rtol ≤ 1e-10)
- Aborted items: 0
