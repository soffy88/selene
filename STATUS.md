# STATUS — Selene / Helios

Canonical task board. Source: trader-view audit (2026-06-30, 7-subsystem deep read).
Numbering is **this audit's** backlog, distinct from the earlier 24-item / 5-subsystem
rounds (see memory `opt-pr-3`).

---

## 🔒 Never

- **Never enable live mainnet trading.** The system MUST stay `EXEC_MODE=NOTIFY_ONLY`.
  No deployed strategy has out-of-sample alpha evidence yet. Do NOT set
  `AUTO_EXEC` / `CONFIRM_THEN_EXEC` with `ENVIRONMENT=production`, and do NOT
  remove the `_assert_safe_exec_mode` boot guard or the `I_UNDERSTAND_LIVE_AUTO_EXEC`
  ack requirement.
- Never weaken a risk gate to make a test pass. Gates fail closed.
- Never treat a missing/None feature as a confirmed signal (three-state discipline).
- Never silently smooth a *signal* in the sel_v2 path (state-machine dwell is OK).

---

## 🔄 In Progress

- **P0-2** Spot-price-drives-perp-strategy — making the instrument configurable, default perp.

---

## 📋 Backlog — P0 (live-safety / signal-trust; must precede any live ambition)

- [ ] **P0-2** Spot-price-drives-perp-strategy. Tick/LOB use spot `BTC-USDT`
      (`v2_tick_collector.py:18`) while OI/funding/liq + traded instrument are perp
      `BTC-USDT-SWAP`. Make instrument configurable, default to the perp.
- [ ] **P0-3** Synthetic in-process stop. Add exchange-native STOP order capability
      + slippage bound (gated; never runs under NOTIFY_ONLY).
- [ ] **P0-4** Real-strategy backtest DSR degenerate (`n_trials=1`,
      `backtest/v2_strategy_backtest.py:73`) — never deflates for the real search.
- [ ] **P0-5** No backtest pass-gate is enforced; `passed` is computed but consumed
      by nothing (`gateway/main.py:399`). Make "guilty until proven innocent" binding.
- [ ] **P0-6** Frontend violates observe-only iron law (recommendation cards +
      execute buttons + "建议" language). Honor the law without destroying function.

## 📋 Backlog — P1 (core capability gaps)

- [ ] **P1-1** sel_v2 strategy has no live execution bridge (paper↔live divergent).
      Build a gated bridge; flag for human verification before any live use.
- [ ] **P1-2** Strategy 2 inert + upper state machine unreachable (LOB/OFI/liq STUB).
- [ ] **P1-3** Cascade extreme defense dead (needs depth/liq/spread data).
- [ ] **P1-4** WS disconnect has no gap recovery (`v2_bar_aggregator.py:28`).
- [ ] **P1-5** Correlation static-fallback side mismatch (`risk/main.py:260`,
      LONG/SHORT vs BUY/SELL never equal) — gate is a no-op at cold start.
- [ ] **P1-6** No Prometheus/Grafana actually deployed (metrics exposed, unscraped).
- [ ] **P1-7** Decision Trail has no UI surface (the moat is invisible).
- [ ] **P1-8** CPCV has no `label_horizon` purge despite multi-bar holds.

## 📋 Backlog — P2 (cleanup / UX)

- [ ] **P2-1** `v2_ofi_features` is an orphan store (0 readers; paper engine recomputes).
- [ ] **P2-2** Misleading names: `hawkes_cascade_warning` (no Hawkes),
      `wavelet_multifractal` (not multifractal), `tda_clustering` (no clustering).
- [ ] **P2-3** smart_router cross-venue split computed but never executed
      (`execution/main.py:177` uses `splits[0]`).
- [ ] **P2-4** Online ICTracker uses tie-naive Spearman (`signal/main.py:166`).
- [ ] **P2-5** Backfilled bars carry fake `vwap=0` in the same column as real VWAP
      (`okx_backfill.py:47`).
- [ ] **P2-6** `/monitor/recommendation` ("建议") endpoint name violates iron law.

---

## ✅ Done

- **P0-1** Perp liquidation-distance guard in RiskGate (`check_liquidation_distance`,
      Gate 5b in `approve()`): rejects when post-trade cross-leverage sits within
      MIN_LIQ_BUFFER_PCT of liquidation, or when the protective stop is at/beyond the
      liquidation price (would liquidate before the stop fills). 7 new tests, 44
      existing risk tests still green. `services/risk/main.py`,
      `tests/unit/test_risk_liquidation_gate.py`.

---

## 🚨 Needs Human

- (none yet)
