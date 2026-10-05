---
name: api_rules
description: 接口域规则 API-001（P99/错误率劣化）——它通常是症状而非根因，如何顺链路找到真因
---

# 接口域细则（API-001）

**API-001 几乎从不是根因，它是症状。** 直接"治理 API-001"没有对应的治理工具，
必须顺调用链找到下游的真因再治。

## 判定

同时满足其一即命中（P1）：

- P99 > **1.0s**（`API_P99_THRESHOLD_S`）
- 错误率 > **1%**（`API_ERR_THRESHOLD`）

## evidence 字段

| 字段 | 实测值（static 基线） | 含义 |
|---|---|---|
| `requests` | 1850 | 窗口内请求数 |
| `p99_s` | 1.244 | P99 延迟（秒） |
| `error_rate_pct` | 1.78 | 错误率（百分比） |
| `thresholds` | `{p99_s: 1.0, error_rate_pct: 1.0}` | 两个阈值 |
| `window_minutes` | live 有值 / static 为 null | 统计窗口 |

> `requests` 很重要：**请求数过少时 P99 与错误率都不可靠**。
> 若 `requests < 50`，先说明样本不足，不要据此下强结论。
> 基线的 1850 次是足够的样本量。

## 定位真因的四步

### 1. 横向对比，确认是单点还是全局

```sql
SELECT url, COUNT(*) AS n,
       ROUND(AVG(request_time), 3) AS avg_s,
       SUM(CASE WHEN status >= 500 THEN 1 ELSE 0 END) AS err_5xx
FROM ingress_logs GROUP BY url ORDER BY avg_s DESC
```

- **只有一个接口差** → 病灶在该接口的下游依赖
- **多个接口一起差** → 往基础设施找（节点资源、RDS、网络）

基线的形态是"仅 `POST /api/orders` 差，其余 7 个接口 P99 ≤ 0.39s"，属于典型单点。

### 2. 用拓扑找出该接口的下游

`get_topology` 看这个服务往下调用了谁，重点关注 `error_rate` 或 `p99_ms` 偏高的边。

### 3. 用 Trace 定位耗时落在哪一段

`query_traces` 取该接口的 span 树，按 `duration_us` 排序。
关键是**看耗时集中在哪一层**：

- 集中在 **DB span** → 查 `slow_logs`，大概率是 DB-002（无索引慢查询）
- 集中在**下游服务** → 递归对该服务重复本流程
- 集中在**本服务自身**（无下游 span）→ 查该服务 Pod 的 CPU/内存水位，
  以及是否命中 CAP-001（无 CPU request 被挤压）

### 4. 交叉验证因果

找到候选根因后，必须验证时间相关性 —— 慢查询的出现时段应与接口劣化时段吻合。
只有"同时存在"不足以证明因果。

## 治理

**没有直接治理 API-001 的工具。** 治它的根因：

| 根因 | 治理 | 参考 |
|---|---|---|
| DB-002 无索引慢查询 | `create_db_index` | `db_rules` |
| CAP-001 缺 CPU request | `patch_deployment(action=set_cpu_request)` | `capacity_rules` |
| HA-003 缺 readiness 探针 | `patch_deployment(action=add_probes)` | `ha_rules` |

治理后 `run_risk_scan` 复扫，API-001 应随根因一起 resolved。
**若根因已治而 API-001 仍 open，说明根因判断错了** —— 回到第 1 步重新走，
不要重复治同一个根因。
