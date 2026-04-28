"""StateLabel enum and StateRecord dataclass for the sel state recognition layer."""
import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sel_engine.features.schema import FeatureVector


class StateLabel(enum.Enum):
    COILING = "Coiling"
    SURGING_UP = "Surging_Up"
    SURGING_DOWN = "Surging_Down"
    DRIFTING_CALM = "Drifting_Calm"
    DRIFTING_CHARGED = "Drifting_Charged"
    CRITICAL = "Critical"
    CASCADE = "Cascade"


STATE_PRIORITY = {
    StateLabel.CASCADE: 7,
    StateLabel.CRITICAL: 6,
    StateLabel.COILING: 5,
    StateLabel.SURGING_UP: 4,
    StateLabel.SURGING_DOWN: 4,
    StateLabel.DRIFTING_CHARGED: 3,
    StateLabel.DRIFTING_CALM: 2,
}


class StateNoneReason(str, enum.Enum):
    """Why state=None was returned. Distinguishes three operationally distinct causes."""
    COLD_START   = "cold_start"    # first WINDOW bars — quantile windows not yet filled
    MISSING_DATA = "missing_data"  # ≥1 WIKI_REQUIRED feature absent; condition short-circuited
    NO_MATCH     = "no_match"      # all required data present; no condition was satisfied
    NOT_APPLICABLE = "not_applicable"  # state is not None — reason field is not relevant


@dataclass
class StateRecord:
    time: datetime
    symbol: str
    state: Optional[StateLabel]        # None during cold start or when no condition matches
    reason: str                         # e.g. "COLD_START", "CASCADE:abs_delta_p@0.98+LV@0.97"
    feature_quantiles: dict             # quantile rank of each feature used
    feature_vector: Optional[FeatureVector] = None
    cold_start: bool = False
    is_legal_transition: bool = True
    transition_from: Optional["StateLabel"] = None
    none_reason: StateNoneReason = StateNoneReason.NOT_APPLICABLE
