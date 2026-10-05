"""U3-b 治理动作生效与指数恢复曲线

backend 的治理工具执行成功后，通过 POST /control/actions 把动作转发到这里，
动作直接改世界可变状态（world_version +1），并让相关故障效应转入指数衰减
（半衰期 RECOVERY_HALFLIFE_TICKS 拍）——这样"治理 → 指标回落 → 复扫 resolved"
是数据自然演算的结果，而不是靠标记硬置。

action_type 与 backend 治理工具的映射（U11 负责转发）：
    patch_deployment(set_replicas)      -> scale_out
    patch_deployment(add_probes/...)    -> patch_resources
    patch_deployment(set_cpu_limit)     -> patch_resources
    create_db_index                     -> create_db_index
    upgrade_rds_instance                -> upgrade_rds
    create_pdb                          -> patch_resources
    （另支持 restart_pod：重启实例清零内存泄漏）
"""
from __future__ import annotations

from typing import Dict

from . import config, world_def as W

ACTION_TYPES = ["scale_out", "restart_pod", "create_db_index", "upgrade_rds", "patch_resources"]


def apply_action(engine, action_type: str, target: str, params: Dict = None,
                 source: str = "backend-agent") -> dict:
    """整段持锁：治理动作是「读状态 → 改 service_state/effects → 记审计」的复合
    操作，中途被 tick 插入会算出前后不一致的结果（如超卖率）。"""
    with engine.lock:
        return _apply_action_locked(engine, action_type, target, params, source)


def _apply_action_locked(engine, action_type: str, target: str, params: Dict = None,
                         source: str = "backend-agent") -> dict:
    params = params or {}
    if action_type not in ACTION_TYPES:
        return {"accepted": False, "error": f"unknown action_type: {action_type}",
                "supported": ACTION_TYPES}

    handler = {
        "scale_out": _scale_out,
        "restart_pod": _restart_pod,
        "create_db_index": _create_db_index,
        "upgrade_rds": _upgrade_rds,
        "patch_resources": _patch_resources,
    }[action_type]
    result = handler(engine, target, params)
    if not result.get("accepted", True):
        return result

    engine.record_audit("action", f"{action_type} target={target} params={params} src={source}")
    entry = {"action_type": action_type, "target": target, "params": params, "source": source,
             "tick": engine.tick_no, "ts_ms": engine.tick_ts_ms,
             "effect": result["effect"], "world_version": engine.world_version}
    engine.recent_actions.appendleft(entry)
    return {"accepted": True, "effect": result["effect"],
            "world_version": engine.world_version, "target": target}


def _decay_target_effects(engine, target: str):
    """让目标实体及其上游的故障效应开始衰减（治理见效）。"""
    engine.start_decay(target)
    for up in W.UPSTREAM.get(target, []):
        engine.start_decay(up)
    # 实例级 target（Pod 名）时，同步衰减其所属服务
    inst = engine.instances.get(target)
    if inst:
        engine.start_decay(inst["service"])


def _scale_out(engine, target: str, params: Dict) -> dict:
    if target not in engine.service_state:
        return {"accepted": False, "error": f"service not found: {target}"}
    st = engine.service_state[target]
    old = st["replicas"]
    new = int(params.get("replicas", old + 1))
    st["replicas"] = new

    # 同步实例列表：扩容时新 Pod 优先落到未覆盖的可用区（治理 HA-001 单可用区）
    existing = [i for i in engine.instances.values()
                if i["service"] == target and i["kind"] == "pod"]
    covered = {i["zone"] for i in existing}
    if new > len(existing):
        spare_nodes = [n for n in W.NODES if n["zone"] not in covered] or W.NODES
        for k in range(new - len(existing)):
            node = spare_nodes[k % len(spare_nodes)]
            pod_name = f"{target}-scaled-{len(existing) + k + 1}"
            cfg = W.SERVICES[target]
            engine.instances[pod_name] = {
                "instance": pod_name, "service": target, "kind": "pod",
                "node": node["name"], "zone": node["zone"],
                "ip": W.pod_ip(node["name"], 20 + k), "namespace": cfg["namespace"],
                "cpu_pct": cfg["cpu_base_pct"], "mem_pct": cfg["mem_base_pct"], "conn_pct": 0.0,
                "bandwidth_in_mbps": cfg["bandwidth_base_mbps"] * 0.5,
                "bandwidth_out_mbps": cfg["bandwidth_base_mbps"] * 0.4,
                "bandwidth_capacity_mbps": cfg["bandwidth_capacity_mbps"],
                "restart_count": 0, "status": "normal", "healthy": True,
            }
            engine.emit_k8s_event("Normal", "Scheduled",
                                  f"Successfully assigned default/{pod_name} to {node['name']}",
                                  "Pod", pod_name)
    elif new < len(existing):
        for inst in existing[new:]:
            engine.instances.pop(inst["instance"], None)

    st["healthy_replicas"] = new
    for inst in engine.instances.values():
        if inst["service"] == target and inst["kind"] == "pod":
            inst["healthy"] = True
            inst["status"] = "normal"
    # 副本增多 → 单实例负载下降；同时故障效应开始衰减
    _decay_target_effects(engine, target)
    engine.emit_k8s_event("Normal", "ScalingReplicaSet",
                          f"Scaled up replica set {target} to {new}", "Deployment", target)
    return {"effect": f"replicas {old}->{new}，新 Pod 已按可用区打散，"
                      f"预计 {config.RECOVERY_HALFLIFE_TICKS * 2} 拍内水位回落"}


def _restart_pod(engine, target: str, params: Dict) -> dict:
    """重启：清零内存泄漏类效应，恢复实例健康。target 可为服务名或实例名。"""
    victims = [i for i in engine.instances.values()
               if i["kind"] == "pod" and (i["instance"] == target or i["service"] == target)]
    if not victims:
        return {"accepted": False, "error": f"no pod matched: {target}"}
    for inst in victims:
        inst["healthy"] = True
        inst["status"] = "normal"
        inst["restart_count"] += 1
        engine.start_decay(inst["instance"])
        engine.emit_k8s_event("Normal", "Started",
                              f"Started container after manual restart: {inst['instance']}",
                              "Pod", inst["instance"])
    svc = victims[0]["service"]
    engine.service_state[svc]["healthy_replicas"] = engine.service_state[svc]["replicas"]
    _decay_target_effects(engine, svc)
    return {"effect": f"重启 {len(victims)} 个实例，内存泄漏效应清零，实例恢复健康"}


def _create_db_index(engine, target: str, params: Dict) -> dict:
    """建索引：把表移出慢查询表，慢日志停止产出，DB 时延回落。"""
    if target not in engine.datastore_state:
        # 允许传表名（如 orders），自动定位所属实例
        for ds, st in engine.datastore_state.items():
            if params.get("table") in st["slow_query_tables"] or target in st["slow_query_tables"]:
                target = ds
                break
        else:
            target = "rds-mysql-order"
    st = engine.datastore_state[target]
    table = params.get("table") or (st["slow_query_tables"][0] if st["slow_query_tables"] else "orders")
    columns = params.get("columns") or ["status", "created_at"]
    if table in st["slow_query_tables"]:
        st["slow_query_tables"].remove(table)
    if table not in st["indexed_tables"]:
        st["indexed_tables"].append(table)
    _decay_target_effects(engine, target)
    idx = f"idx_{table}_{'_'.join(columns)}"
    return {"effect": f"已建索引 {idx}，{table} 表全表扫描消除，慢日志停止产出，"
                      f"连接占用释放（约 4 拍内回落至基线）"}


def _upgrade_rds(engine, target: str, params: Dict) -> dict:
    if target not in engine.datastore_state:
        return {"accepted": False, "error": f"datastore not found: {target}"}
    st = engine.datastore_state[target]
    old = st["max_connections"]
    new = int(params.get("max_connections", old * 2))
    st["max_connections"] = new
    # 连接使用率 = 连接数/上限，上限翻倍 ⇒ 使用率对半
    engine.set_effect(target, "conn_pct", mul=old / new, source="action:upgrade_rds")
    engine.set_effect(target, "mem_pct", mul=0.72, source="action:upgrade_rds")
    _decay_target_effects(engine, target)
    return {"effect": f"max_connections {old}->{new}，连接与内存使用率约对半回落"}


def _patch_resources(engine, target: str, params: Dict) -> dict:
    """配置类修复：探针 / PDB / requests / limits / 可用区打散。"""
    if target not in engine.service_state:
        return {"accepted": False, "error": f"service not found: {target}"}
    st = engine.service_state[target]
    changes = []

    if params.get("add_probes"):
        st["probes"] = True
        changes.append("补全 liveness/readiness 探针")
    if params.get("pdb"):
        st["pdb"] = True
        changes.append("创建 PodDisruptionBudget")
    if "cpu_request" in params:
        st["requests"]["cpu"] = params["cpu_request"]
        changes.append(f"requests.cpu={params['cpu_request']}")
    if "memory_limit" in params:
        st["limits"]["memory"] = params["memory_limit"]
        # 内存 limit 补齐 ⇒ OOM 类内存效应衰减
        engine.start_decay(target, "mem_pct")
        changes.append(f"limits.memory={params['memory_limit']}")
    if "cpu_limit" in params:
        st["limits"]["cpu"] = params["cpu_limit"]
        changes.append(f"limits.cpu={params['cpu_limit']}（超卖率下降）")
    if params.get("remove_zone_affinity"):
        st["zone_affinity"] = None
        zones = sorted({n["zone"] for n in W.NODES})
        pods = [i for i in engine.instances.values()
                if i["service"] == target and i["kind"] == "pod"]
        for idx, inst in enumerate(pods):
            zone = zones[idx % len(zones)]
            node = next(n for n in W.NODES if n["zone"] == zone)
            inst["node"], inst["zone"] = node["name"], zone
        changes.append(f"移除单区亲和，{len(pods)} 副本打散至 {len(zones)} 个可用区")

    if not changes:
        return {"accepted": False, "error": "patch_resources 未提供任何可识别参数",
                "supported_params": ["add_probes", "pdb", "cpu_request", "memory_limit",
                                     "cpu_limit", "remove_zone_affinity"]}
    _decay_target_effects(engine, target)
    oversale = engine.current_oversale_pct()
    return {"effect": "；".join(changes) + f"（当前 CPU 超卖率 {oversale:.1f}%）"}
