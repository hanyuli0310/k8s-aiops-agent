"""工具调用审计：记录每次权限决策与执行结果，供事后追溯。

运维场景的核心合规需求：必须能回答「这条 upgrade_rds_instance 是谁批准的？
依据什么？什么时候？结果如何？」。每条记录都带 decision + reason_type，
对应权限决策链上的具体一层。

设计原则：审计失败绝不能反过来搞挂 Agent —— 所有写入都吞异常只记 warning。
"""
from __future__ import annotations

import json
import logging
import time

from .. import db

logger = logging.getLogger(__name__)

# result_status 取值
OK = "ok"
ERROR = "error"
DENIED = "denied"          # 被规则/模式/工具拒绝
REJECTED = "rejected"      # 用户点了拒绝
TIMEOUT = "timeout"        # 等待确认超时
ABORTED = "aborted"        # 会话中断，工具未执行
INVALID = "invalid"        # validate_input 未通过


def record(run, agent_name: str, tool_name: str, args: dict, decision,
           spec, summary: str, status: str, duration_ms: int = 0):
    """写一条审计记录。

    run: RunContext（取 session_id 与 mode）
    decision: permissions.Decision
    spec: registry.ToolSpec 或 None
    """
    try:
        db.execute(
            """INSERT INTO agent_audit
               (session_id, agent_name, tool_name, args_json, audit_repr,
                decision, reason_type, run_mode, is_destructive,
                result_status, duration_ms, created_at)
               VALUES (:sid, :ag, :tn, :aj, :ar, :d, :rt, :rm, :de, :rs, :ms, :ts)""",
            {
                "sid": getattr(run, "session_id", "?"),
                "ag": (agent_name or "?")[:64],
                "tn": tool_name[:64],
                "aj": json.dumps(args or {}, ensure_ascii=False, default=str)[:4000],
                "ar": (summary or tool_name)[:255],
                "d": decision.behavior,
                "rt": decision.reason_type,
                "rm": getattr(run, "mode", "?"),
                "de": 1 if (spec is not None and spec.is_destructive) else 0,
                "rs": status,
                "ms": int(duration_ms),
                "ts": int(time.time() * 1000),
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("审计写入失败（已忽略，不影响主流程）: %s", e)


def query(limit: int = 100, destructive_only: bool = False,
          session_id: str = None) -> list:
    """查询审计记录（倒序）。"""
    where, params = [], {"l": max(1, min(limit, 500))}
    if destructive_only:
        where.append("is_destructive=1")
    if session_id:
        where.append("session_id=:sid")
        params["sid"] = session_id
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return db.fetch_all(
        f"""SELECT id, session_id, agent_name, tool_name, audit_repr, decision,
                   reason_type, run_mode, is_destructive, result_status,
                   duration_ms, created_at, args_json
            FROM agent_audit {clause}
            ORDER BY created_at DESC, id DESC LIMIT :l""", params)


def summary(session_id: str = None) -> dict:
    """审计概览：按决策与状态统计，供面板顶部展示。"""
    where = "WHERE session_id=:sid" if session_id else ""
    params = {"sid": session_id} if session_id else {}
    rows = db.fetch_all(
        f"""SELECT decision, result_status, is_destructive, COUNT(*) AS c
            FROM agent_audit {where}
            GROUP BY decision, result_status, is_destructive""", params)
    total = sum(r["c"] for r in rows)
    return {
        "total": total,
        "destructive": sum(r["c"] for r in rows if r["is_destructive"]),
        "denied": sum(r["c"] for r in rows
                      if r["result_status"] in (DENIED, REJECTED, TIMEOUT)),
        "executed_ok": sum(r["c"] for r in rows if r["result_status"] == OK),
        "by_status": {r["result_status"]: r["c"] for r in rows},
    }
