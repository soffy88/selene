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

from sel_v2.strategies.params_loader import load_strategy_params, save_strategy_params


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
    """Simulate asyncpg fetch() returning rows from the flat v2_strategy_params
    schema: param_key (strategy-prefixed) / param_value (jsonb-as-string). Tests
    use strategy='h2', so keys are 'h2_<name>'."""
    return [{"param_key": f"h2_{k}", "param_value": str(v)} for k, v in param_map.items()]


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


# ── Writer round-trip (the chain that silently disabled Strategy 2) ────────────

class _InMemoryConn:
    """A minimal in-memory stand-in for an asyncpg connection backed by a shared
    dict, so save_strategy_params -> load_strategy_params -> from_h2_reference can
    be exercised end-to-end WITHOUT mocking load_strategy_params (the gap that let
    the H2 calibration -> DB break go unnoticed). Emulates JSONB scalars returning
    as strings, as asyncpg does by default."""

    def __init__(self, store: dict):
        self._store = store

    async def executemany(self, query, args):       # save path
        assert "INSERT INTO v2_strategy_params" in query
        for key, value in args:
            self._store[key] = value                 # value is a JSON string
        return None

    async def fetch(self, query, *args):             # load path
        assert "FROM v2_strategy_params" in query
        if args:                                     # WHERE param_key = ANY($1)
            keys = args[0]
            return [{"param_key": k, "param_value": self._store[k]}
                    for k in keys if k in self._store]
        return [{"param_key": k, "param_value": v}   # SELECT all (paper_engine)
                for k, v in self._store.items()]

    async def close(self):
        return None


def _patch_inmemory(store: dict):
    return patch("asyncpg.connect", AsyncMock(return_value=_InMemoryConn(store)))


def test_save_then_load_roundtrip():
    """save_strategy_params writes '{strategy}_{name}' keys that load_strategy_params
    reads back as floats — the exact key contract between offline calibration and
    the live strategies."""
    store: dict = {}
    dsn = "postgresql://test:test@localhost/test"
    with _patch_inmemory(store):
        save_strategy_params("h2", {
            "mu_ref": 0.093136,
            "alpha_ref": 0.023899,
            "beta_ref": 0.043163,
            "branching_ratio_threshold": 0.85,
        }, db_url=dsn)

        # keys are strategy-prefixed in storage
        assert set(store) == {
            "h2_mu_ref", "h2_alpha_ref", "h2_beta_ref", "h2_branching_ratio_threshold",
        }

        got = load_strategy_params("h2", ["mu_ref", "alpha_ref", "beta_ref"], db_url=dsn)
    assert got["mu_ref"] == pytest.approx(0.093136, rel=1e-6)
    assert got["alpha_ref"] == pytest.approx(0.023899, rel=1e-6)
    assert got["beta_ref"] == pytest.approx(0.043163, rel=1e-6)


def test_calibration_persist_enables_strategy2_params():
    """Regression for the silent S2 disable: after the H2 reference params exist,
    HawkesParams.from_h2_reference() must succeed through the REAL params_loader
    (no mock), rather than raise RuntimeError and disable Strategy 2."""
    from sel_v2.strategies.hawkes_intensity import HawkesParams

    store: dict = {}
    dsn = "postgresql://test:test@localhost/test"
    with _patch_inmemory(store):
        # Before calibration: the rows are absent -> S2 would be disabled.
        with pytest.raises(RuntimeError):
            HawkesParams.from_h2_reference(db_url=dsn)

        save_strategy_params("h2", {
            "mu_ref": 0.093136, "alpha_ref": 0.023899, "beta_ref": 0.043163,
        }, db_url=dsn)

        params = HawkesParams.from_h2_reference(db_url=dsn)

    assert params.mu == pytest.approx(0.093136, rel=1e-6)
    assert params.alpha == pytest.approx(0.023899, rel=1e-6)
    assert params.beta == pytest.approx(0.043163, rel=1e-6)
    assert params.branching_ratio == pytest.approx(0.023899 / 0.043163, rel=1e-6)
