# ChatBI 安全说明

本文档面向使用方 / DBA，覆盖三块内容：

1. **SQL 安全校验**：模型生成的 SQL 在执行前会经过 sqlglot AST 级校验与改写。
2. **身份与令牌**：API 只信任 HMAC 签名令牌，不接受请求方自填角色。
3. **数据库账号**：请 DBA 创建只读账号 `chatbi_ro`，应用统一使用该账号连接。

---

## 1. SQL 安全校验（security.py）

所有 SQL 在 `QuerySecurityManager.secure_sql()` 中用 sqlglot（dialect=mysql）解析，**解析失败一律拒绝**。规则：

| 规则 | 说明 |
|------|------|
| 仅单条 SELECT | 支持 `WITH ... SELECT`、`UNION`（各分支必须是 SELECT）；多语句、DDL/DML 一律拒绝 |
| 表白名单 | 只允许 `security_policy.json` 里 `allowed_tables` 列出的 9 张表；CTE 名称不算真实表，但**禁止 CTE 与数据表白名单 / 禁止 schema / 行级权限表同名**（防借同名 CTE 绕过行级过滤，所有角色一律拒绝） |
| 禁止库名前缀 | `a.b` 形式一律拒绝；`information_schema` / `mysql` / `performance_schema` / `sys` 一律拒绝 |
| 函数黑名单 | `SLEEP` / `BENCHMARK` / `LOAD_FILE` / `GET_LOCK` / `RELEASE_LOCK` / `UUID_SHORT`（大小写不敏感，按 AST 函数名判断）；`REPLACE()` 函数允许，`REPLACE INTO` 语句拒绝 |
| 禁止 INTO / 变量 | `INTO OUTFILE` / `INTO DUMPFILE` / `INTO @var`、`@@` 系统变量、用户变量及赋值 |
| 禁止加锁读 | `FOR UPDATE` / `LOCK IN SHARE MODE` |
| 行数上限 | `MAX_ROWS`（默认 1000）：无 LIMIT 自动补齐；LIMIT 超上限压到上限；UNION 对整体加 LIMIT |
| 体量上限 | 单查询最多 `MAX_TABLES_PER_QUERY` 张表（默认 6）、子查询嵌套深度 `MAX_SUBQUERY_DEPTH`（默认 5） |
| 敏感列 | ops 角色在任何位置（SELECT / WHERE / ORDER BY / 子查询）引用 `dim_products.unit_cost`、`dim_products.head_freight_cost` 直接拒绝；`SELECT *` 自动展开成非敏感列 |

字符串字面量里的分号、`CREATE` 等关键字**不会**触发拦截（全部基于 AST，不做原文关键词搜索）。

策略文件：`security_policy.json`（路径可用 `SECURITY_POLICY_PATH` 覆盖），包含表名单、函数黑名单、敏感列、脱敏列、行级权限表名单。

## 2. 行级权限（亚马逊店铺）

角色：

- `admin`：不限制。
- `ops`（运营）：只能看自己负责的店铺。店铺归属来自 `dim_stores.owner`（如 运营A → US01、CA01；运营B → UK01、DE01）。服务启动时及每 10 分钟（`STORE_CACHE_TTL_SECONDS`，默认 600s）从 `dim_stores` 刷新映射，代码不写死店铺清单。
- 其余角色（finance / sales）暂未实现，出现即报「未配置的角色」；完全未知的角色报「未知角色」。

实现：ops 查询时，AST 中每一个带 `store_id` 的表（`amz_*` 六张、`dim_stores`、`dim_products`）都被替换成子查询：

```sql
(SELECT * FROM amz_order_items WHERE store_id IN ('US01', 'CA01')) AS o
```

子查询 / CTE / UNION 各分支 / JOIN / 逗号连接 / 反引号 / 自连接里的每一张表都会被包裹，无法绕过。`exchange_rates` 无 `store_id`，不包裹。运营没有绑定店铺时拒绝查询。

## 3. 身份认证（令牌）

- 请求头：`Authorization: Bearer <签名令牌>`。
- 令牌载荷：`user_id`、`role`、`exp`（过期时间），ops 角色另含 `operator_name`。
- 签名：HMAC-SHA256，密钥为环境变量 `AUTH_SECRET`。
- 校验失败（签名错误 / 过期 / 缺 `role` 等字段）返回 **401**。
- 请求体里的 `user_role` / `user_region` / `user_id` 以及 `x-user-role` 等请求头**一律忽略**，无法自填角色提权。
- 开发模式：仅当 `ALLOW_DEMO_ADMIN=true` 时允许无令牌访问并以 `demo_admin`（admin 角色）执行；**默认关闭**。

### 生成令牌

`scripts/make_token.py`：

```bash
export AUTH_SECRET="换成随机长密钥"

# admin
uv run python scripts/make_token.py --user-id u_zhang --role admin

# ops（运营A，负责 US01/CA01）
uv run python scripts/make_token.py --user-id u_ops_a --role ops --operator-name 运营A --ttl 86400
```

调用示例：

```bash
curl -X POST http://localhost:8000/api/v1/query \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"question": "上月销售额是多少"}'
```

内部评测（`evaluator.py` / `run_eval.py`）走 `UserContext.demo_admin()`，不经过 HTTP 令牌。

## 4. DBA 操作：只读账号（请手动执行）

请用管理员账号在 MySQL 8.x 执行以下 SQL，创建应用专用只读账号，并把 `.env` 切到该账号：

```sql
-- 1. 创建只读账号（请把密码换成强密码，写入 .env 的 DB_PASSWORD）
CREATE USER IF NOT EXISTS 'chatbi_ro'@'%'
  IDENTIFIED BY 'REPLACE_WITH_STRONG_PASSWORD';

-- 2. 仅授予 chatbi_demo 库 9 张白名单表的 SELECT
GRANT SELECT ON chatbi_demo.dim_stores        TO 'chatbi_ro'@'%';
GRANT SELECT ON chatbi_demo.dim_products      TO 'chatbi_ro'@'%';
GRANT SELECT ON chatbi_demo.exchange_rates    TO 'chatbi_ro'@'%';
GRANT SELECT ON chatbi_demo.amz_order_items   TO 'chatbi_ro'@'%';
GRANT SELECT ON chatbi_demo.amz_ads_daily     TO 'chatbi_ro'@'%';
GRANT SELECT ON chatbi_demo.amz_traffic_daily TO 'chatbi_ro'@'%';
GRANT SELECT ON chatbi_demo.amz_fba_inventory TO 'chatbi_ro'@'%';
GRANT SELECT ON chatbi_demo.amz_settlement    TO 'chatbi_ro'@'%';
GRANT SELECT ON chatbi_demo.amz_returns       TO 'chatbi_ro'@'%';

-- 3. 显式确认没有 FILE / PROCESS / SUPER 等危险权限（默认即无，此处仅示例核查）
SHOW GRANTS FOR 'chatbi_ro'@'%';
-- 应只看到对上述 9 张表的 SELECT，不应出现 FILE、PROCESS、SUPER、GRANT OPTION

FLUSH PRIVILEGES;
```

`.env` 对应修改：

```dotenv
DB_USER="chatbi_ro"
DB_PASSWORD="REPLACE_WITH_STRONG_PASSWORD"
DB_NAME="chatbi_demo"
```

说明：

- 不授予 `FILE`：`LOAD_FILE()` / `INTO OUTFILE` 即使绕过应用层也无法读写服务器文件（应用层也会拦截）。
- 不授予 `INSERT/UPDATE/DELETE/DDL`：应用层只放行 SELECT，数据库层再兜底。
- `SET SESSION max_execution_time` / `SET SESSION TRANSACTION READ ONLY` 是会话级变量设置，普通账号即可执行，无需额外授权。

## 5. 执行层防护（database.py）

- 每次查询前：`SET SESSION TRANSACTION READ ONLY` + `SET SESSION max_execution_time = QUERY_TIMEOUT_MS`（默认 30000 毫秒）。
- 结果取 `fetchmany(MAX_ROWS + 1)`：超出上限只返回前 `MAX_ROWS` 行，`last_query_info.truncated = True`，前端提示「结果已截断」。
- 慢查询 EXPLAIN 只对已通过校验的最终 SQL 执行。
- 审计日志：logger `chatbi.audit`，JSON 行输出（时间、user_id、role、operator_name、原始问题、原始 SQL、最终 SQL、是否拦截及原因、耗时、返回行数、是否截断），**不记录结果数据**。

## 6. 追踪与数据合规（LangSmith）

开启 `LANGSMITH_TRACING=true` 后，各阶段 span 会把输入输出上传到 LangSmith。默认**只上传 SQL 文本、问题、行数、耗时等元数据，不上传查询结果行**：

- `DatabaseClient.execute`（span「SQL执行」）：输入只保留 `sql` / `question`（用户上下文只保留 `role` 字符串，不上传 `user_id` / `operator_name`）；输出只保留列名与 `row_count`，任何结果行都不会离开本机。
- 单条问答链路（span「问答链路」）：默认丢弃全部结果行，只保留 `row_count`。本地调试如需在 trace 里看样本数据，可设 `LANGSMITH_TRACE_ROWS=true`，此时保留前 5 行；**生产环境务必保持默认 `false`**。
- Agent 多步链路：步骤执行 / 结果汇总 / 报告生成等 span 的步骤结果只上传状态、列名与行数，中间结果正文不进 trace。
- 结果格式化 span：只上传列名、行数与文本长度，不上传表格正文。

注意：**报告生成阶段会把聚合结果发给云端大模型**（用于生成分析报告文本），这是业务功能本身决定的。系统只把每步最多 3 行预览和一行摘要放进 prompt（`report_generator._compress_step_result`），不会把整表结果发给云端；若结果不允许出域，请关闭报告生成或改用私有化部署的模型。

生产环境建议：

- 使用私有化的 `LANGSMITH_ENDPOINT` 指向自托管/内网网关，数据不出内网；或
- 直接关闭追踪（`LANGSMITH_TRACING=false`，缺省即关闭）。

## 7. 已知边界 / 需要决策

- 敏感列名单当前为 `dim_products.unit_cost`、`dim_products.head_freight_cost`（`security_policy.json` 的 `restricted_columns`），**待业务确认**。
- `masked_columns`（输出脱敏，默认 `customer_phone` / `customer_email`）目前只对非 admin 角色生效，当前模拟表结构里还没有这些列。
- ops 之外的角色（finance / sales 等）尚未定义策略，出现即拒绝。
- 令牌不支持吊销（除改 `AUTH_SECRET` 全员失效）；`exp` 默认 24h，按需调整 `--ttl`。
