"""FeatureVector dataclass and per-feature availability flags for the sel engine."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class FeatureAvailability:
    close: bool = True
    delta_p_pct: bool = True
    sigma_p_24h: bool = False       # needs 24+ bars of history
    # WIKI_REQUIRED: following three groups need real-time collectors deployed
    H: bool = False                  # WIKI_REQUIRED: orderbook_collector must be running ≥60 samples/bar
    top_5_bid_size: bool = False     # WIKI_REQUIRED: orderbook_collector
    top_5_ask_size: bool = False     # WIKI_REQUIRED: orderbook_collector
    total_depth: bool = False        # WIKI_REQUIRED: orderbook_collector
    spread_bps: bool = False         # WIKI_REQUIRED: orderbook_collector
    TF: bool = False                 # WIKI_REQUIRED: trade_flow_collector must be running
    OI: bool = False                 # available once oi_persister runs and DB has data
    funding_rate: bool = True        # available from Redis key cw4:funding_rates
    LV: bool = False                 # depends on total_depth + spread_bps
    absorption_ratio: bool = False   # depends on TF + delta_p_pct (non-zero)
    price_autocorr_12h: bool = False  # needs 13+ bars
    price_autocorr_24h: bool = False  # needs 25+ bars
    price_autocorr_48h: bool = False  # needs 49+ bars
    sigma_p_d2: bool = False          # needs 3+ sigma_p values in history
    H_change_rate_std_12h: bool = False  # needs 13+ bars of H history
    OI_hurst: bool = False             # needs 48+ OI values
    delta_H: bool = False              # needs consecutive bars of H; WIKI_REQUIRED


@dataclass
class FeatureVector:
    time: datetime
    symbol: str
    # Price layer
    close: float = 0.0
    delta_p_pct: Optional[float] = None
    sigma_p_24h: Optional[float] = None
    # Liquidity layer
    H: Optional[float] = None
    H_sample_count: int = 0
    # Orderbook depth
    top_5_bid_size: Optional[float] = None
    top_5_ask_size: Optional[float] = None
    total_depth: Optional[float] = None
    spread_bps: Optional[float] = None
    # Flow layer
    TF: Optional[float] = None
    OI: Optional[float] = None
    funding_rate: Optional[float] = None
    # Derived indicators
    LV: Optional[float] = None
    absorption_ratio: Optional[float] = None
    price_autocorr_12h: Optional[float] = None
    price_autocorr_24h: Optional[float] = None
    price_autocorr_48h: Optional[float] = None
    sigma_p_d2: Optional[float] = None
    H_change_rate_std_12h: Optional[float] = None
    OI_hurst: Optional[float] = None
    delta_H: Optional[float] = None   # |H_current - H_previous_bar|; doc §4.6 Cond4
    # Metadata
    availability: FeatureAvailability = field(default_factory=FeatureAvailability)
