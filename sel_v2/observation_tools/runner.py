"""
ObservationRunner — unified scheduler for all 7 observation-only tools.

Triggered every 4H bar close.  Runs B1/B2/TDA2/I1/T2/W2/H3 in sequence and
writes results to v2_inverse_vocab_events via DBWriter.

v2.1 §2.2 hard discipline:
  - Runner output NEVER feeds back into Strategy 1 or Strategy 2 decisions.
  - Any exception in a single tool is logged and skipped; other tools proceed.

Vocab mapping (for v2_inverse_vocab_events.vocab field):
  B1 → 'hmm_disagreement'         (when B1 signal fires)
  B2 → 'hmm_boundary'             (regime boundary crossing)
  TDA2 → 'tda_clustering_disagreement'
  I1 → 'pe_trend_warning'
  T2 → 'te_funding_to_price'
  W2 → 'multifractal_spectrum_wide'
  H3 → 'hawkes_cascade_early_warning'
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from sel_v2.observation_tools.base import BarFeatures, ObservationResult
from sel_v2.observation_tools.bayesian_hmm import BayesianHMM
from sel_v2.observation_tools.hmm_boundary_arbiter import HMMBoundaryArbiter
from sel_v2.observation_tools.tda_clustering import TDAClustering
from sel_v2.observation_tools.permutation_entropy import PermutationEntropy
from sel_v2.observation_tools.transfer_entropy_rolling import TransferEntropyRolling
from sel_v2.observation_tools.wavelet_multifractal import WaveletMultifractal
from sel_v2.observation_tools.hawkes_cascade_warning import HawkesCascadeWarning
from sel_v2.observation_tools.chan_tools import ChanDivergence, ChanPivot
from sel_v2.observation_tools.killzone import KillzoneAnomaly
from sel_v2.observation_tools.swing_structure import SwingStructureTool

logger = logging.getLogger(__name__)

_VOCAB_MAP: dict[str, str] = {
    "B1": "hmm_disagreement",
    "B2": "hmm_boundary",
    "TDA2": "tda_clustering_disagreement",
    "I1": "pe_trend_warning",
    "T2": "te_funding_to_price",
    "W2": "multifractal_spectrum_wide",
    "H3": "hawkes_cascade_early_warning",
    # v2.2 lens batch (CHAN-1/chan_retest rejected offline — lens_verdict_v1)
    "CHAN2": "chan_divergence",
    "CHAN3": "chan_pivot",
    "ICT2": "swing_structure",
    "ICT3": "killzone_anomaly",
}

_TOOL_SOURCE_MAP: dict[str, str] = {
    "B1": "hmm",
    "B2": "hmm",
    "TDA2": "tda",
    "I1": "permutation_entropy",
    "T2": "transfer_entropy",
    "W2": "wavelet",
    "H3": "hawkes",
    "CHAN2": "chan",
    "CHAN3": "chan",
    "ICT2": "ict",
    "ICT3": "ict",
}

# Lens tools write per-bar events to v2_inverse_vocab_events via fired_sink /
# persist_lens_vocab_events below. The 7 legacy tools stay out of that path —
# they never wrote vocab events from the recent-window replay, and their
# Month-3 baselines must not change.
_LENS_TOOL_IDS = frozenset({"CHAN2", "CHAN3", "ICT2", "ICT3"})


class ObservationRunner:
    """
    Orchestrates all 7 observation-only tools and persists signals.

    Usage (async, in bar_runner event loop)::

        runner = ObservationRunner(db_writer)
        await runner.process_bar(bar_features)

    Usage (offline / tests, no DB)::

        runner = ObservationRunner(db_writer=None)
        results = runner.process_bar_sync(bar_features)
    """

    def __init__(self, db_writer=None) -> None:
        self._tools = [
            BayesianHMM(),
            HMMBoundaryArbiter(),
            TDAClustering(),
            PermutationEntropy(),
            TransferEntropyRolling(),
            WaveletMultifractal(),
            HawkesCascadeWarning(),
            # v2.2 lens batch (observation-only; offline gate: lens_verdict_v1)
            ChanDivergence(),
            ChanPivot(),
            SwingStructureTool(),
            KillzoneAnomaly(),
        ]
        self._db = db_writer
        self._bars_processed: int = 0

    # ── Sync interface (offline replay / testing) ─────────────────────────

    def process_bar_sync(self, bar: BarFeatures) -> list[ObservationResult]:
        """
        Run all tools on bar, return results.  Does NOT write to DB.
        """
        results: list[ObservationResult] = []
        for tool in self._tools:
            try:
                result = tool.update(bar)
                results.append(result)
            except Exception as exc:
                logger.warning(
                    "ObservationRunner: tool %s raised on %s: %s",
                    tool.tool_id,
                    bar.timestamp,
                    exc,
                )
        self._bars_processed += 1
        return results

    # ── Async interface (live bar runner) ─────────────────────────────────

    async def process_bar(self, bar: BarFeatures) -> list[ObservationResult]:
        """
        Run all tools and persist signals that fire to v2_inverse_vocab_events.
        """
        results = self.process_bar_sync(bar)
        if self._db is not None:
            await self._persist(bar, results)
        return results

    async def _persist(
        self, bar: BarFeatures, results: list[ObservationResult]
    ) -> None:
        for result in results:
            if not result.signal:
                continue
            vocab = _VOCAB_MAP.get(result.tool_id, result.tool_id.lower())
            tool_source = _TOOL_SOURCE_MAP.get(result.tool_id, result.tool_id.lower())
            try:
                await self._db.write_inverse_vocab_event(
                    timestamp=result.timestamp,
                    vocab=vocab,
                    tool_source=tool_source,
                    intensity=result.value if result.value else None,
                    associated_state=None,
                    tool_metadata={
                        "label": result.label,
                        "confidence": result.confidence,
                        "threshold": result.threshold,
                        **result.metadata,
                    },
                    observation_only=True,
                )
            except Exception as exc:
                logger.warning(
                    "ObservationRunner: failed to persist %s signal: %s",
                    result.tool_id,
                    exc,
                )

    # ── State inspection ──────────────────────────────────────────────────

    def ready_tools(self) -> list[str]:
        """Return tool IDs that have warmed up and are ready."""
        return [t.tool_id for t in self._tools if t.is_ready()]

    def all_ready(self) -> bool:
        return all(t.is_ready() for t in self._tools)

    @property
    def bars_processed(self) -> int:
        return self._bars_processed

    def reset_all(self) -> None:
        """Reset all tools to initial state (used in testing)."""
        for t in self._tools:
            t.reset()
        self._bars_processed = 0


# ── Recent-window driver (UI surface, #3) ───────────────────────────────────────
def run_recent_observations(
    df,
    *,
    oi_series=None,
    funding_series=None,
    window: int = 540,
    state_by_ts=None,
    fired_sink=None,
):
    """Feed the last `window` 4H bars through a fresh ObservationRunner and return the LATEST
    per-tool results (the observation-only tools' current readings).

    The tools are observation-only (they never touch trading), but they had no caller and no UI
    — this warms them up over the recent window so the SEL panel can show what HMM / TDA / TE /
    wavelet / permutation-entropy / Hawkes-cascade are currently saying. Returns
    list[ObservationResult] (empty if too few bars).

    v2.2 lens batch: `state_by_ts` (dict timestamp → sel state label) feeds the
    CHAN/ICT lens tools; if `fired_sink` is a list, every fired lens-tool result
    across the WHOLE replay window is appended to it (per-bar events for
    persist_lens_vocab_events — the replay is deterministic, so the (timestamp,
    vocab) upsert collapses repeats across refresh cycles). The 7 legacy tools
    never enter fired_sink."""
    import numpy as np

    n = len(df)
    if n < 2:
        return []
    closes = df["close"].values.astype(float)
    highs = df["high"].values.astype(float) if "high" in df else None
    lows = df["low"].values.astype(float) if "low" in df else None
    vols = df["volume"].values.astype(float) if "volume" in df else np.ones(n)
    times = list(df["time"])
    lo = max(1, n - window)
    runner = ObservationRunner(db_writer=None)
    results = []
    for i in range(lo, n):
        ts = times[i]
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        bar = BarFeatures(
            timestamp=ts,
            log_return=float(np.log(closes[i] / closes[i - 1]))
            if closes[i - 1] > 0
            else 0.0,
            volume=float(vols[i]),
            funding_rate=(
                float(funding_series[i])
                if funding_series is not None and np.isfinite(funding_series[i])
                else None
            ),
            open_interest=(
                float(oi_series[i])
                if oi_series is not None and np.isfinite(oi_series[i])
                else None
            ),
            high=float(highs[i]) if highs is not None else None,
            low=float(lows[i]) if lows is not None else None,
            close=float(closes[i]),
            state=state_by_ts.get(ts) if state_by_ts else None,
        )
        results = runner.process_bar_sync(bar)  # last iteration = current reading
        if fired_sink is not None:
            fired_sink.extend(
                r for r in results if r.signal and r.tool_id in _LENS_TOOL_IDS
            )
    return results


async def persist_lens_vocab_events(conn, fired) -> int:
    """Upsert fired lens-tool events into v2_inverse_vocab_events, keyed
    (timestamp, vocab). Idempotent across the per-refresh window replay — the
    deterministic tools regenerate identical events each cycle and the upsert
    collapses them (self-healing backfill after downtime). DBWriter
    (sel_v2/strategies/db_writer.py) is code-freeze-frozen, so the upsert lives
    here; same ON CONFLICT idiom."""
    n = 0
    for r in fired:
        meta = dict(r.metadata)
        associated_state = meta.pop("associated_state", None)
        meta.update(
            {"label": r.label, "confidence": r.confidence, "threshold": r.threshold}
        )
        try:
            await conn.execute(
                """
                INSERT INTO v2_inverse_vocab_events
                    (timestamp, vocab, intensity, associated_state,
                     tool_source, observation_only, tool_metadata)
                VALUES ($1, $2, $3, $4, $5, TRUE, $6::jsonb)
                ON CONFLICT (timestamp, vocab) DO UPDATE SET
                    intensity = EXCLUDED.intensity,
                    associated_state = EXCLUDED.associated_state,
                    tool_metadata = EXCLUDED.tool_metadata
                WHERE v2_inverse_vocab_events.tool_metadata
                      IS DISTINCT FROM EXCLUDED.tool_metadata
                   OR v2_inverse_vocab_events.associated_state
                      IS DISTINCT FROM EXCLUDED.associated_state
                """,
                r.timestamp,
                _VOCAB_MAP.get(r.tool_id, r.tool_id.lower()),
                float(r.value),
                associated_state,
                _TOOL_SOURCE_MAP.get(r.tool_id, r.tool_id.lower()),
                json.dumps(meta, default=str),
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("persist_lens_vocab_events(%s): %s", r.tool_id, exc)
    return n


async def persist_latest_observations(conn, results) -> int:
    """Upsert the latest per-tool observation readings into v2_observation_latest (one row per
    tool). Idempotent — the SEL observation panel reads this."""
    n = 0
    for r in results:
        try:
            await conn.execute(
                """
                INSERT INTO v2_observation_latest
                    (tool_id, source, signal, value, threshold, label, confidence, timestamp, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NOW())
                ON CONFLICT (tool_id) DO UPDATE SET
                    source = EXCLUDED.source, signal = EXCLUDED.signal, value = EXCLUDED.value,
                    threshold = EXCLUDED.threshold, label = EXCLUDED.label,
                    confidence = EXCLUDED.confidence, timestamp = EXCLUDED.timestamp, updated_at = NOW()
                """,
                r.tool_id,
                _TOOL_SOURCE_MAP.get(r.tool_id),
                bool(r.signal),
                float(r.value),
                float(r.threshold),
                r.label,
                float(r.confidence),
                r.timestamp,
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("persist_latest_observations(%s): %s", r.tool_id, exc)
    return n
