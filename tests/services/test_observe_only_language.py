"""Observe-only iron-law tests (audit P0-6).

Helios discipline: the system observes & explains, it does not advise. The mode-switch
surface used to say "建议切换到 AUTO_EXEC（全自动执行）" — both an iron-law violation and a
nudge toward disabling the live-safety gate. It must now report threshold *status* only,
never a switch recommendation.
"""
from services.monitoring.report import generate_recommendation

# Imperative advice phrasings that must never appear in the mode surface.
_FORBIDDEN = ["建议切换", "推荐切换", "建议切换到", "should switch", "recommend switching"]


def _inputs(mean_ic=0.06, mean_ir=1.5, avg_prob=0.62, per_day=3, stable=True,
            total_n=40, fill_rate=0.9, dd=0.0, circuit=False):
    return dict(
        ic_analysis={"mean_ic": mean_ic, "mean_ir": mean_ir, "total_n": total_n},
        signal_analysis={"per_day": per_day, "avg_win_prob": avg_prob},
        regime_analysis={"is_stable": stable, "dominant_pct": 0.7},
        risk_status={"current_dd_pct": dd * 100, "circuit_broken": circuit},
        order_analysis={"fill_rate": fill_rate},
    )


def test_thresholds_met_reports_status_not_advice():
    rec = generate_recommendation(current_mode="NOTIFY_ONLY", **_inputs())
    # thresholds for CONFIRM are met...
    assert rec["next_mode"] == "CONFIRM_THEN_EXEC"
    # ...but framed as observed threshold status + an explicit no-advice disclaimer
    assert "门槛已满足" in rec["action"]
    assert "人工决策" in rec["action"] and "不作建议" in rec["action"]
    for bad in _FORBIDDEN:
        assert bad not in rec["action"]


def test_auto_exec_thresholds_never_phrased_as_advice():
    rec = generate_recommendation(
        current_mode="CONFIRM_THEN_EXEC",
        **_inputs(mean_ic=0.10, mean_ir=2.0, total_n=80, fill_rate=0.95))
    assert rec["next_mode"] == "AUTO_EXEC"
    for bad in _FORBIDDEN:
        assert bad not in rec["action"]
    assert "不作建议" in rec["action"]


def test_blocked_and_holding_paths_carry_no_advice():
    blocked = generate_recommendation(current_mode="NOTIFY_ONLY", **_inputs(circuit=True))
    holding = generate_recommendation(current_mode="NOTIFY_ONLY", **_inputs(mean_ic=0.0))
    for rec in (blocked, holding):
        for bad in _FORBIDDEN:
            assert bad not in rec["action"]
