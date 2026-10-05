"""保留期清理（KTD-8）：观测数据 2h、realtime_metrics 24h。

理由（spec v1.1 KTD-13）：实测 30min 窗数据在共享 RDS 上 run_risk_scan 5.6s，
若留 24h 观测数据，backend 每 60s 的定时扫描会被自己拖死。
"""
from __future__ import annotations

import logging
import time

from . import config, db

logger = logging.getLogger(__name__)

# 表 → 时间列单位（秒 / 毫秒）
OBSERVED_TABLES = {
    "ingress_logs": "s", "app_logs": "s", "slow_logs": "s",
    "k8s_events": "s", "trace_spans": "s", "metrics": "ms",
}


def run_once() -> dict:
    now_s = int(time.time())
    deleted = {}
    cutoff_s = now_s - int(config.RETENTION_HOURS * 3600)
    for table, unit in OBSERVED_TABLES.items():
        cutoff = cutoff_s * 1000 if unit == "ms" else cutoff_s
        n = db.execute(f"DELETE FROM {table} WHERE ts < :c", {"c": cutoff})
        if n:
            deleted[table] = n
    rt_cutoff_ms = (now_s - int(config.REALTIME_RETENTION_HOURS * 3600)) * 1000
    n = db.execute("DELETE FROM realtime_metrics WHERE ts < :c", {"c": rt_cutoff_ms})
    if n:
        deleted["realtime_metrics"] = n
    if deleted:
        logger.info("保留期清理: %s", deleted)
    return deleted
