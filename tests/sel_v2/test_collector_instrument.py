"""Instrument-consistency tests (audit P0-2).

The strategy trades the BTC-USDT PERPETUAL. Before this fix the price/microstructure
feed (ticks + order book) was collected from SPOT BTC-USDT while OI/funding/liquidations
came from the swap — so the state machine ran on a different instrument than it traded
(spot/perp basis drift). All v2 collectors must now subscribe to the swap instId while
storing the shared base symbol so downstream joins still line up.
"""
import importlib

import pytest

COLLECTORS = [
    "sel_v2.data.v2_tick_collector",
    "sel_v2.data.v2_lob_collector",
    "sel_v2.data.v2_liquidation_collector",
    "sel_v2.data.v2_derivatives_collector",
]


@pytest.mark.parametrize("modname", COLLECTORS)
def test_feeds_the_perp_stores_the_base(modname):
    m = importlib.import_module(modname)
    assert m.INST_ID == "BTC-USDT-SWAP", f"{modname} must subscribe to the perp swap"
    assert m.BASE_SYMBOL == "BTC-USDT", f"{modname} must store the shared base symbol"


@pytest.mark.parametrize("modname", COLLECTORS)
def test_inst_id_derives_from_base_symbol(monkeypatch, modname):
    # Changing SYMBOLS must move the instId with it (still a swap).
    monkeypatch.setenv("SYMBOLS", "ETH-USDT")
    for k in ("TICK_INST_ID", "LOB_INST_ID", "LIQ_INST_ID", "DERIV_INST_ID"):
        monkeypatch.delenv(k, raising=False)
    m = importlib.reload(importlib.import_module(modname))
    assert m.BASE_SYMBOL == "ETH-USDT"
    assert m.INST_ID == "ETH-USDT-SWAP"
    monkeypatch.undo()
    importlib.reload(m)  # restore module-level defaults for other tests
