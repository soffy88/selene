# Selene Paper Startup Pre-flight Report — 2026-04-29

生成時刻: 2026-04-29 12:04 UTC

## 前提条件チェック

| チェック項目 | 結果 | 詳細 |
|---|---|---|
| v2_bars_4h | ❌ NG | count=0, required≥180, dry-run — no DB connection |
| v2_strategy_params | ❌ NG | dry-run |
| v2_lob_snapshots | ❌ NG | count=0, dry-run |
| helixa.derivatives_snapshots | ❌ NG | helixa GRANT not yet applied — expected |

## 起動可否判断

❌ 未充足条件あり — 起動を保留してください。

## Phase History 初期化 SQL（実行前に Wiki + Claude 双重レビュー必須）

```sql
-- K1 Phase 0 initial records (v2.1 §7.1)
-- Execute once when paper trading starts.
INSERT INTO v2_strategy_phase_history (
    timestamp, strategy, from_phase, to_phase,
    rolling_W, rolling_R, sample_size,
    kelly_fraction_estimated, kelly_cap_lower, kelly_cap_upper,
    decision_id, created_at
) VALUES
    (NOW(), 'strategy_1', NULL, 'phase_0', NULL, NULL, 0, NULL, 0.20, 0.20, NULL, NOW()),
    (NOW(), 'strategy_2', NULL, 'phase_0', NULL, NULL, 0, NULL, 0.10, 0.10, NULL, NOW());
```

## Decision Trail 起動イベント SQL

```sql
-- Paper startup decision_trail event (v2.0 §27)
-- Execute once after phase_history is inserted.
INSERT INTO v2_decision_trail (
    timestamp, decision_type, trigger_source, target_component,
    wiki_decision, decision_basis, created_at, created_by
) VALUES (
    NOW(),
    'paper_startup',
    'manual',
    'paper_engine',
    'paper trading 正式起動',
    'Wave 5 実装完了 + Helixa 実盘稳定 ≥ 3 月（確認待ち） + LOB collector 30 日充足',
    NOW(),
    'wiki'
);
```

## 注記

- 上記 SQL は自動実行されません。Wiki が手動で実行してください。
- 実行後 `v2_strategy_phase_history` と `v2_decision_trail` のレコードを確認すること。
- Paper trading 起動後、Wave 6（K1 Phase 2 切換）は Month 3 評估まで禁止。
