"""
PaperStrategyEngine (Phase 4) — wires the sel_v2 state machine + strategy filters +
dual sub-accounts into a single bar-driven engine.

The async PaperEngine (paper_engine.py) previously had a stub `_process_bar`. This module is
the brain it was missing: given a frame of 4H bars it runs, per bar,

    StateRecognizer  ->  Strategy1/2 entry filters  ->  exit checks  ->  DualSubAccountEngine

reusing the existing, tested strategy modules verbatim (it does not reimplement any decision
logic). It is deliberately synchronous and side-effect free so it can be unit-tested without a
DB/Redis; the async engine calls `process_frame()` and persists the resulting positions/trades.

Feature series (σ, Hawkes BR, TDA L¹) are built with the same precompute functions the offline
replay uses, so paper and replay produce identical states for identical input.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from sel_v2.scheduler.bar_runner import BarRunner
from sel_v2.strategies.cusum_short import CUSUMShort, CUSUMTrigger
from sel_v2.strategies.strategy1_entry import Strategy1EntryFilter
from sel_v2.strategies.strategy2_entry import Strategy2EntryFilter
from sel_v2.strategies.strategy_exit import check_strategy1_exit, check_strategy2_exit
from sel_v2.strategies.sub_account import DualSubAccountEngine

logger = logging.getLogger("paper_strategy_engine")

_SIGMA_WINDOW = 180

# Strategy-2 inverse-vocab classifier thresholds (v1 heuristics on real tick/LOB
# microstructure — documented & replaceable). See _micro_vocab_series.
_VOCAB_VOL_WINDOW = 60      # bars for the rolling taker-volume percentile baseline
_VOCAB_VOL_Q = 0.80        # taker volume above this percentile = "high activity"
_SWEEP_MOVE = 0.005        # |bar return| ≥ 0.5% with flow → aggressive Sweep
_ABSORB_MOVE = 0.0015      # high volume but |return| ≤ 0.15% → flow Absorbed
_OFI_PERSIST_BARS = 2      # net taker flow same direction for this many bars
_TYPE_A_SEQ_BARS = 6       # an Absorption within this many bars before a Sweep → Type A


# Inlined from sel_v2/scheduler/replay.py (kept identical) so this engine is importable
# without ripser when TDA is disabled — replay imports tda_critical at module load.
def precompute_sigma_series(close: np.ndarray):
    n = len(close)
    log_returns = np.diff(np.log(close))
    sigma = np.full(n, np.nan)
    sigma_pctile = np.full(n, np.nan)
    for i in range(_SIGMA_WINDOW, n):
        sigma[i] = float(np.std(log_returns[i - _SIGMA_WINDOW: i]))
    for i in range(_SIGMA_WINDOW * 2, n):
        window = sigma[i - _SIGMA_WINDOW: i]
        valid = window[np.isfinite(window)]
        if len(valid) > 0:
            sigma_pctile[i] = float(np.mean(valid <= sigma[i]))
    return sigma, sigma_pctile


def precompute_tda_pctile_series(tda_l1: np.ndarray, pctile_window: int = 540, q: float = 0.95):
    n = len(tda_l1)
    pctile = np.full(n, np.nan)
    for i in range(pctile_window, n):
        seg = tda_l1[i - pctile_window: i]
        valid = seg[np.isfinite(seg) & (seg > 0)]
        if len(valid) >= 10:
            pctile[i] = float(np.mean(valid <= tda_l1[i]))
    return pctile


@dataclass
class _S1Meta:
    direction: str
    entry_price: float
    entry_time: object
    critical_reduced: bool = False


@dataclass
class _S2Meta:
    direction: str
    entry_price: float
    entry_time: object
    cusum_peak: float = 0.0
    batches: int = 0


@dataclass
class PaperStrategyEngine:
    total_nav_usdt: float = 100_000.0
    instrument: str = "BTC-USDT"
    hawkes_threshold: float = 0.85
    tda_threshold: float = 0.000097
    skip_hawkes: bool = False
    skip_tda: bool = False
    # Strategy-2 Hawkes H1 cold-start params (mu, alpha, beta). If None, the tracker loads
    # them from v2_strategy_params (production). Injectable so the engine runs without a DB.
    hawkes_params: Optional[tuple] = None

    accounts: DualSubAccountEngine = field(init=False)
    _s1_filter: Strategy1EntryFilter = field(init=False)
    _s2_filter: Strategy2EntryFilter = field(init=False)
    _s2_cusum: CUSUMShort = field(init=False)
    _s1_meta: dict = field(init=False, default_factory=dict)
    _s2_meta: dict = field(init=False, default_factory=dict)
    _prev_state: Optional[str] = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.accounts = DualSubAccountEngine(total_nav_usdt=self.total_nav_usdt)
        self._s1_filter = Strategy1EntryFilter()
        # Strategy 2 needs H2 Hawkes calibration (mu/alpha/beta) from v2_strategy_params.
        # If those rows are absent (Wave 1 calibration not run), DISABLE S2 loudly rather
        # than crash the whole engine or silently fabricate params (the design forbids a
        # silent fallback). Strategy 1 (CUSUM + state machine) does not need them and runs.
        self._s2_enabled = True
        try:
            from sel_v2.strategies.hawkes_intensity import HawkesParams, HawkesIntensityTracker
            if self.hawkes_params is not None:
                mu, alpha, beta = self.hawkes_params
                params = HawkesParams(mu=mu, alpha=alpha, beta=beta)
            else:
                params = HawkesParams.from_h2_reference()   # reads v2_strategy_params
            # store_history=False: the per-bar tick feed (process_frame) pushes the
            # full tick stream through this tracker, so unbounded history would blow up.
            self._s2_tracker = HawkesIntensityTracker(params, store_history=False)
            self._s2_filter = Strategy2EntryFilter(hawkes_tracker=self._s2_tracker)
        except Exception as exc:  # noqa: BLE001
            self._s2_enabled = False
            self._s2_filter = None
            self._s2_tracker = None
            logger.warning("Strategy 2 disabled — H2 Hawkes params unavailable (%s). "
                           "Run Wave 1 hawkes_calibration to enable S2; S1 continues.", exc)
        self._s2_cusum = CUSUMShort()

    # ── feature pipeline (mirrors replay.run_replay) ───────────────────────────
    def _build_runner(self, df: pd.DataFrame, oi_series=None, funding_series=None,
                      ofi_series=None, lob_depth_series=None) -> tuple[BarRunner, np.ndarray, np.ndarray]:
        closes = df["close"].values.astype(float)
        n = len(closes)
        sigma_series, sigma_pctile = precompute_sigma_series(closes)

        if self.skip_hawkes:
            hawkes_br = np.full(n, np.nan)
        else:
            from sel_v2.states.hawkes_critical import precompute_branching_ratios
            hawkes_br = precompute_branching_ratios(closes)

        if self.skip_tda:
            tda_l1 = np.full(n, np.nan)
            tda_pctile = np.full(n, np.nan)
        else:
            from sel_v2.states.tda_critical import precompute_tda_l1
            tda_l1 = precompute_tda_l1(closes)
            tda_pctile = precompute_tda_pctile_series(tda_l1)

        runner = BarRunner.from_precomputed(
            df=df, sigma_series=sigma_series, sigma_pctile_series=sigma_pctile,
            hawkes_br_series=hawkes_br, tda_l1_series=tda_l1, tda_l1_pctile_series=tda_pctile,
            hawkes_br_threshold=self.hawkes_threshold, tda_l1_threshold=self.tda_threshold,
            oi_series=oi_series, funding_series=funding_series, ofi_proxy_series=ofi_series,
            lob_depth_series=lob_depth_series,
        )
        log_returns = np.diff(np.log(closes), prepend=np.log(closes[0]))
        return runner, sigma_series, log_returns

    @staticmethod
    def _strategy_label(state: str) -> str:
        """Adapt the recognizer's StateLabel value to the label convention the strategy
        entry/exit modules compare against. The recognizer emits 'Drifting_Calm'/'Drifting_Charged'
        (underscore) but the strategies test for 'Drifting-Calm'/'Drifting-Charged' (hyphen);
        without this bridge S1 can never enter via Drifting-Charged and exit branches misfire."""
        return state.replace("Drifting_", "Drifting-")

    @staticmethod
    def _zscore(log_return: float, sigma: float) -> float:
        if not np.isfinite(sigma) or sigma <= 0:
            return 0.0
        return float(log_return / sigma)

    def _s1_trigger_from_decision(self, dec) -> CUSUMTrigger:
        """Reconstruct the CUSUM-Mid trigger the entry filter computed this bar (so the exit
        checker sees the same trigger without double-updating the accumulator)."""
        triggered = dec.action in ("ENTER_LONG", "ENTER_SHORT")
        direction = dec.direction if triggered else None
        return CUSUMTrigger(
            triggered=triggered, direction=direction,
            cusum_positive=dec.cusum_positive, cusum_negative=dec.cusum_negative,
            threshold=dec.cusum_threshold, intensity_coeff=0.0,
        )

    def _micro_vocab_series(self, df: pd.DataFrame, micro: Optional[dict]):
        """Per-bar Strategy-2 inputs from real microstructure (item: S2 vocab wiring).

        Returns (vocab_list, flow_dir) where vocab_list[i] is a set of inverse-vocab
        tags and flow_dir[i] ∈ {-1,0,+1} is the net taker-flow sign. v1 heuristics:
          Sweep      — high taker volume + a same-direction ≥0.5% bar move (aggressive
                       consumption sweeping levels).
          Absorption — high taker volume but ≤0.15% move (flow absorbed by limits).
          Crowding   — net taker flow the same direction for ≥ _OFI_PERSIST_BARS bars.
        All empty when no tick/LOB data covers the bar (conservative)."""
        n = len(df)
        vocab: list[set] = [set() for _ in range(n)]
        flow_dir = np.zeros(n)
        if micro is None:
            return vocab, flow_dir

        taker_net = micro.get("taker_net")
        taker_vol = micro.get("taker_vol")
        lob_imb = micro.get("lob_imb")
        close = df["close"].values.astype(float)
        open_ = df["open"].values.astype(float)

        # Flow direction: taker net if present, else LOB imbalance.
        for i in range(n):
            v = taker_net[i] if (taker_net is not None and np.isfinite(taker_net[i])) else (
                lob_imb[i] if (lob_imb is not None and np.isfinite(lob_imb[i])) else np.nan)
            flow_dir[i] = 0.0 if not np.isfinite(v) else float(np.sign(v))

        for i in range(n):
            if taker_vol is None or not np.isfinite(taker_vol[i]):
                continue
            lo = max(0, i - _VOCAB_VOL_WINDOW)
            window = taker_vol[lo:i]
            window = window[np.isfinite(window)]
            if len(window) < 10:
                continue
            vol_high = taker_vol[i] >= np.quantile(window, _VOCAB_VOL_Q)
            pm = (close[i] - open_[i]) / open_[i] if open_[i] else 0.0
            if vol_high and abs(pm) >= _SWEEP_MOVE and np.sign(pm) == flow_dir[i] and flow_dir[i] != 0:
                vocab[i].add("Sweep")
            if vol_high and abs(pm) <= _ABSORB_MOVE:
                vocab[i].add("Absorption")
            # Crowding: persistent same-direction net flow.
            if flow_dir[i] != 0 and i >= _OFI_PERSIST_BARS - 1:
                if all(flow_dir[i - k] == flow_dir[i] for k in range(_OFI_PERSIST_BARS)):
                    vocab[i].add("Crowding")

        # Type-A sequence: an Absorption (large limit orders soak up flow) followed
        # within _TYPE_A_SEQ_BARS by a Sweep is the canonical reversal setup. Carry
        # the recent Absorption onto the Sweep bar so its vocab = {Absorption, Sweep},
        # which Strategy2._classify_entry_type reads as Type A (reversal).
        for i in range(n):
            if "Sweep" not in vocab[i]:
                continue
            lo = max(0, i - _TYPE_A_SEQ_BARS)
            if any("Absorption" in vocab[j] for j in range(lo, i)):
                vocab[i].add("Absorption")
        return vocab, flow_dir

    # ── per-bar processing ─────────────────────────────────────────────────────
    def process_frame(self, df: pd.DataFrame, oi_series=None, funding_series=None,
                      ofi_series=None, tick_times=None, micro=None) -> dict:
        """Run the full engine over a frame of 4H bars (ascending by time).
        Optional OI/funding/OFI series (helixa-derived) unlock the Coiling / Drifting-Charged
        entry states. ``tick_times`` (sorted Unix-second array of trade arrivals) drives the
        Strategy-2 H1 Hawkes intensity so it reflects real trade clustering instead of a flat
        baseline. Returns a summary dict; positions/PnL live in `self.accounts`."""
        df = df.sort_values("time").reset_index(drop=True)
        # Total top-of-book depth per bar (bid+ask) feeds the Cascade thin-book condition
        # (audit P1-3); None when LOB data isn't flowing → cond stays None (conservative).
        lob_depth_series = micro.get("lob_depth") if micro else None
        runner, sigma_series, log_returns = self._build_runner(
            df, oi_series, funding_series, ofi_series, lob_depth_series=lob_depth_series)
        n = len(df)
        states: list[str] = []
        self.records = []   # StateRecord per bar, for DB persistence (item #6)

        # Tick stream for the H1 Hawkes feed (point-in-time: only ticks ≤ bar time).
        tick_times = None if tick_times is None else list(tick_times)
        tick_idx = 0
        n_ticks = 0 if tick_times is None else len(tick_times)

        # Per-bar Strategy-2 inverse-vocab + flow direction from real microstructure.
        vocab_series, flow_dir = self._micro_vocab_series(df, micro)

        for i in range(n):
            rec = runner.process_bar(i)
            self.records.append(rec)
            raw_state = rec.state.value
            state = self._strategy_label(raw_state)   # strategy-convention label
            ts = rec.timestamp
            mark = float(df["close"].iloc[i])
            z_t = self._zscore(float(log_returns[i]), float(sigma_series[i]))
            t_unix = ts.timestamp() if hasattr(ts, "timestamp") else float(i)

            # Accrue one bar of funding on positions already open (entered in a
            # prior bar) before this bar's opens/exits, so funding cost is deducted
            # from realised PnL at exit. No-op without a funding series.
            self._accrue_funding(funding_series, i)

            # Feed real trade arrivals up to this bar time into the S2 Hawkes tracker,
            # so λ*(t) captures recent trade clustering (item: tick→H1 wiring).
            if self._s2_enabled and tick_times is not None and self._s2_tracker is not None:
                while tick_idx < n_ticks and tick_times[tick_idx] <= t_unix:
                    self._s2_tracker.add_event(float(tick_times[tick_idx]))
                    tick_idx += 1

            # ── Strategy 1: entry filter updates CUSUM-Mid once, gives decision ──
            s1_dec = self._s1_filter.evaluate(
                bar_timestamp=ts if hasattr(ts, "timestamp") else _as_dt(ts),
                z_t=z_t, state_4h=state, current_duration_4h=rec.duration_4h,
                subaccount_nav_usdt=self.accounts.subaccount_1.nav,
            )
            s1_trig = self._s1_trigger_from_decision(s1_dec)
            self._manage_s1_exits(state, mark, ts, s1_trig)
            self._maybe_open_s1(s1_dec, state, mark, ts)

            # ── Strategy 2: engine-owned CUSUM-Short feeds the filter and exits ──
            if self._s2_enabled:
                s2_trig = self._s2_cusum.update(z_t, t_unix)
                self._manage_s2_exits(state, mark, ts, s2_trig)
                self._maybe_open_s2(t_unix, s2_trig, state, mark, ts,
                                    vocab=vocab_series[i], flow_dir=flow_dir[i])

            # ── portfolio-wide cascade red line ──
            if state == "Cascade":
                for closed in self.accounts.cascade_close_all(mark, ts):
                    self._forget(closed.position.id)

            self._prev_state = state
            states.append(raw_state)

        return self._summary(df, states)

    def bar_signal_series(self, df: pd.DataFrame, oi_series=None, funding_series=None,
                          ofi_series=None) -> np.ndarray:
        """Per-bar CUSUM-Short z-signal (standardised log return) — the momentum
        signal that feeds both strategies' triggers. Exposed so its predictive edge
        (IC / hit-rate vs forward returns) can be measured. Same feature pipeline as
        process_frame; no trades are taken."""
        df = df.sort_values("time").reset_index(drop=True)
        _, sigma_series, log_returns = self._build_runner(df, oi_series, funding_series, ofi_series)
        n = len(df)
        sig = np.full(n, np.nan)
        for i in range(n):
            sig[i] = self._zscore(float(log_returns[i]), float(sigma_series[i]))
        return sig

    # ── funding accrual ─────────────────────────────────────────────────────────
    def _accrue_funding(self, funding_series, i: int) -> None:
        """Accrue bar i's funding rate onto every open position. funding_series is
        the per-bar (as-of) funding rate aligned to the bar grid; NaN/None means
        unknown → no accrual (conservative)."""
        if funding_series is None:
            return
        try:
            fr = float(funding_series[i])
        except (IndexError, TypeError, ValueError):
            return
        if not math.isfinite(fr):
            return
        for acct in (self.accounts.subaccount_1, self.accounts.subaccount_2):
            for pos in acct.open_positions:
                pos.accrue_funding(fr)

    # ── Strategy 1 helpers ─────────────────────────────────────────────────────
    def _maybe_open_s1(self, dec, state, mark, ts) -> None:
        if dec.action not in ("ENTER_LONG", "ENTER_SHORT"):
            return
        acct = self.accounts.subaccount_1
        if not acct.can_open():
            return
        size_pct = max(0.0, dec.base_size_pct * dec.size_modifier)
        if size_pct <= 0:
            return
        pos = acct.open_position(
            direction=dec.direction, entry_price=mark, size_pct=size_pct,
            leverage=dec.suggested_leverage, instrument=self.instrument,
            entry_time=ts, entry_state=state,
        )
        if pos:
            self._s1_meta[pos.id] = _S1Meta(dec.direction, mark, ts)
            logger.info("S1 OPEN %s %s size%%=%.3f lev=%.1f @ %.2f",
                        dec.direction, state, size_pct, dec.suggested_leverage, mark)

    def _manage_s1_exits(self, state, mark, ts, trig) -> None:
        acct = self.accounts.subaccount_1
        for pos in list(acct.open_positions):
            meta = self._s1_meta.get(pos.id)
            if meta is None:
                continue
            dec = check_strategy1_exit(
                direction=meta.direction, entry_price=meta.entry_price, entry_time=meta.entry_time,
                mark_price=mark, current_time=ts, state_4h=state, cusum_trigger=trig,
                prev_state_4h=self._prev_state, critical_already_reduced=meta.critical_reduced,
            )
            self._apply_exit(acct, pos.id, dec, mark, ts, is_s1=True)

    # ── Strategy 2 helpers ─────────────────────────────────────────────────────
    def _maybe_open_s2(self, t_unix, trig, state, mark, ts, vocab=None, flow_dir=0.0) -> None:
        # OFI persistently same direction as the CUSUM signal: real taker-flow sign
        # (flow_dir, already persistence-gated via the Crowding tag) agrees with the
        # CUSUM direction. None when there's no flow data → Type B stays conservative.
        ofi_persist = None
        if trig.triggered and flow_dir != 0 and trig.direction in ("LONG", "SHORT"):
            cusum_sign = 1.0 if trig.direction == "LONG" else -1.0
            ofi_persist = ("Crowding" in (vocab or set())) and (flow_dir == cusum_sign)
        dec = self._s2_filter.evaluate(
            t=t_unix, cusum_trigger=trig, state_4h=state,
            inverse_vocab=list(vocab) if vocab else None,
            ofi_persistent_same_direction=ofi_persist,
            subaccount_nav_usdt=self.accounts.subaccount_2.nav,
        )
        if dec.action not in ("ENTER_LONG", "ENTER_SHORT"):
            return
        acct = self.accounts.subaccount_2
        if not acct.can_open():
            return
        direction = "LONG" if dec.action == "ENTER_LONG" else "SHORT"
        # base_size_pct now carries the §14.4 base (10%) scaled by CUSUM intensity;
        # the flat 10% is only a defensive floor if a decision lacks it entirely.
        size_pct = max(0.0, getattr(dec, "base_size_pct", 0.0) or 0.0)
        if size_pct <= 0:
            size_pct = 0.10  # §14.4 base = 10% of sub-account-2
        pos = acct.open_position(
            direction=direction, entry_price=mark, size_pct=size_pct,
            leverage=dec.suggested_leverage, instrument=self.instrument,
            entry_time=ts, entry_state=state,
        )
        if pos:
            self._s2_meta[pos.id] = _S2Meta(direction, mark, ts,
                                            cusum_peak=abs(trig.cusum_positive if direction == "LONG"
                                                           else trig.cusum_negative))
            logger.info("S2 OPEN %s %s lev=%.1f @ %.2f", direction, state, dec.suggested_leverage, mark)

    def _manage_s2_exits(self, state, mark, ts, trig) -> None:
        acct = self.accounts.subaccount_2
        for pos in list(acct.open_positions):
            meta = self._s2_meta.get(pos.id)
            if meta is None:
                continue
            cval = trig.cusum_positive if meta.direction == "LONG" else trig.cusum_negative
            meta.cusum_peak = max(meta.cusum_peak, abs(cval))
            dec = check_strategy2_exit(
                direction=meta.direction, entry_price=meta.entry_price, entry_time=meta.entry_time,
                mark_price=mark, current_time=ts, state_4h=state, cusum_trigger=trig,
                cusum_peak_since_entry=meta.cusum_peak, batches_triggered=meta.batches,
            )
            applied = self._apply_exit(acct, pos.id, dec, mark, ts, is_s1=False)
            if applied == "batch":
                meta.batches += 1
                if meta.batches >= 3 and acct.close_position(pos.id, mark, ts, "batch_exit_final"):
                    # third 1/3 batch — close the residual so the position terminates cleanly
                    self._forget(pos.id)

    # ── shared exit application ────────────────────────────────────────────────
    def _apply_exit(self, acct, pos_id, dec, mark, ts, *, is_s1: bool) -> Optional[str]:
        action = dec.action
        if action in ("HOLD", "OBSERVE"):
            return None
        if action == "EXIT_FULL":
            closed = acct.close_position(pos_id, mark, ts, dec.reason)
            if closed:
                self._forget(pos_id)
            return "full"
        if action in ("REDUCE_50",):
            acct.reduce_position(pos_id, dec.partial_fraction or 0.5, mark, ts, dec.reason)
            if is_s1 and pos_id in self._s1_meta:
                self._s1_meta[pos_id].critical_reduced = True
            return "reduce"
        if action in ("EXIT_BATCH_33",):
            acct.reduce_position(pos_id, dec.partial_fraction or (1 / 3), mark, ts, dec.reason)
            # caller (_manage_s2_exits) closes the residual after the third batch
            return "batch"
        # unknown action -> safest is full exit
        closed = acct.close_position(pos_id, mark, ts, dec.reason)
        if closed:
            self._forget(pos_id)
        return "full"

    def _forget(self, pos_id) -> None:
        self._s1_meta.pop(pos_id, None)
        self._s2_meta.pop(pos_id, None)

    def _summary(self, df, states) -> dict:
        from collections import Counter
        mark = float(df["close"].iloc[-1])
        a1, a2 = self.accounts.subaccount_1, self.accounts.subaccount_2
        return {
            "bars": len(df),
            "state_counts": dict(Counter(states)),
            "s1": {"nav": round(a1.nav, 2), "open": len(a1.open_positions),
                   "closed": len(a1.closed_positions)},
            "s2": {"nav": round(a2.nav, 2), "open": len(a2.open_positions),
                   "closed": len(a2.closed_positions)},
            "total_equity": round(self.accounts.total_equity(mark), 2),
        }


def _as_dt(ts):
    return pd.Timestamp(ts).to_pydatetime()
