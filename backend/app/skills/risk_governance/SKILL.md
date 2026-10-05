---
name: risk_governance
description: 风险治理总纲——风险类型到治理工具的映射、治理动作的顺序依赖、闭环流程与告警规则生成
when_to_use: 需要执行治理动作、制定治理计划、生成告警规则，或治理后复扫不 resolved 时
allowed-tools: run_risk_scan, get_risk_report, patch_deployment, create_pdb, create_db_index, upgrade_rds_instance, create_risk_rule, sql_query
---

# 风险治理总纲

13 条内置规则分五个域，**各域的判定逻辑与治理手段差别很大**，细则见本 Skill 的
reference 文档（用 `load_skill("risk_governance", reference="...")` 取）：

| 域 | 规则 | reference |
|---|---|---|
| 高可用 | HA-001~004 | `ha_rules` |
| 容量 | CAP-001~004 | `capacity_rules` |
| 数据库 | DB-001/002 | `db_rules` |
| 接口 | API-001 | `api_rules` |
| 缓存 | CACHE-001/002 | `cache_rules` |

## ⚠️ 规则之外：这 13 条不是问题的全集

这些规则是人预先写好的，只能覆盖**已经被想到过的**故障模式。
真实发生过的漏网：Redis 缓存雪崩时，当时的 11 条规则里没有任何一条涉及缓存层，
规则侧只报出了下游的 API-001（接口变慢）—— 根因完全报不出来。
后来补上了 CACHE-001/002，但补的只是这一个已知缺口；消息队列积压、
连接池耗尽、外部依赖限流、客户端重试风暴、证书过期……仍然一条规则都没有。

所以：

- **`run_risk_scan` 返回 0 条新风险，不能当成“系统健康”回复用户。**
  用户反映接口慢/报错时，风险报告只是其中一份证据，必须另外直接查指标、日志、Trace。
- 判断无规则覆盖的异常，用这三步（不依赖任何预设阈值）：
  1. **水位对比**—— `query_metrics` 拉当前值，与同一实例更长窗口的均值比，看偏离幅度；
  2. **时间对齐**—— 异常抬头的时刻与哪些事件（发布、扩缩容、流量突增）对得上；
  3. **拓扑位置**—— `get_topology` / `query_traces` 看谁在上游；
     多个服务同时变慢时，**共享依赖（数据库/缓存）比各服务自身更可能是根因**。
- 结论落在规则覆盖范围之外时，在回答里明说（“此项无内置规则覆盖，以下为指标/日志直接得出”），
  让用户知道这条结论的依据强度与规则命中不同；若值得长期监控，用 `create_risk_rule` 沉淀
  （见下文“AI 生成告警规则”）—— 这是把一次排查经验变成下次自动判定的唯一途径。
- 反过来也成立：**规则命中也不等于它就是根因**。API-001 大多数时候是别人的症状（慢查询、
  缓存失效、下游超时），直接拿它当结论会把真正的根因放过。

## 风险类型 → 治理工具速查

| 风险 | 治理动作 | 工具调用 |
|------|----------|----------|
| 单可用区部署 (HA-001) | 移除单区亲和+多区打散 | `patch_deployment(name, action=remove_zone_affinity)` |
| 单副本 (HA-002) | 扩副本到 >=2 | `patch_deployment(name, action=set_replicas, value="2")` |
| 缺探针 (HA-003) | 补全双探针 | `patch_deployment(name, action=add_probes)` |
| 缺 PDB (HA-004) | 创建 PDB | **`create_pdb(app=服务名, min_available=1)`** ← 参数名是 `app` 不是 `name` |
| 缺 CPU request (CAP-001) | 补 request | `patch_deployment(name, action=set_cpu_request, value="500m")` |
| 缺内存 limit (CAP-002) | 补 limit | `patch_deployment(name, action=set_memory_limit, value="2048Mi")` |
| CPU 超卖 >150% (CAP-003) | 下调 CPU limit | `patch_deployment(name, action=set_cpu_limit, value=...)`，需对多个服务执行，**算法见 `capacity_rules`** |
| RDS 内存高水位 (CAP-004) | 升配实例 | `upgrade_rds_instance(instance_id, target_spec)` |
| RDS 连接高水位 (DB-001) | 治本=治慢查询；治标=升配 | 先 `create_db_index`，必要时 `upgrade_rds_instance` |
| 无索引慢查询 (DB-002) | 建复合索引 | `create_db_index(table="orders", columns=["status","created_at"])` |
| 接口 P99/错误率劣化 (API-001) | 治理其根因（通常是 DB-002） | 先诊断确认根因再治理 |
| 缓存内存高水位 (CACHE-001) | 先查命中率/大 key，再考虑升配 | 详见 `cache_rules` |
| 缓存 CPU 高水位 (CACHE-002) | 查慢命令/热 key/回源风暴 | 详见 `cache_rules` |

## ⚠️ 治理动作存在顺序依赖：CAP-003 必须最后治

CAP-003 判定的是**命名空间级聚合**：`所有 Pod 的 CPU limit 合计 ÷ 节点可分配 CPU`。
它会被其他治理动作反向推高：

- **`set_replicas` 扩副本会增加 Pod 数量，从而抬高 CPU limit 合计。**
  实测（static 数据集）：order-service 副本 1→3，limit 合计 39300m→47300m（+8000m），
  超卖率 167.95%→202.14%，单次上升 **34.2 个百分点**。
  若先把 CAP-003 压到 150% 以下再扩副本，那 8000m 的下调等于白做。
- `set_cpu_request` 只改 request，**不影响** CAP-003（它只看 limit）。
- CAP-003 只统计 **default 命名空间**内的 Pod，`nginx-ingress` 等其他命名空间不计入。

**正确顺序**：

1. 先治所有单体配置类风险（HA-001/002/003/004、CAP-001/002、DB-002、CAP-004）；
2. `run_risk_scan` 复扫；
3. **最后**处理 CAP-003 —— 此时才知道扩副本后的真实超卖率；
4. 再复扫确认闭环。

若先治 CAP-003 再扩副本，会出现「刚压下去又被顶上来」，表现为反复治理同一条风险却始终不 resolved。

## 治理流程

1. 先 `get_risk_report` 或 `run_risk_scan` 拿到当前 open 风险；
2. 向用户列出计划执行的治理动作清单，**必须等用户确认**（用户明确说"执行/确认/都修了"才动手）；
3. **第一批**：逐项治理单体配置类风险（HA-*、CAP-001/002、CAP-004、DB-002），汇报每项结果；
4. `run_risk_scan` 复扫，看还剩哪些 open；
5. **第二批**：若 CAP-003 仍 open，按 `capacity_rules` 的算法计算下调幅度并执行，再复扫；
6. 输出闭环报告：对比治理前后 open/resolved 数量，说明每条风险的处置结果。

若某条风险复扫后仍 open，先读它最新的 `evidence` 判断是否被其他动作反向影响
（典型就是 CAP-003），不要盲目重复同一个动作。

## AI 生成告警规则

用户要新规则时：先用 `sql_query` 探查相关表的数据分布确定合理阈值，再调 `create_risk_rule`
（rule_id 用 `AI-xxx` 编号）。规则 SQL 必须返回**单行单列数值**。

例（Pod 重启次数告警）：

```sql
SELECT MAX(max) FROM metrics WHERE metric_name='pod.restart_count'
```

配 `threshold=3`、`compare=gt`。

> 注意 `metrics` 表的聚合列名是 `avg` / `max` / `min`，**不是** `avg_value` / `value`。
> 写错会得到 `no such column`（错误信息里会附真实列名）。
