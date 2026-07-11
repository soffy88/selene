"""
ToolEvaluator — v2.1 §3.3 Month 3 evaluation metrics calculator.

Computes per-tool evaluation metrics from v2_inverse_vocab_events and
v2_state_history, then writes results to v2_tool_evaluation_results.

Usage (manual, monthly):
    evaluator = ToolEvaluator(db_writer)
    await evaluator.evaluate_all(phase="month_3", lookback_days=90)

Evaluation metrics (v2.1 §2.2 + §16):
  - lead_time_seconds: median seconds before associated state transition
  - false_positive_rate: signals fired / signals with no subsequent event
  - correlation_with_others: Spearman ρ against other tool signal series
  - sample_size: number of signal events in the lookback window
  - decision: 'upgrade' / 'maintain' / 'deprecate'

Decision rules (v2.1 §2.2):
  A. lead_time > 86400s (24h) AND fp_rate < 0.30  → 'upgrade'
  B. 0 < lead_time < 86400s OR fp_rate 0.30-0.60  → 'maintain'
  C. fp_rate > 0.60 OR lead_time < 0              → 'deprecate'
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

_OBSERVATION_TOOL_IDS = ["B1", "B2", "TDA2", "I1", "T2", "W2", "H3"]

_VOCAB_TO_TOOL = {
    "hmm_disagreement": "B1",
    "hmm_boundary": "B2",
    "tda_clustering_disagreement": "TDA2",
    "pe_trend_warning": "I1",
    "te_funding_to_price": "T2",
    "multifractal_spectrum_wide": "W2",
    "hawkes_cascade_early_warning": "H3",
}

_TOOL_NAMES = {
    "B1": "Bayesian HMM 軟検証",
    "B2": "HMM 境界仲裁",
    "TDA2": "TDA + clustering 状態識別",
    "I1": "Permutation Entropy 趋勢予警",
    "T2": "TE 滚動監控",
    "W2": "Wavelet 多分形谱宽",
    "H3": "Hawkes Cascade 早期予警",
}


class ToolEvaluator:
    """
    Computes Month 3 / Month 6 evaluation metrics for observation-only tools.

    Designed to run monthly against a live TimescaleDB database.
    Falls back to stub/offline mode if DB is unavailable.
    """

    def __init__(self, db_writer=None) -> None:
        self._db = db_writer

    async def evaluate_all(
        self,
        phase: str = "month_3",
        lookback_days: int = 90,
    ) -> list[dict[str, Any]]:
        """
        Evaluate all observation tools and return a list of result dicts.

        Each dict matches v2.1 §15.3 tool_evaluation_results schema.
        """
        results = []
        for tool_id in _OBSERVATION_TOOL_IDS:
            result = await self._evaluate_one(tool_id, phase, lookback_days)
            results.append(result)
            if self._db is not None:
                await self._write_result(result)
        return results

    async def _evaluate_one(
        self, tool_id: str, phase: str, lookback_days: int
    ) -> dict[str, Any]:
        """Evaluate a single tool. Returns result dict."""
        now = datetime.now(timezone.utc)
        period_start = now - timedelta(days=lookback_days)

        signals, state_events = await self._fetch_data(tool_id, period_start, now)

        lead_time_sec = self._compute_lead_time(signals, state_events)
        fp_rate = self._compute_fp_rate(signals, state_events, lookback_days)
        sample_size = len(signals)

        decision = self._make_decision(lead_time_sec, fp_rate, sample_size)

        return {
            "tool_id": tool_id,
            "tool_name": _TOOL_NAMES.get(tool_id, tool_id),
            "evaluation_phase": phase,
            "timestamp": now.isoformat(),
            "period_start": period_start.isoformat(),
            "period_end": now.isoformat(),
            "lead_time_seconds": lead_time_sec,
            "false_positive_rate": fp_rate,
            "correlation_with_others": None,  # computed separately in _compute_correlations
            "sample_size": sample_size,
            "decision": decision,
            "decision_reason": self._decision_reason(
                lead_time_sec, fp_rate, sample_size
            ),
            "created_by": "cc",
            "created_at": now.isoformat(),
        }

    async def _fetch_data(
        self, tool_id: str, start: datetime, end: datetime
    ) -> tuple[list[datetime], list[datetime]]:
        """
        Fetch signal timestamps and state-transition timestamps from DB.
        Returns (signal_ts_list, state_event_ts_list).
        Stub: returns empty lists if DB unavailable.
        """
        if self._db is None:
            return [], []

        vocab = next(
            (v for v, t in _VOCAB_TO_TOOL.items() if t == tool_id), tool_id.lower()
        )
        try:
            conn = self._db._conn
            if conn is None:
                return [], []

            # Signal timestamps
            rows_sig = await conn.fetch(
                """
                SELECT timestamp FROM v2_inverse_vocab_events
                WHERE vocab = $1 AND timestamp BETWEEN $2 AND $3
                ORDER BY timestamp
                """,
                vocab,
                start,
                end,
            )
            signal_ts = [r["timestamp"] for r in rows_sig]

            # State transition timestamps (any transition = potential true event).
            # v2_state_history holds a one-time pre-2026-06-15 backfill mixed in with
            # live writes (see the STATUS.md data-constraint note) -- bounding to the
            # live domain keeps a wide lookback (e.g. month_3's 90 days) from silently
            # treating backfilled transitions as live tool-evaluation evidence.
            rows_state = await conn.fetch(
                """
                SELECT timestamp FROM v2_state_history
                WHERE timestamp BETWEEN $1 AND $2
                  AND timestamp >= '2026-06-15'
                  AND transition_from IS NOT NULL
                ORDER BY timestamp
                """,
                start,
                end,
            )
            state_ts = [r["timestamp"] for r in rows_state]

            return signal_ts, state_ts

        except Exception as e:
            logger.warning("ToolEvaluator fetch failed for %s: %s", tool_id, e)
            return [], []

    def _compute_lead_time(
        self, signals: list[datetime], state_events: list[datetime]
    ) -> Optional[float]:
        """
        Compute median lead time in seconds: signal_ts → next state_event_ts.
        Returns None if insufficient data.
        """
        if not signals or not state_events:
            return None

        leads = []
        state_arr = np.array([s.timestamp() for s in state_events])
        for sig_ts in signals:
            sig_unix = sig_ts.timestamp()
            after = state_arr[state_arr >= sig_unix]
            if len(after) > 0:
                leads.append(float(after[0] - sig_unix))

        return float(np.median(leads)) if leads else None

    def _compute_fp_rate(
        self,
        signals: list[datetime],
        state_events: list[datetime],
        lookback_days: int,
        follow_window_seconds: float = 86400.0,  # 24h follow-up
    ) -> Optional[float]:
        """
        False positive rate = signals with no state event within follow_window.
        Returns None if insufficient data (< 5 signals).
        """
        if len(signals) < 5:
            return None

        state_arr = np.array([s.timestamp() for s in state_events])
        fp_count = 0
        for sig_ts in signals:
            sig_unix = sig_ts.timestamp()
            window_end = sig_unix + follow_window_seconds
            in_window = state_arr[(state_arr >= sig_unix) & (state_arr <= window_end)]
            if len(in_window) == 0:
                fp_count += 1
        return fp_count / len(signals)

    def _make_decision(
        self,
        lead_time: Optional[float],
        fp_rate: Optional[float],
        sample_size: int,
    ) -> str:
        """v2.1 §2.2 decision rules."""
        if sample_size < 5:
            return "maintain"  # insufficient data to decide

        if fp_rate is not None and fp_rate > 0.60:
            return "deprecate"
        if lead_time is not None and lead_time < 0:
            return "deprecate"
        if (
            lead_time is not None
            and lead_time > 86400.0
            and fp_rate is not None
            and fp_rate < 0.30
        ):
            return "upgrade"
        return "maintain"

    def _decision_reason(
        self,
        lead_time: Optional[float],
        fp_rate: Optional[float],
        sample_size: int,
    ) -> str:
        if sample_size < 5:
            return f"Insufficient sample (n={sample_size} < 5). Maintain pending more data."
        parts = []
        if lead_time is not None:
            parts.append(f"lead_time={lead_time / 3600:.1f}h")
        if fp_rate is not None:
            parts.append(f"fp_rate={fp_rate:.2%}")
        parts.append(f"n={sample_size}")
        return "; ".join(parts)

    async def compute_correlations(
        self, phase: str = "month_3", lookback_days: int = 90
    ) -> dict[str, dict[str, float]]:
        """
        Compute pairwise Spearman correlation between tool signal series.
        Returns dict[tool_id → dict[other_tool_id → rho]].
        """
        if self._db is None:
            return {}

        now = datetime.now(timezone.utc)
        start = now - timedelta(days=lookback_days)

        all_signals: dict[str, list[datetime]] = {}
        for tool_id in _OBSERVATION_TOOL_IDS:
            signals, _ = await self._fetch_data(tool_id, start, now)
            all_signals[tool_id] = signals

        # Build hourly binary series for correlation
        n_hours = int(lookback_days * 24)
        start_unix = start.timestamp()
        series: dict[str, np.ndarray] = {}
        for tid, sigs in all_signals.items():
            arr = np.zeros(n_hours)
            for s in sigs:
                h = int((s.timestamp() - start_unix) / 3600)
                if 0 <= h < n_hours:
                    arr[h] = 1.0
            series[tid] = arr

        from scipy.stats import spearmanr

        corr: dict[str, dict[str, float]] = {}
        for tid in _OBSERVATION_TOOL_IDS:
            corr[tid] = {}
            for other in _OBSERVATION_TOOL_IDS:
                if tid == other:
                    continue
                rho, _ = spearmanr(series[tid], series[other])
                corr[tid][other] = float(rho) if not np.isnan(rho) else 0.0

        return corr

    async def _write_result(self, result: dict[str, Any]) -> None:
        """Write evaluation result to v2_tool_evaluation_results."""
        if self._db is None or self._db._conn is None:
            return
        try:
            await self._db._conn.execute(
                """
                INSERT INTO v2_tool_evaluation_results (
                    timestamp, tool_id, tool_name, evaluation_phase,
                    lead_time_seconds, false_positive_rate,
                    correlation_with_others, sample_size,
                    decision, decision_reason, created_by, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12
                )
                """,
                datetime.fromisoformat(result["timestamp"]),
                result["tool_id"],
                result["tool_name"],
                result["evaluation_phase"],
                result.get("lead_time_seconds"),
                result.get("false_positive_rate"),
                json.dumps(result.get("correlation_with_others")),
                result["sample_size"],
                result["decision"],
                result.get("decision_reason", ""),
                result["created_by"],
                datetime.fromisoformat(result["created_at"]),
            )
        except Exception as e:
            logger.warning("ToolEvaluator write failed: %s", e)
