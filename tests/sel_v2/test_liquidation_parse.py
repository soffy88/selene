"""Liquidation-collector parse tests (SEL P0-2: v2_liquidations was永远 empty).

Captured from the real OKX liquidation-orders channel: instId lives on the OUTER item;
details[].instId is always None. The collector filtered on detail.instId, so every
liquidation was skipped → 0 rows → the Cascade liquidation-pulse defense was dead.
"""

from datetime import timezone

from sel_v2.data.v2_liquidation_collector import extract_liquidation_rows

# Real shape (see probe): instId on item, details carry side/sz/bkPx/bkLoss/ts, no instId.
_MSG = {
    "arg": {"channel": "liquidation-orders", "instType": "SWAP"},
    "data": [
        {
            "instId": "BTC-USDT-SWAP",
            "details": [
                {"instId": None, "side": "sell", "sz": "12", "bkPx": "60000.5", "bkLoss": "3.2", "ts": "1782862113207"}
            ],
        },
        {
            "instId": "O-USDT-SWAP",
            "details": [
                {"instId": None, "side": "buy", "sz": "446", "bkPx": "0.5157", "bkLoss": "0", "ts": "1782862113207"}
            ],
        },
    ],
}


def test_filters_on_outer_instid_and_maps_details():
    rows = extract_liquidation_rows(_MSG, "BTC-USDT-SWAP", "BTC-USDT")
    assert len(rows) == 1  # only the BTC item, O-USDT skipped
    ts, symbol, side, size, price, loss = rows[0]
    assert symbol == "BTC-USDT" and side == "sell"
    assert size == 12.0 and price == 60000.5 and loss == 3.2
    assert ts.tzinfo == timezone.utc


def test_old_filter_would_have_dropped_everything():
    # Sanity: details[].instId is None, so the old `detail.instId == inst_id` filter matched nothing.
    assert all(d["instId"] is None for it in _MSG["data"] for d in it["details"])


def test_missing_bkpx_defaults_zero():
    msg = {
        "data": [{"instId": "BTC-USDT-SWAP", "details": [{"side": "buy", "sz": "1", "ts": "1782862113207"}]}]
    }  # no bkPx/bkLoss
    rows = extract_liquidation_rows(msg, "BTC-USDT-SWAP", "BTC-USDT")
    assert rows[0][4] == 0.0 and rows[0][5] == 0.0


def test_no_data_key_safe():
    assert extract_liquidation_rows({"event": "subscribe"}, "BTC-USDT-SWAP", "BTC-USDT") == []
