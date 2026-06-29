"""Paper-engine persistence tests (optimization item #6).

The deployed v2 paper engine never wrote v2_trades / v2_state_history, so its
own API (sel_v2/paper_interface/api.py) read empty tables. These tests verify
the persistence wiring without a DB: a deterministic trade id (idempotent across
the engine's full-history replays) and that _persist_results maps positions to
the writer correctly.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from sel_v2.paper.paper_engine import PaperEngine, _trade_id
from sel_v2.strategies.sub_account import Position, ClosedPosition


def test_trade_id_is_deterministic_and_distinct():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = _trade_id("strategy_1", "subaccount_1", t, "LONG", 30000.0)
    b = _trade_id("strategy_1", "subaccount_1", t, "LONG", 30000.0)
    c = _trade_id("strategy_1", "subaccount_1", t, "SHORT", 30000.0)
    assert a == b           # idempotent across replays
    assert a != c           # different trade → different id
    assert len(a) == 36     # uuid string


class _RecordingWriter:
    def __init__(self):
        self.trades = []
        self.state_calls = 0

    async def write_states_bulk(self, records):
        self.state_calls += 1
        return len(records)

    async def upsert_trade(self, **kw):
        self.trades.append(kw)
        return True


def _pos(**kw):
    base = dict(id="x", strategy="strategy_1", sub_account="subaccount_1",
                direction="LONG", entry_price=30000.0, size_usdt=1000.0,
                leverage=2.0, instrument="BTC-USDT",
                entry_time=datetime(2026, 1, 1, tzinfo=timezone.utc), entry_state="Critical")
    base.update(kw)
    return Position(**base)


def _make_engine():
    open_pos = _pos(id="o1", entry_price=31000.0)
    closed_inner = _pos(id="c1", entry_price=30000.0)
    closed = ClosedPosition(position=closed_inner, exit_price=30500.0,
                            exit_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
                            exit_reason="EXIT_FULL", pnl_usdt=33.3, pnl_pct=0.0166)
    a1 = SimpleNamespace(open_positions=[open_pos], closed_positions=[closed])
    a2 = SimpleNamespace(open_positions=[], closed_positions=[])
    accounts = SimpleNamespace(subaccount_1=a1, subaccount_2=a2)
    return SimpleNamespace(records=["r1", "r2", "r3"], accounts=accounts)


def test_persist_results_maps_positions_and_states():
    eng = _make_engine()
    pe = PaperEngine()
    pe._writer = _RecordingWriter()
    asyncio.run(pe._persist_results(eng))

    w = pe._writer
    assert w.state_calls == 1                      # state history persisted
    assert len(w.trades) == 2                      # one closed + one open

    by_state = {t["entry_state"]: t for t in w.trades}
    # Closed trade carries exit fields
    closed = next(t for t in w.trades if t["exit_price"] is not None)
    assert closed["exit_reason"] == "EXIT_FULL"
    assert closed["pnl_usdt"] == 33.3
    # Open trade has no exit fields
    openrec = next(t for t in w.trades if t.get("exit_price") is None)
    assert openrec["entry_price"] == 31000.0


def test_persist_results_idempotent_ids_across_calls():
    eng = _make_engine()
    pe = PaperEngine()
    pe._writer = _RecordingWriter()
    asyncio.run(pe._persist_results(eng))
    asyncio.run(pe._persist_results(eng))
    ids_first = {t["trade_id"] for t in pe._writer.trades[:2]}
    ids_second = {t["trade_id"] for t in pe._writer.trades[2:]}
    assert ids_first == ids_second  # same logical trades → same ids → DB upsert dedups


def test_persist_results_noop_without_writer():
    eng = _make_engine()
    pe = PaperEngine()  # _writer is None
    asyncio.run(pe._persist_results(eng))  # must not raise
