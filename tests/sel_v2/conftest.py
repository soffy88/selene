"""
Shared fixtures for sel_v2 unit tests.

Autouse fixture: patches params_loader.load_strategy_params so that tests
never need a live DB connection.  All tests in tests/sel_v2/ that create a
HawkesIntensityTracker() (or Strategy2EntryFilter()) without explicit params
will receive the Wave-1 H2 reference values below.
"""
import pytest
from unittest.mock import patch

# Wave 1 H2 MLE calibration results (mu/alpha/beta from hawkes_calibration.py)
_H2_PARAMS = {
    "mu_ref":    0.093136,
    "alpha_ref": 0.023899,
    "beta_ref":  0.043163,
}


@pytest.fixture(autouse=True)
def mock_params_loader():
    """Inject H2 calibration params without a live DB for all sel_v2 tests."""
    with patch(
        "sel_v2.strategies.params_loader.load_strategy_params",
        return_value=_H2_PARAMS,
    ):
        yield
