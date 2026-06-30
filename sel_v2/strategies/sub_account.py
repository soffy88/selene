"""
Dual sub-account paper trading engine  (v2.0 §15)

Physical isolation:
  SubAccount 1 — Strategy 1 (4H trend), 80% of total capital
  SubAccount 2 — Strategy 2 (intraday CUSUM), 20% of total capital

Design principles (§15.2):
  - Sub-accounts operate independently; no cross-account hedging
  - Shared red line: Cascade triggers each account to close its own positions
  - Max concurrent: Strategy 1 = 1, Strategy 2 = 2
  - Paper trading only in Wave 4 — no real OKX API calls

Each position records entry context for DB persistence (v2_trades).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional


Direction = Literal["LONG", "SHORT"]

# §15.1: capital allocation fractions
SUBACCOUNT_1_FRACTION: float = 0.80
SUBACCOUNT_2_FRACTION: float = 0.20

# §13.4 / §14.4: concurrent position limits
MAX_CONCURRENT_S1: int = 1
MAX_CONCURRENT_S2: int = 2

# Transaction-cost model — aligned with backtest/costs.py so the paper engine and
# the backtester charge the same per-trade frictions. Previously paper fills were
# free (mid-price, no fee/slippage/funding), so reported paper PnL was optimistic
# by at least round-trip fees + funding. Cost is charged when a notional unit is
# exited; entry+exit is accounted round-trip at that point.
TAKER_FEE_PCT: float = 0.0004                      # 4 bps per side (taker)
HALF_SPREAD_PCT: float = (2.0 / 10_000.0) / 2.0    # half of a 2 bps spread, per side
ONE_SIDE_COST_PCT: float = TAKER_FEE_PCT + HALF_SPREAD_PCT

# Perpetual funding settles every 8h; the v2 engine steps 4H bars, so one bar is
# half a settlement period. Funding is accrued per bar on open positions and
# deducted from realised PnL at exit.
FUNDING_SETTLEMENT_HOURS: float = 8.0
BAR_HOURS: float = 4.0
FUNDING_BAR_FACTOR: float = BAR_HOURS / FUNDING_SETTLEMENT_HOURS


def round_trip_cost_usdt(notional_usdt: float) -> float:
    """Round-trip (entry+exit) fee + slippage in USDT for a given traded notional."""
    return 2.0 * ONE_SIDE_COST_PCT * notional_usdt


@dataclass
class Position:
    """One open paper position."""
    id: str
    strategy: str            # 'strategy_1' / 'strategy_2'
    sub_account: str         # 'subaccount_1' / 'subaccount_2'
    direction: Direction
    entry_price: float
    size_usdt: float         # notional in USDT (no leverage)
    leverage: float
    instrument: str
    entry_time: datetime
    entry_state: str
    entry_cusum_id: Optional[str] = None
    entry_vocab: Optional[list[str]] = None
    entry_confidence: Optional[float] = None
    # Strategy 2 batch tracking
    cusum_peak_since_entry: float = 0.0
    batches_triggered: int = 0
    # Critical-reduce tracking (Strategy 1)
    critical_reduced: bool = False
    # Accumulated funding cost in USDT (positive = paid to the market), accrued per
    # bar while the position is open and deducted from realised PnL at exit.
    accrued_funding_usdt: float = 0.0

    @property
    def notional_usdt(self) -> float:
        return self.size_usdt * self.leverage

    def accrue_funding(self, funding_rate: float) -> None:
        """Accrue one bar's funding as a cost. Longs pay when the funding rate is
        positive, shorts receive; pro-rated by FUNDING_BAR_FACTOR because a 4H bar
        is half an 8H settlement period."""
        sign = 1.0 if self.direction == "LONG" else -1.0
        self.accrued_funding_usdt += (
            sign * funding_rate * FUNDING_BAR_FACTOR * self.notional_usdt
        )

    def unrealized_pnl_pct(self, mark_price: float) -> float:
        if self.direction == "LONG":
            return (mark_price - self.entry_price) / self.entry_price
        return (self.entry_price - mark_price) / self.entry_price

    def unrealized_pnl_usdt(self, mark_price: float) -> float:
        return self.unrealized_pnl_pct(mark_price) * self.notional_usdt


@dataclass
class ClosedPosition:
    """Audit record for a closed position."""
    position: Position
    exit_price: float
    exit_time: datetime
    exit_reason: str
    pnl_usdt: float
    pnl_pct: float


@dataclass
class SubAccount:
    """
    One paper sub-account.

    Tracks NAV, open positions, and closed history.
    Thread-safety is the caller's responsibility (single-threaded replay assumed).
    """
    name: str           # 'subaccount_1' / 'subaccount_2'
    strategy: str       # 'strategy_1' / 'strategy_2'
    initial_nav: float
    max_concurrent: int

    _nav: float = field(init=False)
    _positions: dict[str, Position] = field(default_factory=dict, init=False)
    _closed: list[ClosedPosition] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._nav = self.initial_nav

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def nav(self) -> float:
        return self._nav

    @property
    def open_positions(self) -> list[Position]:
        return list(self._positions.values())

    @property
    def closed_positions(self) -> list[ClosedPosition]:
        return list(self._closed)

    def unrealized_pnl(self, mark_price: float) -> float:
        return sum(p.unrealized_pnl_usdt(mark_price) for p in self._positions.values())

    def equity(self, mark_price: float) -> float:
        return self._nav + self.unrealized_pnl(mark_price)

    # ── Open / close ──────────────────────────────────────────────────────────

    def can_open(self) -> bool:
        return len(self._positions) < self.max_concurrent

    def open_position(
        self,
        direction: Direction,
        entry_price: float,
        size_pct: float,          # fraction of current nav to commit
        leverage: float,
        instrument: str,
        entry_time: datetime,
        entry_state: str,
        entry_cusum_id: Optional[str] = None,
        entry_vocab: Optional[list[str]] = None,
        entry_confidence: Optional[float] = None,
    ) -> Optional[Position]:
        """
        Open a new paper position. Returns None if max_concurrent is reached.

        size_pct is applied to current nav (no leverage at commitment layer);
        leverage amplifies the notional for PnL calculation.
        """
        if not self.can_open():
            return None

        size_usdt = self._nav * min(size_pct, 1.0)
        pos = Position(
            id=str(uuid.uuid4()),
            strategy=self.strategy,
            sub_account=self.name,
            direction=direction,
            entry_price=entry_price,
            size_usdt=size_usdt,
            leverage=leverage,
            instrument=instrument,
            entry_time=entry_time,
            entry_state=entry_state,
            entry_cusum_id=entry_cusum_id,
            entry_vocab=entry_vocab,
            entry_confidence=entry_confidence,
        )
        self._positions[pos.id] = pos
        return pos

    def close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_time: datetime,
        exit_reason: str,
    ) -> Optional[ClosedPosition]:
        """
        Close a position fully. Returns a ClosedPosition audit record.
        Updates NAV with realised PnL.
        """
        pos = self._positions.pop(position_id, None)
        if pos is None:
            return None

        # pnl_pct stays the gross price move; pnl_usdt is net of round-trip
        # fee+slippage and accrued funding (the real frictions a live fill pays).
        pnl_pct = pos.unrealized_pnl_pct(exit_price)
        gross_pnl = pos.unrealized_pnl_usdt(exit_price)
        cost = round_trip_cost_usdt(pos.notional_usdt)
        pnl_usdt = gross_pnl - cost - pos.accrued_funding_usdt
        self._nav += pnl_usdt

        closed = ClosedPosition(
            position=pos,
            exit_price=exit_price,
            exit_time=exit_time,
            exit_reason=exit_reason,
            pnl_usdt=pnl_usdt,
            pnl_pct=pnl_pct,
        )
        self._closed.append(closed)
        return closed

    def reduce_position(
        self,
        position_id: str,
        fraction: float,
        exit_price: float,
        exit_time: datetime,
        exit_reason: str,
    ) -> Optional[ClosedPosition]:
        """
        Partially close `fraction` of a position.
        Creates a ClosedPosition for the reduced portion; updates the
        remaining position's size_usdt and nav.

        Returns None if position not found.
        """
        pos = self._positions.get(position_id)
        if pos is None:
            return None

        fraction = max(0.0, min(1.0, fraction))
        reduced_usdt = pos.size_usdt * fraction
        pnl_pct = pos.unrealized_pnl_pct(exit_price)
        gross_pnl = reduced_usdt * leverage_pnl(pos.leverage, pnl_pct)
        # The exited fraction round-trips its share of fee+slippage and carries its
        # share of accrued funding; the remainder keeps the rest.
        cost = round_trip_cost_usdt(reduced_usdt * pos.leverage)
        funding_charge = pos.accrued_funding_usdt * fraction
        pos.accrued_funding_usdt -= funding_charge
        pnl_usdt = gross_pnl - cost - funding_charge

        pos.size_usdt -= reduced_usdt
        self._nav += pnl_usdt

        # Create a synthetic closed record for the partial exit
        reduced_pos = Position(
            id=pos.id + f"_partial_{fraction:.2f}",
            strategy=pos.strategy,
            sub_account=pos.sub_account,
            direction=pos.direction,
            entry_price=pos.entry_price,
            size_usdt=reduced_usdt,
            leverage=pos.leverage,
            instrument=pos.instrument,
            entry_time=pos.entry_time,
            entry_state=pos.entry_state,
        )
        closed = ClosedPosition(
            position=reduced_pos,
            exit_price=exit_price,
            exit_time=exit_time,
            exit_reason=exit_reason,
            pnl_usdt=pnl_usdt,
            pnl_pct=pnl_pct,
        )
        self._closed.append(closed)
        return closed

    def cascade_close_all(
        self,
        exit_price: float,
        exit_time: datetime,
    ) -> list[ClosedPosition]:
        """Close all open positions (Cascade red line)."""
        position_ids = list(self._positions.keys())
        results = []
        for pid in position_ids:
            result = self.close_position(pid, exit_price, exit_time, "Cascade red line")
            if result:
                results.append(result)
        return results


def leverage_pnl(leverage: float, pnl_pct: float) -> float:
    """Convert unleveraged PnL fraction to leveraged PnL fraction of committed size."""
    return pnl_pct * leverage


# ── Dual sub-account engine ───────────────────────────────────────────────────

@dataclass
class DualSubAccountEngine:
    """
    Manages both sub-accounts for paper trading.

    Usage:
        engine = DualSubAccountEngine(total_nav_usdt=100_000)
        pos = engine.subaccount_1.open_position(...)
        engine.subaccount_2.open_position(...)
    """
    total_nav_usdt: float

    subaccount_1: SubAccount = field(init=False)
    subaccount_2: SubAccount = field(init=False)

    def __post_init__(self) -> None:
        self.subaccount_1 = SubAccount(
            name="subaccount_1",
            strategy="strategy_1",
            initial_nav=self.total_nav_usdt * SUBACCOUNT_1_FRACTION,
            max_concurrent=MAX_CONCURRENT_S1,
        )
        self.subaccount_2 = SubAccount(
            name="subaccount_2",
            strategy="strategy_2",
            initial_nav=self.total_nav_usdt * SUBACCOUNT_2_FRACTION,
            max_concurrent=MAX_CONCURRENT_S2,
        )

    def total_equity(self, mark_price: float) -> float:
        return (
            self.subaccount_1.equity(mark_price)
            + self.subaccount_2.equity(mark_price)
        )

    def cascade_close_all(
        self,
        exit_price: float,
        exit_time: datetime,
    ) -> list[ClosedPosition]:
        """Trigger Cascade red line on both sub-accounts simultaneously."""
        results = self.subaccount_1.cascade_close_all(exit_price, exit_time)
        results += self.subaccount_2.cascade_close_all(exit_price, exit_time)
        return results
