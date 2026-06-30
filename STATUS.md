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

- **P1-8** CPCV `label_horizon` purge for multi-bar holds.

---

## 📋 Backlog — P0 (live-safety / signal-trust; must precede any live ambition)


## 📋 Backlog — P1 (core capability gaps)

- [ ] **P1-3** Cascade cond-1 unreachable: `lob_depth_pctile` is never computed from the
      (now perp) LOB depth the collector stores. Derive it + wire into BarFeatures.
- [ ] **P1-4** WS disconnect has no gap recovery (`v2_bar_aggregator.py:28`).
- [ ] **P1-6** No Prometheus/Grafana actually deployed (metrics exposed, unscraped).
- [ ] **P1-7** Decision Trail has no UI/API read surface (the moat is invisible).
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

- **P1-5** Correlation static-fallback side bug fixed: `check_corr_exposure` now normalises
      LONG/SHORT vs BUY/SELL to a sign (matching the dynamic path) before summing same-
      direction exposure — the gate previously matched nothing and silently passed all
      correlated concentration at cold start. 3 new tests. `services/risk/main.py`.
- **P1-2** Verified already code-complete + tested (not a code gap): `_micro_vocab_series`
      derives Sweep/Absorption/Crowding vocab and `_maybe_open_s2` derives
      `ofi_persistent_same_direction` from real microstructure, and Type A/B entries are
      exercised in `test_strategy2_entry`. The audit's "S2 inert" reflected the empty-data
      runtime; the residual is deploy-data availability (advanced by P0-2). No code needed.
- **P1-1** Gated sel_v2 → live execution bridge: `decision_to_scored_signal` translates a
      deployed S1/S2 entry decision into the canonical ScoredSignal (protective stop from the
      REAL drawdown-stop pct, regime mapped from 4H state), and `LiveBridge.emit` publishes
      it onto `signal.scored` so the paper-validated strategy reaches the same Kelly-sizing +
      risk-gate (incl. P0-1 liq guard) + P0-3 native-stop path. Default OFF
      (`SEL_V2_LIVE_BRIDGE`); only controls *reachability*, loosens no gate. 7 new tests.
      `sel_v2/paper_interface/live_bridge.py`, `tests/sel_v2/test_live_bridge.py`.
- **P0-6** Observe-only iron law (backend language): the mode-switch surface no longer
      *advises* — "建议切换到 AUTO_EXEC" → neutral threshold-status with an explicit
      "是否切换为人工决策，系统不作建议" disclaimer (report.py advisor + rendered §⑧ +
      monitoring Telegram push). Endpoints renamed `/monitor/recommendation` →
      `/monitor/mode-thresholds` with deprecated aliases + back-compat keys (also closes
      P2-6), in both monitoring and gateway. 3 new tests. NOTE: the v4 *execute-UI*
      (confirm/reject/execute buttons) is a product decision → Needs-Human.
- **P0-5** Backtest verdict is now binding at two real boundaries:
      `enforce_oos_gate()` *raises* `BacktestRejected` on a failing/absent OOS slice (the
      verdict can't be computed-then-ignored), and the live boot guard
      (`_assert_safe_exec_mode`) now also requires `I_HAVE_OOS_EVIDENCE=yes` so no live
      mode can start without proven out-of-sample evidence — guilty until proven innocent.
      NOTIFY_ONLY/PAPER/dev unaffected. 2 new tests + updated guard tests.
      `services/execution/main.py`, `backtest/v2_strategy_backtest.py`.
- **P0-4** Real-strategy backtest DSR no longer degenerate: `n_trials` defaults to
      `effective_calibration_trials()` (product of the calibration knobs the deployed
      config was selected over, = 81), so DSR deflates for selection bias instead of
      collapsing to PSR-vs-0. Documented `CALIBRATION_KNOBS`, overridable. 1 new test.
      `backtest/v2_strategy_backtest.py`, `tests/backtest/test_v2_strategy_backtest.py`.
- **P0-3** Exchange-native protective stops: `place_stop_order` on the adapter
      interface (base default = unsupported; real Binance `STOP_MARKET` + OKX algo
      `conditional`); live fills now place a reduce-only native stop that survives a
      service/feed outage or gap, with a high-severity alert + in-process backstop when
      placement fails; cancelled on close. `websockets` lazy-imported so adapters are
      unit-testable. 7 new tests. `services/execution/adapters/{base,okx,binance}.py`,
      `services/execution/main.py`, `tests/services/test_native_stop_orders.py`.
- **P0-2** Microstructure feed now matches the traded instrument: tick + LOB
      collectors subscribe to the perp `BTC-USDT-SWAP` (was spot), storing the shared
      base symbol so downstream joins are unchanged. Liquidation/derivatives made
      symmetric+configurable; `websockets_proxy` lazy-imported so the modules are now
      unit-testable. 8 new tests. `sel_v2/data/v2_{tick,lob,liquidation,derivatives}_collector.py`,
      `tests/sel_v2/test_collector_instrument.py`.
- **P0-1** Perp liquidation-distance guard in RiskGate (`check_liquidation_distance`,
      Gate 5b in `approve()`): rejects when post-trade cross-leverage sits within
      MIN_LIQ_BUFFER_PCT of liquidation, or when the protective stop is at/beyond the
      liquidation price (would liquidate before the stop fills). 7 new tests, 44
      existing risk tests still green. `services/risk/main.py`,
      `tests/unit/test_risk_liquidation_gate.py`.

---

## 🚨 Needs Human

- **P0-6 frontend product decision**: the only shipped UI is the v4 recommendation/
  execution dashboard (signal cards with entry/SL/TP + confirm/reject/execute buttons).
  Backend advisory *language* is now neutralized, but the execute-UI itself is structurally
  at odds with the Helios observe-only doctrine. Decision needed: keep the v4 execution
  dashboard as a separate product, or build/ship an observe-only Helios UI (decision-trail,
  regime, observation tools) as the primary surface? I did not delete a working feature on
  a judgment call. (Execution remains NOTIFY_ONLY regardless, so the buttons are inert today.)
- **P1-1 bridge end-to-end live validation**: the translator + gated publisher are unit-
  tested, but the full sel_v2-decision → signal.scored → portfolio → risk → execution loop
  needs a real Redis/services run to validate (call-site wiring in the paper/strategy engine
  to actually invoke `LiveBridge.emit` is intentionally NOT added yet — that is the live
  cut-over step and must be a deliberate human action with OOS evidence in hand).
- **P0-3 live bracket lifecycle** needs real-exchange verification before any live use
  (cannot be integration-tested here): native stop placement on the *WebSocket* fill
  path (currently wired only on the immediate-FILLED branch), OCO pairing with take-profit,
  partial-fill stop resizing, and de-duplication against the in-process monitor so a
  fired native stop and the monitor don't both try to close. Capability + unit tests are
  in place; the live wiring is best-effort and gated (never runs under NOTIFY_ONLY).
