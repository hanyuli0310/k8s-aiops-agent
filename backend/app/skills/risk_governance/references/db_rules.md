---
name: db_rules
description: 数据库域两条规则（DB-001 连接高水位 / DB-002 无索引慢查询）的因果关系与治本治标之分
---

# 数据库域细则（DB-001 / DB-002）

这两条**常常同时出现，且有因果关系**：DB-002 是因，DB-001 往往是果。
理解这一点能避免做无效的升配。

## DB-002 无索引慢查询（P2）★ 通常是根因

**判定**：慢日志中存在 `rows_examined` 超过 100 万的 SQL（全表扫描特征）。

**evidence 字段**

| 字段 | 实测值（static 基线） | 含义 |
|---|---|---|
| `slow_sql_kinds` | 3 | 慢 SQL 的种类数（去重后） |
| `slow_log_count` | 50 | 慢日志条数 |
| `min_rows_examined` | 1162951 | 扫描行数的最小值（说明**最好的一条**都扫了 116 万行） |
| `max_rows_sent` | 7 | 返回行数的最大值 |
| `sample_sql` | 见下 | 样本 SQL |

**判断全表扫描的关键比值**：`rows_examined / rows_sent`。
基线里是 `1162951 / 7 ≈ 166135` —— 扫 16 万行只为返回 1 行，索引缺失确凿。
这个比值比绝对值更有说服力，写结论时应该给出它。

样本 SQL：

```sql
-- [外部SQL] 这是业务库 RDS 上跑的语句，orders 表不在本平台白名单里，不要拿它去调 sql_query
SELECT * FROM orders WHERE user_id = 8231 AND status = 'PENDING' ORDER BY created_at DESC
```

**治理**：`create_db_index(table="orders", columns=["status","created_at"])`

### 复合索引的列序怎么定

按 **等值条件列在前、排序列在后**：

- `WHERE user_id = ? AND status = ?` → 等值列 `user_id`、`status`
- `ORDER BY created_at DESC` → 排序列 `created_at`

所以 `(user_id, status, created_at)` 或 `(status, created_at)` 都能避免全表扫。
从 `sample_sql` 里读出实际的 WHERE / ORDER BY 列，**不要套用固定组合**。

## DB-001 RDS 连接使用率高水位（P1）

**判定**：连接使用率**常态**均值 > 80%。

**evidence 字段**

| 字段 | 实测值 | 含义 |
|---|---|---|
| `connection_usage_avg_pct` | 81.9 | 窗口内均值（阈值 80%） |
| `too_many_connections_errors` | 15 | 应用日志里 "Too many connections" 的出现次数 |
| `window_minutes` | live 有值 / static 为 null | 统计窗口 |

`too_many_connections_errors > 0` 说明**已经在实际报错**，不只是水位偏高，
这时优先级应当上调，写结论时要点明。

### 治本还是治标

| 手段 | 性质 | 何时用 |
|---|---|---|
| `create_db_index`（治 DB-002） | **治本** | 慢查询占着连接不放是连接堆积的常见成因 —— 先做这个 |
| `upgrade_rds_instance` | 治标 | 索引治理后连接水位仍不降，才考虑升配 |

**推荐顺序**：先 `create_db_index` → `run_risk_scan` 复扫 → DB-001 若仍 open 再升配。

> 直接升配虽然能立刻压下水位，但慢查询还在，过一段时间会重新涨回来 ——
> 表现为"升配后短期正常、之后复发"。给用户的建议里应当说明这个取舍。

## 与 API-001 的联动

若同时看到 API-001（接口 P99 劣化）指向的接口正好访问这张表，
那么 DB-002 很可能是整条链路的根因。此时按 `api_rules` 的方法验证因果，
治理一次 `create_db_index` 可能同时消掉 DB-002、DB-001、API-001 三条风险。
