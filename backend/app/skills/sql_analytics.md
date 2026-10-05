---
name: sql_analytics
description: 只读 SQL 查询指南——11 张白名单表的真实列名、时间戳单位差异、常用聚合模式与踩坑清单
when_to_use: 需要用 sql_query 做聚合/过滤/关联查询，或按时间窗筛数据时
allowed-tools: sql_query, read_tool_result
---

# 只读 SQL 分析指南

`sql_query` 只允许 `SELECT`，自动加 `LIMIT 50`，且表名走白名单。
**列名与时间单位是本项目最容易出错的两处**，下面给的是从代码 metadata 提取的真实结构。

## ⚠️ 两个高频错误（先看这里）

### 1. `metrics` 表的聚合列叫 `avg` / `max` / `min`

**不是** `avg_value`、**不是** `value`。写错会得到 `no such column`
（错误信息里会附上该表的真实列名，可据此立即纠正，不要连续猜）。

```sql
-- ✅ 正确
SELECT metric_name, ROUND(AVG(avg), 2) AS avg_v, MAX(max) AS peak
FROM metrics WHERE namespace='acs_k8s' GROUP BY metric_name

-- ❌ 错误：avg_value / value 都不存在
SELECT AVG(avg_value) FROM metrics
```

注意 `avg` / `max` / `min` 是 SQL 关键字，某些写法下需要反引号包裹。

### 2. 时间戳单位不统一：日志类是**秒**，指标类是**毫秒**

| 单位 | 表 |
|---|---|
| **秒** | `ingress_logs`、`app_logs`、`slow_logs`、`k8s_events`、`trace_spans` |
| **毫秒** | `metrics`、`realtime_metrics` |

混用会算出几十年的时间差。按时间窗过滤时：

```sql
-- 秒表：最近 30 分钟
SELECT * FROM ingress_logs WHERE ts >= strftime('%s','now') - 1800

-- 毫秒表：最近 30 分钟（注意 × 1000）
SELECT * FROM metrics WHERE ts >= (strftime('%s','now') - 1800) * 1000
```

`k8s_resources`、`topology_edges`、`risk_findings` 没有 `ts` 列（它们是快照/结果表）。

## 表结构速查

| 表 | ts 单位 | 关键列 |
|---|---|---|
| `metrics` | ms | `namespace, metric_name, dims_json, ts, avg, max, min` |
| `realtime_metrics` | ms | `ts, service, instance, kind, metric, value, status` |
| `ingress_logs` | s | `ts, method, url, status, request_time, upstream_status, client_ip, req_id, source_pod` |
| `app_logs` | s | `ts, source_pod, level, pod_ip, message` |
| `slow_logs` | s | `ts, instance_id, db_name, sql_text, query_time, lock_time, rows_examined, rows_sent, governed` |
| `k8s_events` | s | `ts, event_type, reason, message, namespace, involved_object_kind, involved_object_name` |
| `k8s_resources` | — | `kind, namespace, name, spec_json` |
| `trace_spans` | s | `trace_id, span_id, parent_span_id, service, name, kind, start_us, duration_us, status_code, attr_json` |
| `topology_edges` | — | `source, target, call_count, error_rate, avg_ms, p99_ms` |
| `risk_findings` | — | `rule_id, severity, title, resource_ref, evidence_json, status, suggestion, scan_ts` |
| `risk_rules` | — | 规则定义 |

注意 `trace_spans` 有两个时间列：`ts`（**秒**，用于时间窗过滤）与
`start_us` / `duration_us`（**微秒**，用于算耗时）。别混。

### `metrics` 还是 `realtime_metrics`？

两张表都是毫秒时间戳，但用途不同：

- `metrics` —— 云监控口径的分钟级聚合（`avg` / `max` / `min` 三列），
  适合看**趋势**与跨小时对比；
- `realtime_metrics` —— 采集器每 10s 一点的**分实例**水位（一行一个
  `instance` + `metric` + `value`），适合回答"**现在**哪个实例高"。

static 模式下 `realtime_metrics` 是空的（它只由 live 采集器写入），
查之前先确认有数据，别把空结果当成"水位正常"。

## 常用聚合模式

### 接口性能排行（找病灶接口）

```sql
SELECT url, COUNT(*) AS n,
       ROUND(AVG(request_time), 3) AS avg_s,
       SUM(CASE WHEN status >= 500 THEN 1 ELSE 0 END) AS err_5xx
FROM ingress_logs GROUP BY url ORDER BY avg_s DESC
```

### 慢查询按 SQL 指纹聚合

```sql
SELECT db_name, COUNT(*) AS cnt,
       MAX(rows_examined) AS max_examined, MAX(rows_sent) AS max_sent,
       ROUND(AVG(query_time), 3) AS avg_s
FROM slow_logs GROUP BY db_name ORDER BY cnt DESC
```

判断全表扫描看 `rows_examined / rows_sent` 比值，不要只看绝对值。

### Trace 里最慢的 span

```sql
SELECT service, name, kind, ROUND(duration_us/1000.0, 1) AS ms
FROM trace_spans WHERE trace_id = 'xxx' ORDER BY duration_us DESC
```

### 错误日志按服务分布

```sql
SELECT source_pod, level, COUNT(*) AS n
FROM app_logs WHERE level IN ('ERROR','WARN')
GROUP BY source_pod, level ORDER BY n DESC
```

## JSON 列怎么读

`dims_json` / `spec_json` / `attr_json` / `evidence_json` 是 JSON 列。
SQLite 下可用 `json_extract`，但**更稳妥的做法是先 SELECT 出来在结论里自己解析** ——
MySQL 与 SQLite 的 JSON 函数语法有差异，而本项目两种后端都可能在用。

## 结果超长怎么办

`sql_query` 自动 `LIMIT 50`。若结果仍超过工具的体量上限，会落盘并只回预览
（含 `_total` 与字段结构）。此时：

1. 先看 `_total` 判断数据规模；
2. **优先改写 SQL 做聚合**（`GROUP BY` / `COUNT` / `AVG`），而不是分页拉全量；
3. 确实需要原始行时才用 `read_tool_result(path, offset, limit)`。

## 禁止事项

- 只能 `SELECT`。`INSERT/UPDATE/DELETE/DROP/ALTER` 会被直接拒绝。
- 表名必须在白名单内：`metrics, realtime_metrics, ingress_logs, app_logs, slow_logs,
  k8s_events, k8s_resources, trace_spans, topology_edges, risk_findings, risk_rules`。
  查不到想要的表时先确认表名拼写（`k8s_resources` 含数字，容易写成 `k8s_resource`）。
