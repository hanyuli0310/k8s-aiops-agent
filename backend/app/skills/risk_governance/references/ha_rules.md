---
name: ha_rules
description: 高可用域四条规则（HA-001~004）的判定依据、evidence 字段与治理注意点
---

# 高可用域细则（HA-001 ~ HA-004）

四条都是**单体配置类**风险：判定只看单个 Deployment 自己的配置，
互不影响，可并行治理，且不会像 CAP-003 那样被其他动作反向推高。

## HA-001 单可用区部署（P1）

**判定**：某 Deployment 的全部副本落在同一个可用区，或存在钉死单区的 `nodeAffinity`。

**evidence 字段**

| 字段 | 含义 |
|---|---|
| `replicas` | 当前副本数 |
| `zones` | 副本实际分布的可用区列表（长度为 1 即命中） |
| `node_affinity` | 是否存在钉死单区的亲和规则 |

**治理**：`patch_deployment(name, action="remove_zone_affinity")`
—— 该动作会移除单区 nodeAffinity 并加上多可用区打散约束。

> 单副本服务也会命中 HA-001（1 个副本必然在 1 个区）。此时先治 HA-002 扩副本，
> 新 Pod 会自动按可用区打散，HA-001 往往随之 resolved —— 先扩副本能省一次动作。

## HA-002 单副本运行（P1）

**判定**：`replicas == 1`。单点故障，节点下线即服务不可用。

**治理**：`patch_deployment(name, action="set_replicas", value="2")`

> ⚠️ 扩副本会抬高 CAP-003 的 CPU limit 合计（实测单服务 1→3 抬升 34.2 个百分点）。
> 这是"CAP-003 必须最后治"的直接原因，详见总纲与 `capacity_rules`。

## HA-003 缺失探针（P2）

**判定**：缺 `livenessProbe` 或 `readinessProbe`（缺任一即命中）。

**evidence**：`missing_probes` —— 缺哪些探针的列表。

**治理**：`patch_deployment(name, action="add_probes")` 一次补全双探针。

> 缺 readiness 的实际后果是**滚动更新期间流量打到未就绪 Pod**，
> 表现为发布时错误率短时飙升。若同时看到 API-001，要考虑这条是不是诱因。

## HA-004 缺失 PodDisruptionBudget（P2）

**判定**：该服务没有匹配的 PDB。节点维护/驱逐时可能一次性摘掉全部副本。

**evidence**：`pdb_covered_apps` —— 当前已被 PDB 覆盖的 app 列表（本服务不在其中即命中）。

**治理**：`create_pdb(app=服务名, min_available=1)`

> **参数名是 `app`，不是 `name`** —— 与其他治理工具不一致，这是本项目的既有接口差异。
> 传错会得到参数校验失败（不会静默成功）。

## 治理顺序建议（域内）

1. **HA-002** 先做（扩副本可能顺带解决 HA-001）
2. HA-001（若扩副本后仍 open）
3. HA-003 / HA-004（互不影响，可任意顺序）

四条全部完成后 `run_risk_scan` 复扫，再去处理 CAP-003。
