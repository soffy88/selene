# Feature Implementation TODO

Features that are currently None/stub because live collector infrastructure is missing.
Each entry lists the blocking dependency and the affected state conditions.

## WIKI_REQUIRED — orderbook_collector

| Feature | Where computed | Blocked by | State conditions |
|---------|---------------|------------|-----------------|
| `H` | `liquidity.compute_H_from_samples()` | orderbook_collector not running | Coiling Cond2 (skip if None), Critical Cond3 (H_change_rate_std_12h) |
| `delta_H` | `calculator.py` (abs diff of H vs H_history[-1]) | H absent | Cascade Cond4 secondary |
| `H_change_rate_std_12h` | `liquidity.compute_H_change_rate_std()` | H_history absent | Critical Cond3 |
| `H_24h_mean` | `derived.compute_H_24h_mean()` | H_history absent | Drifting-Calm Cond2, Drifting-Charged Cond2 |
| `total_depth` / `spread_bps` | `orderbook.compute_depth_features()` | orderbook_collector not running | LV composite |
| `LV` | `derived.compute_LV()` | total_depth + spread_bps | Cascade Cond3 secondary |

## WIKI_REQUIRED — trade_flow_collector

| Feature | Where computed | Blocked by | State conditions |
|---------|---------------|------------|-----------------|
| `TF` | `flow.get_tf_from_redis()` | trade_flow_collector + Redis | (raw TF; input to absorption_ratio) |
| `TF_history` | maintained by caller | TF collector + caller enabling history | all TF-derived P1 features below |
| `tf_dp_ratio_24h` | `derived.compute_tf_dp_ratio_24h()` | TF_history | Coiling Cond4 |
| `tf_directional_ratio_6h` | `derived.compute_tf_directional_ratio_6h()` | TF_history | Surging Cond2 + direction |
| `abs_tf_24h_sum` | `derived.compute_abs_tf_24h_sum()` | TF_history | Drifting-Calm Cond3, Drifting-Charged Cond3 |
| `absorption_ratio` | `flow.compute_absorption_ratio()` | TF | (informational; not yet in any condition) |

## WIKI_REQUIRED — OI collector

| Feature | Where computed | Blocked by | State conditions |
|---------|---------------|------------|-----------------|
| `OI` / `OI_history` | passed in from caller | OI collector not polling | all OI-derived features below |
| `oi_change_rate_24h` | `derived.compute_oi_change_rate_24h()` | OI_history ≥ 25 bars | Coiling Cond3, Drifting-Calm Cond4 |
| `OI_hurst` | `derived.compute_hurst_rs()` | OI_history ≥ 20 bars | Critical Cond4, Drifting-Charged Cond4 |

## Consequence of absent data

Until collectors are live and warmed up (requires ~25–48 bars of history minimum),
all state conditions that short-circuit on None data will fail, and post-cold-start
bars will return `state=None, reason="NO_STATE_MATCHED"` per doc §10.1 principle 3.
Paper trading engine handles `state=None` as `NO_ACTION`.

Only Cascade and Critical can fire without WIKI_REQUIRED data (they rely on price
features + OI_hurst and LV, both of which require respective collectors).
