"""Phase 4 — PaperStrategyEngine: integration + orchestration wiring.

Verifies the engine that replaces the old `_process_bar` stub: the full
precompute -> recognizer -> entry filters -> exits -> sub-accounts pipeline runs,
and the open/exit orchestration (the glue this module adds) behaves correctly.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd

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
    return pd.DataFrame({"time": pd.to_datetime(times), "close": close, "open": close,
                         "high": close * 1.01, "low": close * 0.99, "volume": 1000.0})


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
        eng = PaperStrategyEngine(total_nav_usdt=100_000, skip_hawkes=True, skip_tda=True,
                                  hawkes_params=(0.1, 0.3, 0.5))
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
        return PaperStrategyEngine(total_nav_usdt=100_000, skip_hawkes=True, skip_tda=True,
                                   hawkes_params=(0.1, 0.3, 0.5))

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
        eng._maybe_open_s1(_FakeS1Decision("OBSERVE"), "Drifting-Calm", 30000.0,
                           datetime(2024, 1, 5, tzinfo=timezone.utc))
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
