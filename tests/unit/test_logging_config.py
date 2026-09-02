"""JSON logging helper tests (optimization item #23)."""

import json
import logging

from shared.logging_config import JsonFormatter, get_request_id, set_request_id


def _record(msg="hello", level=logging.INFO, name="svc.mod"):
    return logging.LogRecord(name=name, level=level, pathname=__file__, lineno=1, msg=msg, args=(), exc_info=None)


def test_json_formatter_basic_fields():
    set_request_id("")
    out = JsonFormatter(service="risk").format(_record("started"))
    d = json.loads(out)
    assert d["level"] == "INFO"
    assert d["logger"] == "svc.mod"
    assert d["msg"] == "started"
    assert d["service"] == "risk"
    assert "ts" in d
    assert "request_id" not in d  # omitted when unset


def test_json_formatter_includes_request_id():
    set_request_id("order-123")
    try:
        d = json.loads(JsonFormatter().format(_record()))
        assert d["request_id"] == "order-123"
        assert get_request_id() == "order-123"
    finally:
        set_request_id("")


def test_json_formatter_args_interpolation():
    rec = logging.LogRecord("n", logging.WARNING, __file__, 1, "x=%d", (5,), None)
    d = json.loads(JsonFormatter().format(rec))
    assert d["msg"] == "x=5"
    assert d["level"] == "WARNING"


def test_json_formatter_exception_field():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord("n", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    d = json.loads(JsonFormatter().format(rec))
    assert "exc" in d and "ValueError" in d["exc"]
