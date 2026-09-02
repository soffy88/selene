"""Phase 4 — PaperStrategyEngine: integration + orchestration wiring.

Verifies the engine that replaces the old `_process_bar` stub: the full
precompute -> recognizer -> entry filters -> exits -> sub-accounts pipeline runs,
and the open/exit orchestration (the glue this module adds) behaves correctly.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pytest

from sel_v2.paper.strategy_engine import PaperStrategyEngine
from sel_v2.strategies.cusum_short import CUSUMTrigger


def _bars(n=400, seed=3):
    rng = np.random.default_rng(seed)
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    times = [t0 + timedelta(hours=4 * i) for i in range(n)]
    rets = rng.normal(0, 0.012, n)
    rets[150:175] += 0.04
    rets[300:320] -= 0.05
    close = 30000 * np.exp(np.cumsum(rets))
    return pd.DataFrame(
        {
            "time": pd.to_datetime(times),
            "close": close,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "volume": 1000.0,
        }
    )


@dataclass
class _FakeS1Decision:
    action: str
    direction: Optional[str] = None
    base_size_pct: float = 0.05
    size_modifier: float = 1.0
    suggested_leverage: float = 2.0
    cusum_positive: float = 0.0
    cusum_negative: float = 0.0
    cusum_threshold: float = 0.0


class TestIntegration:
    def test_full_frame_runs_and_conserves_nav_without_trades(self):
        eng = PaperStrategyEngine(
            total_nav_usdt=100_000,
            skip_hawkes=True,
            skip_tda=True,
            hawkes_params=(0.1, 0.3, 0.5),
        )
        summary = eng.process_frame(_bars())
        # pipeline ran over every bar
        assert summary["bars"] == 400
        assert sum(summary["state_counts"].values()) == 400
        # no entry states reached without OI/funding -> NAV is exactly preserved
        assert eng.accounts.subaccount_1.nav == 80_000.0
        assert eng.accounts.subaccount_2.nav == 20_000.0
        assert summary["total_equity"] == 100_000.0

    def test_summary_shape(self):
        eng = PaperStrategyEngine(skip_hawkes=True, skip_tda=True, hawkes_params=(0.1, 0.3, 0.5))
        s = eng.process_frame(_bars(200))
        for k in ("bars", "state_counts", "s1", "s2", "total_equity"):
            assert k in s
        for acct in ("s1", "s2"):
            assert set(s[acct]) == {"nav", "open", "closed"}


class TestEntryExitOrchestration:
    """Drive the open/exit glue directly (decision logic itself is covered elsewhere)."""

    def _engine(self):
        return PaperStrategyEngine(
            total_nav_usdt=100_000,
            skip_hawkes=True,
            skip_tda=True,
            hawkes_params=(0.1, 0.3, 0.5),
        )

    def test_s1_entry_opens_position_and_tracks_meta(self):
        eng = self._engine()
        ts = datetime(2024, 1, 5, tzinfo=timezone.utc)
        eng._maybe_open_s1(_FakeS1Decision("ENTER_LONG", "LONG"), "Coiling", 30000.0, ts)
        acct = eng.accounts.subaccount_1
        assert len(acct.open_positions) == 1
        pos = acct.open_positions[0]
        assert pos.id in eng._s1_meta
        assert eng._s1_meta[pos.id].direction == "LONG"

    def test_observe_does_not_open(self):
        eng = self._engine()
        eng._maybe_open_s1(
            _FakeS1Decision("OBSERVE"),
            "Drifting-Calm",
            30000.0,
            datetime(2024, 1, 5, tzinfo=timezone.utc),
        )
        assert len(eng.accounts.subaccount_1.open_positions) == 0

    def test_cascade_state_forces_full_exit(self):
        eng = self._engine()
        ts = datetime(2024, 1, 5, tzinfo=timezone.utc)
        eng._maybe_open_s1(_FakeS1Decision("ENTER_LONG", "LONG"), "Coiling", 30000.0, ts)
        assert len(eng.accounts.subaccount_1.open_positions) == 1
        # a non-triggered CUSUM + Cascade state -> check_strategy1_exit returns EXIT_FULL
        trig = CUSUMTrigger(False, None, 0.0, 0.0, 2.0, 0.0)
        eng._prev_state = "Coiling"
        eng._manage_s1_exits("Cascade", 29000.0, ts + timedelta(hours=4), trig)
        assert len(eng.accounts.subaccount_1.open_positions) == 0
        assert len(eng.accounts.subaccount_1.closed_positions) == 1
        # meta cleaned up
        assert eng._s1_meta == {}

    def test_drawdown_stop_closes_losing_long(self):
        eng = self._engine()
        ts = datetime(2024, 1, 5, tzinfo=timezone.utc)
        eng._maybe_open_s1(_FakeS1Decision("ENTER_LONG", "LONG"), "Coiling", 30000.0, ts)
        trig = CUSUMTrigger(False, None, 0.0, 0.0, 2.0, 0.0)
        eng._prev_state = "Coiling"
        # -5% move breaches the -3% S1 drawdown stop
        eng._manage_s1_exits("Drifting-Calm", 30000.0 * 0.95, ts + timedelta(hours=4), trig)
        assert len(eng.accounts.subaccount_1.open_positions) == 0
        closed = eng.accounts.subaccount_1.closed_positions
        assert len(closed) == 1 and closed[0].pnl_usdt < 0


class TestDecisionTrailReconstruction:
    """GL1 T0.3/D1 acceptance: take any v2_strategy_decision row, and decision_trail
    (the JSONB snapshot) alone must fully reconstruct the numeric inputs that decision
    consumed — not a separately-recomputed value (that's exactly the v2_ofi_features
    orphan-store problem D1 retired)."""

    def test_decision_trail_reconstructs_bar_inputs(self):
        n = 400
        df = _bars(n)
        rng = np.random.default_rng(7)
        oi = np.cumsum(rng.normal(0, 1, n))
        funding = rng.normal(0, 0.0001, n)
        entropy = 2.0 + 0.1 * np.sin(np.linspace(0, 20, n))
        micro = {
            "taker_net": rng.normal(0, 100, n),
            "taker_vol": np.abs(rng.normal(500, 50, n)),
            "lob_imb": rng.normal(0, 10, n),
            "lob_depth": np.abs(rng.normal(1000, 50, n)),
            "entropy": entropy,
        }
        eng = PaperStrategyEngine(
            total_nav_usdt=100_000,
            skip_hawkes=True,
            skip_tda=True,
            hawkes_params=(0.1, 0.3, 0.5),
        )
        eng.process_frame(df, oi_series=oi, funding_series=funding, micro=micro)
        rows = eng.decision_trail(last_n=n)
        assert rows, "expected at least one decision row"

        # BarRunner timestamps go through numpy (tz-naive after df["time"].values), so
        # match by strategy + recency rather than exact tz-aware equality against df.
        s1_rows = [r for r in rows if r[1] == "strategy_1"]
        assert s1_rows
        row = max(s1_rows, key=lambda r: r[0])
        _, strat, action, reason, step, state, direction, trail = row

        # Independently rebuild the same bar's features and assert the snapshot matches —
        # proves decision_trail carries the real values, not placeholders.
        runner, sigma_series, log_returns = eng._build_runner(
            df.sort_values("time").reset_index(drop=True),
            oi,
            funding,
            None,
            lob_depth_series=micro["lob_depth"],
            entropy_series=micro["entropy"],
        )
        feat = runner.build_features(n - 1)
        assert trail["entropy_4h"] == pytest.approx(feat.entropy_4h)
        if feat.entropy_variance is not None:
            assert trail["entropy_variance"] == pytest.approx(feat.entropy_variance)
        else:
            assert trail["entropy_variance"] is None
        assert trail["entropy_variance_rising"] == feat.entropy_variance_rising
        if feat.oi_change_rate is not None:
            assert trail["oi_change_rate"] == pytest.approx(feat.oi_change_rate)
        assert trail["funding_rate"] == pytest.approx(float(funding[-1]))
        assert trail["taker_net"] == pytest.approx(float(micro["taker_net"][-1]))
        assert trail["taker_vol"] == pytest.approx(float(micro["taker_vol"][-1]))
        assert trail["lob_imb"] == pytest.approx(float(micro["lob_imb"][-1]))
        # CUSUM values must be present (consumed by the entry filter this bar).
        assert "cusum_positive" in trail and "cusum_negative" in trail and "cusum_threshold" in trail

    def test_decision_trail_round_trips_through_json_encoding(self):
        """The snapshot must survive json.dumps/json.loads unchanged (asyncpg JSONB
        round-trip via DBWriter) — the actual persistence path, not just the in-memory dict."""
        import json

        eng = PaperStrategyEngine(
            total_nav_usdt=100_000,
            skip_hawkes=True,
            skip_tda=True,
            hawkes_params=(0.1, 0.3, 0.5),
        )
        eng.process_frame(_bars(60))
        rows = eng.decision_trail(last_n=60)
        assert rows
        trail = rows[0][-1]
        encoded = json.dumps(trail, default=str)
        assert set(json.loads(encoded)) == set(trail)


class TestStalenessEnforcement:
    """GL1 T0.4: staleness.enforcement_for() must actually gate the engine, not just
    exist as an unused module. Drives the shell-layer guards directly (decision logic
    in strategies/strategy_exit.py is untouched — RS1/R1)."""

    def _engine(self):
        return PaperStrategyEngine(
            total_nav_usdt=100_000,
            skip_hawkes=True,
            skip_tda=True,
            hawkes_params=(0.1, 0.3, 0.5),
        )

    def test_s1_entry_blocked_when_funding_oi_stale(self):
        eng = self._engine()
        ts = datetime(2024, 1, 5, tzinfo=timezone.utc)
        eng._maybe_open_s1(_FakeS1Decision("ENTER_LONG", "LONG"), "Coiling", 30000.0, ts, blocked=True)
        assert len(eng.accounts.subaccount_1.open_positions) == 0

    def test_s1_entry_opens_when_not_blocked(self):
        eng = self._engine()
        ts = datetime(2024, 1, 5, tzinfo=timezone.utc)
        eng._maybe_open_s1(_FakeS1Decision("ENTER_LONG", "LONG"), "Coiling", 30000.0, ts, blocked=False)
        assert len(eng.accounts.subaccount_1.open_positions) == 1

    def test_s2_entry_blocked_when_ticks_stale(self):
        eng = self._engine()
        eng._s2_trail = []  # normally initialised by process_frame(); direct-call test
        ts = datetime(2024, 1, 5, tzinfo=timezone.utc)
        trig = CUSUMTrigger(
            triggered=True,
            direction="LONG",
            cusum_positive=3.0,
            cusum_negative=0.0,
            threshold=1.0,
            intensity_coeff=3.0,
        )
        eng._maybe_open_s2(
            t_unix=1_000_000.0,
            trig=trig,
            state="Surging",
            mark=30000.0,
            ts=ts,
            vocab={"Absorption", "Sweep"},
            flow_dir=1.0,
            blocked=True,
        )
        assert len(eng.accounts.subaccount_2.open_positions) == 0

    def test_cusum_reversal_exit_suppressed_but_drawdown_stop_continues(self):
        """The matrix's key distinction: CUSUM-reversal exit pauses, drawdown/time
        stops don't — same stale-ticks condition, different exit reasons."""
        eng = self._engine()
        ts = datetime(2024, 1, 5, tzinfo=timezone.utc)
        eng._maybe_open_s1(_FakeS1Decision("ENTER_LONG", "LONG"), "Coiling", 30000.0, ts)
        assert len(eng.accounts.subaccount_1.open_positions) == 1

        # A CUSUM-reverse trigger alone (no drawdown, no Cascade) would normally EXIT_FULL —
        # with pause_cusum_reversal=True it must be suppressed (position stays open).
        reverse_trig = CUSUMTrigger(
            triggered=True,
            direction="SHORT",
            cusum_positive=0.0,
            cusum_negative=2.5,
            threshold=1.0,
            intensity_coeff=0.0,
        )
        eng._prev_state = "Coiling"
        eng._manage_s1_exits(
            "Coiling",
            30000.0,
            ts + timedelta(hours=4),
            reverse_trig,
            pause_cusum_reversal=True,
        )
        assert len(eng.accounts.subaccount_1.open_positions) == 1  # CUSUM reversal suppressed

        # The SAME stale condition must not block a drawdown stop (-5% move).
        eng._manage_s1_exits(
            "Drifting-Calm",
            30000.0 * 0.95,
            ts + timedelta(hours=8),
            CUSUMTrigger(False, None, 0.0, 0.0, 2.0, 0.0),
            pause_cusum_reversal=True,
        )
        assert len(eng.accounts.subaccount_1.open_positions) == 0  # drawdown stop unaffected
        assert eng.accounts.subaccount_1.closed_positions[-1].pnl_usdt < 0

    def test_cusum_reversal_exit_fires_when_not_paused(self):
        eng = self._engine()
        ts = datetime(2024, 1, 5, tzinfo=timezone.utc)
        eng._maybe_open_s1(_FakeS1Decision("ENTER_LONG", "LONG"), "Coiling", 30000.0, ts)
        reverse_trig = CUSUMTrigger(
            triggered=True,
            direction="SHORT",
            cusum_positive=0.0,
            cusum_negative=2.5,
            threshold=1.0,
            intensity_coeff=0.0,
        )
        eng._prev_state = "Coiling"
        eng._manage_s1_exits(
            "Coiling",
            30000.0,
            ts + timedelta(hours=4),
            reverse_trig,
            pause_cusum_reversal=False,
        )
        assert len(eng.accounts.subaccount_1.open_positions) == 0

    def test_process_frame_staleness_only_gates_the_current_bar(self):
        """Staleness is a live/wall-clock concept — replaying history under
        process_frame() must not retroactively flag old bars as stale."""
        from sel_v2.runtime.staleness import StalenessEnforcement

        eng = self._engine()
        df = _bars(200)
        staleness = {
            "funding_oi": StalenessEnforcement(
                source="funding_oi",
                stale=True,
                reason_code="STALE_FUNDING_OI",
                block_s1_entry=True,
            )
        }
        eng.process_frame(df, staleness=staleness)
        rows = eng.decision_trail(last_n=200)
        s1_rows = sorted((r for r in rows if r[1] == "strategy_1"), key=lambda r: r[0])
        # Only the LAST bar's snapshot should carry the stale reason code.
        assert all("stale_reason_codes" not in r[-1] for r in s1_rows[:-1])
        assert s1_rows[-1][-1].get("stale_reason_codes") == ["STALE_FUNDING_OI"]
