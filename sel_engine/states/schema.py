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


@dataclass
class StateRecord:
    time: datetime
    symbol: str
    state: Optional[StateLabel]        # None during cold start
    reason: str                         # e.g. "COLD_START", "CASCADE:sigma_p_24h@0.98+|delta_p|@0.96"
    feature_quantiles: dict             # quantile rank of each feature used
    feature_vector: Optional[FeatureVector] = None
    cold_start: bool = False
    is_legal_transition: bool = True
    transition_from: Optional["StateLabel"] = None
