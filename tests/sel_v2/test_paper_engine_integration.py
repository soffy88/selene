"""Phase 4 integration — full PaperStrategyEngine path through real entries and exits.

Unlike the orchestration unit tests (which drive the open/exit helpers directly), this feeds a
realistic bar + funding frame through process_frame so the whole pipeline executes:
  recognizer -> Drifting-Charged state -> CUSUM-Mid trigger -> S1 entry -> exit conditions -> close.

This is the test that surfaced the recognizer/strategy label mismatch ('Drifting_Charged' vs
'Drifting-Charged'); without the engine's label adapter, zero trades occur.
"""

import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

from sel_v2.paper.strategy_engine import PaperStrategyEngine


def _scenario():
    rng = np.random.default_rng(11)
    n = 560
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    times = [t0 + timedelta(hours=4 * i) for i in range(n)]
    rets = rng.normal(0, 0.01, n)
    rets[470:500] += (
        0.009  # gentle up-drift -> CUSUM-Mid accumulates while σ stays mid-band
    )
    rets[505:515] -= 0.04  # reversal/drop -> drawdown & cusum-reverse exits
    close = 30000 * np.exp(np.cumsum(rets))
    df = pd.DataFrame(
        {
            "time": pd.to_datetime(times),
            "close": close,
            "open": close,
            "high": close * 1.003,
            "low": close * 0.997,
            "volume": 1000.0,
        }
    )
    funding = np.full(
        n, 0.0008
    )  # persistent positive funding -> unlocks Drifting-Charged
    return df, funding


def test_full_pipeline_produces_real_trades():
    df, funding = _scenario()
    eng = PaperStrategyEngine(
        total_nav_usdt=100_000,
        skip_hawkes=True,
        skip_tda=True,
        hawkes_params=(0.1, 0.3, 0.5),
    )
    summary = eng.process_frame(df, funding_series=funding)

    # OI/funding unlocked the entry-eligible state
    assert summary["state_counts"].get("Drifting_Charged", 0) > 0
    # the full pipeline actually opened and closed S1 positions
    closed = eng.accounts.subaccount_1.closed_positions
    assert len(closed) >= 1, "no trades — label mismatch or entry path broken"
    # exits are driven by real strategy conditions, not a catch-all
    reasons = " ".join(c.exit_reason for c in closed).lower()
    assert any(
        k in reasons for k in ("drawdown", "cusum", "time stop", "cascade", "state")
    )


def test_nav_is_consistent_with_realized_pnl():
    df, funding = _scenario()
    eng = PaperStrategyEngine(
        total_nav_usdt=100_000,
        skip_hawkes=True,
        skip_tda=True,
        hawkes_params=(0.1, 0.3, 0.5),
    )
    eng.process_frame(df, funding_series=funding)
    a1 = eng.accounts.subaccount_1
    realized = sum(c.pnl_usdt for c in a1.closed_positions)
    # nav == initial + realized holds regardless of open positions: open_position()
    # takes no margin out of _nav (size is notional exposure) and close_position()
    # adds realized pnl back — unrealized pnl lives in equity(), never in nav.
    # (This test previously also asserted "no open positions", a scenario-shape
    # artifact of the frozen-at-2.0 CUSUM threshold era; with the adaptive threshold
    # fixed (2026-07-09) this scenario legitimately ends holding a position.)
    assert len(a1.closed_positions) >= 1  # the pipeline actually traded
    assert abs(a1.nav - (80_000.0 + realized)) < 1e-6


def test_label_adapter_bridges_recognizer_and_strategy():
    # the recognizer emits underscores; the strategies expect hyphens.
    assert PaperStrategyEngine._strategy_label("Drifting_Charged") == "Drifting-Charged"
    assert PaperStrategyEngine._strategy_label("Drifting_Calm") == "Drifting-Calm"
    assert PaperStrategyEngine._strategy_label("Cascade") == "Cascade"
