"""Tests for SmartRouter._analyze_book — price-impact aware venue costing.

Regression: the VWAP loop accumulated USD into both numerator and denominator, so
avg_price algebraically collapsed to best_price and impact_pct was ALWAYS 0 — the
router ranked venues on spread+fee only and never reflected depth/impact."""

from __future__ import annotations

import math

from services.execution.routing.smart_router import FEES, SmartRouter


def _book(asks, bids):
    return {"asks": asks, "bids": bids}


def test_impact_is_nonzero_and_correct_for_large_order():
    r = SmartRouter()
    # BUY 250 USD across asks 100/101/102 (1 unit each):
    #   fills 100@100 (1.0), 101@101 (1.0), 49@102 (0.4804) → base=2.4804
    #   avg = 250 / 2.4804 = 100.79 → impact ≈ 0.79%
    res = r._analyze_book("binance", _book([[100, 1], [101, 1], [102, 1]], [[99.9, 1]]), "BUY", 250.0, "LIMIT")
    assert res is not None
    spread_half = ((100 - 99.9) / 100) / 2
    fee = FEES["binance"]["maker"]
    expected_impact = (250 / (1.0 + 1.0 + 49 / 102) - 100) / 100
    assert math.isclose(expected_impact, 0.0079, rel_tol=5e-2)
    assert res.expected_cost_pct == round(spread_half + expected_impact + fee, 6)
    # impact must be a material part of the cost, not dropped
    assert res.expected_cost_pct > spread_half + fee + 0.005


def test_larger_order_costs_more_than_tiny_order():
    """Monotonic in size: a tiny order touches only the best level (impact≈0); a
    large order walks the book (impact>0). This fails if impact is hardcoded to 0."""
    r = SmartRouter()
    book = _book([[100, 1], [101, 1], [102, 1]], [[99.9, 1]])
    tiny = r._analyze_book("binance", book, "BUY", 10.0, "LIMIT")
    large = r._analyze_book("binance", book, "BUY", 250.0, "LIMIT")
    assert large.expected_cost_pct > tiny.expected_cost_pct


def test_sell_side_impact_uses_bids():
    r = SmartRouter()
    res = r._analyze_book("okx", _book([[100.1, 1]], [[100, 1], [99, 1], [98, 1]]), "SELL", 250.0, "LIMIT")
    assert res is not None
    assert res.expected_cost_pct > 0  # selling into descending bids → real impact


def test_empty_book_returns_none():
    r = SmartRouter()
    assert r._analyze_book("binance", _book([], []), "BUY", 100.0, "LIMIT") is None
