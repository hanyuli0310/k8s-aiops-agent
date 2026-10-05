---
name: log_analysis
description: 日志下钻方法论——从海量日志收敛到具体 trace_id 的四步法、错误聚类与时间相关性验证
when_to_use: 需要分析错误日志、定位异常时间段、从日志找到可下钻的 trace_id 时
allowed-tools: query_logs, sql_query, query_traces, read_tool_result
---

# 日志下钻方法论

日志的问题不是"查不到"，而是**一次查出上万条、看不出重点**。
核心思路是**先聚合收敛、再定点下钻**，而不是从头翻原始日志。

## ⚠️ 先看清你拿到的是全量还是预览

`query_logs` 一次可能返回几万字符，超限时会落盘并只回**结构感知预览**：

```json
{"row_count": 487, "logs": {"_total": 487, "_sample": [ …前 3 条… ]}}
```

**不要以为只有 3 条。** 先读 `_total` 判断规模，再决定策略：

| `_total` | 策略 |
|---|---|
| < 50 | 直接看样本即可下结论 |
| 50 ~ 数千 | **改用 `sql_query` 聚合**，不要拉全量 |
| 数千以上 | 必须聚合；确需原文时用 `read_tool_result(path, offset, limit)` 分页 |

## 四步收敛法

### 第 1 步：按维度聚合，找出异常集中在哪

```sql
SELECT source_pod, level, COUNT(*) AS n
FROM app_logs
WHERE ts >= strftime('%s','now') - 1800
GROUP BY source_pod, level ORDER BY n DESC
```

看错误是**集中在单个 Pod** 还是**散布在整个服务**：

- 集中单 Pod → 该实例问题（OOM、磁盘、节点异常）
- 整个服务都有 → 代码问题或其下游依赖问题

### 第 2 步：按时间分桶，定位异常起始时刻

```sql
SELECT ts / 60 * 60 AS minute_bucket, COUNT(*) AS n
FROM app_logs
WHERE level='ERROR' AND ts >= strftime('%s','now') - 3600
GROUP BY minute_bucket ORDER BY minute_bucket
```

**突增的那个时间点最有价值** —— 它把"什么变了"的搜索范围从一小时压到一分钟。
拿到这个时刻后，去 `k8s_events` 查同一时刻有没有发布/重启/驱逐事件。

### 第 3 步：错误消息聚类

日志里的 `message` 通常含变量（ID、耗时），直接 `GROUP BY message` 会一条一组。
**按前缀截断再聚合**：

```sql
SELECT substr(message, 1, 60) AS pattern, COUNT(*) AS n
FROM app_logs WHERE level='ERROR'
GROUP BY pattern ORDER BY n DESC
```

出现频次最高的 pattern 就是主要错误类型。次高的也要看 —— 有时真因藏在第二名里
（最高频的往往只是级联失败的表象）。

### 第 4 步：拿到 trace_id 定点下钻

日志聚合只能告诉你"哪类错误多"，**要看完整调用链必须落到 trace**：

- `ingress_logs` 有 `req_id`
- `trace_spans` 有 `trace_id`

从错误样本里取出一个具体 id，然后 `query_traces(trace_id=...)` 看完整 span 树。
**一条有代表性的完整 trace 比一万条日志更能说明问题。**

## 时间相关性验证（避免把巧合当因果）

找到候选原因后必须验证时间吻合：

```sql
-- 错误的时间范围
SELECT MIN(ts), MAX(ts) FROM app_logs WHERE level='ERROR' AND message LIKE '%timeout%'
-- 候选原因（如慢查询）的时间范围
SELECT MIN(ts), MAX(ts) FROM slow_logs WHERE rows_examined > 1000000
```

两者的时间窗**必须重叠**。只有"都存在"不构成因果 —— 这是日志分析里最常见的误判。

## 各日志表的用途

| 表 | 用途 | 关键字段 |
|---|---|---|
| `app_logs` | 应用自身报错 | `level`、`message`、`source_pod` |
| `ingress_logs` | 入口请求成败与耗时 | `status`、`request_time`、`url`、`req_id` |
| `slow_logs` | 数据库慢查询 | `rows_examined`、`rows_sent`、`sql_text` |
| `k8s_events` | 发布/重启/驱逐/调度失败 | `reason`、`message`、`involved_object_name` |

> `k8s_events` 经常是 0 行 —— 说明窗口内集群没有事件发生，**不是采集故障**。
> 但它一旦有内容（如 `OOMKilled`、`FailedScheduling`）往往直接给出答案，值得先看一眼。

> 这些表的 `ts` 都是**秒**级（`metrics` 才是毫秒）。详见 `sql_analytics`。
