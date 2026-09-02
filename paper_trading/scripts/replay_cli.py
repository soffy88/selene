"""
CLI tool for replaying DecisionTrail sequences.

Usage:
    # With DB (load from TimescaleDB):
    python -m paper_trading.scripts.replay_cli --symbol BTCUSDT --start 2026-01-01 --end 2026-01-31

    # Demo mode (no args, no DB required):
    python -m paper_trading.scripts.replay_cli
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Synthetic trail factory for demo mode
# ---------------------------------------------------------------------------


def _make_synthetic_trails(n: int = 5):
    """Create *n* synthetic DecisionTrail records for demo / testing."""
    from paper_trading.trail import DecisionTrail

    base_time = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    states = ["Coiling", "Surging_Up", "Drifting_Charged", "Drifting_Calm", "Coiling"]
    prev_states = [None, "Coiling", "Surging_Up", "Drifting_Charged", "Drifting_Calm"]
    actions = ["no_action", "open_long", "hold", "close", "no_action"]
    rule_ids = [
        "coiling_after_drifting_prepare",
        "surging_up_from_coiling",
        "drifting_charged_after_surging_reduce",
        "drifting_calm_after_surging_close",
        "coiling_default_no_action",
    ]

    trails = []
    nav = 10_000.0
    for i in range(n):
        from datetime import timedelta

        t = base_time + timedelta(hours=i)
        trail = DecisionTrail(
            time=t,
            symbol="BTCUSDT",
            config_hash="demo0000deadbeef",
            using_claude_default=True,
            current_state=states[i % len(states)],
            previous_state=prev_states[i % len(prev_states)],
            state_history=[s for s in states[: i + 1]],
            state_reason=f"synthetic bar {i}",
            close=60_000.0 + i * 100,
            sigma_p_24h=0.15 + i * 0.01,
            H=0.45,
            TF=None,
            OI=None,
            funding_rate=0.0001,
            LV=None,
            absorption_ratio=None,
            OI_hurst=None,
            feature_completeness=0.44,
            nav_usdt=nav,
            position_side="long" if i == 1 else None,
            position_size_usdt=2_000.0 if i == 1 else 0.0,
            unrealized_pnl_usdt=50.0 if i == 1 else 0.0,
            realized_pnl_usdt=-50.0 if i >= 3 else 0.0,
            proposed_action=actions[i % len(actions)],
            final_action=actions[i % len(actions)],
            rule_id=rule_ids[i % len(rule_ids)],
            risk_triggered=None,
            risk_details="ok",
            fill_price=60_100.0 if i == 1 else None,
            fill_size_usdt=2_000.0 if i == 1 else None,
            fill_fee_usdt=1.0 if i == 1 else None,
            realized_pnl_this_bar=-50.0 if i == 3 else None,
            risk_check_passed=True,
        )
        trails.append(trail)
    return trails


# ---------------------------------------------------------------------------
# Print summary table
# ---------------------------------------------------------------------------


def _print_table(original: list, replayed: list) -> None:
    header = f"{'Time':20s} {'State':20s} {'Proposed':12s} {'Final':12s} {'Risk':25s} {'PnL':10s}"
    print(header)
    print("-" * len(header))
    for _orig, rep in zip(original, replayed, strict=False):
        risk = rep.risk_triggered or "-"
        pnl = f"{rep.realized_pnl_this_bar:.2f}" if rep.realized_pnl_this_bar is not None else "-"
        state = rep.current_state or "None"
        t = rep.time.strftime("%Y-%m-%d %H:%M")
        print(f"{t:20s} {state:20s} {rep.proposed_action:12s} {rep.final_action:12s} {risk:25s} {pnl:10s}")

    print()
    # Delta summary: count where final_action changed vs original
    changed = sum(1 for o, r in zip(original, replayed, strict=False) if o.final_action != r.final_action)
    print(f"Total bars: {len(replayed)}  |  Decision changed vs original: {changed}")
    risk_triggers = sum(1 for r in replayed if r.risk_triggered is not None)
    print(f"Risk triggers: {risk_triggers}")


# ---------------------------------------------------------------------------
# Async DB-backed load + replay
# ---------------------------------------------------------------------------


async def _run_db(symbol: str, start: datetime, end: datetime) -> None:
    import os

    import asyncpg  # type: ignore

    url = os.environ.get("TIMESCALE_URL", "postgresql://test:test@localhost/test")
    # Convert SQLAlchemy-style URL to asyncpg format
    url = url.replace("postgresql+asyncpg://", "postgresql://")

    pool = await asyncpg.create_pool(url)
    try:
        from paper_trading.db.trail_store import TrailStore

        trails = await TrailStore.query_range(pool, symbol, start, end)
    finally:
        await pool.close()

    if not trails:
        print(f"No trails found for {symbol} in [{start.date()}, {end.date()})")
        return

    from decision.config import load_config
    from paper_trading.replay import DecisionReplayer

    config = load_config()
    replayer = DecisionReplayer(config)
    replayed = replayer.replay(trails)
    _print_table(trails, replayed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="DecisionTrail replay tool")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--start", default=None, help="YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD")
    args = parser.parse_args()

    if args.symbol is None:
        # Demo mode — no DB needed
        print("=== Demo mode (5 synthetic trails) ===\n")
        from decision.config import load_config
        from paper_trading.replay import DecisionReplayer

        config = load_config()
        trails = _make_synthetic_trails(5)
        replayer = DecisionReplayer(config)
        replayed = replayer.replay(trails)
        _print_table(trails, replayed)
        return

    # DB mode
    import asyncio

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    asyncio.run(_run_db(args.symbol, start, end))


if __name__ == "__main__":
    main()
