# cw4 零输出问题诊断报告

**日期**：2026-04-20  
**症状**：cw4 多天零交易零推送  
**EXEC_MODE**：LIVE（.env 实际值，monitoring-service 2026-04-18 报告显示 NOTIFY_ONLY，疑似被改过）

---

## 根因总结（先看这里）

**问题卡在 Layer ①（data-service）+ Layer ⑤（execution-service）**

```
Docker容器 → HTTP_PROXY=http://172.23.224.1:10810 → 超时 (exit 28)
Docker容器 → 直连 Binance → 超时 (exit 28)
```

**172.23.224.1 是 WSL2 宿主机 IP，但 Docker bridge 网络（172.22.0.x）无法路由到该地址。**  
代理从未在容器内生效，Binance REST API 全部失败。同样原因导致 OKX WebSocket 也断开。

---

## 5 层 pipeline 诊断

### Layer ① data-service（市场数据）❌ 完全失败

**日志摘要**（持续至今）：
```
2026-04-20 06:09:13 [WARNING] binance_rest: GET error attempt 1: 
2026-04-20 06:09:30 [WARNING] binance_rest: GET error attempt 2: 
2026-04-20 06:09:48 [WARNING] binance_rest: GET error attempt 3: 
（错误信息为空 = aiohttp 连接超时，str(exception) = ""）
```

**代码证据（binance_rest.py:60）**：
```python
_PROXY = os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or None
# → 读到 http://172.23.224.1:10810
# → 容器内 curl --proxy http://172.23.224.1:10810 → exit 28 (TIMEOUT)
# → 容器内 curl 直连 Binance → exit 28 (TIMEOUT)
```

**candles 最新时间戳**：`2026-04-19 07:00:00 UTC`（已停止写入 **23+ 小时**）  
**OI/Funding**：请求 `testnet.binancefuture.com` → 持续返回 HTTP 202（testnet 无真实数据）  
**判断**：❌ Binance REST 100% 失败，candles 停更，OI/LSR 全为空

---

### Layer ② signal-service（信号生成）❌ 无信号产出

**日志摘要**（非 health/metrics 日志，过去 24 小时仅两条）：
```
2026-04-19 20:21:30 HMM BTCUSDT: state=crisis confidence=1.00
2026-04-19 20:21:30 HMM ETHUSDT: state=crisis confidence=1.00
2026-04-19 20:21:30 HMM SOLUSDT: state=range  confidence=1.00
2026-04-20 02:30:21 HMM BTCUSDT: state=crisis confidence=1.00（同上，未变化）
```

**阻断链**（任意一环返回 → signal 被丢弃）：

| 门槛 | 代码位置 | 当前状态 |
|------|---------|---------|
| Regime.UNKNOWN 策略：block TREND_CONFIRM + BEAR_SQUEEZE + 其他类型 | main.py:380 | ADX+ATR=UNKNOWN（无新K线） |
| `best_score.win_probability < 0.55` | main.py:386 | 无新K线 → 无评分 |
| `data_quality < 0.75` | main.py:391 | freshness=0（OI/LSR全空）→ 质量 ≈ 0.35 |
| HMM crisis → kelly_scalar=0.1（抑制强度） | main.py:319 | 已触发 |

**signals 表**：0 行  
**判断**：❌ 无新K线 + data_quality 过低 = 三重过滤全部触发，无信号产出

---

### Layer ③ portfolio-service ⚪ 未触发

**日志**：仅 /health + /metrics 请求，无业务日志  
**portfolio_snapshots 表**：0 行  
**判断**：上游无信号，portfolio 从未收到任何指令

---

### Layer ④ risk-service ⚪ 未触发

**日志**：仅 /health + /metrics，无拦截记录  
**判断**：信号从未到达风控层，不是拦截者

---

### Layer ⑤ execution-service ❌ 独立失败（即使有信号也无法执行）

**日志**（持续至今，每 75 秒一次）：
```
2026-04-20 05:58:55 [WARNING] adapter.okx: OKX WS error: timed out during opening handshake | retry 60s
2026-04-20 06:00:09 [WARNING] adapter.okx: OKX WS error: timed out during opening handshake | retry 60s
...（24 小时未中断）
```

**原因**：与 Layer ① 相同——OKX WebSocket 也通过代理，容器内代理不通  
**EXEC_MODE=LIVE**：尝试连接真实 OKX，不是 paper/testnet  
**orders 表**：0 行  
**判断**：❌ 即使信号存在，OKX 连接永久失败，无法下单

---

### notification-service ⚠️ 配置缺失

**WX_WEBHOOK length**：1（= 空，仅换行符）  
**FEISHU_WEBHOOK length**：1（= 空，仅换行符）  
**旧 Redis 错误**：2026-04-16 startup 期间 redis 未就绪（现已恢复，可忽略）  
**判断**：即使有告警事件，企微/飞书 webhook 未配置，STRONG 级推送无法发出

---

### monitoring-service ⚠️ 自知无数据

**2026-04-18 健康简报关键内容**：
```
当前运行模式：👁 NOTIFY_ONLY
IC 统计：⏳ 尚无 IC 数据，继续运行积累中...
样本需求: ≥ 20 条  当前: 0 条
```

**注意**：.env 实际是 `EXEC_MODE=LIVE`，monitoring 报告显示 `NOTIFY_ONLY` ——  
说明 monitoring-service 读取的是 Redis 中的运行模式缓存或另一个 key，两者不一致。  
**判断**：monitoring 正确反映系统状态（无信号、无 IC），但无法自愈

---

## 完整证据链

```
[Docker容器] ──proxy──▶ 172.23.224.1:10810
                              ↓
                         TIMEOUT (exit 28)  ← 容器 curl 实测确认
                              ↓
[data-service] fetch_klines() → 全部返回 None
                              ↓
[candles table] 停止写入 @ 2026-04-19 07:00 UTC
                              ↓
[signal-service] STREAM_MARKET_CANDLES 无新消息
  + data_quality ≈ 0.35 < 0.75（OI/LSR 全空）
  + Regime.UNKNOWN（无新ATR计算）
                              ↓
[signals table] 0 行
                              ↓
[portfolio / risk / execution] 全部 idle
                              ↓
[notification] 无事件可推
```

---

## 之前 Claude Code 修复尝试的无效路径（不要再走）

根据症状推测已经试过的路径（均未触及根因）：
- ❌ 调整 `MIN_WIN_PROBABILITY` 阈值 → 无新数据时阈值无意义
- ❌ 修改 signal 评分逻辑 → 无K线进来评分不会触发
- ❌ 检查 notification-service / Telegram bot → 信号从未到达通知层
- ❌ 调整 `CONSECUTIVE_DAYS_REQUIRED` / monitoring 门槛 → 和信号生成无关
- ❌ 重启部分服务 → 重启后代理仍不通，重启无效

---

## 推荐修复路径（按优先级，不执行等确认）

### Fix A（最小改动，解决 Layer ①）：让容器能访问代理

**问题**：`172.23.224.1:10810` 是 WSL2 宿主机 IP，Docker bridge 网络无法路由

**选项 A1**：在 docker-compose.yml 中将代理 IP 改为 Docker 网关（推荐）
```yaml
environment:
  HTTP_PROXY: "http://host.docker.internal:10810"
  HTTPS_PROXY: "http://host.docker.internal:10810"
```

**选项 A2**：查出 Docker bridge 网关 IP，更新 .env
```bash
docker network inspect cryptowatch_cw4 | grep Gateway
# 通常是 172.22.0.1，如果代理监听 0.0.0.0 则可用
```

**选项 A3**：在 Windows 代理（Clash/v2ray）上开启"允许局域网连接"，  
并确保监听 `0.0.0.0:10810`（而非 `127.0.0.1:10810`）

> 修复后需重启 data-service + execution-service 让新 env 生效

---

### Fix B（解决 Layer ②的 data_quality 问题）：换 OI/LSR 数据源

**问题**：`testnet.binancefuture.com` 的 OI/LSR 端点返回 HTTP 202（无数据）  
→ `long_ratio` 永远为 None → freshness = 0 → data_quality ≈ 0.35 < 0.75

**方案**：将 OI/LSR 端点改回主网 `fapi.binance.com`（Fix A 修好代理后即可生效）  
或临时降低 `MIN_DATA_QUALITY` 到 0.4（治标）

---

### Fix C（解决 Layer ⑤）：EXEC_MODE 和 OKX 连接

**当前**：`EXEC_MODE=LIVE` + OKX WS 永久超时  
**选项**：
- 改 `EXEC_MODE=PAPER`（execution-service 用模拟模式，不依赖 OKX 连接）
- 或 Fix A 修代理后 OKX WS 也能通

---

### Fix D（notification）：配置 webhook

```bash
# .env 中补充
WX_WEBHOOK=https://qyapi.weixin.qq.com/...
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/...
```

---

## 修复优先级

| 优先级 | 修复 | 效果 |
|--------|------|------|
| 🔴 P0 | Fix A：修代理路由 | 解锁 data-service REST + OKX WS |
| 🔴 P0 | Fix C：改 EXEC_MODE=PAPER | 即使代理不通也能产 paper 订单 |
| 🟡 P1 | Fix B：换 OI/LSR 主网源 | 修 data_quality 门槛 |
| 🟢 P2 | Fix D：配 webhook | 告警推送 |

**最快验证**：Fix A 后运行  
```bash
docker exec cryptowatch-data-service-1 curl -s --proxy http://host.docker.internal:10810 https://api.binance.com/api/v3/ping
```
如返回 `{}` 则代理通，重启 data-service 即可开始补 candles。
