"""sel market state recognition layer (Wave 2 + Wave 3)."""

from .conditions import (
    check_cascade,
    check_coiling,
    check_critical,
    check_drifting_calm,
    check_drifting_charged,
    check_surging,
)
from .engine import StateEngine
from .health import EXPECTED_RATE_RANGES, HealthMonitor, HealthReport
from .recognizer import StateRecognizer, compute_state_distribution
from .schema import STATE_PRIORITY, StateLabel, StateRecord
from .thresholds import RollingQuantileCalculator
from .transition import DWELL_TIMES, LEGAL_TRANSITIONS, CascadeCooling, DwellFilter, LegalityChecker

__all__ = [
    # Wave 2
    "StateLabel",
    "StateRecord",
    "STATE_PRIORITY",
    "RollingQuantileCalculator",
    "check_cascade",
    "check_critical",
    "check_coiling",
    "check_surging",
    "check_drifting_charged",
    "check_drifting_calm",
    "StateRecognizer",
    "compute_state_distribution",
    # Wave 3
    "DwellFilter",
    "CascadeCooling",
    "LegalityChecker",
    "DWELL_TIMES",
    "LEGAL_TRANSITIONS",
    "HealthMonitor",
    "HealthReport",
    "EXPECTED_RATE_RANGES",
    "StateEngine",
]
