"""Idempotency-key helpers for order placement (Phase 3 execution-safety)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from services.execution.adapters.base import is_duplicate_order, sanitize_client_id


class TestSanitize:
    def test_uuid_becomes_32_hex(self):
        cid = sanitize_client_id("3f2504e0-4f89-41d3-9a0c-0305e82c3301")
        assert cid == "3f2504e04f8941d39a0c0305e82c3301"
        assert len(cid) == 32 and cid.isalnum()

    def test_truncates_to_32(self):
        assert len(sanitize_client_id("a" * 50)) == 32

    def test_empty(self):
        assert sanitize_client_id("") == ""

    def test_open_close_keys_distinct_and_safe(self):
        # mirrors services/execution/main.py:_client_oid
        def client_oid(order_id, kind="O"):
            return f"{order_id.replace('-', '')[:31]}{kind}"

        oid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
        o, c = client_oid(oid, "O"), client_oid(oid, "C")
        assert o != c
        for k in (o, c):
            assert len(sanitize_client_id(k)) <= 32


class TestDuplicateDetection:
    def test_binance_duplicate_code(self):
        assert is_duplicate_order("-2010", "Duplicate order sent.")
        assert is_duplicate_order("-4015", "")

    def test_okx_duplicate_code(self):
        assert is_duplicate_order("51020", "")

    def test_message_marker(self):
        assert is_duplicate_order("", "clOrdId already exists")
        assert is_duplicate_order("", "Order was REPEATED")

    def test_non_duplicate(self):
        assert not is_duplicate_order("51008", "Insufficient balance")
        assert not is_duplicate_order("", "rate limited")
