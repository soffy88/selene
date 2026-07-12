# ICT-8 SMT Divergence 实证 v1(BTC/ETH 4H,预注册)

- 生成:`python -m sel_v2.offline.smt_study`(整文件覆写)
- 数据:v2_bars_4h BTC⋈ETH 对齐 4380 bars,2024-07-12 → 2026-07-12(ETH 深史 2026-07-12 经 binance_backfill 回填)
- BTC/ETH 4H 收益相关性:**0.818**(高相关是 SMT 语义的前提——背离必须是异常)
- 预注册:双资产 1.5×ATR zigzag;枢轴配对窗 ±6 bar;事件=双方确认后;检验 bear→BTC 前向<基准 / bull→>;pass=双方向 p<0.10,单=partial

## 事件:34(bear 20 / bull 14)

| 方向 | n | 事件前向均值(BTC 6-bar) | 基准均值 | MW p | 备注 |
|---|---:|---:|---:|---:|---|
| bearish SMT(背离高点) | 20 | +0.46% | +0.01% | 0.7284 | 期望< |
| bullish SMT(背离低点) | 14 | +0.59% | +0.01% | 0.1239 | 期望> |

## verdict:**fail**
