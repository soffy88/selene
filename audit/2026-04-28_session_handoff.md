# Selene Session Handoff — 2026-04-28

## Cold Start 状态
- t=0 = 2026-04-28T11:00:00+00:00 UTC
- 完成时间 = 2026-05-28T11:00:00+00:00 UTC
- 当前进度: 2 bar / 720 bar (0.28%)

## 调度器状态
- sel-bar-runner: 运行中（selene-sel-bar-runner-1）
- 下次触发: 下个 UTC 整点 + 30 秒
- consecutive_failures: 0
- last_bar_processed: 2026-04-28T14:00:00+00:00

## 数据库
- sel_features: 2 行
- sel_state_sequence: 2 行
- 所有 record cold_start=true（直到 2026-05-28T11:00:00Z）

## Collector 健康
- sel-orderbook / sel-trade-flow / sel-oi：运行中，标 unhealthy（EC-01，假报警）
- data-service：运行中（提供 candles）

## 已知 EC 优先级表

| EC | 描述 | 优先级 | 触发条件 | 计划修复窗口 |
|---|---|---|---|---|
| EC-01 | collector unhealthy 假报警 | A（低） | 运维 | 运维窗口 |
| EC-10 | StateEngine 重启内存丢失 | C | post-warmup | cold start 后 |
| EC-11 | 失败 bar 无持久记录 | B | 持续 | 待决策 |
| EC-12 | backfill/validate_e2e open_time | B | 手动运行时 | cold start 后 |
| EC-13 | asyncpg 无超时 | B | 长空闲后 | 持续观察 |
| EC-14 | funding_rate JSON 解析 | C | 每 bar | 待决策 |
| EC-15 | trail_store 大小写访问 | B | cold start 结束后 | 2026-05-21 |
| EC-16 | paper_interface schema 不匹配 | A | cold start 结束后 | 2026-05-21 |

## Cold Start 期间严禁
- 修改 sel_engine/features/ 或 sel_engine/states/ 任何代码
- 修改特征计算阈值或窗口
- 重置数据库或回填历史 bar
- 部署到不同 instrument

## 接手时第一件事
1. 读本文件
2. 检查健康：docker exec helios-redis redis-cli -n 3 GET sel:scheduler:last_run
3. 确认 sel_state_sequence 行数 = 距 t=0 已过小时数（允许 ±1）
4. 等待用户 task 派发
