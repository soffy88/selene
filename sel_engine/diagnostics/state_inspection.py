"""
sel_engine/diagnostics/state_inspection.py

Read-only diagnostic: pulls last 12 days of BTC 1H candles + sel state sequence,
detects state runs, samples up to 5 instances per state, and generates a report.

Usage:
    python -m sel_engine.diagnostics.state_inspection [--symbol BTCUSDT] [--days 12]

Data sources (in priority order):
  1. sel_state_sequence + candles tables in TimescaleDB
  2. If sel_state_sequence is empty: run StateEngine in-memory on fetched candles
  3. If candles also empty: synthetic data with a clear warning

Never writes to any sel_engine table. All reads are SELECT-only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import asyncpg

# ── paths ──────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_REPORT_PATH = _HERE / "state_inspection_report.md"
_FIGURES_DIR = _HERE / "figures"
_FIGURES_DIR.mkdir(exist_ok=True)

# ── constants ──────────────────────────────────────────────────────────────
ALL_STATES = [
    "Cascade",
    "Critical",
    "Coiling",
    "Surging_Up",
    "Surging_Down",
    "Drifting_Charged",
    "Drifting_Calm",
]
MAX_SAMPLES = 5
SPARKLINE_BLOCKS = " ▁▂▃▄▅▆▇█"


# ─────────────────────────────────────────────────────────────────────────────
# ASCII helpers
# ─────────────────────────────────────────────────────────────────────────────


def _sparkline(values: list[float], width: int = 48) -> str:
    """Single-line Unicode block sparkline."""
    if not values:
        return "(empty)"
    lo, hi = min(values), max(values)
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    if hi == lo:
        return "─" * len(values)

    def _ch(v: float) -> str:
        idx = round((v - lo) / (hi - lo) * (len(SPARKLINE_BLOCKS) - 1))
        return SPARKLINE_BLOCKS[idx]

    return "".join(_ch(v) for v in values)


def _ascii_chart(
    times: list[datetime],
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    height: int = 8,
    width: int = 56,
) -> str:
    """
    Multi-line OHLC bar chart saved to an ASCII .txt file.
    Each column = one 1H bar.  Pipe '│' = high-low wick, block '█'/'▄'/'▀' = body.
    """
    n = len(closes)
    if n == 0:
        return "(no data)"

    # Subsample if more bars than display width
    if n > width:
        step = n / width
        idx = [int(i * step) for i in range(width)]
        times = [times[i] for i in idx]
        opens = [opens[i] for i in idx]
        highs = [highs[i] for i in idx]
        lows = [lows[i] for i in idx]
        closes = [closes[i] for i in idx]
        n = len(closes)

    lo = min(lows)
    hi = max(highs)
    span = hi - lo or 1.0

    def _row(v: float) -> int:
        """Map price to row index (0 = top)."""
        return height - 1 - round((v - lo) / span * (height - 1))

    lines = []
    # Build grid: height rows × n cols
    grid = [[" "] * n for _ in range(height)]
    for col, (o, h, low, c) in enumerate(zip(opens, highs, lows, closes, strict=False)):
        top_wick = _row(h)
        bot_wick = _row(low)
        body_top = _row(max(o, c))
        body_bot = _row(min(o, c))
        for row in range(height):
            if body_top <= row <= body_bot:
                grid[row][col] = "█" if c >= o else "░"
            elif top_wick <= row <= bot_wick:
                grid[row][col] = "│"

    # Price axis labels
    label_w = 10
    for row_i, row in enumerate(grid):
        price = hi - row_i / (height - 1) * span
        label = f"{price:>9.0f} "
        lines.append(label + "".join(row))

    # Time axis: mark first bar of every 6H
    " " * label_w
    tick_row = []
    for col, t in enumerate(times):
        if t.hour % 6 == 0 and col > 0:
            tick_row.append("┬")
        else:
            tick_row.append("─")
    lines.append(" " * label_w + "└" + "".join(tick_row))

    # Time labels
    lbl_line = " " * (label_w + 1)
    for _col, t in enumerate(times):
        if t.hour % 6 == 0:
            tag = t.strftime("%d/%H")
            lbl_line += tag
            lbl_line += " " * max(0, 6 - len(tag) - 1) + " "
    lines.append(lbl_line.rstrip())

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# DB queries
# ─────────────────────────────────────────────────────────────────────────────

_CANDLES_SQL = """
SELECT time, open, high, low, close, volume
FROM candles
WHERE symbol = $1
  AND interval = '1h'
  AND time >= $2
ORDER BY time ASC
"""

_STATE_SEQ_SQL = """
SELECT
    time, state, direction, is_cold_start,
    reason, transition_from,
    close_price, sigma_p_24h, H, TF, OI, funding_rate, LV,
    feature_completeness
FROM sel_state_sequence
WHERE symbol = $1
  AND time >= $2
ORDER BY time ASC
"""

_SEL_FEATURES_SQL = """
SELECT
    time, close, delta_p_pct, sigma_p_24h,
    H, TF, OI, funding_rate, LV,
    absorption_ratio, price_autocorr_12h, price_autocorr_24h, price_autocorr_48h,
    sigma_p_d2, OI_hurst
FROM sel_features
WHERE symbol = $1
  AND time >= $2
ORDER BY time ASC
"""


async def _fetch_all(
    dsn: str,
    symbol: str,
    since: datetime,
    candle_since: Optional[datetime] = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Returns (candles, state_seq, sel_features). Each is a list of plain dicts.
    candle_since: if provided, fetch candles starting here (earlier than `since`)
                  so the in-memory engine has enough warmup bars.
    state_seq and sel_features always start from `since`.
    """
    effective_candle_since = candle_since if candle_since is not None else since
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=3, timeout=10)
    try:
        async with pool.acquire() as conn:
            candle_rows = await conn.fetch(_CANDLES_SQL, symbol, effective_candle_since)
            state_rows = await conn.fetch(_STATE_SEQ_SQL, symbol, since)
            # sel_features may not exist yet — tolerate
            try:
                feat_rows = await conn.fetch(_SEL_FEATURES_SQL, symbol, since)
            except asyncpg.exceptions.UndefinedTableError:
                feat_rows = []
    finally:
        await pool.close()

    return (
        [dict(r) for r in candle_rows],
        [dict(r) for r in state_rows],
        [dict(r) for r in feat_rows],
    )


# ─────────────────────────────────────────────────────────────────────────────
# In-memory engine fallback
# ─────────────────────────────────────────────────────────────────────────────


async def _run_engine_in_memory(candles: list[dict], symbol: str) -> list[dict]:
    """
    Run StateEngine + FeatureCalculator directly (no DB/Redis) on candle data.
    Bypasses StateOutputService to avoid pg_pool/redis dependency.
    """
    from sel_engine.features.calculator import FeatureCalculator
    from sel_engine.states.engine import StateEngine
    from sel_engine.states.schema import StateLabel

    engine = StateEngine(symbol)
    calculator = FeatureCalculator()

    close_history: list[float] = []
    sigma_history: list[float] = []
    H_history: list[float] = []
    OI_history: list[float] = []

    results = []
    for row in candles:
        close = float(row["close"])
        close_history.append(close)
        if len(close_history) > 800:
            close_history = close_history[-800:]

        fv = await calculator.compute(
            bar_time=row["time"],
            symbol=symbol,
            closes=close_history,
            sigma_p_history=sigma_history,
            H_history=H_history,
            OI_history=OI_history,
            OI_current=None,
            total_depth_24h_mean=None,
            spread_bps_24h_mean=None,
            bids=None,
            asks=None,
            H_samples=None,
            redis=None,
        )

        if fv.sigma_p_24h is not None:
            sigma_history.append(fv.sigma_p_24h)
            if len(sigma_history) > 100:
                sigma_history = sigma_history[-100:]

        record = engine.process(fv)

        direction = None
        if record.state == StateLabel.SURGING_UP:
            direction = "Up"
        elif record.state == StateLabel.SURGING_DOWN:
            direction = "Down"

        results.append(
            {
                "time": record.time,
                "state": record.state.value if record.state else None,
                "direction": direction,
                "is_cold_start": record.cold_start,
                "reason": record.reason,
                "transition_from": record.transition_from.value if record.transition_from else None,
                "close_price": fv.close if fv.close else close,
                "delta_p_pct": fv.delta_p_pct,
                "sigma_p_24h": fv.sigma_p_24h,
                "H": fv.H,
                "TF": fv.TF,
                "OI": fv.OI,
                "funding_rate": fv.funding_rate,
                "LV": fv.LV,
                "absorption_ratio": fv.absorption_ratio,
                "price_autocorr_12h": fv.price_autocorr_12h,
                "price_autocorr_24h": fv.price_autocorr_24h,
                "price_autocorr_48h": fv.price_autocorr_48h,
                "sigma_p_d2": fv.sigma_p_d2,
                "OI_hurst": fv.OI_hurst,
                "feature_completeness": 0.0,
            }
        )
    return results


# ─────────────────────────────────────────────────────────────────────────────
# State run detection
# ─────────────────────────────────────────────────────────────────────────────


def _detect_runs(state_rows: list[dict], candle_map: dict) -> list[dict]:
    """
    Run-length encode the state sequence.
    Returns list of run dicts: {state, start, end, bars: [merged_bar_dicts]}.
    Cold-start (state=None) rows are retained as state='COLD_START'.
    """
    runs: list[dict] = []
    current_state: Optional[str] = "__SENTINEL__"
    current_bars: list[dict] = []

    def _flush():
        if current_bars:
            runs.append(
                {
                    "state": current_state,
                    "start": current_bars[0]["time"],
                    "end": current_bars[-1]["time"],
                    "bars": list(current_bars),
                }
            )

    for sr in state_rows:
        raw_state = sr.get("state") or ("COLD_START" if sr.get("is_cold_start") else "UNKNOWN")
        t = sr["time"]
        # Merge candle OHLCV into the bar dict
        merged = dict(sr)
        if t in candle_map:
            c = candle_map[t]
            merged.update(
                {
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "volume": float(c["volume"]),
                }
            )
            if merged.get("close_price") is None:
                merged["close_price"] = float(c["close"])

        if raw_state != current_state:
            _flush()
            current_state = raw_state
            current_bars = [merged]
        else:
            current_bars.append(merged)

    _flush()
    return runs


# ─────────────────────────────────────────────────────────────────────────────
# Sampling
# ─────────────────────────────────────────────────────────────────────────────


def _sample_runs(runs: list[dict], n: int = MAX_SAMPLES) -> list[dict]:
    """Take up to n evenly-spaced runs for temporal variety."""
    if len(runs) <= n:
        return runs
    indices = [int(i * (len(runs) - 1) / (n - 1)) for i in range(n)]
    # Deduplicate while preserving order
    seen = set()
    result = []
    for i in indices:
        if i not in seen:
            seen.add(i)
            result.append(runs[i])
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Feature snapshot table
# ─────────────────────────────────────────────────────────────────────────────

_FEATURE_DISPLAY = [
    ("close_price", "close (USDT)"),
    ("delta_p_pct", "ΔP/P (%)"),
    ("sigma_p_24h", "σ(P)_24H"),
    ("funding_rate", "funding_rate"),
    ("H", "H (entropy)"),
    ("TF", "TF (taker flow)"),
    ("OI", "OI"),
    ("LV", "LV"),
    ("absorption_ratio", "absorption_ratio"),
    ("price_autocorr_12h", "autocorr_12h"),
    ("price_autocorr_24h", "autocorr_24h"),
    ("price_autocorr_48h", "autocorr_48h"),
    ("sigma_p_d2", "σ(P)_d2"),
    ("OI_hurst", "OI_hurst"),
]


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        if abs(v) >= 10_000:
            return f"{v:,.0f}"
        if abs(v) >= 1:
            return f"{v:.4f}"
        return f"{v:.6f}"
    return str(v)


def _feature_table(bars: list[dict], feat_map: dict) -> str:
    """Markdown table: feature × (start bar, mid bar, end bar, mean, min, max)."""
    if not bars:
        return "(no bars)"

    # Merge sel_features data (more complete) into bars via time key
    merged_bars = []
    for b in bars:
        t = b["time"]
        merged = dict(b)
        if t in feat_map:
            # sel_features overrides state_seq where not None
            for k, v in feat_map[t].items():
                if v is not None or k not in merged or merged[k] is None:
                    merged[k] = v
        merged_bars.append(merged)

    n = len(merged_bars)
    start_bar = merged_bars[0]
    mid_bar = merged_bars[n // 2]
    end_bar = merged_bars[-1]

    lines = ["| Feature | Start | Mid | End | Mean | Min | Max |", "|---|---|---|---|---|---|---|"]

    for col_key, label in _FEATURE_DISPLAY:
        vals = [b.get(col_key) for b in merged_bars]
        numeric = [float(v) for v in vals if v is not None]

        s_val = _fmt(start_bar.get(col_key))
        m_val = _fmt(mid_bar.get(col_key))
        e_val = _fmt(end_bar.get(col_key))

        if numeric:
            mean_v = _fmt(sum(numeric) / len(numeric))
            min_v = _fmt(min(numeric))
            max_v = _fmt(max(numeric))
        else:
            mean_v = min_v = max_v = "—"

        lines.append(f"| {label} | {s_val} | {m_val} | {e_val} | {mean_v} | {min_v} | {max_v} |")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────────────────────────────────────


def _write_figure(run_idx: int, state: str, bars: list[dict]) -> Optional[str]:
    """Save ASCII OHLC chart to figures/. Returns relative path or None."""
    times = [b["time"] for b in bars if "open" in b]
    opens = [float(b["open"]) for b in bars if "open" in b]
    highs = [float(b["high"]) for b in bars if "open" in b]
    lows = [float(b["low"]) for b in bars if "open" in b]
    closes = [float(b["close_price"]) for b in bars if b.get("close_price") is not None and "open" in b]

    if not closes:
        return None

    chart_txt = _ascii_chart(times, opens, highs, lows, closes)
    safe_state = state.replace(" ", "_")
    fname = f"{safe_state}_run{run_idx:02d}.txt"
    fpath = _FIGURES_DIR / fname
    fpath.write_text(chart_txt, encoding="utf-8")
    return str(fpath.relative_to(_HERE))


def _build_report(
    symbol: str,
    days: int,
    since: datetime,
    state_source: str,
    runs_by_state: dict[str, list[dict]],
    total_active_bars: int,
    feat_map: dict,
) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# sel State Inspection Report",
        "",
        f"**Symbol:** {symbol}  ",
        f"**Period:** last {days} days (since {since.strftime('%Y-%m-%d %H:%M UTC')})  ",
        f"**State source:** {state_source}  ",
        f"**Generated:** {now_str}  ",
        f"**Active bars (non-cold-start):** {total_active_bars}",
        "",
        "> Pure read-only diagnostic. No state logic modified.",
        "> Thresholds are PLACEHOLDER — calibrate with v1.0.md when available.",
        "",
        "---",
        "",
        "## Summary",
        "",
        "| State | Runs | Bars | Rate | Avg run (bars) | Avg run (hours) |",
        "|---|---|---|---|---|---|",
    ]

    for state in ALL_STATES:
        state_runs = runs_by_state.get(state, [])
        n_runs = len(state_runs)
        n_bars = sum(len(r["bars"]) for r in state_runs)
        rate = n_bars / total_active_bars if total_active_bars > 0 else 0.0
        avg_run = (n_bars / n_runs) if n_runs > 0 else 0.0
        lines.append(f"| {state} | {n_runs} | {n_bars} | {rate:.1%} | {avg_run:.1f} | {avg_run:.1f}h |")

    lines += ["", "---", ""]

    # Per-state sections
    for state in ALL_STATES:
        state_runs = runs_by_state.get(state, [])
        n_runs = len(state_runs)
        n_bars = sum(len(r["bars"]) for r in state_runs)
        rate = n_bars / total_active_bars if total_active_bars > 0 else 0.0
        avg_run = (n_bars / n_runs) if n_runs > 0 else 0.0

        lines += [
            f"## State: {state}",
            "",
            f"- **Runs:** {n_runs}",
            f"- **Total bars:** {n_bars}",
            f"- **Rate:** {rate:.2%} of active bars",
            f"- **Avg run duration:** {avg_run:.1f} bars ({avg_run:.1f}h)",
            "",
        ]

        if n_runs == 0:
            lines += [
                "_No instances of this state in the inspection window._",
                "",
                "---",
                "",
            ]
            continue

        samples = _sample_runs(state_runs)
        lines.append(f"Showing {len(samples)} of {n_runs} runs (evenly spaced for temporal coverage).")
        lines.append("")

        for i, run in enumerate(samples, 1):
            bars = run["bars"]
            n_b = len(bars)
            start_t = run["start"].strftime("%Y-%m-%d %H:%M UTC")
            end_t = run["end"].strftime("%Y-%m-%d %H:%M UTC")
            closes = [float(b["close_price"]) for b in bars if b.get("close_price") is not None]
            spark = _sparkline(closes)

            # First bar reason
            first_reason = bars[0].get("reason") or "—"
            trans_from = bars[0].get("transition_from") or "—"

            lines += [
                f"### Run {i} of {len(samples)}: {start_t} → {end_t} ({n_b} bars)",
                "",
                f"**Entered from:** {trans_from}  ",
                f"**Trigger reason (first bar):** `{first_reason}`  ",
                "",
                f"**Close sparkline** ({len(closes)} bars):  ",
                f"`{spark}`",
                f"(`{_fmt(min(closes)) if closes else '—'}` → `{_fmt(max(closes)) if closes else '—'}` USDT range)",
                "",
            ]

            # ASCII chart saved to file
            fig_path = _write_figure(i, state, bars)
            if fig_path:
                lines += [
                    f"**OHLC chart:** see [`{fig_path}`]({fig_path})",
                    "",
                ]

            # Feature table
            lines += [
                "**Feature snapshot** (start / mid / end / mean / min / max over run):  ",
                "",
                _feature_table(bars, feat_map),
                "",
            ]

        lines += ["---", ""]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Terminal summary
# ─────────────────────────────────────────────────────────────────────────────


def _print_summary(
    runs_by_state: dict[str, list[dict]],
    total_active_bars: int,
    total_cold_bars: int,
    state_source: str,
) -> None:
    col_w = [22, 6, 6, 7, 12, 14]
    header = (
        f"{'State':<{col_w[0]}} {'Runs':>{col_w[1]}} {'Bars':>{col_w[2]}} "
        f"{'Rate':>{col_w[3]}} {'AvgRun(h)':>{col_w[4]}} {'AvgRun(bars)':>{col_w[5]}}"
    )
    sep = "─" * sum(col_w + [len(col_w)])

    print()
    print("=== sel State Inspection — Terminal Summary ===")
    print(f"State source : {state_source}")
    print(f"Active bars  : {total_active_bars}  |  Cold-start bars: {total_cold_bars}")
    print(sep)
    print(header)
    print(sep)

    for state in ALL_STATES:
        state_runs = runs_by_state.get(state, [])
        n_runs = len(state_runs)
        n_bars = sum(len(r["bars"]) for r in state_runs)
        rate = n_bars / total_active_bars if total_active_bars > 0 else 0.0
        avg_run = (n_bars / n_runs) if n_runs > 0 else 0.0

        warn = " ⚠" if n_bars == 0 else ""
        print(
            f"{state + warn:<{col_w[0]}} {n_runs:>{col_w[1]}} {n_bars:>{col_w[2]}} "
            f"{rate:>{col_w[3]}.1%} {avg_run:>{col_w[4]}.1f} {avg_run:>{col_w[5]}.1f}"
        )

    print(sep)
    print()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


async def main(symbol: str, days: int) -> None:
    dsn = os.environ.get(
        "TIMESCALE_URL",
        "postgresql://cw4:changeme@timescaledb:5432/cw4",
    ).replace("postgresql+asyncpg://", "postgresql://")

    now = datetime.now(timezone.utc)
    display_since = now - timedelta(days=days)
    # Fetch 31 extra days of candles so in-memory engine can exit 720-bar cold start
    warmup_since = display_since - timedelta(days=31)
    since = display_since  # alias used in report header
    state_source = "TimescaleDB (sel_state_sequence)"

    # ── 1. Fetch data ────────────────────────────────────────────────────────
    candles: list[dict] = []
    state_rows: list[dict] = []
    feat_rows: list[dict] = []

    try:
        print(f"Connecting to DB: {dsn.split('@')[-1]} …")
        candles, state_rows, feat_rows = await _fetch_all(dsn, symbol, since=display_since, candle_since=warmup_since)
        print(
            f"Fetched: {len(candles)} candles (from {warmup_since.strftime('%Y-%m-%d')}), "
            f"{len(state_rows)} state rows, {len(feat_rows)} feature rows"
        )
    except Exception as exc:
        print(f"[WARN] DB unavailable: {exc}")
        # 720 warmup bars + full display window guarantees cold-start exit
        total_synth = 720 + days * 24
        print(f"[WARN] Proceeding with synthetic data ({total_synth} bars: 720 warmup + {days * 24} display)")
        import random

        random.seed(42)
        price = 50_000.0
        now_h = now.replace(minute=0, second=0, microsecond=0)
        for i in range(total_synth):
            t = now_h - timedelta(hours=(total_synth - i))
            delta = random.gauss(0, 0.002) * price
            price = max(40_000, price + delta)
            candles.append(
                {
                    "time": t,
                    "open": price - abs(random.gauss(0, 50)),
                    "high": price + abs(random.gauss(0, 100)),
                    "low": price - abs(random.gauss(0, 100)),
                    "close": price,
                    "volume": random.uniform(100, 5000),
                }
            )
        state_source = "synthetic (DB unavailable)"

    # ── 2. If sel_state_sequence empty → run engine in-memory ───────────────
    if not state_rows and candles:
        n_warmup = sum(1 for c in candles if c["time"] < display_since)
        n_display = sum(1 for c in candles if c["time"] >= display_since)
        print(
            f"sel_state_sequence is empty — running StateEngine in-memory on "
            f"{len(candles)} candles ({n_warmup} warmup + {n_display} display) …"
        )
        state_source = "in-memory StateEngine (sel_state_sequence was empty)"
        try:
            all_engine_rows = await _run_engine_in_memory(candles, symbol)
            # Keep only bars in the display window to avoid cold-start noise
            state_rows = [r for r in all_engine_rows if r["time"] >= display_since]
            cold_in_display = sum(1 for r in state_rows if r.get("is_cold_start"))
            print(
                f"Engine produced {len(all_engine_rows)} total rows; "
                f"{len(state_rows)} in display window "
                f"({cold_in_display} still cold-start after warmup)"
            )
        except Exception as exc:
            print(f"[WARN] In-memory engine failed: {exc}")
            state_rows = []

    # ── 3. Build index structures ────────────────────────────────────────────
    candle_map = {r["time"]: r for r in candles}
    feat_map = {r["time"]: r for r in feat_rows}

    # ── 4. Detect state runs ─────────────────────────────────────────────────
    all_runs = _detect_runs(state_rows, candle_map)

    # ── 5. Partition into counts ─────────────────────────────────────────────
    cold_bars = sum(len(r["bars"]) for r in all_runs if r["state"] == "COLD_START")
    active_bars = sum(len(r["bars"]) for r in all_runs if r["state"] not in ("COLD_START", "UNKNOWN", None))

    runs_by_state: dict[str, list[dict]] = {s: [] for s in ALL_STATES}
    for run in all_runs:
        if run["state"] in runs_by_state:
            runs_by_state[run["state"]].append(run)

    # ── 6. Terminal summary ──────────────────────────────────────────────────
    _print_summary(runs_by_state, active_bars, cold_bars, state_source)

    # ── 7. Markdown report ───────────────────────────────────────────────────
    report_md = _build_report(
        symbol=symbol,
        days=days,
        since=since,
        state_source=state_source,
        runs_by_state=runs_by_state,
        total_active_bars=active_bars if active_bars > 0 else max(1, len(candles)),
        feat_map=feat_map,
    )
    _REPORT_PATH.write_text(report_md, encoding="utf-8")
    print(f"Report written → {_REPORT_PATH}")
    print(f"ASCII charts  → {_FIGURES_DIR}/")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="sel state inspection diagnostic")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--days", default=12, type=int)
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(main(symbol=args.symbol, days=args.days))
