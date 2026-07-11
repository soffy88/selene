"""
Tests for ObservationTool base class and isolation discipline (v2.1 §2.2).

Critical check: observation tool outputs NEVER flow into Strategy 1/2 code.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from sel_v2.observation_tools.base import (
    BarFeatures,
    ObservationResult,
    ObservationTool,
    ToolEvaluationMetrics,
)
from sel_v2.observation_tools import (
    BayesianHMM,
    HMMBoundaryArbiter,
    TDAClustering,
    PermutationEntropy,
    TransferEntropyRolling,
    WaveletMultifractal,
    HawkesCascadeWarning,
    ObservationRunner,
)

ALL_TOOL_CLASSES = [
    BayesianHMM,
    HMMBoundaryArbiter,
    TDAClustering,
    PermutationEntropy,
    TransferEntropyRolling,
    WaveletMultifractal,
    HawkesCascadeWarning,
]

_TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_bar(ret: float = 0.001, vol: float = 100.0) -> BarFeatures:
    return BarFeatures(timestamp=_TS, log_return=ret, volume=vol)


# ── Base class contract ───────────────────────────────────────────────────────


def test_all_tools_are_subclasses_of_observation_tool():
    for cls in ALL_TOOL_CLASSES:
        assert issubclass(cls, ObservationTool), (
            f"{cls.__name__} not subclass of ObservationTool"
        )


def test_all_tools_have_tool_id():
    for cls in ALL_TOOL_CLASSES:
        instance = cls()
        assert hasattr(instance, "tool_id"), f"{cls.__name__} missing tool_id"
        assert isinstance(instance.tool_id, str) and len(instance.tool_id) > 0


def test_all_tools_implement_update_and_is_ready():
    for cls in ALL_TOOL_CLASSES:
        instance = cls()
        assert callable(getattr(instance, "update", None))
        assert callable(getattr(instance, "is_ready", None))


def test_all_tools_return_observation_result():
    for cls in ALL_TOOL_CLASSES:
        instance = cls()
        result = instance.update(make_bar())
        assert isinstance(result, ObservationResult), (
            f"{cls.__name__}.update() should return ObservationResult"
        )


def test_observation_result_has_required_fields():
    for cls in ALL_TOOL_CLASSES:
        instance = cls()
        result = instance.update(make_bar())
        assert isinstance(result.tool_id, str)
        assert isinstance(result.signal, bool)
        assert isinstance(result.value, float)
        assert isinstance(result.threshold, float)
        assert isinstance(result.label, str)
        assert isinstance(result.confidence, float)
        assert isinstance(result.metadata, dict)


def test_warming_label_when_not_ready():
    """
    Newly created tools return label='WARMING' (or 'NO_DATA' for data-starved
    stubs like T2 with missing funding_rate) until warmed up.
    """
    # T2 returns NO_DATA when funding_rate is None (correct STUB behaviour)
    pre_ready_labels = {"WARMING", "NO_DATA"}
    for cls in ALL_TOOL_CLASSES:
        instance = cls()
        if not instance.is_ready():
            result = instance.update(make_bar())
            assert result.label in pre_ready_labels, (
                f"{cls.__name__}: expected pre-ready label in {pre_ready_labels}, "
                f"got {result.label}"
            )


# ── Isolation discipline (v2.1 §2.2) ─────────────────────────────────────────


def test_observation_tools_not_imported_in_strategy_modules():
    """
    Strategy 1 / Strategy 2 / state machine code must NOT import from
    sel_v2.observation_tools. This enforces the v2.1 §2.2 hard discipline.
    """
    import importlib
    import sys

    strategy_modules = [
        "sel_v2.strategies.strategy1_entry",
        "sel_v2.strategies.strategy2_entry",
        "sel_v2.strategies.strategy_exit",
        "sel_v2.states.recognizer",
        "sel_v2.states.conditions",
        "sel_v2.states.transitions",
        "sel_v2.states.critical_logic",
    ]
    obs_pkg = "sel_v2.observation_tools"

    for mod_name in strategy_modules:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue  # module not yet implemented is OK
        src = inspect.getsource(mod)
        assert obs_pkg not in src, (
            f"ISOLATION VIOLATION: {mod_name} imports from {obs_pkg}. "
            f"Observation tools must never influence strategy decisions (v2.1 §2.2)."
        )


# ── ObservationRunner ─────────────────────────────────────────────────────────


def test_runner_has_11_tools():
    # 7 legacy + 4 v2.2 lens tools (CHAN2/CHAN3/ICT2/ICT3; CHAN1 rejected offline)
    runner = ObservationRunner()
    assert len(runner._tools) == 11


def test_runner_process_bar_sync_returns_11_results():
    runner = ObservationRunner()
    bar = make_bar()
    results = runner.process_bar_sync(bar)
    assert len(results) == 11


def test_runner_process_bar_sync_all_observation_results():
    runner = ObservationRunner()
    bar = make_bar()
    results = runner.process_bar_sync(bar)
    for r in results:
        assert isinstance(r, ObservationResult)


def test_runner_bars_processed_counter():
    runner = ObservationRunner()
    assert runner.bars_processed == 0
    runner.process_bar_sync(make_bar())
    assert runner.bars_processed == 1
    runner.process_bar_sync(make_bar())
    assert runner.bars_processed == 2


def test_runner_reset_clears_counter():
    runner = ObservationRunner()
    runner.process_bar_sync(make_bar())
    runner.reset_all()
    assert runner.bars_processed == 0
    for tool in runner._tools:
        assert not tool.is_ready()


def test_runner_tool_exception_does_not_propagate():
    """If one tool raises, runner should catch and continue."""
    runner = ObservationRunner()

    class _BrokenTool(ObservationTool):
        tool_id = "BROKEN"

        def update(self, bar):
            raise RuntimeError("intentional test error")

        def is_ready(self):
            return True

    runner._tools.append(_BrokenTool())
    bar = make_bar()
    results = runner.process_bar_sync(bar)
    # Should return results for the 11 real tools only (broken one is skipped)
    assert len(results) == 11


def test_runner_no_db_does_not_write(monkeypatch):
    """Runner with db_writer=None processes bar without error."""
    runner = ObservationRunner(db_writer=None)
    bar = make_bar()
    results = runner.process_bar_sync(bar)
    assert len(results) == 11
