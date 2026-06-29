"""Structured (JSON) logging — opt-in via LOG_FORMAT=json (optimization item #23).

Zero-dependency JSON formatter + setup_logging() helper. Services call
setup_logging() at startup; with LOG_FORMAT=json each record is emitted as a
single-line JSON object (service, level, logger, message, timestamp, plus a
correlation id when set via set_request_id()), which a log aggregator can parse.
Default stays human text so nothing breaks until a deployment opts in.
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import json
import logging
import os

# Correlation id (e.g. a request/order id) attached to every record while set.
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


def set_request_id(value: str) -> None:
    _request_id.set(value or "")


def get_request_id() -> str:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str = ""):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": _dt.datetime.fromtimestamp(record.created, _dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if self.service:
            payload["service"] = self.service
        rid = _request_id.get()
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(service: str = "", level: int | None = None) -> None:
    """Configure the root logger. LOG_FORMAT=json → JSON lines, else human text.
    Service name is taken from the arg or the SERVICE_NAME env var."""
    service = service or os.getenv("SERVICE_NAME", "")
    lvl = level if level is not None else getattr(
        logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

    handler = logging.StreamHandler()
    if os.getenv("LOG_FORMAT", "text").lower() == "json":
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)
