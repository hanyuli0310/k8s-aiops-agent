"""K8s 资源快照渲染：由引擎的 service_state / instances 单向派生。

这是 spec v1.1 修订点 P0-3 的落地：世界状态是唯一真相源，k8s 快照每次请求时**现渲染**，
不存在"快照与世界状态两本账"的漂移问题。backend 的规则引擎照旧读这份快照即可。

产出结构与 data/data/k8s_resources.json 同构：nodes / deployments / pods / services /
poddisruptionbudgets 五个 kubectl 风格 List。
"""
from __future__ import annotations

from .. import world_def as W

DEFAULT_PROBE = {
    "httpGet": {"path": "/healthz", "port": 8080},
    "initialDelaySeconds": 10, "periodSeconds": 10, "timeoutSeconds": 3, "failureThreshold": 3,
}


def _resources(st: dict) -> dict:
    """只输出实际存在的键——配置缺陷表现为「键缺失」，不能是值为 0（规则用 in 判定）。"""
    out = {}
    if st["requests"]:
        out["requests"] = dict(st["requests"])
    if st["limits"]:
        out["limits"] = dict(st["limits"])
    return out


def render(engine) -> dict:
    nodes, deployments, pods, services, pdbs = [], [], [], [], []

    for node in W.NODES:
        nodes.append({
            "apiVersion": "v1", "kind": "Node",
            "metadata": {
                "name": node["name"],
                "labels": {
                    "kubernetes.io/hostname": node["name"],
                    "topology.kubernetes.io/region": W.REGION,
                    "topology.kubernetes.io/zone": node["zone"],
                    "node.kubernetes.io/instance-type": W.INSTANCE_TYPE,
                },
            },
            "status": {
                "capacity": dict(W.NODE_CAPACITY),
                "allocatable": dict(W.NODE_ALLOCATABLE),
                "addresses": [{"type": "InternalIP", "address": node["ip"]}],
                "nodeInfo": {"kubeletVersion": f"v{W.K8S_VERSION}.9-aliyun.1"},
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        })

    for svc, st in engine.service_state.items():
        cfg = W.SERVICES[svc]
        container = {
            "name": svc,
            "image": f"registry.cn-hangzhou.aliyuncs.com/prod/{svc}:{cfg['version']}",
            "ports": [{"containerPort": 8080, "protocol": "TCP"}],
            "resources": _resources(st),
        }
        if st["probes"]:
            container["livenessProbe"] = dict(DEFAULT_PROBE)
            container["readinessProbe"] = dict(DEFAULT_PROBE)
        pod_spec = {"containers": [container]}
        if st["zone_affinity"]:
            pod_spec["affinity"] = {"nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{
                    "matchExpressions": [{
                        "key": "topology.kubernetes.io/zone", "operator": "In",
                        "values": [st["zone_affinity"]],
                    }]}]}}}
        else:
            pod_spec["topologySpreadConstraints"] = [{
                "maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone",
                "whenUnsatisfiable": "ScheduleAnyway",
                "labelSelector": {"matchLabels": {"app": svc}},
            }]

        deployments.append({
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": svc, "namespace": cfg["namespace"], "labels": {"app": svc}},
            "spec": {
                "replicas": st["replicas"],
                "selector": {"matchLabels": {"app": svc}},
                "template": {"metadata": {"labels": {"app": svc}}, "spec": pod_spec},
            },
            "status": {"replicas": st["replicas"], "readyReplicas": st["healthy_replicas"],
                       "availableReplicas": st["healthy_replicas"]},
        })

        services.append({
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": svc, "namespace": cfg["namespace"]},
            "spec": {"selector": {"app": svc}, "type": "ClusterIP",
                     "ports": [{"port": 80 if cfg["kind"] == "ingress" else 8080,
                                "targetPort": 8080, "protocol": "TCP"}]},
        })

        if st["pdb"]:
            pdbs.append({
                "apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
                "metadata": {"name": f"{svc}-pdb", "namespace": cfg["namespace"]},
                "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app": svc}}},
            })

    for name, inst in engine.instances.items():
        if inst["kind"] != "pod":
            continue
        svc = inst["service"]
        st = engine.service_state[svc]
        container = {"name": svc,
                     "image": f"registry.cn-hangzhou.aliyuncs.com/prod/{svc}:{W.SERVICES[svc]['version']}",
                     "resources": _resources(st)}
        if st["probes"]:
            container["livenessProbe"] = dict(DEFAULT_PROBE)
            container["readinessProbe"] = dict(DEFAULT_PROBE)
        pods.append({
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": inst["namespace"], "labels": {"app": svc}},
            "spec": {"nodeName": inst["node"], "containers": [container]},
            "status": {
                "podIP": inst["ip"],
                "phase": "Running" if inst.get("healthy", True) else "CrashLoopBackOff",
                "containerStatuses": [{"name": svc, "restartCount": inst["restart_count"],
                                       "ready": inst.get("healthy", True)}],
            },
        })

    return {
        "generated_at": engine.tick_ts_ms,
        "cluster": {"name": W.CLUSTER, "region": W.REGION, "version": W.K8S_VERSION},
        "nodes": {"apiVersion": "v1", "kind": "List", "items": nodes},
        "deployments": {"apiVersion": "v1", "kind": "List", "items": deployments},
        "pods": {"apiVersion": "v1", "kind": "List", "items": pods},
        "services": {"apiVersion": "v1", "kind": "List", "items": services},
        "poddisruptionbudgets": {"apiVersion": "v1", "kind": "List", "items": pdbs},
    }
