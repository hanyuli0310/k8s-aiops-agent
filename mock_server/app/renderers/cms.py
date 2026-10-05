"""CMS 渲染：把引擎的 metric_buffer 渲染为 DescribeMetricList 风格响应。

格式坑（KTD-6，与 data/Mock数据说明.md §3.2 一致）：
- Datapoints 是 **JSON 字符串**，消费方须二次 json.loads
- 顶层附加 Namespace/MetricName（Mock 扩展，便于单文件承载多指标）
- NextToken 恒为 null（未模拟分页）
"""
from __future__ import annotations

import json
from typing import List


def list_metrics(engine) -> List[dict]:
    """GET /cms/ListMetrics：列出当前有数据的全部 (Namespace, MetricName)。"""
    seen = []
    _, metas = engine.snapshot_metrics()
    for meta in metas.values():
        item = {"Namespace": meta["namespace"], "MetricName": meta["metric"]}
        if item not in seen:
            seen.append(item)
    return sorted(seen, key=lambda x: (x["Namespace"], x["MetricName"]))


def describe_metric_list(engine, namespace: str, metric_name: str,
                         start_ms: int = None, end_ms: int = None) -> dict:
    """GET /cms/DescribeMetricList：聚合该指标全部维度组的数据点。"""
    datapoints = []
    # buffer 与 meta 必须一起快照（同一把锁内），否则可能 buffer 有 key、meta 没有
    buffers, metas = engine.snapshot_metrics()
    for key, buf in buffers.items():
        meta = metas.get(key)
        if meta is None or meta["namespace"] != namespace or meta["metric"] != metric_name:
            continue
        for ts_ms, value in buf:
            if start_ms is not None and ts_ms < start_ms:
                continue
            if end_ms is not None and ts_ms > end_ms:
                continue
            dp = {"timestamp": ts_ms}
            dp.update(meta["dims"])
            # 单点无区间统计，Average/Maximum/Minimum 同值（真实 CMS 在聚合周期内才有差异）
            dp["Average"] = value
            dp["Maximum"] = round(value * 1.01, 2)
            dp["Minimum"] = round(value * 0.99, 2)
            datapoints.append(dp)
    datapoints.sort(key=lambda d: d["timestamp"])
    return {
        "Code": "200",
        "RequestId": f"MOCK-{engine.tick_no:06d}",
        "Namespace": namespace,
        "MetricName": metric_name,
        "Period": "60",
        "Datapoints": json.dumps(datapoints, ensure_ascii=False),   # 格式坑：JSON 字符串
        "NextToken": None,
    }
