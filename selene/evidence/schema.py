"""OOS / shadow / data-manifest / release artifact schemas (P1-1, P1-2, P2-5)."""

from __future__ import annotations

OOS_SCHEMA_VERSION = "oos-artifact-v1"
SHADOW_SCHEMA_VERSION = "shadow-artifact-v1"
MANIFEST_SCHEMA_VERSION = "data-manifest-v1"
RELEASE_SCHEMA_VERSION = "release-manifest-v1"

OOS_REQUIRED = (
    "schema_version",
    "artifact_id",
    "generated_at",
    "expires_at",
    "strategy_commit",
    "image_digest",
    "strategy_config_digest",
    "risk_policy_digest",
    "feature_schema_version",
    "data_manifest_digest",
    "train_range",
    "embargo_range",
    "oos_range",
    "instruments",
    "venues",
    "cost_model",
    "search_trials",
    "metrics",
    "cpcv",
    "gate_verdict",
    "fail_reasons",
    "producer",
    "artifact_digest",
)

SHADOW_REQUIRED = (
    "schema_version",
    "artifact_id",
    "generated_at",
    "expires_at",
    "strategy_commit",
    "image_digest",
    "gate_verdict",
    "days_run",
    "regimes_seen",
    "unexplained_halts",
    "duplicate_intents",
    "unresolved_reconcile_diff",
    "stale_data_actions",
    "ledger_gaps",
    "artifact_digest",
)

MANIFEST_REQUIRED = (
    "schema_version",
    "manifest_id",
    "generated_at",
    "source",
    "license",
    "fetched_at",
    "object_digest",
    "symbols",
    "venues",
    "time_range",
    "bar_count",
    "tick_count",
    "missing_rate",
    "duplicate_rate",
    "out_of_order_rate",
    "provenance",
    "timezone",
    "feature_available_at",
    "manifest_digest",
)

RELEASE_REQUIRED = (
    "release_id",
    "git_sha",
    "image_digests",
    "config_digest",
    "db_schema_version",
    "risk_policy_digest",
    "oos_artifact_id",
    "shadow_artifact_id",
    "required_ci_run",
    "created_at",
)

OOS_GATES = {
    "oos_closed_trades_min": 100,
    "oos_closed_trades_per_strategy_min": 50,
    "oos_active_states_min": 3,
    "sharpe_after_cost_min": 1.0,
    "deflated_sharpe_prob_min": 0.90,
    "bootstrap_sharpe_p5_min": 0.0,
    "max_drawdown_max": 0.15,
    "cpcv_median_sharpe_min": 0.0,
    "cpcv_pbo_max": 0.20,
    "catastrophic_loss_max": 0,
    "lookahead_findings_max": 0,
}

SHADOW_GATES = {
    "days_run_min": 30,
    "regimes_min": 3,
    "unexplained_halts_max": 0,
    "duplicate_intents_max": 0,
    "unresolved_reconcile_diff_max": 0,
    "stale_data_actions_max": 0,
    "ledger_gaps_max": 0,
}
