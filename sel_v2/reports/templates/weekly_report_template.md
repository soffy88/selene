# Selene 週報 YYYY-WW

## 週期情報

- 時間範囲: YYYY-MM-DD 00:00 UTC ~ YYYY-MM-DD 00:00 UTC
- 報告生成時間: YYYY-MM-DD HH:MM UTC

---

## 状態機統計

### 4H 状態分布（本週）
| 状態 | 時長 (h) | 占比 (%) | 切換次数 |
|---|---|---|---|
| Coiling | x | x | x |
| Surging | x | x | x |
| Drifting-Calm | x | x | x |
| Drifting-Charged | x | x | x |
| Critical | x | x | x |
| Cascade | x | x | x |

### 状態転換事件
- 転換 1: Coiling → Surging, YYYY-MM-DD HH:MM, 触発条件 [...]
- 転換 2: ...

---

## CUSUM 触発統計

### CUSUM-Mid（策略 1）
- 触発次数: x
- 平均 peak C 値: x
- 閾値 h_t 範囲: [x, y]

### CUSUM-Short（策略 2）
- 触発次数: x
- 類型 A（反転）入場比例: x%
- 類型 B（突破）入場比例: x%

---

## 反推詞彙出現統計
| 詞彙 | 出現次数 | 入場決策中の役割 |
|---|---|---|
| Sweep | x | 策略 2 類型 A 増強 / 策略 1 増強 |
| Absorption | x | ... |
| Imprinting | x | observation-only |
| Saturation | x | ... |
| Exhaustion | x | ... |
| Crowding | x | ... |
| Release | x | ... |

---

## 策略 1 表現

### 入場/出場
- 入場次数: x
- 出場次数: x
- 平均持倉: x 時間
- Time Stop 触発次数: x

### PnL
- 総 PnL（USDT）: x
- 勝率: x%
- 平均盈利/亏損: x / x
- 最大単笔回撤: x

### 風控触発
- Critical 減倉: x 次
- Cascade 清倉: x 次
- 浮亏 -3% 止損: x 次

---

## 策略 2 表現

### 入場/出場
- 入場次数: x
- 出場次数: x
- 類型 A 入場比例: x%
- 類型 B 入場比例: x%
- Time Stop 24h 触発次数: x

### PnL
- 総 PnL（USDT）: x
- 勝率: x%
- 類型 A 勝率: x%
- 類型 B 勝率: x%
- 平均盈利/亏損: x / x

### 風控触発
- CUSUM 創新高 SL: x 次
- 浮亏 -2% 止損: x 次
- Cascade 清倉: x 次

---

## 協調層事件
- 反向対冲発生次数: x
- 同向独立運行次数: x

---

## 異常事件日誌
- データ欠失/遅延: [...]
- API エラー: [...]
- 決策異常: [...]

---

## 週末口座状態
- サブ口座 1 資金: x USDT
- サブ口座 2 資金: x USDT
- 総資金 vs 先週: +x.x%

---

## Observation-only ツール触発統計（v2.1）

| ツール ID | ツール名 | 週間触発数 | 備考 |
|---|---|---|---|
| B1 | Bayesian HMM 軟検証 | x | x |
| B2 | HMM 境界仲裁 | x | x |
| TDA2 | TDA + clustering | x | x |
| I1 | Permutation Entropy | x | x |
| T2 | TE 滚動監控 | x | x |
| W2 | Wavelet 多分形 | x | x |
| H3 | Hawkes Cascade 早期預警 | x | x |

---

## Wiki 視角（Wiki 手動追加）

[Wiki 在此 append — 異常情況 / 直覚判断 / 関心問題]

---

## Claude 反馈（次回対話生成, Wiki 貼り付け可）

[Claude 反馈]

---

## 調参決策記録

[本週是否調参, 調了什么 — 必须与 decision_trail 同步]
