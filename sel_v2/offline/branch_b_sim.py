"""Branch B pyramid simulator (Wave V22-A, offline gate — v2.2 design draft D1/D2/D4/D5).

Wiki-ruled (2026-07-11), parameters frozen — do not retune on replay results:

    entry:  k-th confirmed RE_PUSH within a Surging leg (see sel_v2.offline.substate)
            k=1 -> +30%, k=2 -> +45%, k=3 -> +25% of the quota basis (S1 Kelly cap,
            normalised here to 1.0); k>=4 or terminal_flag already latched -> no add.
    exit (priority order, first match per bar wins for full exits):
        1. parent state -> Cascade                      -> exit all
        2. close breaks the moving structural stop       -> exit all
        3. terminal_flag first latches                   -> reduce 50% (one-time)
        4. parent state leaves Surging (any other state)  -> exit all
        5. total leg unrealized P&L <= -3%                -> exit all
        6. held >= 30 days (180 4H bars)                  -> exit all
    costs: taker 0.08% + 2bp slippage, both sides, computed independently per
        batch-fragment (a fragment is what a single exit event closes).

Pure function over OHLC + parent state series (v2_state_annotation) + the
sub-state-machine output (sel_v2.offline.substate.compute_substates). Offline-only —
never imported by any live path. Writes nothing; the caller persists to
v2_sim_v22_trades or a report file, never v2_state_history / v2_trades (red line).
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from sel_v2.offline.substate import BarSubState, Event

BATCH_WEIGHTS: dict[int, float] = {1: 0.30, 2: 0.45, 3: 0.25}
MAX_BATCH_K = 3

TAKER_FEE = 0.0008
SLIPPAGE = 0.0002
ROUND_TRIP_COST_RATE = 2 * (TAKER_FEE + SLIPPAGE)  # entry + exit, each side

DRAWDOWN_STOP_PCT = -0.03
TIME_STOP_BARS = 180  # 30 days * 24h / 4h bars

SURGING_STATE = "Surging"
CASCADE_STATE = "Cascade"


@dataclasses.dataclass
class Batch:
    k: int
    weight: float
    entry_price: float
    entry_bar: int


@dataclasses.dataclass
class Leg:
    leg_id: int
    direction: int
    batches: list = dataclasses.field(default_factory=list)  # list[Batch], active only
    terminal_cut_done: bool = False

    @property
    def total_weight(self) -> float:
        return sum(b.weight for b in self.batches)


@dataclasses.dataclass
class TradeRecord:
    leg_id: int
    batch_k: int
    direction: int
    entry_price: float
    entry_bar: int
    exit_price: float
    exit_bar: int
    exit_reason: str
    weight: float
    bars_held: int
    net_pnl: float  # quota-fraction units; sum across a leg's fragments = leg P&L


class AccountingError(RuntimeError):
    """Raised on the Wave's STOP condition: a leg's weight left [0, 1] bounds."""


def _fragment_pnl(direction: int, weight: float, entry_price: float, exit_price: float) -> float:
    gross = weight * direction * (exit_price / entry_price - 1.0)
    cost = weight * ROUND_TRIP_COST_RATE
    return gross - cost


def _unrealized_pct(leg: Leg, price: float) -> Optional[float]:
    total = leg.total_weight
    if total <= 0:
        return None
    pnl = sum(b.weight * leg.direction * (price / b.entry_price - 1.0) for b in leg.batches)
    return pnl / total


def simulate_branch_b(close, parent_state: list[str], substates: list[BarSubState]) -> list[TradeRecord]:
    """Run the Branch B pyramid simulation over the full bar series. Returns one
    TradeRecord per (batch, exit-event) fragment — a batch not cut by TERMINAL_FLAG
    before its final exit produces exactly one record; one cut before final exit
    produces two (the 50%-cut fragment, then the remaining-50% fragment)."""
    n = len(close)
    records: list[TradeRecord] = []
    leg: Optional[Leg] = None
    leg_counter = 0

    for i in range(n):
        b = substates[i]
        parent = parent_state[i]
        price = float(close[i])

        if leg is not None:
            exited = False

            # 1: parent state -> Cascade
            if parent == CASCADE_STATE:
                _close_all(leg, price, i, "cascade", records)
                leg, exited = None, True

            # 2: structural stop break
            if not exited and Event.STRUCTURE_BREAK in b.events:
                _close_all(leg, price, i, "stop_break", records)
                leg, exited = None, True

            # 3: terminal flag first latch -> 50% reduce (not a full exit)
            if not exited and leg is not None and Event.TERMINAL_FLAG in b.events and not leg.terminal_cut_done:
                _cut_half(leg, price, i, records)
                leg.terminal_cut_done = True

            # 4: parent state left Surging for any other reason
            if not exited and leg is not None and parent != SURGING_STATE:
                _close_all(leg, price, i, "left_surging", records)
                leg, exited = None, True

            # 5: total leg unrealized P&L <= -3%
            if not exited and leg is not None:
                u = _unrealized_pct(leg, price)
                if u is not None and u <= DRAWDOWN_STOP_PCT:
                    _close_all(leg, price, i, "drawdown", records)
                    leg, exited = None, True

            # 6: time stop, 30 days held (from the leg's first entry)
            if not exited and leg is not None:
                first_entry_bar = leg.batches[0].entry_bar
                if i - first_entry_bar >= TIME_STOP_BARS:
                    _close_all(leg, price, i, "time_stop", records)
                    leg, exited = None, True

        # Entries: a RE_PUSH this bar opens k=1 or adds k=2/3.
        if parent == SURGING_STATE and Event.RE_PUSH in b.events:
            k = b.push_count
            if leg is None:
                if k == 1:
                    leg_counter += 1
                    leg = Leg(leg_id=leg_counter, direction=b.direction)
                    leg.batches.append(Batch(k=1, weight=BATCH_WEIGHTS[1], entry_price=price, entry_bar=i))
            elif not leg.terminal_cut_done and 2 <= k <= MAX_BATCH_K and k not in {bt.k for bt in leg.batches}:
                leg.batches.append(Batch(k=k, weight=BATCH_WEIGHTS[k], entry_price=price, entry_bar=i))

            if leg is not None and (leg.total_weight > 1.0 + 1e-9 or leg.total_weight < -1e-9):
                raise AccountingError(f"leg {leg.leg_id} weight {leg.total_weight} outside [0,1] at bar {i}")

    # Force-close anything still open at the end of the data (not a modeled exit rule —
    # just so no leg is left silently open; excluded from the gate report's exit-reason
    # distribution, footnoted separately).
    if leg is not None:
        _close_all(leg, float(close[-1]), n - 1, "data_end", records)

    return records


def _cut_half(leg: Leg, price: float, bar: int, records: list[TradeRecord]) -> None:
    for batch in leg.batches:
        half = batch.weight / 2.0
        records.append(
            TradeRecord(
                leg_id=leg.leg_id,
                batch_k=batch.k,
                direction=leg.direction,
                entry_price=batch.entry_price,
                entry_bar=batch.entry_bar,
                exit_price=price,
                exit_bar=bar,
                exit_reason="terminal_50pct",
                weight=half,
                bars_held=bar - batch.entry_bar,
                net_pnl=_fragment_pnl(leg.direction, half, batch.entry_price, price),
            )
        )
        batch.weight = half


def _close_all(leg: Leg, price: float, bar: int, reason: str, records: list[TradeRecord]) -> None:
    for batch in leg.batches:
        if batch.weight <= 0:
            continue
        records.append(
            TradeRecord(
                leg_id=leg.leg_id,
                batch_k=batch.k,
                direction=leg.direction,
                entry_price=batch.entry_price,
                entry_bar=batch.entry_bar,
                exit_price=price,
                exit_bar=bar,
                exit_reason=reason,
                weight=batch.weight,
                bars_held=bar - batch.entry_bar,
                net_pnl=_fragment_pnl(leg.direction, batch.weight, batch.entry_price, price),
            )
        )
    leg.batches = []
