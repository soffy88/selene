"""
Shared fixtures for sel_v2 unit tests.

Autouse fixture: patches params_loader.load_strategy_params so that tests
never need a live DB connection.
"""

from unittest.mock import patch

import pytest

# Wave 1 H2 MLE calibration results
_H2_PARAMS = {
    "mu_ref": 0.093136,
    "alpha_ref": 0.023899,
    "beta_ref": 0.043163,
    "branching_ratio_threshold": 0.85,
    "event_sigma": 1.0,
    "eta_ref": 0.5537,
}

_TDA1_PARAMS = {
    "l1_threshold_p95": 0.000097,
    "rolling_window_bars": 540.0,
}


def _params_side_effect(strategy: str, param_names: list, **kwargs) -> dict:
    """Return appropriate params dict based on strategy name."""
    if strategy == "h2":
        return {k: _H2_PARAMS[k] for k in param_names if k in _H2_PARAMS}
    if strategy == "tda1":
        return {k: _TDA1_PARAMS[k] for k in param_names if k in _TDA1_PARAMS}
    return {}


@pytest.fixture(autouse=True)
def mock_params_loader():
    """Inject strategy params without a live DB for all sel_v2 tests."""
    with patch(
        "sel_v2.strategies.params_loader.load_strategy_params",
        side_effect=_params_side_effect,
    ):
        yield
