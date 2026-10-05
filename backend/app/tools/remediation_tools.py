"""治理工具（模拟执行）：直接修改数据库内的 K8s 资源快照 / 打治理标记，
使 run_risk_scan 复扫时对应风险转为 resolved，形成可验证的治理闭环。

U11（spec v1.1）：live 模式下每个工具成功后额外把动作转发到 mock_server
（工具名→action_type 映射见 _forward），mock 世界状态变更后指标自然恢复，
下轮采集+扫描即可观察到 finding 自然消除；转发失败不影响工具返回值。
对接真实集群时替换为 kubectl patch / RDS API 调用。
"""
from __future__ import annotations

import json
import time

from .. import db
from ..providers import mock_control
from .registry import tool

# patch_deployment 的 action → mock 控制面 (action_type, params 构造器)
_PATCH_FORWARD = {
    "set_replicas": lambda v: ("scale_out", {"replicas": int(v or 2)}),
    "add_probes": lambda v: ("patch_resources", {"add_probes": True}),
    "set_cpu_request": lambda v: ("patch_resources", {"cpu_request": v or "500m"}),
    "set_memory_limit": lambda v: ("patch_resources", {"memory_limit": v or "2048Mi"}),
    "set_cpu_limit": lambda v: ("patch_resources", {"cpu_limit": v or "2000m"}),
    "remove_zone_affinity": lambda v: ("patch_resources", {"remove_zone_affinity": True}),
}


def _get_deployment(name: str):
    row = db.fetch_one(
        "SELECT id, namespace, spec_json FROM k8s_resources WHERE kind='Deployment' AND name=:n", {"n": name})
    if not row:
        return None, None
    return row, db.json_load(row["spec_json"])


def _save_deployment(row_id: int, spec: dict):
    db.execute("UPDATE k8s_resources SET spec_json=:s WHERE id=:id",
               {"s": json.dumps(spec, ensure_ascii=False), "id": row_id})


def _record_governance(key: str, content: str):
    """记录治理动作到长期记忆。

    走 memory.remember() 的 upsert 而非裸 INSERT —— key 已是资源级
    （patch:{name}:{action} / pdb:{app} / db_index:{table} / rds_upgrade:{id}），
    同一资源重复治理只更新最新状态。裸 INSERT 会让 governance 记录无限增长，
    把 recall() 的名额挤满，诊断结论反而被排出上下文。
    """
    from ..harness import memory
    memory.remember("governance", key, content)


DEFAULT_PROBE = {
    "httpGet": {"path": "/healthz", "port": 8080},
    "initialDelaySeconds": 10, "periodSeconds": 10, "timeoutSeconds": 3, "failureThreshold": 3,
}


@tool(
    "patch_deployment",
    "修补 Deployment 配置（模拟 kubectl patch）。支持的动作："
    "set_replicas（扩缩副本，新 Pod 自动按可用区打散）、add_probes（补全存活/就绪探针）、"
    "set_cpu_request（补 CPU request，如 '500m'）、set_memory_limit（补内存 limit，如 '2048Mi'）、"
    "set_cpu_limit（下调 CPU limit 治理超卖，如 '2000m'）、"
    "remove_zone_affinity（移除钉死单可用区的 nodeAffinity 并加多可用区打散约束）。",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Deployment 名"},
            "action": {"type": "string",
                       "enum": ["set_replicas", "add_probes", "set_cpu_request",
                                "set_memory_limit", "set_cpu_limit", "remove_zone_affinity"]},
            "value": {"type": "string", "description": "set_replicas 传数字；set_cpu_request/set_cpu_limit 如 500m；set_memory_limit 如 2048Mi"},
        },
        "required": ["name", "action"],
    },
    is_destructive=True,
    check_permissions=lambda a: "ask",
    audit_repr=lambda a: f"patch {a.get('name')} {a.get('action')}={a.get('value')}",
)
def patch_deployment(name: str, action: str, value: str = None):
    row, spec = _get_deployment(name)
    if not spec:
        return {"error": f"Deployment {name} 不存在"}
    pod_spec = spec["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    changed = ""

    if action == "set_replicas":
        replicas = int(value or 2)
        old = spec["spec"]["replicas"]
        spec["spec"]["replicas"] = replicas
        _sync_pod_replicas(name, row["namespace"], replicas, container)
        changed = f"replicas: {old} -> {replicas}（新 Pod 已按可用区打散调度）"
    elif action == "add_probes":
        added = []
        for probe in ("livenessProbe", "readinessProbe"):
            if probe not in container:
                container[probe] = dict(DEFAULT_PROBE)
                added.append(probe)
        changed = f"补全探针: {added}" if added else "探针已齐全，无需修改"
    elif action == "set_cpu_request":
        container.setdefault("resources", {}).setdefault("requests", {})["cpu"] = value or "500m"
        _sync_pod_resources(name, "requests", "cpu", value or "500m")
        changed = f"requests.cpu = {value or '500m'}"
    elif action == "set_memory_limit":
        container.setdefault("resources", {}).setdefault("limits", {})["memory"] = value or "2048Mi"
        _sync_pod_resources(name, "limits", "memory", value or "2048Mi")
        changed = f"limits.memory = {value or '2048Mi'}"
    elif action == "set_cpu_limit":
        container.setdefault("resources", {}).setdefault("limits", {})["cpu"] = value or "2000m"
        _sync_pod_resources(name, "limits", "cpu", value or "2000m")
        changed = f"limits.cpu = {value or '2000m'}"
    elif action == "remove_zone_affinity":
        pod_spec.pop("affinity", None)
        pod_spec["topologySpreadConstraints"] = [{
            "maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone",
            "whenUnsatisfiable": "DoNotSchedule",
            "labelSelector": {"matchLabels": {"app": name}},
        }]
        changed = "移除 nodeAffinity，新增按可用区打散的 topologySpreadConstraints"
        _respread_pods(name)

    _save_deployment(row["id"], spec)
    _record_governance(f"patch:{name}:{action}", changed)
    # U11：动作反馈到 mock 世界（live 模式；失败仅 warning）
    fwd = None
    if action in _PATCH_FORWARD:
        action_type, params = _PATCH_FORWARD[action](value)
        fwd = mock_control.forward_action(action_type, name, params)
    return {"status": "patched", "deployment": name, "action": action, "change": changed,
            "mock_feedback": (fwd or {}).get("effect") or (fwd or {}).get("skipped"),
            "note": "已更新集群快照，可 run_risk_scan 复扫验证"}


def _zone_nodes() -> dict:
    nodes = db.fetch_all("SELECT spec_json FROM k8s_resources WHERE kind='Node'")
    out = {}
    for n in nodes:
        spec = db.json_load(n["spec_json"])
        zone = spec["metadata"]["labels"].get("topology.kubernetes.io/zone")
        out.setdefault(zone, []).append(spec["metadata"]["name"])
    return out


def _app_pods() -> list:
    return db.fetch_all("SELECT id, name, spec_json FROM k8s_resources WHERE kind='Pod'")


def _sync_pod_replicas(app: str, namespace: str, replicas: int, container_tpl: dict):
    """模拟调度器：副本数变化后同步 Pod 快照，新 Pod 优先落到未覆盖的可用区。"""
    zone_nodes = _zone_nodes()
    node_zone = {n: z for z, ns in zone_nodes.items() for n in ns}
    pods = []
    for p in _app_pods():
        spec = db.json_load(p["spec_json"])
        if spec["metadata"].get("labels", {}).get("app") == app:
            pods.append((p["id"], spec))
    if len(pods) > replicas:  # 缩容：删多余 Pod
        for pid, _ in pods[replicas:]:
            db.execute("DELETE FROM k8s_resources WHERE id=:id", {"id": pid})
        return
    covered = {node_zone.get(spec["spec"].get("nodeName")) for _, spec in pods}
    zones_sorted = sorted(zone_nodes, key=lambda z: (z in covered, z))  # 未覆盖可用区优先
    for i in range(len(pods), replicas):
        zone = zones_sorted[i % len(zones_sorted)]
        pod_name = f"{app}-scaled-{i + 1}"
        pod = {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": namespace, "labels": {"app": app}},
            "spec": {"nodeName": zone_nodes[zone][0],
                     "containers": [{"name": app, "resources": json.loads(json.dumps(container_tpl.get("resources", {})))}]},
            "status": {"podIP": f"10.244.9.{i + 10}", "phase": "Running"},
        }
        db.execute(
            "INSERT INTO k8s_resources (kind, namespace, name, spec_json) VALUES ('Pod', :ns, :n, :s)",
            {"ns": namespace, "n": pod_name, "s": json.dumps(pod, ensure_ascii=False)})


def _sync_pod_resources(app: str, section: str, key: str, value: str):
    """Deployment 改资源后同步到存量 Pod 快照（保证超卖率复算闭环）。"""
    for p in _app_pods():
        spec = db.json_load(p["spec_json"])
        if spec["metadata"].get("labels", {}).get("app") != app:
            continue
        for c in spec["spec"]["containers"]:
            c.setdefault("resources", {}).setdefault(section, {})[key] = value
        db.execute("UPDATE k8s_resources SET spec_json=:s WHERE id=:id",
                   {"s": json.dumps(spec, ensure_ascii=False), "id": p["id"]})


def _respread_pods(app: str):
    """移除单区亲和后，模拟调度器把该 app 的 Pod 重新打散到多个可用区。"""
    zone_nodes = _zone_nodes()
    zones = sorted(zone_nodes)
    idx = 0
    for p in _app_pods():
        spec = db.json_load(p["spec_json"])
        if spec["metadata"].get("labels", {}).get("app") != app:
            continue
        zone = zones[idx % len(zones)]
        spec["spec"]["nodeName"] = zone_nodes[zone][0]
        db.execute("UPDATE k8s_resources SET spec_json=:s WHERE id=:id",
                   {"s": json.dumps(spec, ensure_ascii=False), "id": p["id"]})
        idx += 1


@tool(
    "create_pdb",
    "为指定应用创建 PodDisruptionBudget（模拟 kubectl apply），minAvailable 默认 1。",
    {
        "type": "object",
        "properties": {
            "app": {"type": "string", "description": "应用名（app 标签）"},
            "min_available": {"type": "integer", "description": "默认 1"},
        },
        "required": ["app"],
    },
    is_destructive=True,
    check_permissions=lambda a: "ask",
    audit_repr=lambda a: f"创建 PDB for {a.get('app')} minAvailable={a.get('min_available', 1)}",
)
def create_pdb(app: str, min_available: int = 1):
    dep = db.fetch_one(
        "SELECT namespace FROM k8s_resources WHERE kind='Deployment' AND name=:n", {"n": app})
    if not dep:
        return {"error": f"Deployment {app} 不存在"}
    name = f"{app}-pdb"
    if db.fetch_one("SELECT id FROM k8s_resources WHERE kind='PodDisruptionBudget' AND name=:n", {"n": name}):
        return {"status": "exists", "pdb": name}
    pdb = {
        "apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
        "metadata": {"name": name, "namespace": dep["namespace"]},
        "spec": {"minAvailable": min_available, "selector": {"matchLabels": {"app": app}}},
    }
    db.execute(
        "INSERT INTO k8s_resources (kind, namespace, name, spec_json) VALUES ('PodDisruptionBudget', :ns, :n, :s)",
        {"ns": dep["namespace"], "n": name, "s": json.dumps(pdb, ensure_ascii=False)})
    _record_governance(f"pdb:{app}", f"创建 PDB {name} minAvailable={min_available}")
    fwd = mock_control.forward_action("patch_resources", app, {"pdb": True})
    return {"status": "created", "pdb": name, "min_available": min_available,
            "mock_feedback": fwd.get("effect") or fwd.get("skipped"),
            "note": "已写入集群快照，可 run_risk_scan 复扫验证"}


@tool(
    "create_db_index",
    "为数据库表创建索引（模拟 DDL 执行），治理无索引慢查询。执行后慢日志打上已治理标记，"
    "DB-002/DB-001/API-001 在复扫时转为 resolved。",
    {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "表名，如 orders"},
            "columns": {"type": "array", "items": {"type": "string"},
                        "description": "索引列，如 [\"status\", \"created_at\"]"},
        },
        "required": ["table", "columns"],
    },
    is_destructive=True,
    check_permissions=lambda a: "ask",
    audit_repr=lambda a: f"建索引 {a.get('table')}({','.join(a.get('columns') or [])})",
)
def create_db_index(table: str, columns: list):
    idx_name = f"idx_{table}_{'_'.join(columns)}"
    ddl = f"CREATE INDEX {idx_name} ON {table}({', '.join(columns)})"
    affected = db.execute("UPDATE slow_logs SET governed=1 WHERE sql_text LIKE :t",
                          {"t": f"%{table}%"})
    _record_governance(f"db_index:{table}", f"已执行 {ddl}，治理慢日志 {affected} 条")
    fwd = mock_control.forward_action("create_db_index", table,
                                      {"table": table, "columns": columns})
    return {"status": "created", "index": idx_name, "ddl": ddl,
            "governed_slow_logs": affected,
            "mock_feedback": fwd.get("effect") or fwd.get("skipped"),
            "note": "慢查询已打治理标记，复扫时 DB-002 及关联风险转为 resolved"}


@tool(
    "upgrade_rds_instance",
    "升配数据库或缓存实例规格（模拟执行），用于内存/连接高水位（CAP-004/DB-001/CACHE-001）。"
    "它是治标手段：若根因是慢查询或缓存回源风暴，水位会再次涨回去，应先治根因。",
    {
        "type": "object",
        "properties": {
            # 不举具体实例名：原来写的示例 `rds-mysql-01` 只存在于旧 static 数据集，
            # live 世界里叫 rds-mysql-order / rds-mysql-core。工具 schema 会进系统层证据池，
            # 模型引用这个名字时事实核对认为它"有出处"而不报警 —— 典型的幻觉源。
            # 改成引导从数据里取名字，世界再演进也不会再过期。
            "instance_id": {"type": "string",
                            "description": "实例 ID，取自风险 evidence 的 resource_ref "
                                           "或 query_metrics 结果的 instanceId，不要凭记忆填"},
            "target_spec": {"type": "string", "description": "目标规格描述，如 '8C32G' 或 'max_connections上调至2000'"},
        },
        "required": ["instance_id"],
    },
    is_destructive=True,
    check_permissions=lambda a: "ask",
    audit_repr=lambda a: f"RDS 升配 {a.get('instance_id')} → {a.get('target_spec') or '默认规格'}",
)
def upgrade_rds_instance(instance_id: str, target_spec: str = "内存翻倍升配"):
    _record_governance(f"rds_upgrade:{instance_id}", f"升配 {instance_id}：{target_spec}")
    fwd = mock_control.forward_action("upgrade_rds", instance_id, {})
    return {"status": "upgraded", "instance": instance_id, "target_spec": target_spec,
            "mock_feedback": fwd.get("effect") or fwd.get("skipped"),
            "note": "升配已提交，水位回落后复扫 CAP-004/DB-001 转为 resolved"}


@tool(
    "list_governance_actions",
    "列出已执行的全部治理动作记录。",
    is_read_only=True, concurrency_safe=True
)
def list_governance_actions():
    rows = db.fetch_all(
        "SELECT mem_key, content, created_at FROM agent_memory WHERE scope='governance' ORDER BY created_at DESC")
    return {"actions": rows}
