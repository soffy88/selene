"""
StateEngine — Wave 3 full pipeline entry point.

FeatureVector → StateRecognizer → DwellFilter → CascadeCooling → LegalityChecker
             → StateRecord (with health metadata).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sel_engine.features.schema import FeatureVector

from .health import HealthMonitor, HealthReport
from .recognizer import StateRecognizer
from .schema import StateLabel, StateRecord
from .transition import CascadeCooling, DwellFilter, LegalityChecker


class StateEngine:
    """
    Full pipeline: FeatureVector → (raw state from recognizer) → (dwell filter) →
    (cascade cooling) → (legality check) → StateRecord with metadata.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.recognizer = StateRecognizer()
        self.dwell = DwellFilter()
        self.cooling = CascadeCooling(cooldown_bars=6)
        self.legality = LegalityChecker()
        self.health = HealthMonitor()
        self._last_confirmed: Optional[StateLabel] = None

    def process(self, fv: FeatureVector) -> StateRecord:
        """Process one FeatureVector through the full pipeline."""
        raw = self.recognizer.recognize(fv)
        filtered = self.dwell.apply(raw, self._last_confirmed)
        cooled = self.cooling.apply(filtered, self._last_confirmed)
        checked = self.legality.check(cooled, self._last_confirmed)

        if checked.state is not None and not checked.cold_start:
            self._last_confirmed = checked.state

        self.health.record(checked)
        return checked

    def health_report(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> HealthReport:
        """Generate a HealthReport for the given time window (or full history)."""
        return self.health.generate(start, end)
