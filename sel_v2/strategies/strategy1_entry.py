"""
Strategy 1 entry decision tree  (sel-language-v2.0 §13.2)

4H trend-following strategy: enters only when the market is in Coiling or
Drifting-Charged state AND CUSUM-Mid triggers a directional signal.

Decision tree (7 steps → sizing):
  Step 1: 4H state filter — must be Coiling or Drifting-Charged
  Step 2: Min-dwell check — Coiling ≥ 6 bars, Drifting-Charged ≥ 4 bars
  Step 3: CUSUM-Mid trigger — C+/C- > rolling threshold → direction
  Step 4: Derivatives filter                               [§13.2 Step 4]
    4a. funding rate pctile (>90th LONG → abort, <10th SHORT → abort)
    4b. OI direction (against candidate → halve size)
    4c. liquidation pulse (>5× 30-day mean in last 1h → abort)
  Step 5: On-chain filter (observation-only, non-blocking)  [STUB]
  Step 6: Divergence check (>0.3% cross-exchange → abort)   [STUB]
  Step 7: Inverse vocab enhancement (optional, non-blocking) [Wave 5 STUB]
  Step 8: Position sizing

STUB items:
  Step 4:  derivatives data from helixa (activate after GRANT + connection)
  Step 5:  v2_onchain_exchange_flows (no source)
  Step 6:  cross-exchange spread feed (no source)
  Step 7:  inverse vocab engine (Wave 5)

See sel_v2/strategies/STUB_BOUNDARIES.md for the full registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

from sel_v2.strategies.cusum_short import CUSUMShort, CUSUMTrigger

EntryAction = Literal["ENTER_LONG", "ENTER_SHORT", "OBSERVE", "ABORT"]

# §13.2: state filter
_VALID_ENTRY_STATES = frozenset({"Coiling", "Drifting-Charged"})
_ABORT_STATES = frozenset({"Drifting-Calm", "Surging", "Critical", "Cascade"})

# §13.4: base_size = 20% of sub-account 1 nav
BASE_SIZE_FRACTION: float = 0.20
MAX_LEVERAGE: float = 2.0  # spot-primary; perp only for funding arbitrage

# §13.2 Step 4: funding extreme thresholds
FUNDING_LONG_ABORT_PCTILE: float = 0.90
FUNDING_SHORT_ABORT_PCTILE: float = 0.10

# §13.2 Step 4b: OI against candidate → halve position
OI_REDUCTION_FACTOR: float = 0.50

# §13.2 Step 7: vocab modifiers
_VOCAB_MODIFIERS: dict[str, float] = {
    "Sweep": +0.20,
    "Saturation": +0.15,
    "Absorption": -0.30,  # adverse signal — reduce confidence
}

# CUSUM-Mid window: 30 days in seconds (vs 7 days for CUSUMShort)
CUSUM_MID_THRESHOLD_WINDOW_SEC: int = 30 * 24 * 3600


@dataclass
class Strategy1EntryDecision:
    action: EntryAction
    reason: str
    direction: Optional[Literal["LONG", "SHORT"]] = None
    state_4h: Optional[str] = None
    step_reached: int = 0
    # sizing outputs
    base_size_pct: float = 0.0  # fraction of sub-account nav
    size_modifier: float = 1.0  # from OI + vocab adjustments
    suggested_leverage: float = 1.0
    # diagnostics
    cusum_positive: float = 0.0
    cusum_negative: float = 0.0
    cusum_threshold: float = 0.0
    funding_pctile: Optional[float] = None
    oi_direction_against: bool = False
    vocab_modifier: float = 0.0


@dataclass
class Strategy1EntryFilter:
    """
    Stateful entry filter for Strategy 1.

    Holds the CUSUM-Mid accumulator. Call evaluate() once per 4H bar close.
    """

    cusum_mid: CUSUMShort = field(
        default_factory=lambda: CUSUMShort(
            drift_k=0.5,
            threshold_quantile=0.95,
            threshold_window_sec=CUSUM_MID_THRESHOLD_WINDOW_SEC,
        )
    )

    def evaluate(
        self,
        bar_timestamp: datetime,
        z_t: float,
        state_4h: Optional[str],
        current_duration_4h: int,
        *,
        # Step 4 derivatives — STUB (None until helixa GRANT active)
        funding_pctile: Optional[float] = None,
        oi_direction: Optional[Literal["rising", "falling", "flat"]] = None,
        liquidation_pulse_1h: bool = False,
        # Step 5 on-chain — STUB (observation-only)
        large_withdrawal_active: Optional[bool] = None,
        large_deposit_active: Optional[bool] = None,
        # Step 6 divergence — STUB
        cross_spread_pct: Optional[float] = None,
        # Step 7 vocab — Wave 5 STUB
        vocab: Optional[list[str]] = None,
        # sizing
        subaccount_nav_usdt: float = 80_000.0,
    ) -> Strategy1EntryDecision:
        """
        Evaluate one 4H bar through the full Strategy 1 entry decision tree.

        Args:
            bar_timestamp:        4H bar close time (used as t for CUSUM)
            z_t:                  standardised 4H return for this bar
            state_4h:             current 4H state label
            current_duration_4h:  bars spent in current state (for min-dwell)
            funding_pctile:       current funding rate percentile [0,1] (STUB)
            oi_direction:         'rising'/'falling'/'flat' (STUB)
            liquidation_pulse_1h: True if liq pulse >5× 30d mean in last 1h (STUB)
            large_withdrawal_active: True if large BTC withdrawals active (STUB)
            large_deposit_active:   True if large BTC deposits active (STUB)
            cross_spread_pct:     cross-exchange spread % (STUB)
            vocab:                list of active inverse vocab tags (Wave 5 STUB)
            subaccount_nav_usdt:  sub-account 1 NAV for sizing
        """
        t = bar_timestamp.timestamp()

        # ── Step 1: state filter ──────────────────────────────────────────────
        if state_4h not in _VALID_ENTRY_STATES:
            # Still update CUSUM so it accumulates history even during invalid states
            self.cusum_mid.update(z_t, t)
            return Strategy1EntryDecision(
                action="OBSERVE",
                reason=f"Step 1: state={state_4h!r} not in {{Coiling, Drifting-Charged}}",
                state_4h=state_4h,
                step_reached=1,
            )

        # ── Step 2: min-dwell check ───────────────────────────────────────────
        _min_dwell = 6 if state_4h == "Coiling" else 4
        if current_duration_4h < _min_dwell:
            self.cusum_mid.update(z_t, t)
            return Strategy1EntryDecision(
                action="OBSERVE",
                reason=(f"Step 2: {state_4h} dwell={current_duration_4h} < min={_min_dwell} bars"),
                state_4h=state_4h,
                step_reached=2,
            )

        # ── Step 3: CUSUM-Mid trigger ─────────────────────────────────────────
        trigger: CUSUMTrigger = self.cusum_mid.update(z_t, t)

        if not trigger.triggered:
            return Strategy1EntryDecision(
                action="OBSERVE",
                reason="Step 3: CUSUM-Mid not triggered",
                state_4h=state_4h,
                step_reached=3,
                cusum_positive=trigger.cusum_positive,
                cusum_negative=trigger.cusum_negative,
                cusum_threshold=trigger.threshold,
            )

        direction = trigger.direction  # 'LONG' or 'SHORT'
        assert direction is not None

        # ── Step 4a: funding rate filter ──────────────────────────────────────
        if funding_pctile is not None:
            if direction == "LONG" and funding_pctile > FUNDING_LONG_ABORT_PCTILE:
                return Strategy1EntryDecision(
                    action="ABORT",
                    reason=(
                        f"Step 4a: funding_pctile={funding_pctile:.2f} > "
                        f"{FUNDING_LONG_ABORT_PCTILE:.0%} — Crowding top, "
                        "LONG entry blocked"
                    ),
                    direction=direction,
                    state_4h=state_4h,
                    step_reached=4,
                    funding_pctile=funding_pctile,
                    cusum_positive=trigger.cusum_positive,
                    cusum_negative=trigger.cusum_negative,
                    cusum_threshold=trigger.threshold,
                )
            if direction == "SHORT" and funding_pctile < FUNDING_SHORT_ABORT_PCTILE:
                return Strategy1EntryDecision(
                    action="ABORT",
                    reason=(
                        f"Step 4a: funding_pctile={funding_pctile:.2f} < "
                        f"{FUNDING_SHORT_ABORT_PCTILE:.0%} — Crowding bottom, "
                        "SHORT entry blocked"
                    ),
                    direction=direction,
                    state_4h=state_4h,
                    step_reached=4,
                    funding_pctile=funding_pctile,
                    cusum_positive=trigger.cusum_positive,
                    cusum_negative=trigger.cusum_negative,
                    cusum_threshold=trigger.threshold,
                )

        # ── Step 4b: OI direction check ───────────────────────────────────────
        oi_against = False
        if oi_direction is not None:
            if direction == "LONG" and oi_direction == "falling":
                oi_against = True
            elif direction == "SHORT" and oi_direction == "rising":
                oi_against = True

        # ── Step 4c: liquidation pulse check ─────────────────────────────────
        if liquidation_pulse_1h:
            return Strategy1EntryDecision(
                action="ABORT",
                reason="Step 4c: liquidation pulse >5× 30d mean in last 1h — abort",
                direction=direction,
                state_4h=state_4h,
                step_reached=4,
                funding_pctile=funding_pctile,
                cusum_positive=trigger.cusum_positive,
                cusum_negative=trigger.cusum_negative,
                cusum_threshold=trigger.threshold,
            )

        # ── Step 5: on-chain filter (observation-only, non-blocking) ─────────
        # STUB: no v2_onchain_exchange_flows data. Log intent only.
        # When data becomes available, large_withdrawal_active + LONG → +15% size
        # large_deposit_active + LONG → -15% size (potential sell pressure)

        # ── Step 6: divergence check ──────────────────────────────────────────
        if cross_spread_pct is not None and cross_spread_pct > 0.3:
            return Strategy1EntryDecision(
                action="ABORT",
                reason=(
                    f"Step 6: cross-exchange spread {cross_spread_pct:.2f}% > 0.3% "
                    "— divergence detected, wait for convergence"
                ),
                direction=direction,
                state_4h=state_4h,
                step_reached=6,
                funding_pctile=funding_pctile,
                cusum_positive=trigger.cusum_positive,
                cusum_negative=trigger.cusum_negative,
                cusum_threshold=trigger.threshold,
            )

        # ── Step 7: inverse vocab enhancement (Wave 5 STUB) ──────────────────
        vocab_modifier = 0.0
        vocab_tags = set(vocab or [])
        for tag, mod in _VOCAB_MODIFIERS.items():
            if tag in vocab_tags:
                vocab_modifier += mod

        # ── Step 8: position sizing ───────────────────────────────────────────
        # size_modifier combines OI reduction and vocab signals
        size_modifier = 1.0
        if oi_against:
            size_modifier *= OI_REDUCTION_FACTOR

        # CUSUM intensity coefficient: min(1.5, C_peak/h) — §13.4
        cusum_coeff = min(1.5, trigger.intensity_coeff)

        # Vocab modifier capped to prevent extreme values
        vocab_scale = max(0.5, min(1.5, 1.0 + vocab_modifier))
        size_modifier *= vocab_scale * cusum_coeff

        base_size_pct = BASE_SIZE_FRACTION
        final_size_pct = min(BASE_SIZE_FRACTION, base_size_pct * size_modifier)

        action: EntryAction = "ENTER_LONG" if direction == "LONG" else "ENTER_SHORT"

        return Strategy1EntryDecision(
            action=action,
            reason=(
                f"Step 8: Entry approved — {direction} "
                f"state={state_4h} dwell={current_duration_4h} "
                f"C+={trigger.cusum_positive:.3f} C-={trigger.cusum_negative:.3f} "
                f"h={trigger.threshold:.3f} "
                f"size_modifier={size_modifier:.2f}"
                + (" oi_against=True(halved)" if oi_against else "")
                + (f" vocab={list(vocab_tags)}" if vocab_tags else "")
            ),
            direction=direction,
            state_4h=state_4h,
            step_reached=8,
            base_size_pct=final_size_pct,
            size_modifier=size_modifier,
            suggested_leverage=MAX_LEVERAGE,
            cusum_positive=trigger.cusum_positive,
            cusum_negative=trigger.cusum_negative,
            cusum_threshold=trigger.threshold,
            funding_pctile=funding_pctile,
            oi_direction_against=oi_against,
            vocab_modifier=vocab_modifier,
        )
