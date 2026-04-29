# Helixa → selene_app 只读访问配置

## 目的

sel_v2 策略引擎需要衍生品数据（OI、资金费率）和 taker 流量数据来激活以下 STUB：

| STUB | 数据来源 | 用途 |
|---|---|---|
| `v2_derivatives_snapshots` | `helixa.derivatives_snapshots` | Strategy 1 Step 4a/4b（funding/OI 过滤） |
| `ofi_90pct` (check_surging) | `helixa.taker_flow_1m` | check_surging OFI 代理 |
| OI 方向检查 | `helixa.open_interest_history` | Strategy 1 Step 4b |

这些表位于 `helixa` database，与 sel_v2 所在的 `selene` database 是同一 PostgreSQL 实例（platform-postgres）的不同 database。`helixa_grants.sql` 只发出 `GRANT SELECT`，不创建任何对象，不修改 helixa 中的任何内容。

## kanpan 需要执行的步骤

**前提**：以 superuser（`kanpan`）身份连接到 `helixa` database。

```bash
# 在 platform-postgres 容器内执行
docker exec -it platform-postgres psql -U kanpan -d helixa \
  -f /path/to/sel_v2/db/helixa_grants.sql
```

或直接在容器内：

```bash
docker exec platform-postgres psql -U kanpan -d helixa << 'EOF'
\i /app/sel_v2/db/helixa_grants.sql
EOF
```

如果无法挂载文件，可以直接粘贴 `helixa_grants.sql` 内容到 psql 提示符执行。

## 验证（kanpan 执行 GRANT 后，由 selene_app 验证）

```bash
# 以 selene_app 身份，连接到 helixa 验证 SELECT 权限
docker exec platform-postgres psql -U selene_app -d helixa -c "
SELECT COUNT(*) FROM public.derivatives_snapshots WHERE symbol='BTC';
"
# 期望: 返回行数 > 0（当前约 1728 行）

docker exec platform-postgres psql -U selene_app -d helixa -c "
SELECT COUNT(*) FROM public.taker_flow_1m WHERE symbol='BTC/USDT-SWAP';
"
# 期望: 返回行数 > 0（当前约 388 行）
```

如果返回 `permission denied` → GRANT 未生效，重新检查执行用户是否为 kanpan superuser。

## 撤销权限

如需撤销（反向 SQL），见 `helixa_grants.sql` 文件末尾的注释块，或直接执行：

```sql
-- 在 helixa database 中以 kanpan 身份执行
REVOKE SELECT ON TABLE public.derivatives_snapshots FROM selene_app;
REVOKE SELECT ON TABLE public.taker_flow_1m           FROM selene_app;
REVOKE SELECT ON TABLE public.funding_rate_history    FROM selene_app;
REVOKE SELECT ON TABLE public.open_interest_history   FROM selene_app;
REVOKE SELECT ON TABLE public.orderbook               FROM selene_app;
REVOKE SELECT ON TABLE public.ohlcv                   FROM selene_app;
REVOKE USAGE  ON SCHEMA public                        FROM selene_app;
REVOKE CONNECT ON DATABASE helixa                     FROM selene_app;
```

## 接入后 sel_v2 代码端配置

GRANT 生效后，sel_v2 策略引擎需要一个第二个 DB 连接池指向 helixa：

```python
HELIXA_DB_URL = (
    f"postgresql://selene_app:{SELENE_APP_PASSWORD}"
    f"@platform-postgres:5432/helixa"
)
```

具体接入时机：Wave 4 中 `strategy1_entry.py` Step 4a/4b 的衍生品过滤从 STUB 切换到真实数据时。

## 安全边界

- `selene_app` 仅有 `SELECT` 权限，无法写入、修改或删除 helixa 中的任何数据
- 未授权的表（如 `signals`、`reports` 等）不在 GRANT 范围内
- 本配置不涉及 FDW/dblink，不在 selene 中创建任何 FOREIGN TABLE 对象
