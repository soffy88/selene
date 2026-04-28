"""sel market state recognition layer (Wave 2 + Wave 3)."""
from .schema import StateLabel, StateRecord, STATE_PRIORITY
from .thresholds import RollingQuantileCalculator
from .conditions import (
    check_cascade,
    check_critical,
    check_coiling,
    check_surging,
    check_drifting_charged,
    check_drifting_calm,
)
from .recognizer import StateRecognizer, compute_state_distribution
from .transition import DwellFilter, CascadeCooling, LegalityChecker, DWELL_TIMES, LEGAL_TRANSITIONS
from .health import HealthMonitor, HealthReport, EXPECTED_RATE_RANGES
from .engine import StateEngine

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
