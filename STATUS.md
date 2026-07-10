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

- P2 cleanups (P2-4 tie-aware Spearman, P2-1 OFI orphan, P2-5 vwap, P2-3 router split).

---

## 📋 Backlog — P0 (live-safety / signal-trust; must precede any live ambition)


## 📋 Backlog — P1 (core capability gaps)


## 📋 Backlog — P2 (cleanup / UX)

- [ ] **P2-1** `v2_ofi_features` orphan store — deferred (documented in Needs-Human).
- [ ] **P2-2** Misleading observation-tool names — deferred (docstrings already self-disclaim;
      rename is high-import-churn for low value).
- [ ] **P2-3** smart_router cross-venue split not executed — deferred (single-venue +
      NOTIFY_ONLY make it low-value now; needs live multi-venue validation).

---

## ✅ Done

### Full-health audit + fixes (2026-07-03) — see `audit/2026-07-03_full_health_audit.md`

Live-stack体检发现 docker `healthy` 是假象：**23 张表全空**（OKX 在本环境被全局封锁，
采集器 13h 断供）、v4 `signal.scored`=0、healthcheck 对空表隐形。已修 + 实证：
- **P0-a** OKX 全局不可达 / Binance 经 `helios-proxy:2080` 可达；compose proxy 默认值改为可用代理。
- **P0-c** 新增 `sel_v2/data/binance_backfill.py`，`v2_bars_4h` 从 Binance 回填 **4380 bars（2yr，已落库）**。
- **P0-d** healthcheck 新增空表检测（STALE 或 EMPTY 都告警）——补上让 13h 全断供隐形的盲区。
- **P1-a** onchain→signal 桥修 2 个 import bug 并接线；`signal.raw` 不再是孤儿流（已实证消费）。
- **P1-b** `/metrics` 误用未初始化的 `redis_client.health_check` → 改 `connections.redis_health`（6 服务）。
- **P1-d** composite `EFFECTIVE_WEIGHTS`：social/orderbook 死权重置零 + 重归一化，score 不再被稀释。
- **P1-e** `shared/db/connections.py` 加 asyncpg/redis 超时（治 EC-13 静默挂起 + DNS 抖动）。
- **P2-c** `backtest/costs.py` 增 per-symbol 成本档 + `cost_params_for()`，engine 按 symbol 取值。

**OKX→Binance 采集器迁移(2026-07-03,已实证)**:实证 OKX 与 Binance WS 均不可达,Binance REST 可达
(代理抖动),故迁为 REST 轮询。新增 `sel_v2/data/binance_rest.py`;`v2_derivatives_snapshots`
(premiumIndex+openInterest,30s)、`v2_lob_snapshots`(depth,60s)、`v2_bars_4h`(--loop 前向轮询)
**均已实测写入** → 解锁 Coiling/Drifting-Charged(OI/funding)+ Cascade/Critical(LOB)。
`v2_ticks`(REST 有损)未迁、`v2_liquidations`(仅 WS)不可迁 → 见 Needs-Human。

⚠️ **P1-6 已回退**：`e0e1cbc` 删了自带 prometheus/grafana，改由平台中央 `prometheus-agent` 采集
（本审计的 `/metrics` item #12 即为此适配）。STATUS 早前「P1-6 done」记录作废。
⚠️ **部署持久性**：v4 服务源码打进镜像，本次热补丁经 `docker cp`+restart，**需 `compose build` 才持久**。

### SEL live-ops rounds (2026-06-30 → 07-01) — commits `d9297ec`…`ebb636b`

Live-deployment debugging + optimization of the sel_v2 (SEL) subsystem, driven against the
running docker-compose stack (real DB/collectors/paper engine). Distinct from the audit
backlog above. All verified live; all tests green (suite ~1148+).

**Frontend / observability (SEL tab now a real cockpit):**
- `f9949da` — gateway had **no `DB_URL`**, so EVERY `/api/v2/sel/*` PG endpoint 500'd — the
  real reason "S1/S2 were invisible". Added DB_URL to the gateway env. (Root infra bug.)
- `a13ce52` — S1/S2 strategy panel + `GET /sel/strategy/summary` (open/closed/PnL/win-rate
  from `v2_trades`, current state, no Redis).
- `74f3b07` — per-bar "why no entry": engine captures the latest S1/S2 `EntryDecision`,
  persisted to `v2_paper_latest_decision`, shown on the panel (action·step·reason).
- `bf80c6e` — **BTC candlestick chart + regime-state annotation** on the SEL tab (vendored
  TradingView Lightweight Charts, no CDN; `GET /sel/chart` joins bars⋈state). Repopulated
  `v2_state_history` with current-code states so markers are meaningful.
- `d86074c` — chart legend lists all **6 states**, dims the 2 that never occur
  (Coiling / Drifting-Charged — the OI/entropy-gated states, same root as S1 not trading).
- `d76f770` — state-history table columns fixed: the blank 方向/置信度 (no source — the
  state machine is deterministic, `sub_state` always NULL) replaced by real
  from/via/duration; feature-completeness + cold-start derived from `state_features`.
- `6ec73f4` — **counterfactual S1 overlay** on the chart (toggle): assuming the unavailable
  OI/entropy/funding gates pass, S1 had ~68 entries over 2yr (40% win, +11k USDT) — shown as
  ▲/▼/○ markers with a loud "NOT a validated backtest" banner. `v2_counterfactual_trades` +
  `GET /sel/counterfactual`.
- `fde3eee` (#2) — **full per-bar decision trail** persisted to `v2_strategy_decision`
  (self-healing upsert), joined into state-history as an "S1决策" column (per-bar action·step).
- `ebb636b` (#3) — the **7 observation-only tools** (HMM regime/boundary, TDA clustering,
  permutation/transfer entropy, wavelet, Hawkes cascade) had a runner but NO caller — now run
  over the recent window, persisted to `v2_observation_latest` (throttled to new-bar),
  `GET /sel/observations` + a SEL observation panel.

**Signal correctness / data:**
- `d9297ec` — **Coiling/Drifting-Charged never formed** because `entropy_pctile` /
  `funding_pctile` / `oi_change_rate_pctile` were 100% null. Wired LOB entropy into BarFeatures
  and made the rolling-percentile window **adaptive** (emit once ≥30 obs) so a recently-started
  feed produces a percentile instead of waiting for the full 360-bar window.
- `2d90d24` — **liquidation collector filtered the wrong field**: OKX puts `instId` on the
  outer item (details[].instId is None), so `v2_liquidations` was **永远 0** and the Cascade
  liquidation-pulse defense was dead. Now filters on item.instId (captured from the live
  channel; pure `extract_liquidation_rows` + tests).
- `708903d` — `okx_backfill` fetched **spot** candles (historical bars were spot while the live
  feed is perp). Now defaults to the perp `{symbol}-SWAP`. NOTE: measured basis is only ~0.05%,
  so re-backfilling the existing 2yr is **low-value** — code fixed for future, existing data
  left as-is by choice.
- `d837b4a` (P1-4) — `write_states_bulk` now `ON CONFLICT DO UPDATE` (was DO NOTHING) with a
  WHERE guard, so `v2_state_history` **self-heals** on recompute instead of keeping stale
  first-written states (no more manual TRUNCATE+repopulate).

**Performance:**
- `97035f6` (#4) — the full-history replay ran on every tick, recomputing σ/Hawkes/**TDA(ripser
  over ~4500 bars)** each time. Now cached by a closes signature and reused when no new 4H bar
  sealed (the dominant case). Engine output unchanged (verified state_counts identical).

**Diagnosis that did NOT need a code change (documented for the record):**
- **S1/S2 have 0 trades** — verified NOT a bug: the only ~15 days with OI/LOB data were a
  sustained high-vol regime, so S1's entry states (Coiling/Drifting-Charged, which need low/mid
  vol) never formed. Pipeline is wired; S1 will trade when the market consolidates.
- **OI history is unbackfillable** — OKX caps `open-interest-history` at ~16 days (pagination
  no-ops). So a faithful historical S1/S2 OOS is impossible from OKX; a 3rd-party OI source
  (Coinglass/…) is the only path. See Needs-Human.

**Deferred SEL follow-ups (not yet done):** 3rd-party historical OI (for real OOS);
`v2_ofi_features` orphan store (table doesn't even exist — decide wire-or-drop); Cascade
cond-2 needs live liquidation data to actually flow (collector now fixed, awaiting events);
Surging Up/Down direction (sub_state unused); S2 counterfactual (needs tick-driven Hawkes).

- **P2-5** VWAP now NULL (unknown) instead of fake 0.0 where it can't be computed (REST
      backfill + zero-volume bars), so a reader can't mistake a placeholder for a real VWAP.
      `sel_v2/data/{okx_backfill,v2_bar_aggregator}.py`. (P2-6 done with P0-6.)
- **P2-4** Online IC is now tie-correct: ICTracker uses average ranks + Pearson-on-ranks
      (`_spearman`, equals `scipy.stats.spearmanr` under ties) instead of the
      1−6Σd²/(n(n²−1)) shortcut that assumed no ties — discretised scores / flat bars no
      longer bias the IC used for sizing throttle. 5 new tests. `services/signal/main.py`.
- **P1-6** Prometheus + Grafana now deployed in docker-compose (were absent): prometheus
      scrapes the gateway `/metrics` (target verified to match `gateway:5000`) with 30d
      retention, grafana on :3000, both on helios-net with persistent volumes. Compose +
      prometheus.yml validated as parseable. Container bring-up needs a real Docker host →
      Needs-Human. `docker-compose.yml`.
- **P1-7** Rich decision-trail read API: `GET /sel/decision-trail/full` exposes the per-bar
      `sel_decision_trail` (feature snapshot, state+reason, proposed-vs-final action, matched
      rule, risk veto+details, fill, config_hash) — the Helios moat that was persisted but had
      no read surface. Degrades to [] when the table is absent. 2 new tests.
      `sel_v2/paper_interface/api.py`. (Frontend trail tab still pending — see P0-6 Needs-Human.)
- **P1-4** Bar-aggregator gap recovery: an empty 4H bar (WS outage lost its trades) is no
      longer silently skipped — it's recovered from the official OKX perp candle
      (tick_count=0 marks REST-recovered) so the 4H series stays contiguous; only an
      unrecoverable bar logs an explicit GAP. Pure `build_bar_row`/`parse_rest_candle`
      extracted for testing. 7 new tests. `sel_v2/data/v2_bar_aggregator.py`.
- **P1-3** Cascade cond-1 reachable: BarRunner now derives `lob_depth_pctile` (rolling
      7-day rank of total top-of-book bid+ask depth) from the collected perp LOB and wires it
      into BarFeatures; paper engine aggregates `AVG(bid_depth+ask_depth)` per bar. A thin book
      now yields a low pctile so "σ extreme AND thin book" can fire. 3 new tests.
      `sel_v2/scheduler/bar_runner.py`, `sel_v2/paper/{strategy_engine,paper_engine}.py`.
- **P1-8** CPCV now purges label overlap: `run_cpcv` threads `label_horizon` through to
      `oskill.cpcv_pipeline` and the engine passes `MAX_HOLD_HOURS` (trades hold up to 24
      hourly bars), so train samples whose labels overlap the test window are purged instead
      of leaking future info and flattering PBO/path-Sharpe. 2 tests (incl. forwarding).
      `backtest/cpcv.py`, `backtest/engine.py`.
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

- **数据源迁移 OKX→Binance (2026-07-03)**: OKX 在本部署环境永久不可达（每代理 403/SSL-EOF，
  平台 `*_OKX_FLAG=0`）；Binance 仅经 `helios-proxy:2080` 可达。live WS 采集器（tick/lob/deriv/liq）
  是 OKX-WS 专用，恢复实时微结构数据需将其移植到 Binance WS——核心大改且本地无法跑测试
  （无 pytest/oprim/oskill wheel），不宜盲改。已用 Binance REST 救活 `v2_bars_4h` 作过渡。
  同时决定 v4 signal 链存废：接 `v2_bars_4h`→`market.candles`（无生产者），或全押 sel_v2+LiveBridge。
  `.env:30-31` 的 proxy（受保护文件）仍指向失效 IP，需人工改为 `helios-proxy:2080`。
- **回测严谨性（须先于任何 live 野心，需 CI 私有 wheel 验证）**: (a) PBO 现为 Sharpe 符号代理而非
  真 CSCV（vendored `oskill` 已有实现未用）；(b) 真 CPCV 路径 `test_cpcv_wiring.py:147` 硬 skip、
  生产调未导出的 `oskill.cpcv_pipeline` → 静默 `cpcv=None`；(c) `I_HAVE_OOS_EVIDENCE` 仅 env 荣誉检查，
  应绑定 committed OOS artifact。本地无 oskill/pytest，改后无法自证 → 留 CI。
- **SEL historical OOS is blocked by data, not code** (2026-07-01): S1/S2 entry states need OI,
  and OKX only serves ~16 days of OI history (LOB/entropy: no history at all). So a faithful
  "where would S1/S2 have traded" backtest is impossible from OKX — decision needed on a
  **3rd-party historical OI source** (Coinglass/Laevitas/Amberdata; some paid). Until then S1
  can only accrue evidence *forward* (it trades once the market consolidates). The chart's
  "反事实成交" toggle shows the OI-gates-assumed upper bound, clearly labelled as not-a-backtest.
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
- **P1-3 remaining Cascade conditions**: cond-1 (thin book) is now wired; cond-2
  (`liquidation_pulse`) needs a `v2_liquidations`→per-bar aggregation (tractable, separate)
  and cond-3 (`cross_exchange_spread`) is structurally N/A while single-venue (OKX only) —
  a second venue feed (P2: 2nd exchange) is required. Validate cond-1 firing with real LOB
  data on deploy.
- **P2-1 OFI orphan store**: `v2_ofi_features` (ofi_persister) computes the same per-bar OFI
  the paper engine already recomputes inline (`_load_microstructure_series`), so it's a
  write-only duplicate. Decision needed: either point the paper engine at the persisted store
  (removes the duplicate compute, but changes the data path — validate equivalence on real
  data first) or drop the persister + its compose service. Left intact pending that call.
- **P1-6 observability bring-up**: prometheus+grafana are declared in compose but need a real
  Docker host to verify containers start and scraping works; add Grafana dashboards/datasource
  provisioning once running. Other FastAPI services still need to adopt `shared/metrics.py` to
  appear (scrape jobs already declared).
- **P0-3 live bracket lifecycle** needs real-exchange verification before any live use
  (cannot be integration-tested here): native stop placement on the *WebSocket* fill
  path (currently wired only on the immediate-FILLED branch), OCO pairing with take-profit,
  partial-fill stop resizing, and de-duplication against the in-process monitor so a
  fired native stop and the monitor don't both try to close. Capability + unit tests are
  in place; the live wiring is best-effort and gated (never runs under NOTIFY_ONLY).
