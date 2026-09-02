from __future__ import annotations

import pytest

from selene.qualification.paper_chain import run_paper_chain
from shared.runtime.release_identity import ExecModeError


def test_paper_chain_happy_path_zero_duplicate_side_effects():
    result = run_paper_chain(environ={"EXEC_MODE": "PAPER"})
    names = [s.name for s in result.stages]
    assert names == [
        "scanner",
        "signal",
        "portfolio",
        "risk",
        "execution",
        "order_lifecycle",
        "gateway",
    ]
    assert all(s.status == "PASS" for s in result.stages)
    assert result.exec_mode == "PAPER"
    assert result.order_state == "CLOSED"
    assert result.side_effect_submits == 1
    assert result.duplicate_side_effects == 0
    assert result.headers["X-Actor"] == "qualification"


def test_paper_chain_refuses_live_mode():
    with pytest.raises(RuntimeError, match="PAPER"):
        run_paper_chain(environ={"EXEC_MODE": "LIMITED_LIVE"})


def test_paper_chain_refuses_unknown_mode():
    with pytest.raises(ExecModeError):
        run_paper_chain(environ={"EXEC_MODE": "LIVE"})
