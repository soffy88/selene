"""
Strategy 2 entry decision tree  (sel-language-v2.0 §14.2 + v2.1-patches §5.1)

v2.1 H1 integration: inserts Hawkes intensity gate as Step 1b between
CUSUM-Short trigger (Step 1a) and the rest of the decision tree.

Decision tree (v2.1):
  Step 1a: CUSUM-Short trigger → direction candidate  [v2.0]
  Step 1b: Hawkes intensity gate λ*(t) > h_λ          [v2.1 H1 NEW]
           ↳ FAIL → action=OBSERVE (log, no entry)
  Step 2:  4H state veto — Cascade only                [v2.0]
  Step 3:  Inverse vocab classification (A/B/abort)    [v2.0, stub]
  Step 4:  Cascade risk filter (liquidation pulse)     [v2.0, stub]
  Step 5:  Cross-exchange divergence check             [v2.0, stub]
  Step 6:  Position size computation                   [v2.0]

Steps 3–5 are marked STUB — full implementation requires LOB + liquidation
feeds which are not yet flowing (v2_liquidations = 0 rows).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from sel_v2.strategies.cusum_short import CUSUMTrigger
from sel_v2.strategies.hawkes_intensity import HawkesIntensityTracker, RollingIntensityThreshold

EntryAction = Literal["ENTER_LONG", "ENTER_SHORT", "OBSERVE", "ABORT"]

# Per §14.4: base_size = subaccount_2 × 10%; max concurrent = 2; leverage = 3x
BASE_SIZE_FRACTION: float = 0.10
MAX_LEVERAGE: float = 3.0
HIGH_CONF_LEVERAGE: float = 5.0


@dataclass
class EntryDecision:
    action: EntryAction
    reason: str
    cusum_direction: Optional[Literal["LONG", "SHORT"]] = None
    cusum_intensity_coeff: float = 0.0    # C/h ratio for sizing
    hawkes_lambda: Optional[float] = None
    hawkes_threshold: Optional[float] = None
    hawkes_passed: Optional[bool] = None
    state_4h: Optional[str] = None
    suggested_leverage: float = MAX_LEVERAGE
    step_reached: int = 0                 # which step caused abort/observe


@dataclass
class Strategy2EntryFilter:
    """
    Stateful entry filter for Strategy 2.

    Holds references to live CUSUM and Hawkes tracker instances;
    called once per tick event.
    """

    hawkes_tracker: HawkesIntensityTracker = field(
        default_factory=HawkesIntensityTracker
    )
    hawkes_threshold: RollingIntensityThreshold = field(
        default_factory=RollingIntensityThreshold
    )

    def evaluate(
        self,
        t: float,
        cusum_trigger: CUSUMTrigger,
        state_4h: Optional[str],
        inverse_vocab: Optional[list[str]] = None,
        liq_pulse: bool = False,
        cross_spread_pct: Optional[float] = None,
        subaccount_nav_usdt: float = 10_000.0,
    ) -> EntryDecision:
        """
        Evaluate one tick event through the full Strategy 2 entry decision tree.

        Args:
            t:                  Unix timestamp (seconds)
            cusum_trigger:      Output from CUSUMShort.update()
            state_4h:           Current 4H market state label (or None)
            inverse_vocab:      Active inverse vocab tags (Absorption, Sweep, Crowding, …)
            liq_pulse:          True if liquidation pulse detected in last 5 min
            cross_spread_pct:   Cross-exchange price spread % (None = unknown)
            subaccount_nav_usdt: Sub-account 2 NAV for position sizing
        """

        # Step 1a: CUSUM-Short trigger
        if not cusum_trigger.triggered:
            return EntryDecision(
                action="OBSERVE",
                reason="Step 1a: CUSUM-Short not triggered",
                state_4h=state_4h,
                step_reached=1,
            )

        direction = cusum_trigger.direction

        # Step 1b: Hawkes intensity gate (v2.1 H1)
        lam_now = self.hawkes_tracker.intensity(t)
        h_lambda = self.hawkes_threshold.threshold(t)
        passed_h1 = self.hawkes_threshold.above_threshold(t, lam_now)

        if not passed_h1:
            self.hawkes_threshold.add(t, lam_now)
            return EntryDecision(
                action="OBSERVE",
                reason=(
                    f"Step 1b H1: λ*={lam_now:.6f} ≤ h_λ={h_lambda:.6f} "
                    "(trade clustering insufficient — CUSUM downgraded to observation)"
                ),
                cusum_direction=direction,
                cusum_intensity_coeff=cusum_trigger.intensity_coeff,
                hawkes_lambda=lam_now,
                hawkes_threshold=h_lambda,
                hawkes_passed=False,
                state_4h=state_4h,
                step_reached=2,
            )

        self.hawkes_threshold.add(t, lam_now)

        # Step 2: 4H state veto — only Cascade blocks Strategy 2
        if state_4h == "Cascade":
            return EntryDecision(
                action="ABORT",
                reason="Step 2: 4H state = Cascade — Strategy 2 entry blocked",
                cusum_direction=direction,
                hawkes_lambda=lam_now,
                hawkes_threshold=h_lambda,
                hawkes_passed=True,
                state_4h=state_4h,
                step_reached=2,
            )

        # Step 3: Inverse vocab classification (STUB — LOB data not yet flowing)
        vocab = set(inverse_vocab or [])
        entry_type = _classify_entry_type(direction, vocab)
        if entry_type is None:
            return EntryDecision(
                action="ABORT",
                reason=(
                    "Step 3: Inverse vocab conditions unclear — "
                    "cannot classify as Type A (reversal) or Type B (momentum)"
                ),
                cusum_direction=direction,
                hawkes_lambda=lam_now,
                hawkes_threshold=h_lambda,
                hawkes_passed=True,
                state_4h=state_4h,
                step_reached=3,
            )

        # Resolve actual entry direction after vocab classification
        if entry_type == "A":
            # Mean-reversion: enter opposite to CUSUM direction
            actual_direction: Literal["LONG", "SHORT"] = (
                "SHORT" if direction == "LONG" else "LONG"
            )
        else:
            # Momentum: enter same as CUSUM direction
            actual_direction = direction  # type: ignore[assignment]

        # Step 4: Cascade risk filter (STUB — liquidation feed not yet flowing)
        if liq_pulse:
            return EntryDecision(
                action="ABORT",
                reason="Step 4: Liquidation pulse detected in last 5 min — abort",
                cusum_direction=direction,
                hawkes_lambda=lam_now,
                hawkes_threshold=h_lambda,
                hawkes_passed=True,
                state_4h=state_4h,
                step_reached=4,
            )

        # Step 5: Cross-exchange divergence (STUB — spread feed not yet flowing)
        if cross_spread_pct is not None and cross_spread_pct > 0.5:
            return EntryDecision(
                action="ABORT",
                reason=(
                    f"Step 5: Cross-exchange spread {cross_spread_pct:.2f}% > 0.5% "
                    "— Cascade risk too high"
                ),
                cusum_direction=direction,
                hawkes_lambda=lam_now,
                hawkes_threshold=h_lambda,
                hawkes_passed=True,
                state_4h=state_4h,
                step_reached=5,
            )

        # Step 6: Determine leverage
        leverage = _compute_leverage(vocab)

        action: EntryAction = "ENTER_LONG" if actual_direction == "LONG" else "ENTER_SHORT"

        h_lambda_str = f"{h_lambda:.6f}" if h_lambda is not None else "cold"
        return EntryDecision(
            action=action,
            reason=(
                f"Step 6: Entry approved — Type {entry_type} "
                f"({'reversal' if entry_type == 'A' else 'momentum'}) "
                f"λ*={lam_now:.6f} h_λ={h_lambda_str} "
                f"CUSUM_coeff={cusum_trigger.intensity_coeff:.2f}"
            ),
            cusum_direction=direction,
            cusum_intensity_coeff=cusum_trigger.intensity_coeff,
            hawkes_lambda=lam_now,
            hawkes_threshold=h_lambda,
            hawkes_passed=True,
            state_4h=state_4h,
            suggested_leverage=leverage,
            step_reached=6,
        )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _classify_entry_type(
    cusum_direction: Optional[str],
    vocab: set[str],
) -> Optional[Literal["A", "B"]]:
    """
    Classify entry as Type A (reversal) or Type B (momentum).

    STUB: Full implementation requires real-time Sweep/Absorption signals
    from LOB collector. When vocab is empty (no LOB data), returns None
    (abort) per §14.2: "不做赌一把的中间态入场".

    Type A (mean-reversion):
      CUSUM up + Absorption + Sweep same direction

    Type B (momentum):
      CUSUM up + OFI persistent same direction + no Absorption

    Empty vocab → abort.
    """
    if not vocab:
        # No LOB signals available — per spec, abort rather than guess
        return None

    has_absorption = "Absorption" in vocab
    has_sweep = "Sweep" in vocab
    has_crowding = "Crowding" in vocab

    # Type A: reversal signals present
    if has_absorption and has_sweep:
        return "A"

    # Type B: momentum, no absorption
    if not has_absorption:
        return "B"

    # Ambiguous
    return None


def _compute_leverage(vocab: set[str]) -> float:
    """
    Leverage per §14.4.
    High-confidence: multiple vocab enhancements → 5x.
    Default: 3x.
    """
    enhancements = sum(
        1 for tag in ("Absorption", "Sweep", "Crowding", "Divergence")
        if tag in vocab
    )
    if enhancements >= 2:
        return HIGH_CONF_LEVERAGE
    return MAX_LEVERAGE
