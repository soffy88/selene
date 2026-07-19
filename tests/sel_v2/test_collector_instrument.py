"""Instrument-consistency tests (audit P0-2).

The strategy trades the BTC-USDT PERPETUAL. Before this fix the price/microstructure
feed (ticks + order book) was collected from SPOT BTC-USDT while OI/funding/liquidations
came from the swap — so the state machine ran on a different instrument than it traded
(spot/perp basis drift). All v2 collectors must now feed from the perpetual while
storing the shared base symbol so downstream joins still line up.

How the perpetual is named differs per collector since the OKX→Binance migration —
see _assert_perp_instrument — but the invariant this file protects is unchanged:
perp feed, shared base symbol, instrument derived from BASE_SYMBOL.
"""

import importlib

import pytest

COLLECTORS = [
    "sel_v2.data.v2_tick_collector",
    "sel_v2.data.v2_lob_collector",
    "sel_v2.data.v2_liquidation_collector",
    "sel_v2.data.v2_derivatives_collector",
]


def _assert_perp_instrument(m, modname, base: str):
    """Assert the module feeds from `base`'s PERPETUAL, however it names it.

    Two naming styles coexist since the OKX→Binance migration (P1-P5):
      * OKX-era modules keep INST_ID, e.g. 'BTC-USDT-SWAP'.
      * Migrated modules expose FETCH_SYMBOL ('BTCUSDT') and reach the venue only
        through sel_v2.data.binance_rest, whose base is fapi.binance.com — USD-M
        futures, i.e. the perpetual. (Spot would be api.binance.com.)
    Either way the instrument must be the perp and must derive from BASE_SYMBOL.
    """
    if hasattr(m, "INST_ID"):
        assert m.INST_ID == f"{base}-SWAP", f"{modname} must subscribe to the perp swap"
        return

    from sel_v2.data import binance_rest

    assert m.FETCH_SYMBOL == binance_rest.to_binance_symbol(base), (
        f"{modname} must fetch the instrument derived from BASE_SYMBOL"
    )
    assert m.fetch_json is binance_rest.fetch_json, (
        f"{modname} must reach Binance through binance_rest (futures-only client)"
    )
    assert "fapi" in binance_rest.BINANCE_FAPI, (
        "binance_rest must target USD-M futures (the perp), not spot"
    )


@pytest.mark.parametrize("modname", COLLECTORS)
def test_feeds_the_perp_stores_the_base(modname):
    m = importlib.import_module(modname)
    _assert_perp_instrument(m, modname, "BTC-USDT")
    assert m.BASE_SYMBOL == "BTC-USDT", f"{modname} must store the shared base symbol"


@pytest.mark.parametrize("modname", COLLECTORS)
def test_inst_id_derives_from_base_symbol(monkeypatch, modname):
    # Changing SYMBOLS must move the traded instrument with it (still the perp).
    monkeypatch.setenv("SYMBOLS", "ETH-USDT")
    for k in ("TICK_INST_ID", "LOB_INST_ID", "LIQ_INST_ID", "DERIV_INST_ID"):
        monkeypatch.delenv(k, raising=False)
    m = importlib.reload(importlib.import_module(modname))
    assert m.BASE_SYMBOL == "ETH-USDT"
    _assert_perp_instrument(m, modname, "ETH-USDT")
    monkeypatch.undo()
    importlib.reload(m)  # restore module-level defaults for other tests
