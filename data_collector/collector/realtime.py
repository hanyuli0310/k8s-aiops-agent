"""U7 实时采集通道（水位/容量/带宽，默认 10s）

把 /realtime/metrics 的嵌套响应**展平**为 realtime_metrics 行：每实例每指标一行。
指标枚举（契约 C）：cpu_pct / mem_pct / conn_pct / capacity_pct / replicas / max_conn /
bandwidth_in_mbps / bandwidth_out_mbps / bandwidth_usage_pct
"""
from __future__ import annotations

import logging

from . import db
from .client import MockClient

logger = logging.getLogger(__name__)

# (指标名, 取值路径) —— 路径为 (组名, 字段名)
METRIC_PATHS = [
    ("cpu_pct", ("water_level", "cpu_pct")),
    ("mem_pct", ("water_level", "mem_pct")),
    ("conn_pct", ("water_level", "conn_pct")),
    ("capacity_pct", ("capacity", "capacity_pct")),
    ("replicas", ("capacity", "replicas")),
    ("max_conn", ("capacity", "max_conn")),
    ("bandwidth_in_mbps", ("bandwidth", "in_mbps")),
    ("bandwidth_out_mbps", ("bandwidth", "out_mbps")),
    ("bandwidth_usage_pct", ("bandwidth", "usage_pct")),
]


def flatten(resp: dict) -> list:
    """一次响应 → 展平行列表（行数 = 实例数 × 有值指标数）。"""
    ts = resp["ts"]
    rows = []
    for item in resp.get("items", []):
        for metric, (group, field) in METRIC_PATHS:
            value = item.get(group, {}).get(field)
            if value is None:      # 如 Pod 无 max_conn、Pod 的 conn_pct 恒 0 也保留（便于统一查询）
                continue
            rows.append({
                "ts": ts, "service": item["service"], "instance": item["instance"],
                "kind": item["kind"], "metric": metric, "value": float(value),
                "status": item["status"], "raw_json": None,
            })
    return rows


async def run_once(client: MockClient) -> int:
    resp = await client.realtime_metrics()
    if not resp or "items" not in resp:
        return 0
    rows = flatten(resp)
    return db.bulk_insert(db.realtime_metrics, rows)
