"""MockProvider：读本地 mock 数据集，按阿里云 API 形态对外提供数据。

- CMS: describe_metric_list 对齐 DescribeMetricList（Datapoints 为 JSON 字符串，需二次解析）
- SLS: get_logs 对齐 GetLogs（返回 dict 列表，值为字符串），分页
- K8s: get_resources 返回 kubectl 风格快照
接真实环境时仅需替换本文件为真实 SDK 调用，上层 ingest/tools 不改。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from .. import config

LOGSTORE_FILES = {
    "nginx-ingress": "sls_ingress_logs.jsonl",
    "k8s-events": "sls_k8s_events.jsonl",
    "app-log": "sls_app_logs.jsonl",
    "rds_slow_log": "sls_rds_slowlog.jsonl",
    "trace": "sls_trace_spans.jsonl",
}


def _data_dir() -> Path:
    return config.MOCK_DATA_DIR


@lru_cache(maxsize=8)
def _load_json(filename: str):
    return json.loads((_data_dir() / filename).read_text())


@lru_cache(maxsize=8)
def _load_jsonl(filename: str) -> tuple:
    lines = (_data_dir() / filename).read_text().splitlines()
    return tuple(json.loads(l) for l in lines if l.strip())


# --- CMS ---

def describe_metric_list(namespace: str, metric_name: str) -> dict:
    """模拟 CMS DescribeMetricList：返回单个响应（Datapoints 为 JSON 字符串）。"""
    for resp in _load_json("cms_metrics.json"):
        if resp["Namespace"] == namespace and resp["MetricName"] == metric_name:
            return resp
    return {"Code": "404", "Message": f"metric not found: {namespace}/{metric_name}"}


def list_cms_metrics() -> list[dict]:
    """列出全部 16 组指标的 (Namespace, MetricName)。"""
    return [{"Namespace": r["Namespace"], "MetricName": r["MetricName"]}
            for r in _load_json("cms_metrics.json")]


# --- SLS ---

def get_logs(logstore: str, offset: int = 0, lines: int = 1000) -> dict:
    """模拟 SLS GetLogs：分页返回日志行（原始字符串风格）。"""
    if logstore not in LOGSTORE_FILES:
        return {"count": 0, "logs": [], "error": f"unknown logstore: {logstore}"}
    rows = _load_jsonl(LOGSTORE_FILES[logstore])
    page = rows[offset:offset + lines]
    return {"count": len(page), "total": len(rows), "offset": offset, "logs": list(page)}


# --- K8s ---

def get_resources() -> dict:
    """模拟集群快照获取（等价 kubectl get xxx -o json 打包）。"""
    return _load_json("k8s_resources.json")


def get_manifest() -> dict:
    """世界清单（仅验收对账用，业务侧不消费 ground truth）。"""
    return _load_json("world_manifest.json")
