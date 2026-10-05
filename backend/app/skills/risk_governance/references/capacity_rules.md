---
name: capacity_rules
description: 容量域四条规则（CAP-001~004），含 CAP-003 超卖率的反算算法与聚合语义陷阱
---

# 容量域细则（CAP-001 ~ CAP-004）

CAP-001/002/004 是**单体**风险，CAP-003 是**聚合**风险 —— 这个区别决定了治理顺序。

## CAP-001 缺失 CPU request（P1）

**判定**：容器没配 `resources.requests.cpu`。调度器无法为它预留 CPU，
高负载时会被同节点其他 Pod 挤压。

**evidence**：`requests_keys` —— 实际配了哪些 request 键（不含 `cpu` 即命中）。

**治理**：`patch_deployment(name, action="set_cpu_request", value="500m")`

> **不影响 CAP-003** —— 超卖率只看 limit，不看 request。可以放心先做。

## CAP-002 缺失内存 limit（P2）

**判定**：容器没配 `resources.limits.memory`。内存泄漏时会吃光节点内存，
触发 OOM 连带杀掉同节点其他 Pod。

**evidence**：`limits_keys` —— 实际配了哪些 limit 键。

**治理**：`patch_deployment(name, action="set_memory_limit", value="2048Mi")`

## CAP-003 CPU 超卖率超阈值（P1）★ 聚合规则

**判定**：`default` 命名空间内**所有 Pod 的 CPU limit 合计 ÷ 节点可分配 CPU 合计 > 150%`。

**evidence 字段（全部可直接用于计算，别自己再查一遍）**

| 字段 | 实测值（static 基线） | 含义 |
|---|---|---|
| `sum_pod_cpu_limit_m` | 39300 | 当前 CPU limit 合计（毫核） |
| `sum_node_allocatable_m` | 23400 | 节点可分配 CPU 合计（毫核） |
| `snapshot_oversale_pct` | 167.95 | 按快照算出的超卖率 |
| `cms_metric_avg_pct` | 167.94 | CMS 指标侧的均值（交叉验证用） |
| `threshold_pct` | 150.0 | 阈值 |

### 反算下调幅度（不要凭感觉填 value）

```
目标合计 = sum_node_allocatable_m × 1.5
需下调   = sum_pod_cpu_limit_m − 目标合计
```

以基线为例：目标 = 23400 × 1.5 = **35100m**，需下调 = 39300 − 35100 = **4200m**。

然后从 **CPU limit 占用最高的服务**开始下调。注意换算：

> `set_cpu_limit` 的 value 是**单容器**的 limit。
> 对某服务下调的**总收益 = (原单容器 limit − 新值) × 该服务副本数**。
> 忽略副本数会导致下调量算少几倍，复扫仍然 open。

需要按服务查当前 limit 与副本数时：

```sql
SELECT name, spec_json FROM k8s_resources WHERE kind='Pod'
SELECT name, spec_json FROM k8s_resources WHERE kind='Node'
```

### 两个容易踩的聚合语义陷阱

1. **只统计 `default` 命名空间**。`nginx-ingress` 等其他命名空间的 Pod 不计入。
   若把全部 Pod 都算进去会得到 41300m 而不是 39300m，据此算出的下调量偏大。
2. **会被扩副本反向推高**。实测 order-service 副本 1→3：
   limit 合计 39300→47300m，超卖率 167.95%→**202.14%**（+34.2 个百分点）。
   所以 CAP-003 必须放在**所有扩副本动作之后**再治。

## CAP-004 RDS 内存高水位（P2）

**判定**：实例内存使用率**常态**均值 > 85%（不是瞬时峰值）。

**evidence**

| 字段 | 实测值 | 含义 |
|---|---|---|
| `memory_usage_avg_pct` | — | 窗口内均值 |
| `threshold_pct` | 85.0 | 阈值 |
| `window_minutes` | live 模式有值 / static 为 null | 统计窗口 |

**治理**：`upgrade_rds_instance(instance_id, target_spec)`

> 升配是**治标**。若同时存在 DB-002（无索引慢查询），内存压力往往来自
> 全表扫描的临时表与 buffer pool 争用 —— 先按 `db_rules` 建索引，
> 再看内存是否自然回落，能省掉一次升配。

## 域内治理顺序

1. CAP-001 / CAP-002（单体、互不影响，且不动超卖率）
2. CAP-004（若同时有 DB-002，先建索引再判断是否真需要升配）
3. **CAP-003 放最后**，且必须在所有 `set_replicas` 之后
