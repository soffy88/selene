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

import datetime as _dt

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from sel_v2.runtime.staleness import StalenessEnforcement
from sel_v2.scheduler.bar_runner import BarRunner
from sel_v2.strategies.cusum_short import CUSUMShort, CUSUMTrigger
from sel_v2.strategies.inverse_vocab import (
    AbsorptionSignal,
    SweepSignal,
    adaptive_percentile,
    detect_absorption,
    detect_sweep,
)
from sel_v2.strategies.strategy1_entry import Strategy1EntryFilter
from sel_v2.strategies.strategy2_entry import Strategy2EntryFilter
from sel_v2.strategies.strategy_exit import (
    ExitDecision,
    _P_CUSUM_REVERSE,
    _P_HOLD,
    check_strategy1_exit,
    check_strategy2_exit,
)
from sel_v2.strategies.sub_account import DualSubAccountEngine

logger = logging.getLogger("paper_strategy_engine")

_SIGMA_WINDOW = 180

# Strategy-2 inverse-vocab classifier thresholds (v1 heuristics on real tick/LOB
# microstructure — documented & replaceable). See _micro_vocab_series.
_VOCAB_VOL_WINDOW = 60  # bars for the rolling taker-volume percentile baseline
_VOCAB_VOL_Q = 0.80  # taker volume above this percentile = "high activity"
_SWEEP_MOVE = 0.005  # |bar return| ≥ 0.5% with flow → aggressive Sweep
_ABSORB_MOVE = 0.0015  # high volume but |return| ≤ 0.15% → flow Absorbed
_OFI_PERSIST_BARS = 2  # net taker flow same direction for this many bars
_TYPE_A_SEQ_BARS = 6  # an Absorption within this many bars before a Sweep → Type A

# Wave S2C — direction-aware §14.2 detection (inverse_vocab.py) at 4H-bar granularity.
_ATR_WINDOW = 14  # bars for the ATR normaliser in Absorption's price_response
_SWEEP_LOOKBACK_BARS = 12  # 48h / 4h — the "past 48h high/low" a Sweep must touch
_VOCAB_HIST_WINDOW = (
    180  # trailing window feeding the adaptive tf_net / price_response / vol pctiles
)
_LIQ_PULSE_FLOOR_BTC = (
    50.0  # Step-4 absolute liquidation-pulse floor while the pctile is cold
)

# Cache for the expensive price-only per-bar series (σ / Hawkes BR / TDA L¹), keyed on a
# closes signature. The full-history replay runs on every tick; these series only change when a
# new 4H bar seals, so caching skips the heavy TDA(ripser)/Hawkes recompute on every tick (#4).
_PRECOMPUTE_CACHE: dict = {"sig": None, "data": None}


# Inlined from sel_v2/scheduler/replay.py (kept identical) so this engine is importable
# without ripser when TDA is disabled — replay imports tda_critical at module load.
def precompute_sigma_series(close: np.ndarray):
    n = len(close)
    log_returns = np.diff(np.log(close))
    sigma = np.full(n, np.nan)
    sigma_pctile = np.full(n, np.nan)
    for i in range(_SIGMA_WINDOW, n):
        sigma[i] = float(np.std(log_returns[i - _SIGMA_WINDOW : i]))
    for i in range(_SIGMA_WINDOW * 2, n):
        window = sigma[i - _SIGMA_WINDOW : i]
        valid = window[np.isfinite(window)]
        if len(valid) > 0:
            sigma_pctile[i] = float(np.mean(valid <= sigma[i]))
    return sigma, sigma_pctile


def precompute_tda_pctile_series(
    tda_l1: np.ndarray, pctile_window: int = 540, q: float = 0.95
):
    n = len(tda_l1)
    pctile = np.full(n, np.nan)
    for i in range(pctile_window, n):
        seg = tda_l1[i - pctile_window : i]
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
    # Wave S2G: a SECOND accumulator, fed the 1s series, drives ENTRY only.
    # _s2_cusum above stays on the per-bar cadence because _manage_s2_exits
    # advances meta.cusum_peak from its trigger on every bar — moving it to 1s
    # would silently mis-level the CUSUM-decay/reversal exits with no error.
    _s2_cusum_1s: CUSUMShort = field(init=False)
    # Event layer is INJECTED, not constructed here: a fresh engine is built every
    # replay cycle (paper_engine._reprocess_inner), so cluster/daily-cap state kept
    # here would be amnesiac and re-emit. It hangs off the long-lived PaperEngine.
    s2_event_layer: object = None
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
            from sel_v2.strategies.hawkes_intensity import (
                HawkesParams,
                HawkesIntensityTracker,
            )

            if self.hawkes_params is not None:
                mu, alpha, beta = self.hawkes_params
                params = HawkesParams(mu=mu, alpha=alpha, beta=beta)
            else:
                params = HawkesParams.from_h2_reference()  # reads v2_strategy_params
            # store_history=False: the per-bar tick feed (process_frame) pushes the
            # full tick stream through this tracker, so unbounded history would blow up.
            self._s2_tracker = HawkesIntensityTracker(params, store_history=False)
            self._s2_filter = Strategy2EntryFilter(hawkes_tracker=self._s2_tracker)
        except Exception as exc:  # noqa: BLE001
            self._s2_enabled = False
            self._s2_filter = None
            self._s2_tracker = None
            logger.warning(
                "Strategy 2 disabled — H2 Hawkes params unavailable (%s). "
                "Run Wave 1 hawkes_calibration to enable S2; S1 continues.",
                exc,
            )
        self._s2_cusum = CUSUMShort()
        self._s2_cusum_1s = CUSUMShort()
        # S2_EVENTs produced this replay: (event, throttle_reason_or_None)
        self._s2_events = []
        # Latest per-strategy entry decision (action/reason/step) — for the "why no entry"
        # UI panel. Captured on the final bar of each replay.
        self._last_s1 = None
        self._last_s2 = None
        # Decision trails: (re)initialised per replay in process_frame; also set here so
        # the open/exit helpers are callable directly (unit tests drive them without a
        # frame — _maybe_open_s2 appended to a nonexistent attribute and crashed).
        self._s1_trail = []
        self._s2_trail = []
        self._cusum_events = []  # (ts, cusum_type, direction, peak, threshold, z_window)
        self._vocab_events = []  # (ts, vocab, direction, state, details) → v2_inverse_vocab_events

    # ── feature pipeline (mirrors replay.run_replay) ───────────────────────────
    def _precompute_price_features(self, closes):
        """The expensive, price-only per-bar series (σ, Hawkes branching ratio, TDA L¹).
        These are pure functions of `closes` and depend only on trailing windows, so for a
        sealed bar their value never changes — yet the full-history replay recomputed them on
        EVERY tick (TDA via ripser over all ~4500 bars was the hog). Cache by a closes signature
        and reuse when the price history is unchanged (the dominant case: ticks arrive far more
        often than a new 4H bar), so the heavy TDA/Hawkes only runs when a bar actually sealed."""
        import numpy as _np

        n = len(closes)
        sig = (
            n,
            float(closes[0]) if n else 0.0,
            float(closes[-1]) if n else 0.0,
            self.skip_hawkes,
            self.skip_tda,
        )
        cached = _PRECOMPUTE_CACHE.get("sig")
        if cached == sig:
            return _PRECOMPUTE_CACHE["data"]

        sigma_series, sigma_pctile = precompute_sigma_series(closes)
        if self.skip_hawkes:
            hawkes_br = _np.full(n, _np.nan)
        else:
            from sel_v2.states.hawkes_critical import precompute_branching_ratios

            hawkes_br = precompute_branching_ratios(closes)
        if self.skip_tda:
            tda_l1 = _np.full(n, _np.nan)
            tda_pctile = _np.full(n, _np.nan)
        else:
            from sel_v2.states.tda_critical import precompute_tda_l1

            tda_l1 = precompute_tda_l1(closes)
            tda_pctile = precompute_tda_pctile_series(tda_l1)

        data = (sigma_series, sigma_pctile, hawkes_br, tda_l1, tda_pctile)
        _PRECOMPUTE_CACHE["sig"] = sig
        _PRECOMPUTE_CACHE["data"] = data
        return data

    def _build_runner(
        self,
        df: pd.DataFrame,
        oi_series=None,
        funding_series=None,
        ofi_series=None,
        lob_depth_series=None,
        entropy_series=None,
    ) -> tuple[BarRunner, np.ndarray, np.ndarray]:
        closes = df["close"].values.astype(float)
        sigma_series, sigma_pctile, hawkes_br, tda_l1, tda_pctile = (
            self._precompute_price_features(closes)
        )

        runner = BarRunner.from_precomputed(
            df=df,
            sigma_series=sigma_series,
            sigma_pctile_series=sigma_pctile,
            hawkes_br_series=hawkes_br,
            tda_l1_series=tda_l1,
            tda_l1_pctile_series=tda_pctile,
            hawkes_br_threshold=self.hawkes_threshold,
            tda_l1_threshold=self.tda_threshold,
            oi_series=oi_series,
            funding_series=funding_series,
            ofi_proxy_series=ofi_series,
            lob_depth_series=lob_depth_series,
            entropy_series=entropy_series,
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
            triggered=triggered,
            direction=direction,
            cusum_positive=dec.cusum_positive,
            cusum_negative=dec.cusum_negative,
            threshold=dec.cusum_threshold,
            intensity_coeff=0.0,
        )

    def _collect_cusum_cross(
        self, ts, cusum_type: str, c_pos: float, c_neg: float, threshold: float
    ) -> None:
        """Record a CUSUM threshold-cross event into self._cusum_events, mirroring
        CUSUMShort.update's trigger judgment (C+ > h → 'up', C- > h → 'down', dominant on
        tie). Used for both accumulators: 'short' (S2, engine-owned) and 'mid' (S1, from the
        entry filter's reported C±, so strategy1_entry stays untouched). Guards threshold>0
        so the CUSUM-Mid cold-start default (0.0, set on bars that never reach S1 Step 3)
        emits nothing. Idempotent-keyed by (ts, cusum_type) at persist time."""
        if threshold is None or threshold <= 0:
            return
        up, down = c_pos > threshold, c_neg > threshold
        if up and down:  # both crossed → dominant excursion (as in CUSUMShort.update)
            if c_pos >= c_neg:
                down = False
            else:
                up = False
        if up:
            self._cusum_events.append(
                (_as_dt(ts), cusum_type, "up", float(c_pos), float(threshold), None)
            )
        elif down:
            self._cusum_events.append(
                (_as_dt(ts), cusum_type, "down", float(c_neg), float(threshold), None)
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
            v = (
                taker_net[i]
                if (taker_net is not None and np.isfinite(taker_net[i]))
                else (
                    lob_imb[i]
                    if (lob_imb is not None and np.isfinite(lob_imb[i]))
                    else np.nan
                )
            )
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
            if (
                vol_high
                and abs(pm) >= _SWEEP_MOVE
                and np.sign(pm) == flow_dir[i]
                and flow_dir[i] != 0
            ):
                vocab[i].add("Sweep")
            if vol_high and abs(pm) <= _ABSORB_MOVE:
                vocab[i].add("Absorption")
            # Crowding: persistent same-direction net flow.
            if flow_dir[i] != 0 and i >= _OFI_PERSIST_BARS - 1:
                if all(
                    flow_dir[i - k] == flow_dir[i] for k in range(_OFI_PERSIST_BARS)
                ):
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

    def _inverse_vocab_signals(self, df: pd.DataFrame, micro: Optional[dict]):
        """Per-bar direction-aware Absorption/Sweep signals (§14.2, Wave S2C) for Strategy 2.

        Returns (absorptions, sweeps): lists of AbsorptionSignal / SweepSignal, one per bar,
        computed at 4H-bar granularity from data already loaded — bar OHLCV plus the taker
        flow in `micro`. No sub-bar tick queries: in the paper replay a CUSUM trigger lands on
        a bar boundary, so the bar *is* the trigger window. Percentile thresholds are adaptive
        (inverse_vocab.adaptive_percentile) over a trailing window, so a feed with only a few
        days of history abstains (absent) instead of asserting a signal — Absorption warms up
        once ~30 tick-bars accrue; Sweep uses bar volume (full history) and can fire sooner."""
        n = len(df)
        empty_abs = [AbsorptionSignal(present=False) for _ in range(n)]
        empty_swp = [SweepSignal(present=False) for _ in range(n)]
        if n == 0:
            return empty_abs, empty_swp

        high = df["high"].values.astype(float)
        low = df["low"].values.astype(float)
        close = df["close"].values.astype(float)
        open_ = df["open"].values.astype(float)
        volume = df["volume"].values.astype(float)

        # ATR (true range, rolling mean) — the price_response normaliser.
        prev_close = np.concatenate([[close[0]], close[:-1]])
        tr = np.maximum.reduce(
            [high - low, np.abs(high - prev_close), np.abs(low - prev_close)]
        )
        atr = pd.Series(tr).rolling(_ATR_WINDOW, min_periods=1).mean().values

        taker_net = micro.get("taker_net") if micro else None
        taker_vol = micro.get("taker_vol") if micro else None

        # Pre-compute the per-bar scalars so the adaptive histories are simple trailing slices.
        tf_net = np.full(n, np.nan)
        price_resp = np.full(n, np.nan)
        for i in range(n):
            if (
                taker_net is not None
                and taker_vol is not None
                and np.isfinite(taker_vol[i])
                and taker_vol[i] > 0
            ):
                tf_net[i] = abs(taker_net[i]) / taker_vol[i]
            if atr[i] > 0:
                price_resp[i] = abs(close[i] - open_[i]) / atr[i]

        absorptions, sweeps = list(empty_abs), list(empty_swp)
        for i in range(n):
            lo_h = max(0, i - _VOCAB_HIST_WINDOW)
            if taker_vol is not None and np.isfinite(taker_vol[i]):
                absorptions[i] = detect_absorption(
                    taker_net=float(taker_net[i]) if taker_net is not None else 0.0,
                    taker_vol=float(taker_vol[i]),
                    price_delta_abs=abs(close[i] - open_[i]),
                    atr=float(atr[i]),
                    tf_net_history=tf_net[lo_h:i],
                    price_response_history=price_resp[lo_h:i],
                )
            # Sweep: touch of the *prior* 48h extreme (exclude the current bar).
            lo_s = max(0, i - _SWEEP_LOOKBACK_BARS)
            if i > 0:
                high_48h = float(np.max(high[lo_s:i]))
                low_48h = float(np.min(low[lo_s:i]))
                reverted_high = high[i] >= high_48h and close[i] < high_48h
                reverted_low = low[i] <= low_48h and close[i] > low_48h
                sweeps[i] = detect_sweep(
                    high_48h=high_48h,
                    low_48h=low_48h,
                    touch_high=float(high[i]),
                    touch_low=float(low[i]),
                    touch_volume=float(volume[i]),
                    reverted_from_high=bool(reverted_high),
                    reverted_from_low=bool(reverted_low),
                    volume_history=volume[lo_h:i],
                )
        return absorptions, sweeps

    def _cascade_pulses(self, df: pd.DataFrame, micro: Optional[dict], oi_series):
        """Per-bar Step-4 cascade guards (§14.2, Wave S2C): (liq_pulse, oi_drop) bool arrays.

        liq_pulse — bar liquidation volume above its adaptive 30d 95th pct, OR (while the pct
                    history is still cold) above an absolute 50 BTC floor (the spec's empty-
                    window fallback). Needs `micro['liq_vol']`; all-False if the feed is absent.
        oi_drop   — bar-over-bar OI fall *rate* above its adaptive 95th pct.
        Both fire only on real evidence → False when data is cold; the Cascade *state* veto in
        Step 2 remains the hard gate, this is the finer intra-window guard."""
        n = len(df)
        liq = np.zeros(n, dtype=bool)
        oi_drop = np.zeros(n, dtype=bool)

        liq_vol = micro.get("liq_vol") if micro else None
        if liq_vol is not None:
            for i in range(n):
                if not np.isfinite(liq_vol[i]):
                    continue
                lo = max(0, i - _VOCAB_HIST_WINDOW)
                verdict = adaptive_percentile(liq_vol[lo:i], float(liq_vol[i]), 0.95)
                if verdict is True or (
                    verdict is None and liq_vol[i] >= _LIQ_PULSE_FLOOR_BTC
                ):
                    liq[i] = True

        if oi_series is not None:
            oi = np.asarray(oi_series, dtype=float)
            rate = np.full(n, np.nan)
            for i in range(1, min(n, len(oi))):
                if np.isfinite(oi[i]) and np.isfinite(oi[i - 1]) and oi[i - 1] > 0:
                    rate[i] = (oi[i - 1] - oi[i]) / oi[i - 1]  # >0 = OI fell
            for i in range(n):
                if np.isfinite(rate[i]) and rate[i] > 0:
                    lo = max(0, i - _VOCAB_HIST_WINDOW)
                    if adaptive_percentile(rate[lo:i], float(rate[i]), 0.95) is True:
                        oi_drop[i] = True
        return liq, oi_drop

    # ── per-bar processing ─────────────────────────────────────────────────────
    def process_frame(
        self,
        df: pd.DataFrame,
        oi_series=None,
        funding_series=None,
        ofi_series=None,
        tick_times=None,
        ticks_1s=None,
        micro=None,
        staleness: Optional[dict] = None,
        cross_price: Optional[tuple] = None,
    ) -> dict:
        """Run the full engine over a frame of 4H bars (ascending by time).
        Optional OI/funding/OFI series (helixa-derived) unlock the Coiling / Drifting-Charged
        entry states. ``tick_times`` (sorted Unix-second array of trade arrivals) drives the
        Strategy-2 H1 Hawkes intensity so it reflects real trade clustering instead of a flat
        baseline. ``staleness`` (GL1 T0.4) is a {source: StalenessEnforcement} dict for
        currently-stale sources only (fresh sources simply absent); it gates new entries and
        suppresses the CUSUM-reversal exit per the T0.4 matrix — this is a LIVE-cadence concept
        (age vs wall-clock now), so callers doing historical replay just pass None (nothing
        was ever "stale" relative to itself). Returns a summary dict; positions/PnL live in
        `self.accounts`."""
        staleness = staleness or {}
        block_s1_entry = staleness.get(
            "funding_oi", StalenessEnforcement("funding_oi", False)
        ).block_s1_entry
        block_s2_entry = staleness.get(
            "ticks", StalenessEnforcement("ticks", False)
        ).block_s2_entry
        pause_cusum_reversal = staleness.get(
            "ticks", StalenessEnforcement("ticks", False)
        ).pause_cusum_reversal_exit
        stale_reason_codes = sorted(
            e.reason_code for e in staleness.values() if e.stale and e.reason_code
        )
        df = df.sort_values("time").reset_index(drop=True)
        # Total top-of-book depth per bar (bid+ask) feeds the Cascade thin-book condition
        # (audit P1-3); LOB entropy per bar feeds Coiling's entropy_low condition (follow-up
        # B). None when LOB data isn't flowing → cond stays None (conservative).
        lob_depth_series = micro.get("lob_depth") if micro else None
        entropy_series = micro.get("entropy") if micro else None
        runner, sigma_series, log_returns = self._build_runner(
            df,
            oi_series,
            funding_series,
            ofi_series,
            lob_depth_series=lob_depth_series,
            entropy_series=entropy_series,
        )
        n = len(df)
        states: list[str] = []
        self.records = []  # StateRecord per bar, for DB persistence (item #6)
        self._s1_trail = []  # per-bar (ts, state, decision, snapshot) — the full decision trail (#2, GL1 T0.3)
        self._s2_trail = []
        self._cusum_events = []  # CUSUM threshold-cross events this replay → v2_cusum_events
        self._vocab_events = []  # inverse-vocab signatures this replay → v2_inverse_vocab_events

        # Tick stream for the H1 Hawkes feed (point-in-time: only ticks ≤ bar time).
        tick_times = None if tick_times is None else list(tick_times)
        tick_idx = 0
        n_ticks = 0 if tick_times is None else len(tick_times)

        # Per-bar Strategy-2 inverse-vocab + flow direction from real microstructure.
        vocab_series, flow_dir = self._micro_vocab_series(df, micro)
        # Wave S2C: direction-aware §14.2 Absorption/Sweep + Step-4 cascade pulses.
        absorptions, sweeps = self._inverse_vocab_signals(df, micro)
        liq_pulse_series, oi_drop_series = self._cascade_pulses(df, micro, oi_series)

        # cursor for the 1s entry channel: which seconds this bar covers
        prev_bar_unix = None
        for i in range(n):
            # Staleness (GL1 T0.4) is a live-cadence concept (data age vs wall-clock
            # now) — it only means something for the bar being evaluated *right now*.
            # process_frame() replays the *entire* history every cycle (idempotent
            # engine rebuild), so applying "now"'s staleness to bars from days ago
            # would incorrectly flag data that was perfectly fresh at the time.
            is_current_bar = i == n - 1
            rec = runner.process_bar(i)
            self.records.append(rec)
            raw_state = rec.state.value
            state = self._strategy_label(raw_state)  # strategy-convention label
            ts = rec.timestamp
            mark = float(df["close"].iloc[i])
            z_t = self._zscore(float(log_returns[i]), float(sigma_series[i]))
            t_unix = ts.timestamp() if hasattr(ts, "timestamp") else float(i)

            # Full numeric input snapshot for this bar (GL1 T0.3/D1): everything the
            # state machine + strategies actually read this bar, audited alongside the
            # decision it produced — not a separately-recomputed value (v2_ofi_features
            # was a write-only duplicate of this same data; see D1, STATUS.md P2-1).
            snapshot = self._bar_snapshot(
                runner.build_features(i),
                z_t,
                self._series_val(funding_series, i),
                micro,
                i,
            )
            if is_current_bar and stale_reason_codes:
                snapshot["stale_reason_codes"] = stale_reason_codes

            # Accrue one bar of funding on positions already open (entered in a
            # prior bar) before this bar's opens/exits, so funding cost is deducted
            # from realised PnL at exit. No-op without a funding series.
            self._accrue_funding(funding_series, i)

            # Feed real trade arrivals up to this bar time into the S2 Hawkes tracker,
            # so λ*(t) captures recent trade clustering (item: tick→H1 wiring).
            if (
                self._s2_enabled
                and tick_times is not None
                and self._s2_tracker is not None
            ):
                while tick_idx < n_ticks and tick_times[tick_idx] <= t_unix:
                    self._s2_tracker.add_event(float(tick_times[tick_idx]))
                    tick_idx += 1

            # ── Strategy 1: entry filter updates CUSUM-Mid once, gives decision ──
            s1_dec = self._s1_filter.evaluate(
                bar_timestamp=ts if hasattr(ts, "timestamp") else _as_dt(ts),
                z_t=z_t,
                state_4h=state,
                current_duration_4h=rec.duration_4h,
                subaccount_nav_usdt=self.accounts.subaccount_1.nav,
            )
            s1_trig = self._s1_trigger_from_decision(s1_dec)
            self._manage_s1_exits(
                state,
                mark,
                ts,
                s1_trig,
                pause_cusum_reversal=is_current_bar and pause_cusum_reversal,
            )
            self._maybe_open_s1(
                s1_dec,
                state,
                mark,
                ts,
                blocked=is_current_bar and block_s1_entry,
            )
            self._last_s1 = (
                ts,
                state,
                s1_dec,
            )  # latest S1 decision (for the UI "why" panel)
            self._s1_trail.append(
                (
                    ts,
                    state,
                    s1_dec,
                    {
                        **snapshot,
                        "cusum_positive": s1_trig.cusum_positive,
                        "cusum_negative": s1_trig.cusum_negative,
                        "cusum_threshold": s1_trig.threshold,
                    },
                )
            )
            # CUSUM-Mid trigger event (raw threshold cross reported by the S1 filter this bar,
            # independent of the downstream state/funding gates that decide entry).
            self._collect_cusum_cross(
                ts,
                "mid",
                s1_trig.cusum_positive,
                s1_trig.cusum_negative,
                s1_trig.threshold,
            )

            # ── Strategy 2: engine-owned CUSUM-Short feeds the filter and exits ──
            if self._s2_enabled:
                s2_trig = self._s2_cusum.update(z_t, t_unix)
                # CUSUM-Short trigger event (the engine-owned accumulator's threshold cross).
                self._collect_cusum_cross(
                    ts,
                    "short",
                    s2_trig.cusum_positive,
                    s2_trig.cusum_negative,
                    s2_trig.threshold,
                )
                self._manage_s2_exits(
                    state,
                    mark,
                    ts,
                    s2_trig,
                    pause_cusum_reversal=is_current_bar and pause_cusum_reversal,
                )
                # Step 5 cross-exchange divergence — a live/now concept, so only the current
                # bar gets a spread (perp mark vs Binance spot). Stale/absent feed (age>120s
                # or no price) → None → Step 5 degrades (skips, no abort).
                cross_spread_pct = None
                if is_current_bar and cross_price is not None:
                    cp, age = cross_price
                    if cp and mark and age is not None and age <= 120:
                        cross_spread_pct = abs(mark - cp) / mark * 100.0
                # ── Wave S2G: event-driven entry on the 1s channel ──────────
                # The per-bar s2_trig above still drives exits. Entry now comes
                # from confirmed CUSUM clusters on the 1s series: advance that
                # accumulator through this bar's seconds, and evaluate only the
                # events the layer confirms (2nd distinct excursion within 300s).
                # Step 3-5 context stays per-bar — _inverse_vocab_signals is built
                # at 4H granularity by design ("the bar IS the trigger window"), so
                # an event is evaluated against the bar it lands in.
                s2_events = self._advance_1s_channel(ticks_1s, prev_bar_unix, t_unix)
                prev_bar_unix = t_unix
                for ev in s2_events:
                    self._maybe_open_s2(
                        ev.eval_ts.timestamp(),
                        self._trigger_from_event(ev),
                        state,
                        mark,
                        ts,
                        vocab=vocab_series[i],
                        flow_dir=flow_dir[i],
                        absorption=absorptions[i],
                        sweep=sweeps[i],
                        liq_pulse=bool(liq_pulse_series[i]),
                        oi_drop_pulse=bool(oi_drop_series[i]),
                        cross_spread_pct=cross_spread_pct,
                        snapshot=snapshot,
                        blocked=is_current_bar and block_s2_entry,
                        event=ev,
                    )
            # ── portfolio-wide cascade red line ──
            if state == "Cascade":
                for closed in self.accounts.cascade_close_all(mark, ts):
                    self._forget(closed.position.id)

            self._prev_state = state
            states.append(raw_state)

        return self._summary(df, states)

    def bar_signal_series(
        self, df: pd.DataFrame, oi_series=None, funding_series=None, ofi_series=None
    ) -> np.ndarray:
        """Per-bar CUSUM-Short z-signal (standardised log return) — the momentum
        signal that feeds both strategies' triggers. Exposed so its predictive edge
        (IC / hit-rate vs forward returns) can be measured. Same feature pipeline as
        process_frame; no trades are taken."""
        df = df.sort_values("time").reset_index(drop=True)
        _, sigma_series, log_returns = self._build_runner(
            df, oi_series, funding_series, ofi_series
        )
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

    # ── decision-trail snapshot (GL1 T0.3 / D1) ─────────────────────────────────
    @staticmethod
    def _series_val(series, i: int) -> Optional[float]:
        if series is None:
            return None
        try:
            v = float(series[i])
        except (IndexError, TypeError, ValueError):
            return None
        return v if math.isfinite(v) else None

    @classmethod
    def _bar_snapshot(
        cls,
        feat,
        z_t: float,
        funding_val: Optional[float],
        micro: Optional[dict],
        i: int,
    ) -> dict:
        """Full numeric input snapshot for one bar: everything the state machine and
        strategies actually read this bar (GL1 T0.3 / D1 — "决策用什么就审什么").
        v2_ofi_features (ofi_persister) recomputed this same OFI/LOB data as a
        write-only duplicate table; this captures it inline, alongside the decision
        it fed, instead (STATUS.md P2-1)."""
        return {
            "z_t": z_t,
            "sigma_4h": feat.sigma_4h,
            "sigma_pctile": feat.sigma_pctile,
            "entropy_4h": feat.entropy_4h,
            "entropy_pctile": feat.entropy_pctile,
            "entropy_variance": feat.entropy_variance,
            "entropy_variance_rising": feat.entropy_variance_rising,
            "oi_change_rate": feat.oi_change_rate,
            "oi_change_rate_pctile": feat.oi_change_rate_pctile,
            "oi_acceleration": feat.oi_acceleration,
            "funding_rate": funding_val,
            "funding_pctile": feat.funding_pctile,
            "funding_persistent": feat.funding_persistent,
            "hawkes_br": feat.hawkes_br,
            "hawkes_br_threshold": feat.hawkes_br_threshold,
            "tda_l1": feat.tda_l1,
            "tda_l1_pctile": feat.tda_l1_pctile,
            "lob_depth_pctile": feat.lob_depth_pctile,
            "ofi_cumulative_pctile": feat.ofi_cumulative_pctile,
            "taker_net": cls._series_val(micro.get("taker_net") if micro else None, i),
            "taker_vol": cls._series_val(micro.get("taker_vol") if micro else None, i),
            "lob_imb": cls._series_val(micro.get("lob_imb") if micro else None, i),
        }

    # ── Strategy 1 helpers ─────────────────────────────────────────────────────
    def _maybe_open_s1(self, dec, state, mark, ts, blocked: bool = False) -> None:
        if dec.action not in ("ENTER_LONG", "ENTER_SHORT"):
            return
        if (
            blocked
        ):  # GL1 T0.4: funding/OI stale -> S1 risk check unavailable, no new entry
            logger.info("S1 new entry blocked (STALE_FUNDING_OI)")
            return
        acct = self.accounts.subaccount_1
        if not acct.can_open():
            return
        size_pct = max(0.0, dec.base_size_pct * dec.size_modifier)
        if size_pct <= 0:
            return
        pos = acct.open_position(
            direction=dec.direction,
            entry_price=mark,
            size_pct=size_pct,
            leverage=dec.suggested_leverage,
            instrument=self.instrument,
            entry_time=ts,
            entry_state=state,
        )
        if pos:
            self._s1_meta[pos.id] = _S1Meta(dec.direction, mark, ts)
            logger.info(
                "S1 OPEN %s %s size%%=%.3f lev=%.1f @ %.2f",
                dec.direction,
                state,
                size_pct,
                dec.suggested_leverage,
                mark,
            )

    def _manage_s1_exits(
        self, state, mark, ts, trig, pause_cusum_reversal: bool = False
    ) -> None:
        acct = self.accounts.subaccount_1
        for pos in list(acct.open_positions):
            meta = self._s1_meta.get(pos.id)
            if meta is None:
                continue
            dec = check_strategy1_exit(
                direction=meta.direction,
                entry_price=meta.entry_price,
                entry_time=meta.entry_time,
                mark_price=mark,
                current_time=ts,
                state_4h=state,
                cusum_trigger=trig,
                prev_state_4h=self._prev_state,
                critical_already_reduced=meta.critical_reduced,
            )
            dec = self._suppress_cusum_reversal(dec, pause_cusum_reversal)
            self._apply_exit(acct, pos.id, dec, mark, ts, is_s1=True)

    # ── Strategy 2 helpers ─────────────────────────────────────────────────────
    def _advance_1s_channel(self, ticks_1s, prev_unix, bar_unix) -> list:
        """Run the 1s accumulator over (prev_unix, bar_unix] and return confirmed events.

        `ticks_1s` is (unix_seconds ndarray, z ndarray) built by paper_engine from
        the SHARED aggregation + standardisation in sel_v2.data.tick_1s, so the
        engine and the offline harness cannot drift — the S2G acceptance criterion
        is a zero diff between their event sets.

        Returns [] when no 1s feed is supplied, which keeps every existing caller
        and unit test working unchanged (they simply produce no S2 entries).
        """
        layer = self.s2_event_layer
        if ticks_1s is None or layer is None:
            return []
        secs, zs = ticks_1s
        if len(secs) == 0:
            return []
        import numpy as _np

        lo = 0 if prev_unix is None else int(_np.searchsorted(secs, prev_unix, "right"))
        hi = int(_np.searchsorted(secs, bar_unix, "right"))
        out = []
        for k in range(lo, hi):
            t = float(secs[k])
            trig = self._s2_cusum_1s.update(float(zs[k]), t)
            if not trig.triggered:
                continue
            ev = layer.on_trigger(
                _dt.datetime.fromtimestamp(t, _dt.timezone.utc),
                trig.direction,
                max(trig.cusum_positive, trig.cusum_negative),
            )
            if ev is not None:
                out.append(ev)
        return out

    @staticmethod
    def _trigger_from_event(ev) -> CUSUMTrigger:
        """The confirmed cluster expressed as the trigger Step 1a expects.

        The peak is the cluster's max, not the confirming excursion's, so sizing
        reflects the whole disturbance.
        """
        pos = ev.peak if ev.direction == "LONG" else 0.0
        neg = ev.peak if ev.direction == "SHORT" else 0.0
        return CUSUMTrigger(
            triggered=True,
            direction=ev.direction,
            cusum_positive=pos,
            cusum_negative=neg,
            threshold=0.0,
            intensity_coeff=1.0,
        )

    def _maybe_open_s2(
        self,
        t_unix,
        trig,
        state,
        mark,
        ts,
        vocab=None,
        flow_dir=0.0,
        absorption=None,
        sweep=None,
        liq_pulse: bool = False,
        oi_drop_pulse: bool = False,
        cross_spread_pct=None,
        snapshot=None,
        blocked: bool = False,
        event=None,
    ) -> None:
        # OFI persistently same direction as the CUSUM signal: real taker-flow sign
        # (flow_dir, already persistence-gated via the Crowding tag) agrees with the
        # CUSUM direction. None when there's no flow data → Type B stays conservative.
        ofi_persist = None
        if trig.triggered and flow_dir != 0 and trig.direction in ("LONG", "SHORT"):
            cusum_sign = 1.0 if trig.direction == "LONG" else -1.0
            ofi_persist = ("Crowding" in (vocab or set())) and (flow_dir == cusum_sign)
        dec = self._s2_filter.evaluate(
            t=t_unix,
            cusum_trigger=trig,
            state_4h=state,
            inverse_vocab=list(vocab) if vocab else None,
            ofi_persistent_same_direction=ofi_persist,
            liq_pulse=liq_pulse,
            oi_drop_pulse=oi_drop_pulse,
            absorption=absorption,
            sweep=sweep,
            cross_spread_pct=cross_spread_pct,
            subaccount_nav_usdt=self.accounts.subaccount_2.nav,
        )
        # Persist a direction-aware vocab event whenever a signature is present (§14.2, Wave
        # S2C) — telemetry for what Step 3 actually saw, into v2_inverse_vocab_events.
        if trig.triggered:
            for sig, name in ((absorption, "Absorption"), (sweep, "Sweep")):
                if sig is not None and getattr(sig, "present", False):
                    self._vocab_events.append(
                        (
                            _as_dt(ts),
                            name,
                            getattr(sig, "direction", None),
                            state,
                            dict(getattr(sig, "details", {}) or {}),
                        )
                    )
        self._last_s2 = (ts, state, dec)  # latest S2 decision (for the UI "why" panel)
        self._s2_trail.append(
            (
                # Event-driven (Wave S2G): the row is keyed on the EVENT's instant,
                # not the bar's, so two events inside one bar no longer collide on
                # the (timestamp, strategy) primary key.
                (event.eval_ts if event is not None else ts),
                state,
                dec,
                {
                    **(snapshot or {}),
                    "cusum_positive": trig.cusum_positive,
                    "cusum_negative": trig.cusum_negative,
                    "cusum_threshold": trig.threshold,
                    "ofi_persistent_same_direction": ofi_persist,
                    "flow_dir": float(flow_dir) if flow_dir is not None else None,
                    "inverse_vocab": sorted(vocab) if vocab else [],
                },
                (event.event_id if event is not None else None),
            )
        )
        if dec.action not in ("ENTER_LONG", "ENTER_SHORT"):
            return
        # ── Wave S2G throttle (Part 3) ──────────────────────────────────────
        # Applied AFTER the trail append on purpose: a throttled opportunity is
        # recorded, not dropped, so "what did we pass up" stays answerable.
        if event is not None and self.s2_event_layer is not None:
            open_dirs = {
                m.direction for m in self._s2_meta.values() if m.direction is not None
            }
            reason = self.s2_event_layer.throttle_reason(event, open_dirs)
            if reason:
                self._s2_events.append((event, reason))
                logger.info("S2 event %s throttled: %s", event.event_id[:8], reason)
                return
        if blocked:  # GL1 T0.4: ticks stale -> S2 (tick-flow-driven) new entry blocked
            logger.info("S2 new entry blocked (STALE_TICKS)")
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
            direction=direction,
            entry_price=mark,
            size_pct=size_pct,
            leverage=dec.suggested_leverage,
            instrument=self.instrument,
            entry_time=ts,
            entry_state=state,
            entry_confidence=getattr(dec, "entry_confidence", 1.0),
        )
        if pos:
            self._s2_meta[pos.id] = _S2Meta(
                direction,
                mark,
                ts,
                cusum_peak=abs(
                    trig.cusum_positive if direction == "LONG" else trig.cusum_negative
                ),
            )
            logger.info(
                "S2 OPEN %s %s lev=%.1f @ %.2f",
                direction,
                state,
                dec.suggested_leverage,
                mark,
            )

    def _manage_s2_exits(
        self, state, mark, ts, trig, pause_cusum_reversal: bool = False
    ) -> None:
        acct = self.accounts.subaccount_2
        for pos in list(acct.open_positions):
            meta = self._s2_meta.get(pos.id)
            if meta is None:
                continue
            cval = (
                trig.cusum_positive if meta.direction == "LONG" else trig.cusum_negative
            )
            meta.cusum_peak = max(meta.cusum_peak, abs(cval))
            dec = check_strategy2_exit(
                direction=meta.direction,
                entry_price=meta.entry_price,
                entry_time=meta.entry_time,
                mark_price=mark,
                current_time=ts,
                state_4h=state,
                cusum_trigger=trig,
                cusum_peak_since_entry=meta.cusum_peak,
                batches_triggered=meta.batches,
            )
            dec = self._suppress_cusum_reversal(dec, pause_cusum_reversal)
            applied = self._apply_exit(acct, pos.id, dec, mark, ts, is_s1=False)
            if applied == "batch":
                meta.batches += 1
                if meta.batches >= 3 and acct.close_position(
                    pos.id, mark, ts, "batch_exit_final"
                ):
                    # third 1/3 batch — close the residual so the position terminates cleanly
                    self._forget(pos.id)

    # ── shared exit application ────────────────────────────────────────────────
    @staticmethod
    def _suppress_cusum_reversal(dec: ExitDecision, pause: bool) -> ExitDecision:
        """GL1 T0.4: when ticks are stale, the CUSUM-reversal exit specifically is
        paused (the underlying CUSUM signal is computed from tick flow no longer
        arriving) — drawdown / time / Cascade / state-exit stops are untouched, since
        they don't depend on the stale feed. Filters by priority, not by touching
        strategies/strategy_exit.py (the judgment-logic layer stays frozen/R1)."""
        if pause and dec.priority == _P_CUSUM_REVERSE:
            return ExitDecision(
                action="HOLD",
                reason=f"{dec.reason} (suppressed: STALE_TICKS)",
                priority=_P_HOLD,
            )
        return dec

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
            acct.reduce_position(
                pos_id, dec.partial_fraction or 0.5, mark, ts, dec.reason
            )
            if is_s1 and pos_id in self._s1_meta:
                self._s1_meta[pos_id].critical_reduced = True
            return "reduce"
        if action in ("EXIT_BATCH_33",):
            acct.reduce_position(
                pos_id, dec.partial_fraction or (1 / 3), mark, ts, dec.reason
            )
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

    @staticmethod
    def _decision_view(captured) -> Optional[dict]:
        """Flatten a captured (ts, state, EntryDecision) into a UI-friendly dict — the
        'why no entry' explanation for the latest bar."""
        if not captured:
            return None
        ts, state, d = captured
        return {
            "action": getattr(d, "action", None),
            "reason": getattr(d, "reason", ""),
            "step_reached": getattr(d, "step_reached", 0),
            "state_4h": state,
            "direction": getattr(d, "direction", None)
            or getattr(d, "cusum_direction", None),
            "timestamp": _as_dt(ts).isoformat(),
        }

    def latest_decisions(self) -> dict:
        """Latest per-strategy decision dicts (None when a strategy didn't evaluate)."""
        return {
            "strategy_1": self._decision_view(self._last_s1),
            "strategy_2": self._decision_view(self._last_s2),
        }

    def decision_trail(self, last_n: int = 300) -> list:
        """Per-bar decision rows (the audit trail, #2) for the most recent `last_n` bars of
        each strategy, as (timestamp, strategy, action, reason, step_reached, state_4h,
        direction, decision_trail) tuples for v2_strategy_decision. decision_trail (GL1
        T0.3/D1) is the full numeric input snapshot the decision consumed. Bounded so the
        per-tick persist stays cheap — sealed bars don't change, so the recent window plus
        the new bar is enough."""
        rows = []
        for strat, trail in (
            ("strategy_1", self._s1_trail),
            ("strategy_2", self._s2_trail),
        ):
            for entry in trail[-last_n:]:
                # S1 appends 4-tuples and keeps its per-bar cadence untouched by
                # Wave S2G; S2 appends a 5th element, the id of the S2_EVENT this
                # decision came from (including throttled ones). Unpacking this way
                # means S1's append sites did not have to change at all.
                ts, state, d, snapshot = entry[:4]
                event_id = entry[4] if len(entry) > 4 else None
                # event_id rides INSIDE the snapshot rather than widening this
                # tuple. The row shape is positional and has several consumers —
                # appending a field silently changed what r[-1] meant (it had been
                # the trail dict), which is exactly the kind of quiet semantic
                # shift that passes review and fails later. db_writer lifts it out
                # into its own column.
                if event_id is not None:
                    snapshot = {**(snapshot or {}), "event_id": event_id}
                rows.append(
                    (
                        _as_dt(ts),
                        strat,
                        getattr(d, "action", None) or "OBSERVE",
                        getattr(d, "reason", ""),
                        getattr(d, "step_reached", 0),
                        state,
                        getattr(d, "direction", None)
                        or getattr(d, "cusum_direction", None),
                        snapshot,
                    )
                )
        return rows

    def cusum_events(self) -> list:
        """CUSUM threshold-cross events collected this replay, as
        (timestamp, cusum_type, direction, peak_value, threshold_h, z_returns_window) tuples
        for v2_cusum_events. Both accumulators: 'short' (S2, engine-owned) and 'mid' (S1,
        from the entry filter's reported C±). Unbounded — triggers are sparse (~9% of bars)
        and the persist upserts on (timestamp, cusum_type), so the full-history replay
        backfills once and no-ops thereafter (fixes the orphan-table diagnosis: the events
        fired all along but were never persisted)."""
        return self._cusum_events

    def vocab_events(self) -> list:
        """Inverse-vocab signatures seen at S2 trigger bars this replay, as
        (timestamp, vocab, direction, state, details) tuples for v2_inverse_vocab_events
        (Wave S2C Step 3 telemetry — what the direction-aware classifier actually observed)."""
        return self._vocab_events

    def _summary(self, df, states) -> dict:
        from collections import Counter

        mark = float(df["close"].iloc[-1])
        a1, a2 = self.accounts.subaccount_1, self.accounts.subaccount_2
        dec = self.latest_decisions()
        return {
            "bars": len(df),
            "state_counts": dict(Counter(states)),
            "s1": {
                "nav": round(a1.nav, 2),
                "open": len(a1.open_positions),
                "closed": len(a1.closed_positions),
            },
            "s2": {
                "nav": round(a2.nav, 2),
                "open": len(a2.open_positions),
                "closed": len(a2.closed_positions),
            },
            "s1_decision": dec["strategy_1"],
            "s2_decision": dec["strategy_2"],
            "total_equity": round(self.accounts.total_equity(mark), 2),
        }


def _as_dt(ts):
    return pd.Timestamp(ts).to_pydatetime()
