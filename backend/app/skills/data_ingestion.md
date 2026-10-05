---
name: data_ingestion
description: 数据采集指南——static/live 两种模式的差异、七张观测表的来源与格式陷阱、采集结果如何汇报
when_to_use: 需要采集数据入库、汇报采集结果与数据覆盖范围，或采集出现"无增量"时排查
allowed-tools: ingest_data, sql_query, query_metrics
---

# 数据采集指南

## 先判断当前是哪种模式

| 模式 | 数据来源 | `ingest_data` 行为 |
|---|---|---|
| `static` | 仓库内预制数据集（`data/data/*.jsonl`） | 全量采集：**先清空七张表再重建** |
| `live` | mock_server 持续演进 + collector 每 10~60s 采集 | **被拒绝**，返回 skipped |

在 `live` 模式下反复调 `ingest_data` 是无效的 —— 它会返回
`{"status": "skipped", "note": "live 模式由 data_collector 持续采集…"}`。
此时应改用 `sql_query` 汇报现状，不要重试。

> ⚠️ `static` 模式的 `ingest_data` 会 `DELETE` 七张观测表。
> 若当前库正被其他环境的 collector 写入，这一下会清空对方的数据。

## 七张观测表的来源与格式陷阱

| 表 | 来源 | 陷阱 |
|---|---|---|
| `metrics` | CMS `DescribeMetricList` | 聚合列名是 `avg`/`max`/`min`；`ts` 是**毫秒** |
| `ingress_logs` | SLS `nginx-ingress` | SLS 返回值**全是字符串**，入库时才转类型 |
| `app_logs` | SLS `app-log` | 同上；`source_pod` 来自 `__source__` |
| `slow_logs` | SLS `rds_slow_log` | `governed` 字段入库时初始化为 0，治理后才置 1 |
| `k8s_events` | SLS `k8s-events` | 窗口内可能**一条都没有**，0 行是正常的 |
| `trace_spans` | SLS `trace` | `attribute`/`resource` 是 **JSON 字符串**，入库时解开；`start_us`/`duration_us` 是**微秒**；`ts` 是**秒** |
| `k8s_resources` | K8s 快照 | **全量覆盖**（replace），行数恒定不增长 |

## 时间戳单位不统一

**日志类是秒，指标类是毫秒**：

- 秒：`ingress_logs`、`app_logs`、`slow_logs`、`k8s_events`、`trace_spans`
- 毫秒：`metrics`、`realtime_metrics`

这是历史原因（SLS 的 `__time__` 是秒级，CMS 的 `timestamp` 是毫秒级）。
算数据新鲜度或做时间窗过滤时按表区分，混用会得出几十年的荒谬差值。

## 汇报采集结果的要求

**不要只说"采集成功"。** 必须给出：

1. **每张表的入库行数**；
2. **数据的时间覆盖范围**（最早 ~ 最新，换算成可读时间）；
3. **行数为 0 的表要单独说明原因** —— 大概率是该类事件在窗口内没有发生
   （`k8s_events` / `slow_logs` 常见），而不是采集失败。把这个区别讲清楚，
   否则用户会误判为采集有问题。

查询模板：

```sql
SELECT 'ingress_logs' AS t, COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts
FROM ingress_logs
```

（`metrics` 要记得 `ts` 是毫秒，换算时除以 1000）

## "采集无增量"怎么排查

`live` 模式下 collector 日志出现"无增量"时，按顺序查：

1. **mock_server 是否在跑** —— 它是数据源，停了就没有新数据；
2. **水位线是否卡住** —— collector 按 `MAX(ts)` 做增量，若上游时间戳没前进就取不到新数据；
3. **该类事件是否本来就没发生** —— `k8s_events` 长期 0 行属正常。

前两项属于环境问题，不是 Agent 能通过工具解决的，应当如实告知用户去检查进程状态。
