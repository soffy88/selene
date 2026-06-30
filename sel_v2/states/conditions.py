"""
6 state entry condition functions — sel v2.0 §5.

Each function returns a ConditionResult with:
  met: True / False / None
  none_conditions: list of STUB sub-conditions that returned None
  false_conditions: list of sub-conditions that returned False
  details: extra diagnostic fields

Tristate discipline:
  - Any sub-condition explicitly False → met = False
  - All evaluable sub-conditions True, some None → met = True, cold_start applies
  - All sub-conditions None → met = None
  - Empty conditions (Drifting-Calm default) → met = True (fallback)

Note on STUB data: OI, funding, LOB entropy, liquidation, OFI are all None
until their respective collectors come online. State conditions that depend
entirely on STUB data return met=None. Mixed (some real, some STUB) states
return met=True/False based on the available conditions, with cold_start=True.
"""
from __future__ import annotations

from typing import Optional

from sel_v2.states.schema import BarFeatures, ConditionResult
from sel_v2.states.critical_logic import evaluate_critical_entry


# ── Helper ─────────────────────────────────────────────────────────────────────

def _result(
    met: Optional[bool],
    none_conditions: list[str],
    false_conditions: list[str],
    details: dict,
) -> ConditionResult:
    return ConditionResult(met=met, none_conditions=none_conditions,
                          false_conditions=false_conditions, details=details)


def _tristate_all(conditions: list[tuple[str, Optional[bool]]]) -> ConditionResult:
    """
    Aggregate named tristate conditions using AND semantics:
    - Any False → met=False
    - All evaluable True, none False → met=True (even if some None)
    - All None → met=None
    """
    none_conds = [name for name, v in conditions if v is None]
    false_conds = [name for name, v in conditions if v is False]
    true_conds = [name for name, v in conditions if v is True]

    if false_conds:
        met: Optional[bool] = False
    elif true_conds:
        # at least one True, none False → treat as True (STUB data doesn't block)
        met = True
    else:
        # all None
        met = None

    details = {name: v for name, v in conditions}
    return _result(met, none_conds, false_conds, details)


# ── Cascade (§5.7) ─────────────────────────────────────────────────────────────

def check_cascade(features: BarFeatures) -> ConditionResult:
    """
    Cascade enters when ANY of the 3 alternative conditions fires (OR logic).
    Immediate switch, no min-dwell.

    Condition 1: σ > 97th pctile AND lob_depth < 5th pctile
    Condition 2: liquidation_pulse True
    Condition 3: cross_exchange_spread > 0.5%

    Note: lob_depth, liquidation_pulse, cross_spread are all STUB.
    σ alone never triggers Cascade (we need the AND with lob_depth or alternatives).
    With all STUBs, Cascade condition returns None — conservative.
    """
    # Condition 1
    sigma_extreme = features.sigma_pctile >= 0.97
    if features.lob_depth_pctile is None:
        cond1: Optional[bool] = None  # can't confirm lob exhaustion
    elif sigma_extreme and features.lob_depth_pctile <= 0.05:
        cond1 = True
    else:
        cond1 = False

    # Condition 2
    cond2: Optional[bool] = features.liquidation_pulse  # None (STUB)

    # Condition 3
    if features.cross_exchange_spread is None:
        cond3: Optional[bool] = None
    elif features.cross_exchange_spread > 0.5:
        cond3 = True
    else:
        cond3 = False

    none_conds = []
    false_conds = []
    if cond1 is None:
        none_conds.append("lob_depth")
    elif not cond1:
        false_conds.append("sigma_plus_lob")
    if cond2 is None:
        none_conds.append("liquidation_pulse")
    elif not cond2:
        false_conds.append("liquidation_pulse")
    if cond3 is None:
        none_conds.append("cross_exchange_spread")
    elif not cond3:
        false_conds.append("cross_exchange_spread")

    # OR logic: True if any condition is True
    if cond1 is True or cond2 is True or cond3 is True:
        met: Optional[bool] = True
    elif cond1 is False and cond2 is False and cond3 is False:
        met = False
    else:
        # Some are None (STUB), none are True → None (can't determine)
        met = None

    return _result(
        met=met,
        none_conditions=none_conds,
        false_conditions=false_conds,
        details={
            "sigma_pctile": features.sigma_pctile,
            "lob_depth_pctile": features.lob_depth_pctile,
            "liquidation_pulse": features.liquidation_pulse,
            "cross_exchange_spread": features.cross_exchange_spread,
            "cond1": cond1, "cond2": cond2, "cond3": cond3,
        },
    )


# ── Critical (§5.6 + v2.1 §2.1) ───────────────────────────────────────────────

def check_critical(features: BarFeatures) -> ConditionResult:
    """
    Critical via v2.1 §2.1 "择优" logic.
    Delegates to critical_logic.evaluate_critical_entry().
    Returns met=True only when critical_logic says entered.
    """
    cc = evaluate_critical_entry(features)

    none_conds = list(cc.none_tags)
    false_conds = []
    if not cc.met:
        if cc.a1 is False:
            false_conds.append("A1_sigma")
        if cc.b is False:
            false_conds.append("B_hawkes")
        if cc.c is False:
            false_conds.append("C_tda")

    return _result(
        met=True if cc.met else False,
        none_conditions=none_conds,
        false_conditions=false_conds,
        details={
            "a1": cc.a1, "a2": cc.a2,
            "a_full": cc.a_full, "a_partial": cc.a_partial,
            "b": cc.b, "c": cc.c,
            "path1": cc.path1_met, "path2": cc.path2_met,
            "hawkes_br": features.hawkes_br,
            "hawkes_threshold": features.hawkes_br_threshold,
            "tda_l1_pctile": features.tda_l1_pctile,
        },
    )


# ── Surging (§5.3) ─────────────────────────────────────────────────────────────

def check_surging(features: BarFeatures) -> ConditionResult:
    """
    Surging from Coiling/Drifting-Charged via Release.

    From Coiling (primary path):
      - Price breaks 24h high or low (computable)
      - σ > 70th pctile (computable)
      - OFI cumulative direction 90th pctile (live when OFI data present)
      - OI acceleration (STUB)

    Conservative principle (v2.1 §2.1): price_breakout + σ are necessary but not
    sufficient. At least one of the two flow signals (OFI or OI acceleration)
    must be non-None (and True) to return met=True. If both are None →
    met=None (state unidentifiable; prevents Drifting_Calm → Surging bypass of the
    Coiling/Drifting-Charged intermediate state required by §6 transition graph).
    """
    # Price breakout
    has_breakout: Optional[bool] = None
    if features.price_breakout_up is not None and features.price_breakout_down is not None:
        has_breakout = features.price_breakout_up or features.price_breakout_down
    elif features.price_breakout_up is not None:
        has_breakout = features.price_breakout_up
    elif features.price_breakout_down is not None:
        has_breakout = features.price_breakout_down

    # σ jump
    sigma_jump: Optional[bool] = features.sigma_pctile >= 0.70

    # OFI cumulative direction in the top decile (90th pctile of the 7-day rank).
    # BarRunner populates ofi_cumulative_pctile from the OFI proxy series. OFI is a
    # POSITIVE confirmation signal combined by the "at least one flow True" rule, so
    # an extreme reading confirms (True) while a non-extreme or missing reading
    # abstains (None) — it must not return False, which _tristate_all treats as a
    # hard veto and would block Surging on the common (non-extreme) case.
    ofi_pctile_high: Optional[bool] = (
        True if (features.ofi_cumulative_pctile is not None
                 and features.ofi_cumulative_pctile >= 0.90)
        else None
    )

    # OI acceleration (STUB)
    oi_accel: Optional[bool] = features.oi_acceleration

    # Necessary conditions: price breakout and σ jump must both be True
    if has_breakout is False or sigma_jump is False:
        fc = []
        if has_breakout is False:
            fc.append("price_breakout")
        if sigma_jump is False:
            fc.append("sigma_jump_70")
        return _result(
            met=False,
            none_conditions=[],
            false_conditions=fc,
            details={"sigma_pctile": features.sigma_pctile,
                     "price_breakout_up": features.price_breakout_up,
                     "price_breakout_down": features.price_breakout_down},
        )

    flow_signals = [ofi_pctile_high, oi_accel]
    if all(v is None for v in flow_signals):
        return _result(
            met=None,
            none_conditions=["ofi_90pct", "oi_acceleration"],
            false_conditions=[],
            details={"sigma_pctile": features.sigma_pctile, "flow": None},
        )

    conditions = [
        ("price_breakout", has_breakout),
        ("sigma_jump_70", sigma_jump),
        ("ofi_90pct", ofi_pctile_high),
        ("oi_acceleration", oi_accel),
    ]
    return _tristate_all(conditions)


# ── Coiling (§5.2) ─────────────────────────────────────────────────────────────

def check_coiling(features: BarFeatures) -> ConditionResult:
    """
    Coiling: narrow range, energy accumulating.

    Conditions:
      - σ < 30th pctile (computable, necessary)
      - LOB entropy < 30th pctile (STUB)
      - OI change rate > 0 (STUB)
      - |funding| < 80th pctile (STUB)

    Conservative principle (v2.1 §2.1): σ is necessary but not sufficient.
    At least one of the three STUB accumulation signals must be non-None (and True)
    to return met=True. If σ<30th but all three STUBs are None → met=None
    (state unidentifiable; arbitration falls back to Drifting-Calm).
    """
    sigma_low: Optional[bool] = features.sigma_pctile < 0.30
    if sigma_low is False:
        return _result(
            met=False,
            none_conditions=[],
            false_conditions=["sigma_lt_30pct"],
            details={"sigma_pctile": features.sigma_pctile},
        )

    entropy_low: Optional[bool]
    if features.entropy_pctile is None:
        entropy_low = None
    else:
        entropy_low = features.entropy_pctile < 0.30

    oi_positive: Optional[bool]
    if features.oi_change_rate is None:
        oi_positive = None
    else:
        oi_positive = features.oi_change_rate > 0

    funding_neutral: Optional[bool]
    if features.funding_pctile is None:
        funding_neutral = None
    else:
        funding_neutral = features.funding_pctile < 0.80

    stub_signals = [entropy_low, oi_positive, funding_neutral]
    if all(v is None for v in stub_signals):
        return _result(
            met=None,
            none_conditions=["entropy_lt_30pct", "oi_positive", "funding_neutral"],
            false_conditions=[],
            details={"sigma_pctile": features.sigma_pctile, "accumulation": None},
        )

    conditions = [
        ("sigma_lt_30pct", sigma_low),
        ("entropy_lt_30pct", entropy_low),
        ("oi_positive", oi_positive),
        ("funding_neutral", funding_neutral),
    ]
    return _tristate_all(conditions)


# ── Drifting-Charged (§5.5) ────────────────────────────────────────────────────

def check_drifting_charged(features: BarFeatures) -> ConditionResult:
    """
    Drifting-Charged: no clear direction, but energy accumulating via OI/funding.

    Conditions (either OI or funding must qualify):
      - σ not extreme (30-80th pctile)
      - OI change rate > 70th pctile (STUB)
      - OR funding rate persistently directional (STUB)

    OI/funding are the KEY DEFINING conditions for this state vs Drifting-Calm.
    If both are STUB (None), this state returns met=None — the state cannot be
    identified without the accumulation signal.
    """
    # OI elevated (STUB)
    oi_elevated: Optional[bool]
    if features.oi_change_rate_pctile is None:
        oi_elevated = None
    else:
        oi_elevated = features.oi_change_rate_pctile >= 0.70

    # Funding persistent (STUB)
    funding_pers: Optional[bool] = features.funding_persistent

    # OI or funding must show accumulation — this is the state's defining condition
    if oi_elevated is True or funding_pers is True:
        accumulation: Optional[bool] = True
    elif oi_elevated is False and funding_pers is False:
        accumulation = False
    else:
        accumulation = None  # both STUB — state cannot be identified

    # σ in non-extreme zone — check this first so explicit False propagates
    sigma_mid: Optional[bool] = 0.30 <= features.sigma_pctile < 0.80
    if sigma_mid is False:
        return _result(
            met=False,
            none_conditions=[],
            false_conditions=["sigma_non_extreme"],
            details={"sigma_pctile": features.sigma_pctile},
        )

    # If the defining OI/funding condition is None, state cannot be identified
    if accumulation is None:
        return _result(
            met=None,
            none_conditions=["oi_or_funding_charging"],
            false_conditions=[],
            details={"sigma_pctile": features.sigma_pctile, "accumulation": None},
        )

    conditions = [
        ("sigma_non_extreme", sigma_mid),
        ("oi_or_funding_charging", accumulation),
    ]
    return _tristate_all(conditions)


# ── Drifting-Calm (§5.4) ───────────────────────────────────────────────────────

def check_drifting_calm(features: BarFeatures) -> ConditionResult:
    """
    Drifting-Calm: no direction, no energy build.
    Acts as the default/fallback when higher-priority states don't fire.

    Conditions:
      - σ in 30-60th pctile (computable)
      - LOB entropy in 40-70th pctile (STUB)
      - OI change rate near zero (STUB)
      - No directional OFI (STUB)
    """
    sigma_mid: Optional[bool] = 0.30 <= features.sigma_pctile < 0.60

    entropy_mid: Optional[bool]
    if features.entropy_pctile is None:
        entropy_mid = None
    else:
        entropy_mid = 0.40 <= features.entropy_pctile <= 0.70

    oi_flat: Optional[bool]
    if features.oi_change_rate_pctile is None:
        oi_flat = None
    else:
        # near zero oi change: below 40th pctile
        oi_flat = features.oi_change_rate_pctile <= 0.40

    conditions = [
        ("sigma_30_60pct", sigma_mid),
        ("entropy_40_70pct", entropy_mid),
        ("oi_flat", oi_flat),
    ]
    return _tristate_all(conditions)
