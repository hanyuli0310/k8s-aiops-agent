"""U6 定时采集通道（拓扑类，默认 60s）

KTD-8 采集语义：
- K8s 资源 → **全量替换**（快照语义）
- CMS 指标与 5 类日志/trace → **append + 高水位增量**：
  各类型记录已采集最大 ts，只拉 from=高水位 之后的数据；
  重启后从库内 MAX(ts) 恢复高水位（不丢不重，无需额外游标表）

字段映射与 backend/app/ingest/pipeline.py 保持一致（同一套表结构）。
"""
from __future__ import annotations

import json
import logging

from . import config, db
from .client import MockClient

logger = logging.getLogger(__name__)

LOGSTORE_TABLES = {
    "nginx-ingress": "ingress_logs",
    "app-log": "app_logs",
    "rds_slow_log": "slow_logs",
    "k8s-events": "k8s_events",
    "trace": "trace_spans",
}


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class Watermark:
    """高水位：内存缓存 + 启动时从库内 MAX(ts) 恢复。"""

    def __init__(self):
        self._marks = {}

    def load_from_db(self):
        for logstore, table in LOGSTORE_TABLES.items():
            mx = db.scalar(f"SELECT MAX(ts) FROM {table}")
            self._marks[logstore] = int(mx) if mx else 0
        mx = db.scalar("SELECT MAX(ts) FROM metrics")
        self._marks["metrics"] = int(mx) if mx else 0
        logger.info("高水位恢复: %s", self._marks)

    def get(self, key: str) -> int:
        return self._marks.get(key, 0)

    def set(self, key: str, value: int):
        if value > self._marks.get(key, 0):
            self._marks[key] = value


async def collect_metrics(client: MockClient, wm: Watermark) -> int:
    """CMS 指标：按 (Namespace, MetricName) 逐个拉取，只要高水位之后的点。"""
    metric_list = await client.list_metrics()
    if not metric_list:
        return 0
    since_ms = wm.get("metrics")
    rows, max_ts = [], since_ms
    for m in metric_list:
        resp = await client.describe_metric_list(
            m["Namespace"], m["MetricName"],
            start_ms=since_ms + 1 if since_ms else None)
        if not resp or "Datapoints" not in resp:
            continue
        # 格式坑：Datapoints 是 JSON 字符串，必须二次解析
        for dp in json.loads(resp["Datapoints"]):
            ts = int(dp["timestamp"])
            if ts <= since_ms:
                continue
            dims = {k: v for k, v in dp.items()
                    if k not in ("timestamp", "Average", "Maximum", "Minimum", "Sum", "Value")}
            rows.append({
                "namespace": m["Namespace"], "metric_name": m["MetricName"],
                "dims_json": db.json_dump(dims), "ts": ts,
                "avg": dp.get("Average"), "max": dp.get("Maximum"), "min": dp.get("Minimum"),
            })
            max_ts = max(max_ts, ts)
    written = db.bulk_insert(db.metrics, rows)
    wm.set("metrics", max_ts)
    return written


def _map_ingress(r: dict) -> dict:
    return {
        "ts": _i(r["__time__"]), "method": r["method"], "url": r["url"],
        "status": _i(r["status"]), "request_time": _f(r["request_time"]),
        "upstream_addr": r.get("upstream_addr", ""),
        "upstream_status": _i(r.get("upstream_status")),
        "client_ip": r.get("client_ip", ""), "req_id": r["req_id"],
        "source_pod": r.get("__source__", ""), "raw_json": db.json_dump(r),
    }


def _map_app(r: dict) -> dict:
    return {"ts": _i(r["__time__"]), "source_pod": r["__source__"], "level": r["level"],
            "pod_ip": r.get("pod_ip", ""), "message": r["message"]}


def _map_slow(r: dict) -> dict:
    return {"ts": _i(r["__time__"]), "instance_id": r["instance_id"], "db_name": r["db_name"],
            "sql_text": r["sql_text"], "query_time": _f(r["query_time"]),
            "lock_time": _f(r["lock_time"]), "rows_examined": _i(r["rows_examined"]),
            "rows_sent": _i(r["rows_sent"]), "user_host": r.get("user_host", ""),
            "start_time": r.get("start_time", ""), "governed": 0}


def _map_event(r: dict) -> dict:
    return {"ts": _i(r["__time__"]), "event_type": r["event_type"], "reason": r["reason"],
            "message": r["message"], "namespace": r.get("namespace", ""),
            "involved_object_kind": r.get("involved_object_kind", ""),
            "involved_object_name": r.get("involved_object_name", "")}


def _map_span(s: dict) -> dict:
    # 格式坑：attribute/resource 是 JSON 字符串，入库时解开存 JSON 列
    return {
        "trace_id": s["traceID"], "span_id": s["spanID"], "parent_span_id": s["parentSpanID"],
        "service": s["service"], "host": s.get("host", ""), "name": s["name"], "kind": s["kind"],
        "start_us": s["start"], "duration_us": s["duration"],
        "status_code": s["statusCode"], "status_message": s.get("statusMessage", ""),
        "attr_json": db.json_dump(json.loads(s["attribute"] or "{}")),
        "resource_json": db.json_dump(json.loads(s["resource"] or "{}")),
        "ts": _i(s["__time__"]),
    }


MAPPERS = {
    "nginx-ingress": (_map_ingress, lambda: db.ingress_logs),
    "app-log": (_map_app, lambda: db.app_logs),
    "rds_slow_log": (_map_slow, lambda: db.slow_logs),
    "k8s-events": (_map_event, lambda: db.k8s_events),
    "trace": (_map_span, lambda: db.trace_spans),
}


async def collect_logstore(client: MockClient, wm: Watermark, logstore: str) -> int:
    """单个 logstore 的增量采集（分页拉全）。"""
    since = wm.get(logstore)
    mapper, table_fn = MAPPERS[logstore]
    rows, max_ts = [], since
    offset = 0
    while True:
        resp = await client.get_logs(logstore, from_s=since + 1 if since else None, offset=offset)
        if not resp or not resp.get("count"):
            break
        for raw in resp["logs"]:
            ts = _i(raw["__time__"])
            if ts <= since:
                continue
            rows.append(mapper(raw))
            max_ts = max(max_ts, ts)
        offset += resp["count"]
        if offset >= resp.get("total", 0):
            break
    written = db.bulk_insert(table_fn(), rows)
    wm.set(logstore, max_ts)
    return written


async def collect_k8s(client: MockClient) -> int:
    """K8s 资源：全量替换（KTD-8 例外项）。"""
    snapshot = await client.k8s_resources()
    if not snapshot:
        return 0
    rows = []
    for key in ("nodes", "deployments", "pods", "services", "poddisruptionbudgets"):
        for item in snapshot.get(key, {}).get("items", []):
            rows.append({
                "kind": item["kind"],
                "namespace": item["metadata"].get("namespace", ""),
                "name": item["metadata"]["name"],
                "spec_json": db.json_dump(item),
            })
    return db.replace_table(db.k8s_resources, rows)


async def run_once(client: MockClient, wm: Watermark) -> dict:
    """一轮定时采集。"""
    report = {"k8s_resources": await collect_k8s(client),
              "metrics": await collect_metrics(client, wm)}
    for logstore in LOGSTORE_TABLES:
        report[LOGSTORE_TABLES[logstore]] = await collect_logstore(client, wm, logstore)
    return report
