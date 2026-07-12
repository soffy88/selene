"""ICT-8 SMT divergence (Smart Money Technique) — PREREGISTERED (2026-07-12).

The one distinctive ICT concept the 2026-07-11 round skipped for lack of data:
cross-asset swing divergence between two correlated instruments (BTC/ETH). A
new swing extreme in one asset NOT confirmed by the other is read as engineered
liquidity (one-sided stop run) and implies reversal. Unlocked by the discovery
that the iris md layer already collects ETH ticks and by backfilling 2yr of
ETH-USDT 4H bars into v2_bars_4h (symbol partition).

Mechanization (single spec, parameters fixed before results):
  - both assets: the SAME 1.5x ATR(14) causal zigzag as CHAN-3/ICT-2
    (lens_common.atr_zigzag_swings; every event keys on confirm instants)
  - pivot matching: asset B's last same-type swing whose PIVOT bar is within
    +- MATCH_WINDOW bars of asset A's pivot bar
  - bearish SMT at bar t: A prints a higher swing-high (HH vs its previous
    top) while B's matched swing-high is a lower high (LH) — and vice versa
    for bullish SMT at swing lows; A/B roles are scanned symmetrically
  - event time = max(confirm_A, confirm_B) (knowable only when both confirmed)
  - hypothesis: bearish SMT -> BTC forward 6-bar return BELOW baseline;
    bullish -> above (MW one-sided per direction; pass = both p<0.10,
    single = partial — the ICT-round preregistered rule)

Pure functions over aligned close arrays. Offline-only; touches nothing under
strategies/**, states/**, or the epoch.
"""

from __future__ import annotations

import dataclasses

import numpy as np

from sel_v2.offline.lens_common import Swing, atr_zigzag_swings

MATCH_WINDOW = 6  # bars: max pivot-time distance between the two assets' swings
FWD_BARS = 6


@dataclasses.dataclass
class SMTEvent:
    idx: int  # bar at which the divergence became knowable (max confirm)
    direction: int  # -1 bearish (divergent highs), +1 bullish (divergent lows)
    leader: str  # which asset printed the new extreme ('A' or 'B')


def _tops_bottoms(swings: list[Swing]) -> tuple[list[Swing], list[Swing]]:
    tops = [s for s in swings if s.direction == 1]  # up-swing ends at a top
    bottoms = [s for s in swings if s.direction == -1]
    return tops, bottoms


def _matched(pivots: list[Swing], ref: Swing) -> Swing | None:
    """B's last same-type pivot within MATCH_WINDOW bars of ref's pivot bar."""
    best = None
    for s in pivots:
        if abs(s.end_idx - ref.end_idx) <= MATCH_WINDOW:
            if best is None or s.end_idx > best.end_idx:
                best = s
    return best


def _scan_one_side(
    tops_a: list[Swing],
    tops_b: list[Swing],
    close_a: np.ndarray,
    close_b: np.ndarray,
    direction: int,
    leader: str,
) -> list[SMTEvent]:
    """Divergent highs (direction=-1, uses tops) or lows (+1, pass bottoms +
    the same comparison flipped via `direction`)."""
    out: list[SMTEvent] = []
    for prev, cur in zip(tops_a, tops_a[1:]):
        a_prev, a_cur = close_a[prev.end_idx], close_a[cur.end_idx]
        a_extends = a_cur > a_prev if direction == -1 else a_cur < a_prev
        if not a_extends:
            continue
        b_cur = _matched(tops_b, cur)
        b_prev = _matched(tops_b, prev)
        if b_cur is None or b_prev is None or b_cur.end_idx <= b_prev.end_idx:
            continue
        bp, bc = close_b[b_prev.end_idx], close_b[b_cur.end_idx]
        b_fails = bc < bp if direction == -1 else bc > bp
        if b_fails:
            out.append(
                SMTEvent(
                    idx=max(cur.confirm_idx, b_cur.confirm_idx),
                    direction=direction,
                    leader=leader,
                )
            )
    return out


def detect_smt(
    close_a: np.ndarray,
    atr_a: np.ndarray,
    close_b: np.ndarray,
    atr_b: np.ndarray,
) -> list[SMTEvent]:
    """SMT events over two ALIGNED bar frames (same timestamps index-for-index).
    Scans both leader roles; deduplicates events landing on the same bar with
    the same direction."""
    sw_a = atr_zigzag_swings(close_a, atr_a)
    sw_b = atr_zigzag_swings(close_b, atr_b)
    tops_a, bots_a = _tops_bottoms(sw_a)
    tops_b, bots_b = _tops_bottoms(sw_b)
    events: list[SMTEvent] = []
    events += _scan_one_side(tops_a, tops_b, close_a, close_b, -1, "A")
    events += _scan_one_side(tops_b, tops_a, close_b, close_a, -1, "B")
    events += _scan_one_side(bots_a, bots_b, close_a, close_b, 1, "A")
    events += _scan_one_side(bots_b, bots_a, close_b, close_a, 1, "B")
    seen: set[tuple[int, int]] = set()
    unique = []
    for e in sorted(events, key=lambda e: e.idx):
        key = (e.idx, e.direction)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique
