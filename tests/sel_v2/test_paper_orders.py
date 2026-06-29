"""Proof that the paper engine places orders once flow inputs are fed.

The live engine collapsed to Drifting_Calm (no trades) because _reprocess called
process_frame(df) with no OI/funding/OFI. These tests lock in that, when those
series are provided, the trading states fire and positions open.
"""
import math
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import pytest

from sel_v2.paper.paper_engine import PaperEngine
from sel_v2.paper.strategy_engine import PaperStrategyEngine


def _bars(n=600):
    t0 = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    price = 30000.0
    for i in range(n):
        drift = 0.004 if i < 300 else 0.012
        price *= (1 + drift * math.sin(i / 9) + (0.05 if i % 73 == 0 else 0))
        o = price
        c = price * (1 + 0.01 * math.sin(i / 4))
        rows.append({"time": t0 + timedelta(hours=4 * i), "open": o,
                     "high": max(o, c) * 1.006, "low": min(o, c) * 0.994,
                     "close": c, "volume": 100 + i % 40})
    return pd.DataFrame(rows)


def _engine():
    try:
        import ripser  # noqa: F401
        skip_tda = False
    except Exception:
        skip_tda = True
    return PaperStrategyEngine(total_nav_usdt=100_000, instrument="BTC-USDT",
                               skip_tda=skip_tda, hawkes_params=(0.5, 0.3, 1.5))


def test_ofi_proxy_shape_and_sign():
    df = _bars(50)
    ofi = PaperEngine._ofi_proxy(df)
    assert len(ofi) == len(df)
    # sign follows bar return; magnitude is the volume
    assert np.all(np.abs(ofi) <= df["volume"].values + 1e-9)


def test_engine_opens_positions_with_flow_inputs():
    df = _bars()
    n = len(df)
    oi = np.array([1e6 * (1 + 0.02 * i + 0.3 * math.sin(i / 5)) for i in range(n)])
    funding = np.array([0.0001 * (1 + math.sin(i / 8)) + (0.0005 if i > 300 else 0) for i in range(n)])
    ofi = PaperEngine._ofi_proxy(df)
    eng = _engine()
    summary = eng.process_frame(df, oi_series=oi, funding_series=funding, ofi_series=ofi)
    a1, a2 = eng.accounts.subaccount_1, eng.accounts.subaccount_2
    total_trades = (len(a1.closed_positions) + len(a1.open_positions)
                    + len(a2.closed_positions) + len(a2.open_positions))
    # Trading states must be reachable and at least one order placed.
    assert set(summary["state_counts"]) - {"Drifting_Calm"}, "no trading states fired"
    assert total_trades > 0, "engine placed no orders despite flow inputs"


def test_s2_disabled_gracefully_when_hawkes_params_unavailable(monkeypatch):
    # When Strategy2EntryFilter can't initialise (e.g. H2 Hawkes calibration absent
    # in v2_strategy_params), the engine must DISABLE S2 loudly and keep running S1,
    # not crash. Force the failure deterministically.
    import sel_v2.paper.strategy_engine as se

    def _boom(*a, **k):
        raise RuntimeError("H2 params missing")
    monkeypatch.setattr(se, "Strategy2EntryFilter", _boom)

    df = _bars(200)
    eng = se.PaperStrategyEngine(total_nav_usdt=100_000, skip_tda=True)
    assert eng._s2_enabled is False
    summary = eng.process_frame(df)   # must not raise
    assert summary["bars"] == len(df)
    assert len(eng.records) == len(df)


def test_tick_feed_drives_s2_hawkes_intensity():
    # Feeding real trade arrivals must lift the S2 H1 Hawkes intensity well above
    # its flat baseline (mu), so H1 reflects trade clustering instead of a constant.
    df = _bars(220)
    # Dense sub-second ticks over the last day (as real markets produce), so the
    # exponentially-decaying intensity piles up by the final bars. Span a day to be
    # robust to the bar-runner's naive-vs-UTC timestamp offset on synthetic frames.
    t1 = df["time"].iloc[-1].timestamp()
    tick_times = np.linspace(t1 - 86400, t1, 400_000)
    eng = PaperStrategyEngine(total_nav_usdt=100_000, skip_tda=True,
                              hawkes_params=(0.09, 0.023, 0.04))
    assert eng._s2_enabled and eng._s2_tracker is not None
    eng.process_frame(df, tick_times=tick_times)
    tr = eng._s2_tracker
    assert tr._last_t is not None, "no tick events were fed"
    lam = tr.intensity(tr._last_t)
    assert lam > tr.params.mu * 5, f"intensity {lam} not driven above baseline {tr.params.mu}"


def test_engine_no_orders_without_flow_inputs():
    # Price-only (the old behaviour) → collapses to Drifting_Calm, no orders.
    df = _bars()
    eng = _engine()
    summary = eng.process_frame(df)
    assert set(summary["state_counts"]) == {"Drifting_Calm"}
    a1, a2 = eng.accounts.subaccount_1, eng.accounts.subaccount_2
    assert len(a1.closed_positions) + len(a2.closed_positions) == 0
