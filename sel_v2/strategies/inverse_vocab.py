"""
Inverse-vocabulary (逆向词汇) detection + Type A/B classification for Strategy 2.

Implements the *frozen* sel-language-v2.0 §6 / §14.2 definitions — this module is a
completion of a long-standing STUB (strategy2_entry.py Step 3), NOT a redesign. When it
was written the LOB/tick/liquidation feeds did not exist; they have流 stably since 2026-07,
so the detection can now run on real microstructure.

Two microstructure signatures, both *direction-aware* (§14.2 requires the vocab direction to
agree with the CUSUM candidate before it means anything):

  Absorption (§6: 可见主动流量但价格不动 — effort without result)
    tf_net         = |Σ taker_buy − Σ taker_sell| / Σ total        (directional effort)
    price_response = |ΔP| / ATR                                     (price result)
    present ⟺ tf_net > p80(30d, adaptive)  AND  price_response < p30(30d, adaptive)
    direction = sign of the taker net flow (the side that was absorbed)

  Sweep (§6 / 案例6: stop-hunt then revert)
    touch a past-48h significant high/low (±0.1% tolerance),
    volume at the touch > p90(30d, adaptive),
    price reverts back into the pre-touch range within ~1h.
    direction = 'up' if the high was swept, 'down' if the low was swept.

Type classification (§14.2, verbatim — do not invent branches):
  Type A (reversal):  CUSUM dir X  +  Absorption(X absorbed)  +  Sweep(X same)
      → actual entry = reverse of X
  Type B (momentum):  CUSUM dir X  +  OFI 60m net persistently same as X  +  NO Absorption
      → actual entry = same as X
  neither → ABORT ("类型未明 → 不做赌一把的中间态入场")

The detectors are *pure* (numeric in, dataclass out). Percentile thresholds are supplied by
the caller so this module carries no DB/history state — see `adaptive_percentile` for the
"only N days of data so far" degradation the whole codebase uses (emit only once ≥ min_obs).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import numpy as np

# §6 tolerances / windows. Kept as module constants (implementation parameters, not §14.2
# semantics) so a caller can document exactly what "significant high", "reverts", etc. mean.
SWEEP_TOUCH_TOL: float = 0.001  # ±0.1% counts as touching the 48h extreme
ABSORPTION_TF_Q: float = 0.80  # tf_net must exceed its 30d 80th pct
ABSORPTION_PR_Q: float = 0.30  # price_response must sit below its 30d 30th pct
SWEEP_VOL_Q: float = 0.90  # touch volume must exceed its 30d 90th pct
MIN_PCTILE_OBS: int = 30  # adaptive: no percentile verdict below this many samples

Side = Literal["up", "down"]


@dataclass
class AbsorptionSignal:
    present: bool
    direction: Optional[Side] = None  # taker-net side that got absorbed
    tf_net: Optional[float] = None
    price_response: Optional[float] = None
    details: dict = field(default_factory=dict)


@dataclass
class SweepSignal:
    present: bool
    direction: Optional[Side] = None  # 'up' = swept the high, 'down' = swept the low
    details: dict = field(default_factory=dict)


def adaptive_percentile(
    history, value: float, q: float, min_obs: int = MIN_PCTILE_OBS
) -> Optional[bool]:
    """Is `value` beyond the `q` quantile of `history`? Returns None (not False) when there
    are fewer than `min_obs` finite samples — the three-state discipline the codebase uses:
    a not-yet-warm feed abstains rather than asserting a signal. For q ≥ 0.5 the test is
    `value >= quantile` (an upper-tail exceedance); for q < 0.5 it is `value <= quantile`
    (a lower-tail shortfall, e.g. price_response below its 30th pct)."""
    if history is None:
        return None
    arr = np.asarray(history, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < min_obs or not np.isfinite(value):
        return None
    threshold = float(np.quantile(arr, q))
    return bool(value >= threshold) if q >= 0.5 else bool(value <= threshold)


def detect_absorption(
    taker_net: float,
    taker_vol: float,
    price_delta_abs: float,
    atr: float,
    tf_net_history=None,
    price_response_history=None,
) -> AbsorptionSignal:
    """§6 Absorption: strong *directional* taker effort that fails to move price.
    Inputs are the trigger-window aggregates (Σ buy−sell, Σ|size|, |ΔP|, ATR). Verdict needs
    BOTH percentile tests to pass on adaptive history; if either abstains (cold feed) the
    signal is absent (conservative — never fabricate a reversal setup from thin data)."""
    if taker_vol is None or taker_vol <= 0 or atr is None or atr <= 0:
        return AbsorptionSignal(present=False, details={"reason": "no_flow_or_atr"})
    tf_net = abs(taker_net) / taker_vol
    price_response = abs(price_delta_abs) / atr
    effort_high = adaptive_percentile(tf_net_history, tf_net, ABSORPTION_TF_Q)
    result_low = adaptive_percentile(
        price_response_history, price_response, ABSORPTION_PR_Q
    )
    present = effort_high is True and result_low is True
    direction: Optional[Side] = None
    if present and taker_net != 0:
        direction = "up" if taker_net > 0 else "down"
    return AbsorptionSignal(
        present=present,
        direction=direction,
        tf_net=tf_net,
        price_response=price_response,
        details={
            "effort_high": effort_high,
            "result_low": result_low,
            "tf_net": tf_net,
            "price_response": price_response,
        },
    )


def detect_sweep(
    high_48h: float,
    low_48h: float,
    touch_high: float,
    touch_low: float,
    touch_volume: float,
    reverted_from_high: bool,
    reverted_from_low: bool,
    volume_history=None,
) -> SweepSignal:
    """§6 / 案例6 Sweep: price spikes through a 48h extreme on heavy volume then reverts back
    into range (stop-hunt). Caller supplies the trigger-window extremes it actually reached
    (`touch_high`/`touch_low`), the 48h reference levels, the touch volume, and whether price
    reverted within ~1h. Volume must exceed its adaptive 90th pct. A high sweep dominates when
    both extremes qualify (rare); ties resolve to the high side deterministically."""
    vol_high = adaptive_percentile(volume_history, touch_volume, SWEEP_VOL_Q)
    if vol_high is not True:
        return SweepSignal(present=False, details={"vol_high": vol_high})

    swept_high = (
        high_48h is not None
        and touch_high is not None
        and touch_high >= high_48h * (1.0 - SWEEP_TOUCH_TOL)
        and reverted_from_high
    )
    swept_low = (
        low_48h is not None
        and touch_low is not None
        and touch_low <= low_48h * (1.0 + SWEEP_TOUCH_TOL)
        and reverted_from_low
    )
    if swept_high:
        return SweepSignal(
            present=True,
            direction="up",
            details={"touch_high": touch_high, "high_48h": high_48h},
        )
    if swept_low:
        return SweepSignal(
            present=True,
            direction="down",
            details={"touch_low": touch_low, "low_48h": low_48h},
        )
    return SweepSignal(
        present=False, details={"swept_high": swept_high, "swept_low": swept_low}
    )


def _cusum_side(cusum_direction: Optional[str]) -> Optional[Side]:
    if cusum_direction == "LONG":
        return "up"
    if cusum_direction == "SHORT":
        return "down"
    return None


def classify_entry_type(
    cusum_direction: Optional[str],
    absorption: AbsorptionSignal,
    sweep: SweepSignal,
    ofi_persistent_same_direction: Optional[bool],
) -> Optional[Literal["A", "B"]]:
    """§14.2 Type classification — direction-aware. Returns 'A' (reversal), 'B' (momentum),
    or None (类型未明 → abort). See module docstring for the frozen rules; the direction
    checks (`absorption.direction == side`, `sweep.direction == side`) are what §14.2 means by
    "X 方向被吸 / X 同向" and are the whole reason a non-direction-aware stub kept aborting."""
    side = _cusum_side(cusum_direction)
    if side is None:
        return None

    # Type A (reversal): both reversal signatures present AND aligned with the CUSUM side.
    if (
        absorption.present
        and absorption.direction == side
        and sweep.present
        and sweep.direction == side
    ):
        return "A"

    # Type B (momentum): persistent same-direction taker flow and NO absorption to fade it.
    if ofi_persistent_same_direction is True and not absorption.present:
        return "B"

    return None
