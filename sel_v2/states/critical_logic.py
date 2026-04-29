"""
v2.1 §2.1 Critical state entry logic — "择优 (A + B/C)" combination.

Main conditions:
  A (v2.0 §5.6 + v1.1):
    A1 = σ > 90th pctile + 12h monotone rise (3 × 4H bars)
    A2 = entropy change-rate variance rising  (STUB until LOB available)

  B (v2.1 H2):
    Hawkes branching ratio > threshold (from v2_strategy_params)

  C (v2.1 TDA1):
    TDA L^1 norm > 95th pctile + 12h monotone rise

Entry logic (择优):
  (A_full AND (B OR C)) → Critical
  (A_partial AND B AND C) → Critical
  else → not Critical

A_full   = A1 AND A2 both True  (2/2)
A_partial = A1 True, A2 not True  (1/2)

Tristate discipline:
  True  = condition positively met
  False = condition explicitly not met
  None  = data unavailable — treated as "not confirmed" (conservative)
  Missing data alone is NEVER sufficient to trigger Critical.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sel_v2.states.schema import BarFeatures


# ── Tristate helpers ───────────────────────────────────────────────────────────

def _and(a: Optional[bool], b: Optional[bool]) -> Optional[bool]:
    """Tristate AND: False if either False, True if both True, else None."""
    if a is False or b is False:
        return False
    if a is True and b is True:
        return True
    return None


def _or(a: Optional[bool], b: Optional[bool]) -> Optional[bool]:
    """Tristate OR: True if either True, False if both False, else None."""
    if a is True or b is True:
        return True
    if a is False and b is False:
        return False
    return None


# ── Sub-condition evaluators ───────────────────────────────────────────────────

def _eval_a1(features: BarFeatures) -> Optional[bool]:
    """
    A1: σ > 90th pctile AND 12h monotone rise (= 3 × 4H bars strictly increasing).
    Both sub-parts are computable from price data (not STUB).
    """
    sigma_high = features.sigma_pctile >= 0.90

    if not sigma_high:
        return False  # explicitly not in top-10% volatility

    # σ monotone: 3 consecutive bar sigmas strictly increasing
    if features.sigma_monotone_3bar is None:
        return None  # insufficient bars to check (cold start)
    if not features.sigma_monotone_3bar:
        return False

    return True


def _eval_a2(features: BarFeatures) -> Optional[bool]:
    """
    A2: entropy change-rate variance rising.
    STUB until LOB collector is available — always returns None.
    """
    return features.entropy_variance_rising  # None (STUB)


def _eval_b(features: BarFeatures) -> Optional[bool]:
    """B: Hawkes branching ratio > threshold (v2.1 H2)."""
    if features.hawkes_br is None:
        return None
    return features.hawkes_br > features.hawkes_br_threshold


def _eval_c(features: BarFeatures) -> Optional[bool]:
    """
    C: TDA L^1 norm > 95th pctile + 12h monotone rise.
    Both sub-parts required.
    """
    if features.tda_l1_pctile is None:
        return None

    l1_high = features.tda_l1_pctile >= 0.95
    if not l1_high:
        return False

    if features.tda_l1_monotone_3bar is None:
        return None  # can't verify monotone
    if not features.tda_l1_monotone_3bar:
        return False

    return True


# ── Main evaluation ────────────────────────────────────────────────────────────

@dataclass
class CriticalConditions:
    """Diagnostic breakdown of all Critical sub-conditions."""
    a1: Optional[bool]
    a2: Optional[bool]
    a_full: Optional[bool]    # A1 AND A2 both True (2/2)
    a_partial: Optional[bool] # A1 True, A2 not True (1/2)
    b: Optional[bool]
    c: Optional[bool]
    path1_met: bool            # A_full AND (B OR C)
    path2_met: bool            # A_partial AND B AND C
    met: bool                  # overall Critical entry decision
    none_tags: list[str]       # which sub-conditions returned None


def evaluate_critical_entry(features: BarFeatures) -> CriticalConditions:
    """
    Evaluate v2.1 §2.1 Critical entry logic.

    Conservative: None data alone never triggers Critical.
    Returns CriticalConditions with full diagnostic breakdown.
    """
    a1 = _eval_a1(features)
    a2 = _eval_a2(features)
    b = _eval_b(features)
    c = _eval_c(features)

    # A_full = A1 AND A2 (2/2)
    a_full = _and(a1, a2)

    # A_partial = A1 True, A2 not True (1/2 satisfied).
    # A has 2 sub-conditions; partial means the σ-based A1 is met but A2 is not True.
    # A2=None (STUB) and A2=False both count as "not True".
    # When A_full=True (both met), A_partial is explicitly False (not applicable).
    if a1 is True:
        if a2 is True:
            a_partial: Optional[bool] = False  # fully satisfied — partial doesn't apply
        else:
            a_partial = True   # A1 met, A2 None or False
    elif a1 is False:
        a_partial = False  # σ part fails → neither full nor partial
    else:
        a_partial = None  # a1 is None → can't determine partial status

    # Path 1: A_full AND (B OR C)
    path1_met = (a_full is True) and (_or(b, c) is True)

    # Path 2: A_partial AND B AND C
    path2_met = (a_partial is True) and (b is True) and (c is True)

    met = path1_met or path2_met

    none_tags = []
    if a1 is None:
        none_tags.append("A1_sigma_monotone")
    if a2 is None:
        none_tags.append("A2_entropy_variance")
    if b is None:
        none_tags.append("B_hawkes_br")
    if c is None:
        none_tags.append("C_tda_l1")

    return CriticalConditions(
        a1=a1, a2=a2,
        a_full=a_full, a_partial=a_partial,
        b=b, c=c,
        path1_met=path1_met,
        path2_met=path2_met,
        met=met,
        none_tags=none_tags,
    )
