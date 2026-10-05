"""拓扑梳理工具：从 trace_spans 聚合服务调用边，写 topology_edges。

U9.5：live 模式下只聚合最近 RULE_WINDOW_MINUTES 的 span（append 模式下全表聚合
会越扫越慢，且旧数据会稀释故障期的错误率，拓扑红边出不来）。
"""
from __future__ import annotations

import math
import time

from .. import config, db
from .registry import tool


def _window_s() -> int:
    if not config.is_live():
        return 0
    return int(time.time() - config.RULE_WINDOW_MINUTES * 60)


@tool(
    "build_topology",
    "从 Trace 数据聚合服务调用拓扑：对每个 client span 按 (service, peer.service) 分组，"
    "统计调用次数、错误率、平均/P99 时延，结果写入 topology_edges 表并返回图数据（nodes + edges）。",
    writes_business_data=True, max_result_chars=4000
)
def build_topology():
    rows = db.fetch_all(
        """SELECT service, attr_json, duration_us, status_code
           FROM trace_spans WHERE kind = 'client' AND ts >= :w""", {"w": _window_s()})
    groups = {}
    for r in rows:
        attr = db.json_load(r["attr_json"]) or {}
        peer = attr.get("peer.service")
        if not peer:
            continue
        key = (r["service"], peer)
        g = groups.setdefault(key, {"durations": [], "errors": 0})
        g["durations"].append(r["duration_us"])
        if r["status_code"] == "ERROR":
            g["errors"] += 1

    edges = []
    for (src, dst), g in groups.items():
        ds = sorted(g["durations"])
        n = len(ds)
        edges.append({
            "source": src, "target": dst, "call_count": n,
            "error_rate": round(g["errors"] / n, 4),
            "avg_ms": round(sum(ds) / n / 1000, 1),
            "p99_ms": round(ds[math.ceil(0.99 * n) - 1] / 1000, 1),
        })
    edges.sort(key=lambda e: e["call_count"], reverse=True)

    db.execute("DELETE FROM topology_edges")
    db.bulk_insert(db.topology_edges, edges)

    services = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
    nodes = []
    for s in services:
        dep = db.fetch_one(
            "SELECT namespace, spec_json FROM k8s_resources WHERE kind='Deployment' AND name=:n", {"n": s})
        if dep:
            spec = db.json_load(dep["spec_json"])
            nodes.append({"id": s, "type": "service", "namespace": dep["namespace"],
                          "replicas": spec["spec"]["replicas"]})
        else:
            db_type = "mysql" if "mysql" in s else ("redis" if "redis" in s else "external")
            nodes.append({"id": s, "type": db_type, "namespace": None, "replicas": 1})
    return {"nodes": nodes, "edges": edges, "edge_count": len(edges)}


@tool(
    "get_topology",
    "读取已生成的服务拓扑（topology_edges 表）。若尚未生成请先调 build_topology。",
    is_read_only=True, concurrency_safe=True
)
def get_topology():
    edges = db.fetch_all(
        "SELECT source, target, call_count, error_rate, avg_ms, p99_ms FROM topology_edges ORDER BY call_count DESC")
    if not edges:
        return {"error": "拓扑尚未生成，请先调用 build_topology"}
    services = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
    return {"nodes": [{"id": s} for s in services], "edges": edges}
