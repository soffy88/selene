"""
WeeklyReportGenerator — Wave 5.

DEPRECATED (item #18): consolidate onto `sel_v2/reports/`. Neither report
scheduler is wired into compose. Kept for reference. See docs/CONSOLIDATION.md.

Generates honest markdown reports from one week of DecisionTrail records.
No beautifying: cold-start, failures, low N — all reported as-is.
"""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from paper_trading.trail import DecisionTrail
from sel_engine.states.health import EXPECTED_RATE_RANGES, HealthReport

# All states we know about, for completeness even at 0%
ALL_STATES = [
    "Coiling",
    "Surging_Up",
    "Surging_Down",
    "Drifting_Calm",
    "Drifting_Charged",
    "Critical",
    "Cascade",
]

ALL_ACTIONS = ["open_long", "open_short", "close", "hold", "no_action"]


class WeeklyReportGenerator:
    """Generate a weekly contrast markdown report from DecisionTrail records."""

    def generate(
        self,
        trails: list[DecisionTrail],
        health_report: HealthReport,
        week_iso: str,
        output_dir: str = "reports/weekly",
    ) -> str:
        """Generate markdown report. Returns file path written."""
        os.makedirs(output_dir, exist_ok=True)

        # Derive report metadata from trails (or defaults)
        config_hash = trails[0].config_hash if trails else "unknown"
        using_claude_default = trails[0].using_claude_default if trails else True
        generated_at = datetime.now(tz=timezone.utc).isoformat()

        sections: list[str] = []

        # ── Header ───────────────────────────────────────────────────────────
        sections.append(f"# Selene Weekly Report — {week_iso}\n")
        sections.append(
            f"> Decision rules version: `{config_hash}` "
            f"(Claude default: {using_claude_default})\n"
            f"> Generated: {generated_at}\n"
            f"> ⚠️ All figures are paper trading (simulated). Not real capital.\n"
        )

        if not trails:
            # Cold-start: emit "No data" for every section
            for i, title in enumerate(
                [
                    "State Distribution",
                    "State Transition Quality",
                    "Decision Distribution",
                    "Paper PnL",
                    "Risk Events",
                    "State-Decision Alignment",
                    "Price Outcomes After State (sel hypothesis test)",
                    "Failure Cases — Top 5 Losses",
                    "sel Hypothesis Health",
                ],
                start=1,
            ):
                sections.append(f"## {i}. {title}\n")
                sections.append("No data — trail list is empty.\n")
            content = "\n".join(sections)
            out_path = os.path.join(output_dir, f"{week_iso}.md")
            with open(out_path, "w") as fh:
                fh.write(content)
            return out_path

        # ── Section 1: State Distribution ────────────────────────────────────
        sections.append("## 1. State Distribution\n")
        total_active = health_report.active_bars or 1  # avoid div-by-zero
        table_rows = ["| State | Count | Rate | Expected Range | Status |",
                      "|---|---|---|---|---|"]
        for state in ALL_STATES:
            count = health_report.state_counts.get(state, 0)
            rate = health_report.state_rates.get(state, 0.0)
            lo, hi = EXPECTED_RATE_RANGES.get(state, (None, None))
            if lo is not None and hi is not None:
                expected_str = f"[{lo*100:.0f}%, {hi*100:.0f}%]"
                if rate < lo:
                    status = "⚠️ BELOW"
                elif rate > hi:
                    status = "⚠️ ABOVE"
                else:
                    status = "✅ OK"
            else:
                expected_str = "N/A"
                status = "—"
            table_rows.append(
                f"| {state} | {count} | {rate*100:.1f}% | {expected_str} | {status} |"
            )
        sections.append("\n".join(table_rows))
        warnings = health_report.health_warnings
        sections.append(f"\nHealth warnings: {'; '.join(warnings) if warnings else 'None'}\n")

        # ── Section 2: State Transition Quality ──────────────────────────────
        sections.append("## 2. State Transition Quality\n")
        ltr = health_report.legal_transition_rate
        sections.append(f"Legal transition rate: {ltr*100:.1f}%\n")
        ill = sorted(
            health_report.illegal_transition_types.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )[:5]
        if ill:
            sections.append("Illegal transition types (top 5):")
            sections.append("| Transition | Count |")
            sections.append("|---|---|")
            for trans, cnt in ill:
                sections.append(f"| {trans} | {cnt} |")
        else:
            sections.append("Illegal transition types (top 5): None\n")

        # ── Section 3: Decision Distribution ─────────────────────────────────
        sections.append("\n## 3. Decision Distribution\n")
        action_counts: Counter[str] = Counter()
        for t in trails:
            action_counts[t.final_action] += 1
        sections.append("| Action | Count |")
        sections.append("|---|---|")
        for action in ALL_ACTIONS:
            sections.append(f"| {action} | {action_counts.get(action, 0)} |")

        # ── Section 4: Paper PnL ─────────────────────────────────────────────
        sections.append("\n## 4. Paper PnL\n")
        starting_nav = trails[0].nav_usdt
        ending_nav = trails[-1].nav_usdt

        # Accumulate realized PnL from fills only
        week_pnl = sum(
            t.realized_pnl_this_bar
            for t in trails
            if t.realized_pnl_this_bar is not None
        )
        week_pnl_pct = (week_pnl / starting_nav * 100) if starting_nav else 0.0
        pnl_sign = "+" if week_pnl >= 0 else ""

        # Max drawdown: track nav across bars (use nav_usdt field)
        navs = [t.nav_usdt for t in trails]
        max_nav = navs[0]
        max_dd = 0.0
        for nav in navs:
            if nav > max_nav:
                max_nav = nav
            dd = (nav - max_nav) / max_nav if max_nav else 0.0
            if dd < max_dd:
                max_dd = dd

        sections.append(
            f"Week PnL: {pnl_sign}${week_pnl:.2f} ({pnl_sign}{week_pnl_pct:.2f}%)  \n"
            f"Cumulative PnL: {pnl_sign}${week_pnl:.2f}  \n"
            f"Max drawdown this week: {max_dd*100:.1f}%  \n"
            f"Starting NAV: ${starting_nav:,.2f}  \n"
            f"Ending NAV: ${ending_nav:,.2f}  \n"
        )

        # ── Section 5: Risk Events ────────────────────────────────────────────
        sections.append("## 5. Risk Events\n")
        risk_rule_counts: Counter[str] = Counter()
        for t in trails:
            if t.risk_triggered:
                risk_rule_counts[t.risk_triggered] += 1

        if risk_rule_counts:
            sections.append("| Rule | Count |")
            sections.append("|---|---|")
            for rule, cnt in sorted(risk_rule_counts.items(), key=lambda kv: -kv[1]):
                sections.append(f"| {rule} | {cnt} |")
        else:
            sections.append("| Rule | Count |\n|---|---|\n| (none) | 0 |")

        # Misfire rate: trades opened then force-closed by risk within 24 bars
        open_times: list[int] = []
        for i, t in enumerate(trails):
            if t.final_action in ("open_long", "open_short"):
                open_times.append(i)

        misfire_count = 0
        for open_idx in open_times:
            # Look within next 24 bars for a risk-triggered close
            window_end = min(open_idx + 25, len(trails))
            for j in range(open_idx + 1, window_end):
                if (
                    trails[j].final_action == "close"
                    and trails[j].risk_triggered is not None
                ):
                    misfire_count += 1
                    break

        total_opens = len(open_times)
        if total_opens > 0:
            misfire_rate_pct = misfire_count / total_opens * 100
            sections.append(
                f"\nMisfire rate (opened, force-closed by risk within 24H): "
                f"{misfire_rate_pct:.1f}% ({misfire_count}/{total_opens} opens)\n"
            )
        else:
            sections.append("\nMisfire rate: N/A (no opens this week)\n")

        # ── Section 6: State-Decision Alignment ──────────────────────────────
        sections.append("## 6. State-Decision Alignment\n")
        sections.append(
            "Verifies decisions matched the configured YAML rules:"
        )
        sections.append("| State | Expected Action | Actual Action | Match Rate |")
        sections.append("|---|---|---|---|")

        # Build (state, expected_action) pairs from trail rule_id matches
        # We use (current_state, proposed_action) since the rule was evaluated
        # before risk gate; group by state
        state_rule_pairs: dict[tuple[str, str], tuple[int, int]] = {}
        for t in trails:
            if t.current_state is None:
                continue
            expected = t.proposed_action  # from decision engine = rule-driven
            actual = t.final_action
            key = (t.current_state, expected)
            total, matched = state_rule_pairs.get(key, (0, 0))
            total += 1
            matched += 1 if actual == expected else 0
            state_rule_pairs[key] = (total, matched)

        if state_rule_pairs:
            for (state, expected), (total, matched) in sorted(
                state_rule_pairs.items()
            ):
                rate = matched / total * 100 if total else 0
                sections.append(
                    f"| {state} | {expected} | {expected} | {rate:.0f}% |"
                )
        else:
            sections.append("| (no state data) | — | — | — |")

        # ── Section 7: Price Outcomes After State ─────────────────────────────
        sections.append("\n## 7. Price Outcomes After State (sel hypothesis test)\n")
        sections.append("| State | N bars | Next 24H return (mean) | Next 24H return (median) | Positive rate |")
        sections.append("|---|---|---|---|---|")

        def _median(vals: list[float]) -> float:
            sv = sorted(vals)
            n = len(sv)
            if n == 0:
                return 0.0
            mid = n // 2
            return sv[mid] if n % 2 else (sv[mid - 1] + sv[mid]) / 2

        closes = [t.close for t in trails]
        states_seq = [t.current_state for t in trails]
        total_n_all = sum(
            1
            for i, t in enumerate(trails)
            if t.current_state is not None and t.close is not None
        )
        low_power = total_n_all < 30

        for state in ALL_STATES:
            returns: list[float] = []
            for i, t in enumerate(trails):
                if t.current_state != state or t.close is None:
                    continue
                # Find close 24 bars later
                target_idx = i + 24
                if target_idx < len(trails) and closes[target_idx] is not None:
                    ret = (closes[target_idx] - t.close) / t.close
                    returns.append(ret)
            n = len(returns)
            if n == 0:
                sections.append(f"| {state} | 0 | N/A | N/A | N/A |")
                continue
            mean_ret = sum(returns) / n
            med_ret = _median(returns)
            pos_rate = sum(1 for r in returns if r > 0) / n
            sign_m = "+" if mean_ret >= 0 else ""
            sign_med = "+" if med_ret >= 0 else ""
            sections.append(
                f"| {state} | {n} | {sign_m}{mean_ret*100:.1f}% "
                f"| {sign_med}{med_ret*100:.1f}% | {pos_rate*100:.0f}% |"
            )

        note_power = (
            "> Note: N < 30 for all states — statistical power is very low.\n"
            if low_power
            else "> Note: some states may have N < 30 — treat those rows with caution.\n"
        )
        sections.append(
            "\n" + note_power +
            "> These numbers should NOT be used for parameter calibration.\n"
            "> Reporting only for self-falsification tracking.\n"
        )

        # ── Section 8: Failure Cases — Top 5 Losses ──────────────────────────
        sections.append("## 8. Failure Cases — Top 5 Losses\n")

        # Collect close trails with negative realized PnL
        close_trails = [
            t
            for t in trails
            if t.final_action == "close"
            and t.realized_pnl_this_bar is not None
            and t.realized_pnl_this_bar < 0
        ]
        close_trails.sort(key=lambda t: t.realized_pnl_this_bar)  # most negative first

        sections.append("| # | Open Time | State | Action | Close Time | PnL | Reason Closed |")
        sections.append("|---|---|---|---|---|---|---|")

        if not close_trails:
            sections.append("| — | — | — | — | — | — | — |")
        else:
            top5 = close_trails[:5]
            for rank, t in enumerate(top5, start=1):
                pnl = t.realized_pnl_this_bar or 0.0
                pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                reason = t.risk_triggered or "normal"
                # open_time is estimated from previous_state trail — use current bar time
                open_time = t.time.strftime("%Y-%m-%d %H:%M") if t.time else "unknown"
                state = t.current_state or "None"
                action = t.final_action
                sections.append(
                    f"| {rank} | {open_time} | {state} | {action} | "
                    f"{open_time} | {pnl_str} | {reason} |"
                )

        # ── Section 9: sel Hypothesis Health ─────────────────────────────────
        sections.append("\n## 9. sel Hypothesis Health\n")

        hyp_lines: list[str] = []

        # Check each state against expected range
        for state in ALL_STATES:
            lo, hi = EXPECTED_RATE_RANGES.get(state, (None, None))
            if lo is None:
                continue
            rate = health_report.state_rates.get(state, 0.0)
            if rate < lo:
                hyp_lines.append(
                    f"⚠️ WATCH: {state} rate ({rate*100:.1f}%) below expected floor "
                    f"({lo*100:.1f}%). May indicate {state} conditions are too strict."
                )
            elif rate > hi:
                hyp_lines.append(
                    f"⚠️ WATCH: {state} rate ({rate*100:.1f}%) above expected ceiling "
                    f"({hi*100:.1f}%). May indicate {state} conditions are too lenient."
                )

        # Legal transition rate
        if ltr >= 0.80:
            hyp_lines.append(
                f"✅ PASS: Legal transition rate ({ltr*100:.1f}%) within acceptable range (>80%)."
            )
        else:
            hyp_lines.append(
                f"⚠️ WATCH: Legal transition rate ({ltr*100:.1f}%) below 80% — "
                f"state machine may have logic errors."
            )

        # Misfire rate
        if total_opens > 0:
            misfire_pct = misfire_count / total_opens * 100
            if misfire_pct >= 30:
                hyp_lines.append(
                    f"⚠️ WATCH: Misfire rate ({misfire_pct:.0f}%) elevated — "
                    f"sel assumptions may need recalibration."
                )
            else:
                hyp_lines.append(
                    f"✅ PASS: Misfire rate ({misfire_pct:.0f}%) within acceptable range."
                )
        else:
            hyp_lines.append("— Misfire rate: N/A (no opens this week).")

        if not hyp_lines:
            hyp_lines.append("No hypothesis checks configured.")

        sections.append("\n".join(hyp_lines))
        sections.append("")  # trailing newline

        # ── Assemble and write ────────────────────────────────────────────────
        content = "\n".join(sections) + "\n"
        out_path = os.path.join(output_dir, f"{week_iso}.md")
        with open(out_path, "w") as fh:
            fh.write(content)
        return out_path
