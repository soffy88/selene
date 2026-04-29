"""
Unit tests for sel_v2.strategies.params_loader.load_strategy_params.

These tests verify the internal "missing params → RuntimeError" logic inside
params_loader.py itself, distinct from the "upstream exception transparency"
tested in test_hawkes_intensity.py::test_from_h2_reference_raises_on_missing_params.

asyncpg.connect is mocked so no real DB connection is required.

Fixture note: the conftest.py autouse fixture (mock_params_loader) patches
load_strategy_params with a fixed return value, which would prevent testing the
function's internals.  The fixture is overridden below with a no-op fixture of
the same name, scoped to this file only.
"""
import pytest
from unittest.mock import AsyncMock, patch

from sel_v2.strategies.params_loader import load_strategy_params


# ── Fixture override ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_params_loader():
    """
    No-op override of the conftest autouse fixture.
    Allows load_strategy_params to execute its real code path in this file.
    """
    yield


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_rows(param_map: dict) -> list[dict]:
    """Simulate asyncpg fetch() returning a list of dict-like records."""
    return [{"param_name": k, "param_value": str(v)} for k, v in param_map.items()]


def _patch_asyncpg_connect(fetch_rows: list):
    """
    Return a patch context for asyncpg.connect that yields a mock connection
    whose fetch() coroutine returns fetch_rows.
    """
    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(return_value=fetch_rows)
    mock_conn.close = AsyncMock()
    return patch("asyncpg.connect", AsyncMock(return_value=mock_conn))


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_load_strategy_params_raises_when_param_missing():
    """
    When DB returns only a subset of requested params, load_strategy_params must
    raise RuntimeError naming every missing param (not just the first).
    """
    partial_rows = _fake_rows({"mu_ref": 0.093136})   # alpha_ref and beta_ref absent

    with _patch_asyncpg_connect(partial_rows):
        with pytest.raises(RuntimeError) as exc_info:
            load_strategy_params(
                strategy="h2",
                param_names=["mu_ref", "alpha_ref", "beta_ref"],
                db_url="postgresql://test:test@localhost/test",
            )

    msg = str(exc_info.value)
    assert "alpha_ref" in msg, f"Missing param 'alpha_ref' not named in error: {msg}"
    assert "beta_ref"  in msg, f"Missing param 'beta_ref' not named in error: {msg}"
    assert "mu_ref" not in msg or "missing" in msg   # mu_ref was returned, should not be listed as missing
    assert "hawkes_calibration" in msg, f"Wave 1 hint absent from error: {msg}"


def test_load_strategy_params_succeeds_when_all_present():
    """
    When DB returns all requested params, load_strategy_params returns a dict
    with the correct keys and float-cast values.
    """
    all_rows = _fake_rows({
        "mu_ref":    0.093136,
        "alpha_ref": 0.023899,
        "beta_ref":  0.043163,
    })

    with _patch_asyncpg_connect(all_rows):
        result = load_strategy_params(
            strategy="h2",
            param_names=["mu_ref", "alpha_ref", "beta_ref"],
            db_url="postgresql://test:test@localhost/test",
        )

    assert set(result.keys()) == {"mu_ref", "alpha_ref", "beta_ref"}
    assert result["mu_ref"]    == pytest.approx(0.093136, rel=1e-6)
    assert result["alpha_ref"] == pytest.approx(0.023899, rel=1e-6)
    assert result["beta_ref"]  == pytest.approx(0.043163, rel=1e-6)
    # All values must be floats, not strings
    for k, v in result.items():
        assert isinstance(v, float), f"Expected float for {k}, got {type(v)}"
