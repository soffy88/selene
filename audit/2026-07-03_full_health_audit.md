# Selene/Helios — 全面体检 + 修复记录 (2026-07-03)

驱动方式：对运行中的 docker-compose 栈（18 容器）、`platform-postgres/selene`、`helios-redis`
做实测 + 三路并行代码深审（sel_v2 / services / backtest+tests）。本文件是持久记录；
按优先级列出**发现**与**已执行的修复**（含实证），以及需人决策的条目。

---

## 头号发现：`healthy` 是假象，数据层全空

docker 的 healthy 只探 HTTP 活性，不探数据。实测：

- **SEL v2 流水线全断**：23 张表全部 0 行。根因：tick/lob/deriv/liq 采集器 13h 报
  `Proxy connection timed out`——**OKX 在本环境被全局封锁**（每个代理/主机 403/SSL-EOF/reset，
  平台甚至 `*_OKX_FLAG=0`）；**Binance 可达，仅经 `helios-proxy:2080`**。
- **历史数据消失**：`DATA_FOUNDATION_ROADMAP` 记录 `v2_bars_4h` 曾有 5097 行，现为 0——
  当前指向的库是被重建的空 schema。
- **v4 流水线第一跳断**：`signal.raw`=56（onchain 在产）但 `signal.scored`=0，下游全 0。
  v4 依赖的 `candles`/`audit_log` 表在 `selene` 库不存在 → HMM/权重学习每小时报错。
- **唯一活着的数据源**：`onchain-btc-worker`（出块 + 鲸鱼流）。
- **healthcheck 名不副实**：freshness 规则全部 `if max_ts and ...`，空表 `max()`=NULL → 永不触发。
  这正是 13h 全断供无人察觉的原因。

三个结构性断裂（比数据空更根本）：v4 无 `market.candles` 生产者；onchain→signal 桥是死代码；
sel_v2 与 v4 未接通（LiveBridge 默认 OFF 且未接线）。

---

## 已执行并实证的修复

| 项 | 修复 | 实证 |
|---|---|---|
| **P0-a** | 确认 OKX 全局不可达、Binance 经 `helios-proxy:2080` 可达；compose 16 处 proxy 默认值 `172.23.224.1:30810`(死) → `helios-proxy:2080`(活) | 容器内 python 实测 Binance 200 / OKX 403·SSL-EOF |
| **P0-c** | 新增 `sel_v2/data/binance_backfill.py`，从 Binance 回填 `v2_bars_4h`（source='binance', vwap=NULL, ON CONFLICT DO NOTHING） | **4380 bars, 2024-07-03→2026-07-02, 落库 platform-postgres（持久）** |
| **P0-d** | healthcheck 增 `_freshness_alert`：STALE **或** EMPTY（0 行，过冷启动 grace）都告警；空表不再隐形 | 部署版对空 tick/lob/deriv 判 CRITICAL，对已回填 bars 判健康 ✅ |
| **P1-a** | 修 `onchain/bridge.py` 两个 import bug（错误 redis 模块 + 不存在的 `ensure_consumer_group`）；在 signal lifespan 接线为后台任务 | signal.raw 建组 `signal-service`，58 entries 全 ack，pending=0，桥实时消费 ✅ |
| **P1-b** | `/metrics` 误用 `redis_client.health_check`(未初始化全局) → 改 `connections.redis_health`，6 服务 | 6 服务 clean 窗口 0 error；`selene_redis_up{service="signal"} 1` ✅ |
| **P1-d** | composite 增 `EFFECTIVE_WEIGHTS`：social/orderbook(15% 死权重)置零并把其余重归一化到 1.0，raw 不再被压到 0.85/拉向中性 | 归一化数学校验 0.85→1.0，funding 0.20→0.2353 ✅ |
| **P1-e** | `shared/db/connections.py` 加超时（asyncpg command_timeout=30/timeout=10/inactive=300；redis socket_timeout/health_check_interval/retry）——治 EC-13 静默挂起 + DNS 抖动 | 6 服务带新连接层重启，全 healthy ✅ |
| **P2-c** | `backtest/costs.py` 增 per-symbol 成本档（BTC/ETH/SOL 流动性先验）+ `cost_params_for()`，engine 按 symbol 取值 | resolver + 归一 py 验证；engine 编译通过 ✅ |

**部署方式说明**：v4 服务源码是打进镜像的（无挂载），本次用 `docker cp` + `restart` 热补丁到运行容器。
主机源码已改（工作区未提交）。**若容器被 recreate/rebuild，热补丁会丢失——需 `docker compose build` 把改动烤进镜像才持久。** `v2_bars_4h` 数据在 postgres 中持久，不受此限。

---

## OKX→Binance 采集器迁移（2026-07-03，已执行 + 实证）

实证结论修正了原计划：**Binance WS 也不可用**——`fstream.binance.com` 经 `helios-proxy:2080`
的 CONNECT 隧道能建立，但长连 TLS/WS 被重置（5/5 握手失败 Timeout/ConnReset）。而 Binance **REST**
（`fapi.binance.com`）可达但**代理抖动**（间歇 SSL-EOF/reset）。所以迁移改为 **REST 轮询**——
对抖动代理反而更稳（每次轮询独立重试，无长连可掉）。

新增 `sel_v2/data/binance_rest.py`（含 `fetch_json` 带重试 + 纯 row builder + 专用 `BINANCE_PROXY`
默认 `helios-proxy:2080`，绕开 .env 里失效的 `HTTPS_PROXY`，使 `docker restart` 即可带可用出网）。

| 表 | 迁移 | 实证 | 解锁 |
|---|---|---|---|
| `v2_derivatives_snapshots` | OKX REST → Binance `premiumIndex`+`openInterest` REST 轮询(30s) | **写入中,~30s 一行**(funding/OI/mark/index) | **Coiling / Drifting-Charged**(状态机 #1 缺口) |
| `v2_lob_snapshots` | OKX `books5` WS → Binance `depth` REST 轮询(60s) | **写入中,~60s 一行**(bid/ask depth + entropy) | **Cascade cond-1 + Critical 熵** |
| `v2_bars_4h` | (已回填)+ 新增 `--loop` 前向轮询(600s) 保持新鲜 | **4381 行,最新 00:00,poller PID 存活** | replay / 离线校准 / 4H σ·breakout |
| `v2_ticks` | **未迁移** | REST 轮询逐笔有损(丢单+限频),价值低(仅喂 OFI) | — |
| `v2_liquidations` | **不可迁移** | Binance 清算仅 WS `@forceOrder`,无 REST 端点;WS 不可达 | Cascade cond-2 仍空 |

**持久化已完成(2026-07-03)**:`docker compose build` 已把全部改动烤进镜像并 recreate:
- 4 个 sel_v2 采集服务(lob/derivatives/bar-aggregator)重建;
- 新增 **`v2-bar-poller`** compose 服务(`command: python -m sel_v2.data.binance_backfill --loop 600 --years 0.05`,
  `restart: unless-stopped`)取代临时 nohup,前向保持 bars 新鲜;
- healthcheck 重建(P0-d 空表检测烤入);
- 6 个 v4 服务重建(P1-a 桥 / P1-b metrics / P1-d 因子 / P1-e 超时 全部烤入,recreate 后 0 redis 错误)。
所有改动现**跨容器 recreate 持久**。采集器用专用 `BINANCE_PROXY`(compose 默认 helios-proxy:2080),
不依赖 .env 里失效的 HTTPS_PROXY。实测:19 个 selene 容器全 up,derivatives/lob/bars 持续写入。

## 需人决策（附证据 + 确切修复路径）

- **P0-a 残余**：实时 tick（逐笔）+ 清算需要 WS,而 Binance WS 经现有代理不可达。若要这两类数据,
  需平台在代理上放行 `fstream.binance.com:443` 长连,或提供可达 WS 的出网。OI/funding/LOB/bars 已用 REST 救活。
- **P0-c v4 `candles`/`audit_log` 表缺失**：即上面「结构性断裂#1」。建空表只能消错误噪音，不解决无 feed。
  正解是决定 v4 signal 链存废：接 v2 bars→v4 `market.candles`，或全押 sel_v2+LiveBridge。
- **P2-a 真 CSCV-PBO**：现 `backtest/cpcv.py` 的 PBO 是 Sharpe 符号代理，**高估严谨度**。vendored
  `oskill.validation.probability_of_backtest_overfitting` 已存在但未用。**本地无 oskill wheel，改了无法验证** → 留 CI 环境执行。
- **P2-b 解封真 CPCV 路径**：`tests/backtest/test_cpcv_wiring.py:147` 硬 `skipif(True)`；生产调
  `oskill.cpcv_pipeline`（顶层未导出该符号）→ 静默退化 `cpcv=None`。需私有 wheel 验证，同 P2-a。
- **P2-d OOS 证据诚实化**：`I_HAVE_OOS_EVIDENCE=yes` 仅 env 荣誉检查，可绕过真实回测。应绑定到一条
  committed 的 OOS 结果 artifact。属方法论/产品决策。
- **P1-c eth/sol onchain worker 未部署**：单币 BTC 聚焦下低价值；部署或移除其因子权重。
- **P3 命名诚实**：TDA2(实为总持续度)/W2(实为能量比)/H3(占位符) 改名或补实；HMM 升级在线滤波 + 引入期权 IV 特征。

---

## 铁律保持
`EXEC_MODE=NOTIFY_ONLY` + `ENVIRONMENT=development` + 双 ack 启动闸 全程未动。无资金风险。
本次未弱化任何风控闸门（🔒 Never 遵守）。
