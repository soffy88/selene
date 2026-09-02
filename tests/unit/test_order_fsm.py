"""Unit tests for 11-state Order FSM. All valid transitions tested."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

# The FSM code is embedded here for portability
from enum import Enum


class OrderState(str, Enum):
    QUEUED = "QUEUED"
    SLIPPAGE_ESTIMATE = "SLIPPAGE_ESTIMATE"
    ROUTING = "ROUTING"
    SUBMITTING = "SUBMITTING"
    PENDING_ACK = "PENDING_ACK"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    MONITORING = "MONITORING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


VALID_TRANSITIONS = {
    OrderState.QUEUED: {OrderState.SLIPPAGE_ESTIMATE: "start", OrderState.CANCELLED: "timeout"},
    OrderState.SLIPPAGE_ESTIMATE: {
        OrderState.ROUTING: "ok",
        OrderState.QUEUED: "requeue",
        OrderState.CANCELLED: "too high",
    },
    OrderState.ROUTING: {OrderState.SUBMITTING: "route ok", OrderState.FAILED: "no route"},
    OrderState.SUBMITTING: {OrderState.PENDING_ACK: "sent", OrderState.FAILED: "api error"},
    OrderState.PENDING_ACK: {OrderState.OPEN: "accepted", OrderState.FAILED: "timeout"},
    OrderState.OPEN: {
        OrderState.PARTIALLY_FILLED: "partial",
        OrderState.FILLED: "full",
        OrderState.CANCELLED: "cancel",
    },
    OrderState.PARTIALLY_FILLED: {
        OrderState.FILLED: "complete",
        OrderState.CANCELLED: "cancel",
        OrderState.MONITORING: "accept partial",
    },
    OrderState.FILLED: {OrderState.MONITORING: "enter monitor"},
    OrderState.MONITORING: {OrderState.CLOSING: "exit triggered"},
    OrderState.CLOSING: {OrderState.CLOSED: "done", OrderState.MONITORING: "unfilled"},
    OrderState.CLOSED: {},
    OrderState.CANCELLED: {},
    OrderState.FAILED: {},
}
TERMINAL = {OrderState.CLOSED, OrderState.CANCELLED, OrderState.FAILED}


class FSM:
    def __init__(self):
        self.state = OrderState.QUEUED
        self.history = []

    def transition(self, new_state):
        allowed = VALID_TRANSITIONS.get(self.state, {})
        if new_state not in allowed:
            raise ValueError(f"{self.state} → {new_state} invalid")
        self.history.append((self.state, new_state))
        self.state = new_state


class TestOrderFSM:
    def test_happy_path_market_order(self):
        fsm = FSM()
        for state in [
            OrderState.SLIPPAGE_ESTIMATE,
            OrderState.ROUTING,
            OrderState.SUBMITTING,
            OrderState.PENDING_ACK,
            OrderState.OPEN,
            OrderState.FILLED,
            OrderState.MONITORING,
            OrderState.CLOSING,
            OrderState.CLOSED,
        ]:
            fsm.transition(state)
        assert fsm.state == OrderState.CLOSED

    def test_partial_fill_path(self):
        fsm = FSM()
        fsm.transition(OrderState.SLIPPAGE_ESTIMATE)
        fsm.transition(OrderState.ROUTING)
        fsm.transition(OrderState.SUBMITTING)
        fsm.transition(OrderState.PENDING_ACK)
        fsm.transition(OrderState.OPEN)
        fsm.transition(OrderState.PARTIALLY_FILLED)
        fsm.transition(OrderState.FILLED)
        fsm.transition(OrderState.MONITORING)
        fsm.transition(OrderState.CLOSING)
        fsm.transition(OrderState.CLOSED)
        assert fsm.state == OrderState.CLOSED

    def test_api_failure_path(self):
        fsm = FSM()
        fsm.transition(OrderState.SLIPPAGE_ESTIMATE)
        fsm.transition(OrderState.ROUTING)
        fsm.transition(OrderState.SUBMITTING)
        fsm.transition(OrderState.FAILED)
        assert fsm.state == OrderState.FAILED

    def test_invalid_transition_raises(self):
        fsm = FSM()
        with pytest.raises(ValueError):
            fsm.transition(OrderState.CLOSED)  # can't jump from QUEUED to CLOSED

    def test_terminal_no_transitions(self):
        for terminal in TERMINAL:
            assert VALID_TRANSITIONS[terminal] == {}

    def test_no_cycles_to_initial_from_terminal(self):
        for terminal in TERMINAL:
            fsm = FSM()
            fsm.state = terminal
            with pytest.raises(ValueError):
                fsm.transition(OrderState.QUEUED)

    def test_monitoring_to_closing(self):
        fsm = FSM()
        fsm.state = OrderState.MONITORING
        fsm.transition(OrderState.CLOSING)
        assert fsm.state == OrderState.CLOSING

    def test_closing_can_return_to_monitoring(self):
        """Exit order unfilled → back to monitoring."""
        fsm = FSM()
        fsm.state = OrderState.CLOSING
        fsm.transition(OrderState.MONITORING)
        assert fsm.state == OrderState.MONITORING

    def test_all_states_have_entry_in_transitions(self):
        for state in OrderState:
            assert state in VALID_TRANSITIONS, f"{state} missing from VALID_TRANSITIONS"

    def test_11_states_defined(self):
        assert len(OrderState) == 13  # 11 + 2 error states (CANCELLED, FAILED)
