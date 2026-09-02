"""Orderbook depth features: top-5 bid/ask sizes, total depth, spread."""

import logging

logger = logging.getLogger(__name__)

# WIKI_REQUIRED: All features in this module require orderbook_collector running.
# OKX endpoint: GET /api/v5/market/books?instId=BTC-USDT-SWAP&sz=20
# Collector samples every 60s; 1H bar value = mean of all samples in the bar.
# Until deployed, all functions return None stubs.


def compute_depth_features(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
) -> dict:
    """
    bids/asks: lists of (price, size) sorted best-first.
    Returns: top_5_bid_size, top_5_ask_size, total_depth, spread_bps.
    """
    if not bids or not asks:
        return {
            "top_5_bid_size": None,
            "top_5_ask_size": None,
            "total_depth": None,
            "spread_bps": None,
        }

    top5_bid = sum(size for _, size in bids[:5])
    top5_ask = sum(size for _, size in asks[:5])
    total = top5_bid + top5_ask

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread_bps = ((best_ask - best_bid) / mid * 10_000) if mid > 0 else None

    return {
        "top_5_bid_size": float(top5_bid),
        "top_5_ask_size": float(top5_ask),
        "total_depth": float(total),
        "spread_bps": float(spread_bps) if spread_bps is not None else None,
    }


def compute_depth_features_stub() -> dict:
    """Stub: returns None for all depth features until collector is running."""
    return {
        "top_5_bid_size": None,
        "top_5_ask_size": None,
        "total_depth": None,
        "spread_bps": None,
    }
