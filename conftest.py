import importlib.util
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("MARKET_DATA_PROVIDER", "OKX")
os.environ.setdefault("TIMESCALE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


# ── private quant stack (oprim/oskill) availability ────────────────────────────
# oprim/oskill are private wheels, absent on public CI runners. 51 tests import
# them at *runtime*, deep inside production modules, so a module-level
# importorskip cannot catch them. Skipping the 19 affected files wholesale would
# drop 222 currently-passing tests to hide 51 — so convert ONLY that exact
# failure into a skip, and only when the stack is genuinely unavailable. Every
# other error, including a ModuleNotFoundError for anything else, is re-raised
# untouched, so this cannot mask a real regression.
#
# This is a bridge, not a fix: while it holds, CI does NOT verify any
# private-stack code path. The durable fix is making oprim/oskill installable in
# CI (PRIVATE_INDEX_URL), which also unblocks the docker smoke build.
_PRIVATE_STACK = ("oprim", "oskill")

if importlib.util.find_spec("oprim") is None:

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(item):
        try:
            return (yield)
        except ModuleNotFoundError as exc:
            if exc.name in _PRIVATE_STACK:
                pytest.skip(f"private quant stack ({exc.name}) not installed")
            raise
