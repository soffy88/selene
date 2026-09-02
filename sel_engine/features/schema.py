"""FeatureVector dataclass and per-feature availability flags for the sel engine."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class FeatureAvailability:
    close: bool = True
    delta_p_pct: bool = True
    sigma_p_24h: bool = False  # needs 24+ bars of history
    # WIKI_REQUIRED: following three groups need real-time collectors deployed
    H: bool = False  # WIKI_REQUIRED: orderbook_collector must be running ≥60 samples/bar
    top_5_bid_size: bool = False  # WIKI_REQUIRED: orderbook_collector
    top_5_ask_size: bool = False  # WIKI_REQUIRED: orderbook_collector
    total_depth: bool = False  # WIKI_REQUIRED: orderbook_collector
    spread_bps: bool = False  # WIKI_REQUIRED: orderbook_collector
    TF: bool = False  # WIKI_REQUIRED: trade_flow_collector must be running
    OI: bool = False  # available once oi_persister runs and DB has data
    funding_rate: bool = True  # available from Redis key cw4:funding_rates
    LV: bool = False  # depends on total_depth + spread_bps
    absorption_ratio: bool = False  # depends on TF + delta_p_pct (non-zero)
    price_autocorr_12h: bool = False  # needs 13+ bars
    price_autocorr_24h: bool = False  # needs 25+ bars
    price_autocorr_48h: bool = False  # needs 49+ bars
    sigma_p_d2: bool = False  # needs 3+ sigma_p values in history
    H_change_rate_std_12h: bool = False  # needs 13+ bars of H history
    OI_hurst: bool = False  # needs 48+ OI values
    delta_H: bool = False  # needs consecutive bars of H; WIKI_REQUIRED
    # P1 features — doc §4.1/4.2/4.3/4.4
    oi_change_rate_24h: bool = False  # WIKI_REQUIRED (OI collector); §4.1 Cond3, §4.3 Cond4
    tf_dp_ratio_24h: bool = False  # WIKI_REQUIRED (TF collector); §4.1 Cond4
    price_slope_6h: bool = False  # from closes history; §4.2 Cond1
    tf_directional_ratio_6h: bool = False  # WIKI_REQUIRED (TF collector); §4.2 Cond2+direction
    sigma_rising_12h: bool = False  # from sigma_p_history; §4.2 Cond3 sub-check
    sigma_change_rate_std_6h: bool = False  # from sigma_p_history; §4.2 Cond3 std
    H_24h_mean: bool = False  # WIKI_REQUIRED (H collector); §4.3 Cond2, §4.4 Cond2
    abs_tf_24h_sum: bool = False  # WIKI_REQUIRED (TF collector); §4.3 Cond3, §4.4 Cond3


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
    delta_H: Optional[float] = None  # |H_current - H_previous_bar|; doc §4.6 Cond4
    # P1 features — doc §4.1/4.2/4.3/4.4
    oi_change_rate_24h: Optional[float] = None  # (OI_now - OI_24h) / |OI_24h|; §4.1 Cond3
    tf_dp_ratio_24h: Optional[float] = None  # sum|TF|_24h / sum|ΔP|_24h; §4.1 Cond4; WIKI_REQUIRED
    price_slope_6h: Optional[float] = None  # |lin-reg slope over 6H| / mean_price; §4.2 Cond1
    tf_directional_ratio_6h: Optional[float] = None  # signed [-1,1] TF direction ratio; §4.2 Cond2+dir; WIKI_REQUIRED
    sigma_rising_12h: Optional[bool] = None  # True if sigma[-1] > sigma[-13]; §4.2 Cond3
    sigma_change_rate_std_6h: Optional[float] = None  # std(Δσ) over 6H; §4.2 Cond3 stability
    H_24h_mean: Optional[float] = None  # mean(H_history[-24:]); §4.3 Cond2, §4.4 Cond2; WIKI_REQUIRED
    abs_tf_24h_sum: Optional[float] = None  # sum|TF| over 24 bars; §4.3 Cond3, §4.4 Cond3; WIKI_REQUIRED
    # Metadata
    availability: FeatureAvailability = field(default_factory=FeatureAvailability)
