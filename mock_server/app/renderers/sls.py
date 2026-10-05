"""SLS 渲染：把引擎的 log_buffer / trace_buffer 渲染为 GetLogs 风格响应。

格式坑（KTD-6）：
- 日志类 logstore 的值**全部是字符串**（含 __time__ 秒级时间戳）
- trace logstore 例外：attribute/resource 是 **JSON 字符串**，start/end/duration 为**微秒整数**，
  __time__ 为**秒级整数**（与其他 logstore 的字符串 __time__ 不同，见说明文档 §3.7）
- 分页语义：offset/lines，返回 count（本页）与 total（总数），与 pipeline._iter_logstore 兼容
"""
from __future__ import annotations

import json
from typing import List

LOGSTORES = ["nginx-ingress", "app-log", "rds_slow_log", "k8s-events", "trace"]


def _render_trace_span(span: dict) -> dict:
    """trace span：attribute/resource 转 JSON 字符串，其余字段保持原类型。"""
    out = dict(span)
    out["attribute"] = json.dumps(span["attribute"], ensure_ascii=False)
    out["resource"] = json.dumps(span["resource"], ensure_ascii=False)
    out["logs"] = "[]"
    out["links"] = "[]"
    return out


def get_logs(engine, logstore: str, from_s: int = None, to_s: int = None,
             offset: int = 0, lines: int = 1000) -> dict:
    if logstore not in LOGSTORES:
        return {"count": 0, "total": 0, "offset": offset, "logs": [],
                "error": f"unknown logstore: {logstore}", "available": LOGSTORES}

    # 先取快照再过滤：直接遍历 engine 的 deque 会撞上 tick 的 popleft
    # （RuntimeError: deque mutated during iteration），而整段持锁又会卡住 tick。
    if logstore == "trace":
        buf = engine.snapshot_traces()
        rows: List[dict] = [s for s in buf
                            if (from_s is None or s["__time__"] >= from_s)
                            and (to_s is None or s["__time__"] <= to_s)]
        page = [_render_trace_span(s) for s in rows[offset:offset + lines]]
    else:
        buf = engine.snapshot_logs(logstore)
        rows = [r for r in buf
                if (from_s is None or int(r["__time__"]) >= from_s)
                and (to_s is None or int(r["__time__"]) <= to_s)]
        page = rows[offset:offset + lines]

    return {"count": len(page), "total": len(rows), "offset": offset, "logs": page}
