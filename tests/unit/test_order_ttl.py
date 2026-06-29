"""Working-order TTL decision tests (optimization item #11)."""
from services.execution.main import ttl_action
from services.execution.statemachine.order_fsm import OrderState as S


def test_unfilled_open_past_ttl_cancels():
    assert ttl_action(S.OPEN, 0.0, age_s=400, ttl_s=300) == "cancel"


def test_partial_past_ttl_accepts():
    assert ttl_action(S.PARTIALLY_FILLED, 0.5, age_s=400, ttl_s=300) == "accept_partial"


def test_fresh_order_left_alone():
    assert ttl_action(S.OPEN, 0.0, age_s=100, ttl_s=300) is None


def test_non_working_states_ignored():
    for st in (S.MONITORING, S.FILLED, S.CLOSED, S.QUEUED, S.CANCELLED):
        assert ttl_action(st, 0.0, age_s=99999, ttl_s=300) is None


def test_boundary_exactly_ttl_left_alone():
    # age == ttl is not yet past the deadline
    assert ttl_action(S.OPEN, 0.0, age_s=300, ttl_s=300) is None
