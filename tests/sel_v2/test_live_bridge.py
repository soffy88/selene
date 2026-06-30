"""sel_v2 → live execution bridge tests (audit P1-1).

The bridge translates a deployed sel_v2 entry decision into the canonical ScoredSignal and
(only when explicitly enabled) publishes it onto the live pipeline's `signal.scored` stream,
so the paper-validated strategy can reach the same risk-gated / native-stop execution path.
Default OFF; nothing reaches live without the existing NOTIFY_ONLY + OOS gates.
"""
import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest

from sel_v2.paper_interface.live_bridge import (
    decision_to_scored_signal, LiveBridge, bridge_enabled,
)
from sel_v2.strategies.strategy_exit import S1_DRAWDOWN_STOP, S2_DRAWDOWN_STOP


@dataclass
class _S1Decision:
    action: str = "ENTER"
    reason: str = "ok"
    direction: Optional[str] = "LONG"
    state_4h: Optional[str] = "Coiling"
    base_size_pct: float = 0.05
    suggested_leverage: float = 1.0


@dataclass
class _S2Decision:
    action: str = "ENTER_SHORT"
    reason: str = "ok"
    cusum_direction: Optional[str] = "SHORT"
    base_size_pct: float = 0.04
    state_4h: Optional[str] = "Critical"
    suggested_leverage: float = 3.0


# ── translation ─────────────────────────────────────────────────────────────

def test_s1_long_translation_sets_stop_below_entry():
    sig = decision_to_scored_signal(_S1Decision(), symbol="BTCUSDT",
                                    entry_price=100.0, strategy="strategy_1")
    assert sig is not None
    assert sig.direction.value == "LONG"
    # stop computed from the REAL S1 drawdown-stop pct, below entry for a long
    assert sig.stop_loss == pytest.approx(100.0 * (1 + S1_DRAWDOWN_STOP))
    assert sig.stop_loss > 0 and sig.is_actionable


def test_s2_short_translation_via_action_sets_stop_above_entry():
    sig = decision_to_scored_signal(_S2Decision(), symbol="BTCUSDT",
                                    entry_price=100.0, strategy="strategy_2")
    assert sig is not None
    assert sig.direction.value == "SHORT"
    # short stop is above entry, using the S2 pct
    assert sig.stop_loss == pytest.approx(100.0 * (1 + abs(S2_DRAWDOWN_STOP)))


def test_non_entry_returns_none():
    @dataclass
    class _Obs:
        action: str = "OBSERVE"
        reason: str = "no"
    assert decision_to_scored_signal(_Obs(), symbol="BTCUSDT",
                                     entry_price=100.0, strategy="strategy_2") is None


def test_zero_entry_price_returns_none():
    assert decision_to_scored_signal(_S1Decision(), symbol="BTCUSDT",
                                     entry_price=0.0, strategy="strategy_1") is None


# ── gating ──────────────────────────────────────────────────────────────────

class _StubRedis:
    def __init__(self):
        self.published = []

    async def xadd(self, stream, payload, **kw):
        self.published.append((stream, payload))


def test_emit_is_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("SEL_V2_LIVE_BRIDGE", raising=False)
    assert bridge_enabled() is False
    r = _StubRedis()
    out = asyncio.run(LiveBridge(r).emit(_S1Decision(), symbol="BTCUSDT",
                                         entry_price=100.0, strategy="strategy_1"))
    assert out is None
    assert r.published == []   # nothing reached the live stream


def test_emit_publishes_when_enabled(monkeypatch):
    monkeypatch.setenv("SEL_V2_LIVE_BRIDGE", "on")
    r = _StubRedis()
    out = asyncio.run(LiveBridge(r).emit(_S2Decision(), symbol="BTCUSDT",
                                         entry_price=100.0, strategy="strategy_2"))
    assert out is not None
    assert len(r.published) == 1
    stream, _ = r.published[0]
    assert stream == "signal.scored"


def test_emit_noop_on_non_entry_even_when_enabled(monkeypatch):
    monkeypatch.setenv("SEL_V2_LIVE_BRIDGE", "on")
    r = _StubRedis()

    @dataclass
    class _Abort:
        action: str = "ABORT"
        reason: str = "veto"
    out = asyncio.run(LiveBridge(r).emit(_Abort(), symbol="BTCUSDT",
                                         entry_price=100.0, strategy="strategy_2"))
    assert out is None
    assert r.published == []
