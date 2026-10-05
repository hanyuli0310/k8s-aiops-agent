"""采集 pipeline：MockProvider（阿里云 API 形态）→ 解析 → 批量写数据库。"""
from __future__ import annotations

import json
import logging

from .. import db
from ..providers import mock_aliyun as provider

logger = logging.getLogger(__name__)

PAGE_SIZE = 2000


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


def ingest_cms() -> int:
    rows = []
    for m in provider.list_cms_metrics():
        resp = provider.describe_metric_list(m["Namespace"], m["MetricName"])
        # 阿里云格式坑：Datapoints 是 JSON 字符串，必须二次解析
        for dp in json.loads(resp["Datapoints"]):
            dims = {k: v for k, v in dp.items()
                    if k not in ("timestamp", "Average", "Maximum", "Minimum", "Sum", "Value")}
            rows.append({
                "namespace": m["Namespace"], "metric_name": m["MetricName"],
                "dims_json": dims, "ts": dp["timestamp"],
                "avg": dp.get("Average", dp.get("Sum", dp.get("Value"))),
                "max": dp.get("Maximum"), "min": dp.get("Minimum"),
            })
    db.bulk_insert(db.metrics, rows)
    return len(rows)


def _iter_logstore(logstore: str):
    offset = 0
    while True:
        resp = provider.get_logs(logstore, offset=offset, lines=PAGE_SIZE)
        if not resp["count"]:
            break
        yield from resp["logs"]
        offset += resp["count"]
        if offset >= resp["total"]:
            break


def ingest_ingress() -> int:
    rows = [{
        "ts": _i(r["__time__"]), "method": r["method"], "url": r["url"],
        "status": _i(r["status"]), "request_time": _f(r["request_time"]),
        "upstream_addr": r.get("upstream_addr", ""), "upstream_status": _i(r.get("upstream_status")),
        "client_ip": r.get("client_ip", ""), "req_id": r["req_id"],
        "source_pod": r.get("__source__", ""), "raw_json": r,
    } for r in _iter_logstore("nginx-ingress")]
    db.bulk_insert(db.ingress_logs, rows)
    return len(rows)


def ingest_traces() -> int:
    rows = [{
        "trace_id": s["traceID"], "span_id": s["spanID"], "parent_span_id": s["parentSpanID"],
        "service": s["service"], "host": s.get("host", ""), "name": s["name"], "kind": s["kind"],
        "start_us": s["start"], "duration_us": s["duration"],
        "status_code": s["statusCode"], "status_message": s.get("statusMessage", ""),
        # 格式坑：attribute/resource 是 JSON 字符串，入库时解开
        "attr_json": json.loads(s["attribute"] or "{}"),
        "resource_json": json.loads(s["resource"] or "{}"),
        "ts": s["__time__"],
    } for s in _iter_logstore("trace")]
    db.bulk_insert(db.trace_spans, rows)
    return len(rows)


def ingest_app_logs() -> int:
    rows = [{
        "ts": _i(r["__time__"]), "source_pod": r["__source__"], "level": r["level"],
        "pod_ip": r.get("pod_ip", ""), "message": r["message"],
    } for r in _iter_logstore("app-log")]
    db.bulk_insert(db.app_logs, rows)
    return len(rows)


def ingest_slow_logs() -> int:
    rows = [{
        "ts": _i(r["__time__"]), "instance_id": r["instance_id"], "db_name": r["db_name"],
        "sql_text": r["sql_text"], "query_time": _f(r["query_time"]), "lock_time": _f(r["lock_time"]),
        "rows_examined": _i(r["rows_examined"]), "rows_sent": _i(r["rows_sent"]),
        "user_host": r.get("user_host", ""), "start_time": r.get("start_time", ""), "governed": 0,
    } for r in _iter_logstore("rds_slow_log")]
    db.bulk_insert(db.slow_logs, rows)
    return len(rows)


def ingest_k8s_events() -> int:
    rows = [{
        "ts": _i(r["__time__"]), "event_type": r["event_type"], "reason": r["reason"],
        "message": r["message"], "namespace": r.get("namespace", ""),
        "involved_object_kind": r.get("involved_object_kind", ""),
        "involved_object_name": r.get("involved_object_name", ""),
    } for r in _iter_logstore("k8s-events")]
    db.bulk_insert(db.k8s_events, rows)
    return len(rows)


def ingest_k8s_resources() -> int:
    snapshot = provider.get_resources()
    rows = []
    for key in ("nodes", "deployments", "pods", "services", "poddisruptionbudgets"):
        for item in snapshot[key]["items"]:
            rows.append({
                "kind": item["kind"],
                "namespace": item["metadata"].get("namespace", ""),
                "name": item["metadata"]["name"],
                "spec_json": item,
            })
    db.bulk_insert(db.k8s_resources, rows)
    return len(rows)


def run_full_ingest() -> dict:
    """全量采集（幂等：先清空再入库），返回采集报告。"""
    db.init_db()
    for t in ("metrics", "ingress_logs", "trace_spans", "app_logs",
              "slow_logs", "k8s_events", "k8s_resources"):
        db.execute(f"DELETE FROM {t}")
    report = {
        "metrics": ingest_cms(),
        "ingress_logs": ingest_ingress(),
        "trace_spans": ingest_traces(),
        "app_logs": ingest_app_logs(),
        "slow_logs": ingest_slow_logs(),
        "k8s_events": ingest_k8s_events(),
        "k8s_resources": ingest_k8s_resources(),
    }
    logger.info("ingest done: %s", report)
    return report


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(run_full_ingest(), indent=2))
