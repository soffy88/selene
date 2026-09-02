"""Shared math for the Chan / ICT analysis lenses (v2.2 candidate batch).

Primitives used by both the offline empirical study (`sel_v2.offline.lens_study`)
and the live observation tools (`sel_v2/observation_tools/chan_tools.py`,
`swing_structure.py`) — one code path, so live signals and offline hypothesis
tests can never drift apart.

`leg_census._zigzag_legs` is frozen per its Wave red line, and it does not record
WHEN a pivot became knowable — which the lens hypothesis tests need to avoid
look-ahead (a zigzag pivot is only confirmed once price has already reversed by
the threshold, several bars later). `zigzag_swings` below is the same algorithm
verbatim plus a `confirm_idx` per swing; equivalence with the frozen original is
unit-tested (tests/sel_v2/test_lens_common.py), so the census results and the
lens results stay comparable.

Observation-only, never imported by any live decision path; touches nothing under
strategies/**, states/**, or the epoch.
"""

from __future__ import annotations

import dataclasses
from typing import Callable, Optional

import numpy as np

from sel_v2.offline.substate import ZIGZAG_ATR_MULT, compute_atr  # noqa: F401

SURGING_STATE = "Surging"


# ── zigzag with confirmation instants ────────────────────────────────────────


@dataclasses.dataclass
class Swing:
    """One confirmed zigzag swing. `confirm_idx` is the bar whose close met the
    reversal threshold — the FIRST bar at which this swing is knowable. All
    hypothesis tests and live signals key on confirm_idx, never on end_idx
    (the pivot extreme), which is only identifiable in hindsight."""

    start_idx: int
    end_idx: int  # inclusive, the confirmed pivot bar (the extreme)
    direction: int  # +1 up swing, -1 down swing
    confirm_idx: int  # strictly > end_idx (causality; unit-tested)


def zigzag_swings(prices: np.ndarray, threshold_fn: Callable[[int, float], float]) -> list[Swing]:
    """`leg_census._zigzag_legs` verbatim (equivalence unit-tested), additionally
    recording the bar at which each swing's confirming reversal fired. A final
    in-progress swing (not yet confirmed) is NOT included."""
    n = len(prices)
    if n < 2:
        return []
    seed = 1
    while seed < n and prices[seed] == prices[0]:
        seed += 1
    if seed >= n:
        return []
    phase_up = prices[seed] > prices[0]
    ext_price, ext_idx = prices[0], 0
    last_pivot_idx = 0
    swings: list[Swing] = []
    for i in range(1, n):
        p = prices[i]
        if phase_up:
            if p > ext_price:
                ext_price, ext_idx = p, i
            elif ext_price - p >= threshold_fn(i, ext_price):
                swings.append(Swing(last_pivot_idx, ext_idx, 1, i))
                last_pivot_idx = ext_idx
                phase_up = False
                ext_price, ext_idx = p, i
        else:
            if p < ext_price:
                ext_price, ext_idx = p, i
            elif p - ext_price >= threshold_fn(i, ext_price):
                swings.append(Swing(last_pivot_idx, ext_idx, -1, i))
                last_pivot_idx = ext_idx
                phase_up = True
                ext_price, ext_idx = p, i
    return swings


def atr_zigzag_swings(close: np.ndarray, atr: np.ndarray, mult: float = ZIGZAG_ATR_MULT) -> list[Swing]:
    """1.5x ATR(14) zigzag — the CHAN-3 / ICT-2 shared fine resolution, identical
    parameter to `sel_v2.offline.substate` and `leg_census`'s push-count layer."""

    def thresh(i: int, _extreme: float) -> float:
        return mult * atr[i]

    return zigzag_swings(close, thresh)


def swings_confirmed_asof(swings: list[Swing], bar_idx: int, k: int = 3) -> list[Swing]:
    """The last `k` swings already CONFIRMED at `bar_idx` — the causal view a
    live observer would have had at that bar's close."""
    known = [s for s in swings if s.confirm_idx <= bar_idx]
    return known[-k:]


def pivot_overlap(swings3: list[Swing], close: np.ndarray, atr_i: float) -> tuple[float, float]:
    """CHAN-3 中枢: price-range intersection of 3 consecutive swings.
    Each swing's range is [min, max] of its two pivot closes. Returns
    (overlap_width, overlap_width / atr_i); (nan, nan) if fewer than 3 swings
    or ATR is not positive."""
    if len(swings3) < 3 or not atr_i > 0:
        return float("nan"), float("nan")
    los, his = [], []
    for s in swings3:
        a, b = close[s.start_idx], close[s.end_idx]
        los.append(min(a, b))
        his.append(max(a, b))
    width = max(0.0, min(his) - max(los))
    return width, width / atr_i


# ── ICT-2 swing structure state machine (BOS / CHoCH) ────────────────────────


class SwingStructure:
    """Causal BOS/CHoCH machine over confirmed swings (ICT-2, mechanized).

    state ∈ {'UP', 'DOWN', 'RANGE'}: UP = last two confirmed swing highs AND lows
    both ascending (HH+HL), DOWN mirrored (LH+LL), RANGE otherwise. The state only
    updates when a swing CONFIRMS; the per-bar events are the timely layer:

      BOS_UP     UP:   close breaks above the last confirmed swing high
      CHOCH_DOWN UP:   close breaks below the most recent confirmed higher low
      BOS_DOWN   DOWN: close breaks below the last confirmed swing low
      CHOCH_UP   DOWN: close breaks above the most recent confirmed lower high

    Each reference level fires at most once (first crossing); a newly confirmed
    pivot supplies a fresh reference and re-arms its side.
    """

    def __init__(self) -> None:
        self._highs: list[tuple[int, float]] = []  # confirmed (pivot_idx, price)
        self._lows: list[tuple[int, float]] = []
        self._state = "RANGE"
        self._fired_high_level: Optional[float] = None  # levels already broken
        self._fired_low_level: Optional[float] = None

    @property
    def state(self) -> str:
        return self._state

    def on_swing_confirmed(self, swing: Swing, close: np.ndarray) -> None:
        price = float(close[swing.end_idx])
        if swing.direction == 1:
            self._highs.append((swing.end_idx, price))
        else:
            self._lows.append((swing.end_idx, price))
        if len(self._highs) >= 2 and len(self._lows) >= 2:
            hh = self._highs[-1][1] > self._highs[-2][1]
            hl = self._lows[-1][1] > self._lows[-2][1]
            lh = self._highs[-1][1] < self._highs[-2][1]
            ll = self._lows[-1][1] < self._lows[-2][1]
            self._state = "UP" if (hh and hl) else "DOWN" if (lh and ll) else "RANGE"

    def on_bar(self, close_i: float) -> Optional[str]:
        """Check the current bar's close against the armed reference levels.
        Returns the event name on a first crossing, else None."""
        ref_high = self._highs[-1][1] if self._highs else None
        ref_low = self._lows[-1][1] if self._lows else None
        if self._state == "UP":
            if ref_high is not None and close_i > ref_high and self._fired_high_level != ref_high:
                self._fired_high_level = ref_high
                return "BOS_UP"
            if ref_low is not None and close_i < ref_low and self._fired_low_level != ref_low:
                self._fired_low_level = ref_low
                return "CHOCH_DOWN"
        elif self._state == "DOWN":
            if ref_low is not None and close_i < ref_low and self._fired_low_level != ref_low:
                self._fired_low_level = ref_low
                return "BOS_DOWN"
            if ref_high is not None and close_i > ref_high and self._fired_high_level != ref_high:
                self._fired_high_level = ref_high
                return "CHOCH_UP"
        return None


# ── Surging legs from the annotation stream ──────────────────────────────────


@dataclasses.dataclass
class SurgingLeg:
    """One contiguous run of annotated Surging bars. sel's Surging carries no
    direction (v2 single-Surging; sub_state unused), so direction is inferred
    from the sign of the leg's cumulative log return — the same convention as
    the V22-C census."""

    leg_id: int
    start_idx: int
    end_idx: int  # inclusive last Surging bar
    direction: int  # +1 / -1 / 0 (flat, excluded from same-direction matching)
    end_via: Optional[str]  # transition_via on the first bar AFTER the leg


def surging_legs(states: list[str], transition_via: list[Optional[str]], close: np.ndarray) -> list[SurgingLeg]:
    legs: list[SurgingLeg] = []
    i, n = 0, len(states)
    while i < n:
        if states[i] == SURGING_STATE:
            j = i
            while j < n and states[j] == SURGING_STATE:
                j += 1
            ret = float(np.log(close[j - 1] / close[i])) if close[i] > 0 else 0.0
            legs.append(
                SurgingLeg(
                    leg_id=len(legs),
                    start_idx=i,
                    end_idx=j - 1,
                    direction=int(np.sign(ret)),
                    end_via=transition_via[j] if j < n else None,
                )
            )
            i = j
        else:
            i += 1
    return legs


# ── statistics helpers (all deterministic / seeded) ──────────────────────────


def welch_t_one_sided(a, b) -> tuple[float, float]:
    """One-sided Welch t (mean(a) > mean(b)). Returns (statistic, p)."""
    from scipy.stats import ttest_ind

    res = ttest_ind(a, b, equal_var=False, alternative="greater")
    return float(res.statistic), float(res.pvalue)


def mann_whitney_one_sided(a, b, alternative: str = "greater") -> tuple[float, float]:
    from scipy.stats import mannwhitneyu

    res = mannwhitneyu(a, b, alternative=alternative)
    return float(res.statistic), float(res.pvalue)


def fisher_one_sided(table_2x2) -> float:
    """One-sided Fisher exact p (top-left cell enriched)."""
    from scipy.stats import fisher_exact

    return float(fisher_exact(table_2x2, alternative="greater")[1])


def bootstrap_mean_diff_ci(a, b, n_boot: int = 10_000, seed: int = 42, alpha: float = 0.10) -> tuple[float, float]:
    """Percentile bootstrap CI for mean(a) - mean(b). Seeded → deterministic."""
    rng = np.random.default_rng(seed)
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    diffs = np.empty(n_boot)
    for k in range(n_boot):
        diffs[k] = np.mean(rng.choice(a, size=len(a))) - np.mean(rng.choice(b, size=len(b)))
    return (
        float(np.percentile(diffs, 100 * alpha / 2)),
        float(np.percentile(diffs, 100 * (1 - alpha / 2))),
    )


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact binomial CI for a proportion."""
    from scipy.stats import beta

    lo = 0.0 if k == 0 else float(beta.ppf(alpha / 2, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(1 - alpha / 2, k + 1, n - k))
    return lo, hi


def bh_adjust(pvals: list[float]) -> list[float]:
    """Benjamini-Hochberg q-values (monotone, capped at 1)."""
    m = len(pvals)
    order = np.argsort(pvals)
    q = np.empty(m)
    prev = 1.0
    for rank_from_end, idx in enumerate(reversed(order)):
        rank = m - rank_from_end  # 1-based rank of this p in ascending order
        prev = min(prev, pvals[idx] * m / rank)
        q[idx] = prev
    return [float(x) for x in q]
